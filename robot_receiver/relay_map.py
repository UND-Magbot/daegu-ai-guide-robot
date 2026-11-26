#!/usr/bin/env python3
"""
맵핑 실시간 시각화 릴레이 스크립트
NOS(10.21.31.106)에서 root로 실행

ROS2 토픽 구독 → 로컬 UDP(50000)로 receiver.py에 전달
receiver.py가 서버 요청(MAPPING_ODOM, MAPPING_CLOUD)에 응답

실행:
  su
  source /opt/robot/scripts/setup_ros2.sh
  python3 relay_map.py
"""

import json
import struct
import threading
import time
import math
import socket
import os
import subprocess
import sys

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from nav_msgs.msg import Odometry
from sensor_msgs.msg import PointCloud2

try:
    import numpy as np       # NOS 에 있다(obstacle_guard 가 씀). 없으면 라이다 원본 스캔만 건너뛴다
except ImportError:          # pragma: no cover
    np = None

# ── 로컬 UDP로 receiver.py에 전달 ──
LOCAL_PORT = 50000

# ── 위치 재조정 스캔 = 라이다 원본(/LIDAR/POINTS) 을 줄인 것(11-05) ──
# /LOC_BODY_POINTS 는 같은 점군일 때도 있지만 앞쪽 3.4m 만 담길 때가 있다(11-05 실측 두 번). 그 스캔으로는
# 관제의 지도 맞춤(점수·자동 맞춤·층 도착 방향)이 틀린다. 원본은 360°(정옆 ±80~100° 만 빔)·약 27m.
# lidar_link 와 base_link 는 평면 위치·방향이 같다(같은 순간 두 점군 99.5% 겹침, 11-05).
# 높이 띠(lidar_link 기준, 바닥 약 -0.57m) — 2D 지도(바닥 위 약 0.3~0.9m 를 벽으로 그림)와 맞는 높이만.
# 띠 없이 다 쓰면 실제 프레임 점수 49%, 띠를 쓰면 92.5%(1F 진료대기장소, 11-05).
LIDAR_SCAN_Z = (-0.3, 1.5)
LIDAR_SCAN_RANGE = (0.5, 25.0)   # 몸·다리 점 제외, 먼 잡음 제외
LIDAR_SCAN_BIN_DEG = 1.0         # 방향마다 가장 먼 점 하나 → 최대 360점(UDP 60KB 안)
LIDAR_SCAN_MIN_SEC = 0.4         # 이보다 자주 계산하지 않는다(관제는 0.5초마다 가져감, NOS 부하)
LIDAR_SCAN_FRESH_SEC = 1.5       # 이 안에 원본 스캔이 있으면 /LOC_BODY_POINTS 로 덮어쓰지 않는다


def lidar_scan_points(data: bytes, offsets: dict, point_step: int, n: int) -> list:
    """PointCloud2 원본 바이트 → 방향(1°)마다 가장 먼 [x, y] 목록. 높이·거리 띠 밖은 버린다.

    바닥·천장·몸에 맞은 점을 빼고 그 방향에서 가장 먼 것(=벽)만 남겨 2D 레이저처럼 만든다.
    """
    if np is None or n <= 0 or not all(k in offsets for k in ("x", "y", "z")):
        return []
    dt = np.dtype({"names": ["x", "y", "z"], "formats": ["<f4"] * 3,
                   "offsets": [offsets["x"], offsets["y"], offsets["z"]], "itemsize": point_step})
    r = np.frombuffer(data, dt, count=n)
    x, y, z = r["x"].astype(np.float64), r["y"].astype(np.float64), r["z"].astype(np.float64)
    d = np.hypot(x, y)
    keep = (np.isfinite(d) & np.isfinite(z) & (z > LIDAR_SCAN_Z[0]) & (z < LIDAR_SCAN_Z[1])
            & (d >= LIDAR_SCAN_RANGE[0]) & (d <= LIDAR_SCAN_RANGE[1]))
    x, y, d = x[keep], y[keep], d[keep]
    if len(d) == 0:
        return []
    b = np.floor((np.degrees(np.arctan2(y, x)) + 180.0) / LIDAR_SCAN_BIN_DEG).astype(np.int64)
    order = np.lexsort((-d, b))
    first = np.ones(len(order), bool)
    first[1:] = b[order][1:] != b[order][:-1]
    sel = order[first]
    return [[round(float(a), 3), round(float(c), 3)] for a, c in zip(x[sel], y[sel])]


class MappingRelay(Node):
    def __init__(self):
        super().__init__("mapping_relay")

        self.latest_odom_json = None
        self.latest_cloud_json = None
        self.latest_aligned_json = None
        # 측위(localization) 모드용 — LIO 정합 스캔(위치 재조정 오버레이 소스).
        # /SLAM_* 은 매핑 전용이라 측위 중엔 안 나오고, 측위 모드에선 /LIO_* 가 나온다.
        self.latest_lio_aligned_json = None
        # 측위 스캔(body 프레임, /LOC_BODY_POINTS) — 측위 중 ~10Hz 상시 publish.
        # LIO_ALIGNED 는 측위 중 조용해서, 위치 재조정 오버레이는 이 body 스캔을 쓴다.
        # (프론트가 로봇 pose 로 map 프레임 변환해 얹음 = rviz+TF 와 동일)
        self.latest_loc_body_json = None
        self._lidar_scan_at = 0.0       # 마지막 라이다 원본 스캔 계산 시도 시각(monotonic, 간격 조절용)
        self._lidar_ok_at = 0.0         # 마지막으로 쓸 만한 원본 스캔을 만든 시각(LOC_BODY 대체 판단용)
        self.lock = threading.Lock()

        qos_reliable = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
            depth=10,
        )
        # 라이다/포인트클라우드 센서 토픽은 보통 BEST_EFFORT 로 publish 된다.
        # BEST_EFFORT 구독은 RELIABLE·BEST_EFFORT publisher 양쪽과 호환되므로(역은 불가),
        # /LIO_ALIGNED_POINTS 처럼 QoS 를 모르는 센서 토픽엔 BEST_EFFORT 로 붙는다.
        qos_sensor = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
            depth=5,
        )

        self.create_subscription(Odometry, "/SLAM_ODOM", self.on_odom, qos_reliable)
        self.create_subscription(PointCloud2, "/SLAM_ACCUMULATED_POINTS_MAP", self.on_cloud, qos_reliable)
        self.create_subscription(PointCloud2, "/SLAM_ALIGNED_POINTS", self.on_aligned, qos_reliable)
        # 측위 모드 정합 스캔 — 위치 재조정(RelocalizeModal) 오버레이용. 센서 QoS(BEST_EFFORT).
        self.create_subscription(PointCloud2, "/LIO_ALIGNED_POINTS", self.on_lio_aligned, qos_sensor)
        # 측위 body 스캔(상시 ~10Hz) — 위치 재조정 오버레이 실사용 소스.
        # RELIABLE 로 받는다. publisher(제조사 DDS 앱)가 RELIABLE 이고 한 프레임이 ~1MB 라 수십 조각으로
        # 나뉘는데, BEST_EFFORT 로 받으면 Jetson(다른 장비)에서는 프레임이 하나도 완성되지 않는다
        # (2026-10-01 실측 14초: BEST_EFFORT 0건, RELIABLE 128건). depth=1 — 최신 스캔만 있으면 된다.
        qos_loc_body = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
            depth=1,
        )
        self.create_subscription(PointCloud2, "/LOC_BODY_POINTS", self.on_loc_body, qos_loc_body)
        # 폴백: 일부 로봇 펌웨어는 /LIO_ALIGNED_POINTS·/LOC_BODY_POINTS 를 발행하지 않고
        # /ALIGNED_POINTS 만 내보낸다(측정: publisher 1, 10Hz, frame_id=map).
        # 같은 map 프레임 정합 점군이라 LIO_ALIGNED 캐시를 그대로 채운다.
        # 위 두 토픽이 살아 있는 장비에서는 그쪽이 덮어쓰므로 동작이 바뀌지 않는다.
        self.create_subscription(PointCloud2, "/ALIGNED_POINTS", self.on_lio_aligned, qos_sensor)
        # 라이다 원본(/LIDAR/POINTS) 은 /LOC_BODY_POINTS 가 처음 들어온 뒤(= 측위 모드, 위치 재조정 창)에 구독한다
        # (_subscribe_lidar). 한 프레임 약 15만 점·10Hz 라 받기만 해도 부하가 있어, 매핑 중(측위 토픽 없음)에는 붙지 않는다.
        self._lidar_subscribed = False

        self.get_logger().info(
            "구독 시작: /SLAM_ODOM, /SLAM_ACCUMULATED_POINTS_MAP, /SLAM_ALIGNED_POINTS, "
            "/LIO_ALIGNED_POINTS, /LOC_BODY_POINTS, /ALIGNED_POINTS"
            + (" — /LIDAR/POINTS 는 측위 스캔 첫 수신 뒤" if np is not None else " (numpy 없음 — 라이다 원본 스캔 안 씀)")
        )

    def on_odom(self, msg: Odometry):
        pos = msg.pose.pose.position
        ori = msg.pose.pose.orientation
        siny = 2.0 * (ori.w * ori.z + ori.x * ori.y)
        cosy = 1.0 - 2.0 * (ori.y * ori.y + ori.z * ori.z)
        yaw = math.atan2(siny, cosy)
        data = json.dumps({
            "type": "odom",
            "x": round(pos.x, 4),
            "y": round(pos.y, 4),
            "yaw": round(yaw, 4),
        })
        was_none = self.latest_odom_json is None
        with self.lock:
            self.latest_odom_json = data
        if was_none:
            self.get_logger().info(f"SLAM_ODOM 첫 수신: x={pos.x:.2f}, y={pos.y:.2f}")

    def on_cloud(self, msg: PointCloud2):
        x_offset = y_offset = None
        for field in msg.fields:
            if field.name == "x":
                x_offset = field.offset
            elif field.name == "y":
                y_offset = field.offset

        if x_offset is None or y_offset is None:
            return

        points = []
        point_step = msg.point_step
        raw = bytes(msg.data)
        num_points = len(raw) // point_step
        downsample = 4

        for i in range(0, num_points, downsample):
            offset = i * point_step
            x = struct.unpack_from("<f", raw, offset + x_offset)[0]
            y = struct.unpack_from("<f", raw, offset + y_offset)[0]
            if math.isfinite(x) and math.isfinite(y):
                points.append([round(x, 3), round(y, 3)])

        data = json.dumps({
            "type": "cloud",
            "count": len(points),
            "points": points,
        })
        was_none = self.latest_cloud_json is None
        with self.lock:
            self.latest_cloud_json = data
        if was_none:
            self.get_logger().info(f"SLAM_CLOUD 첫 수신: {len(points)}점")

    def on_aligned(self, msg: PointCloud2):
        x_offset = y_offset = None
        for field in msg.fields:
            if field.name == "x":
                x_offset = field.offset
            elif field.name == "y":
                y_offset = field.offset

        if x_offset is None or y_offset is None:
            return

        points = []
        point_step = msg.point_step
        raw = bytes(msg.data)
        num_points = len(raw) // point_step

        for i in range(0, num_points, 2):
            offset = i * point_step
            x = struct.unpack_from("<f", raw, offset + x_offset)[0]
            y = struct.unpack_from("<f", raw, offset + y_offset)[0]
            if math.isfinite(x) and math.isfinite(y):
                points.append([round(x, 3), round(y, 3)])

        data = json.dumps({
            "type": "aligned",
            "count": len(points),
            "points": points,
        })
        was_none = self.latest_aligned_json is None
        with self.lock:
            self.latest_aligned_json = data
        if was_none:
            self.get_logger().info(f"SLAM_ALIGNED 첫 수신: {len(points)}점")

    def on_lio_aligned(self, msg: PointCloud2):
        """측위 모드 정합 스캔(/LIO_ALIGNED_POINTS) → 위치 재조정 오버레이용.

        on_aligned 과 동일 포맷(type=aligned)으로 캐시해, 프론트가 같은 렌더 경로를 쓴다.
        map 프레임 정합 점군이므로 저장맵 위에 그대로 얹힌다.
        """
        x_offset = y_offset = None
        for field in msg.fields:
            if field.name == "x":
                x_offset = field.offset
            elif field.name == "y":
                y_offset = field.offset
        if x_offset is None or y_offset is None:
            return

        points = []
        point_step = msg.point_step
        raw = bytes(msg.data)
        num_points = len(raw) // point_step
        for i in range(0, num_points, 2):
            offset = i * point_step
            x = struct.unpack_from("<f", raw, offset + x_offset)[0]
            y = struct.unpack_from("<f", raw, offset + y_offset)[0]
            if math.isfinite(x) and math.isfinite(y):
                points.append([round(x, 3), round(y, 3)])

        data = json.dumps({
            "type": "aligned",
            "count": len(points),
            "points": points,
        })
        was_none = self.latest_lio_aligned_json is None
        with self.lock:
            self.latest_lio_aligned_json = data
        if was_none:
            self.get_logger().info(f"LIO_ALIGNED 첫 수신: {len(points)}점")

    def _subscribe_lidar(self):
        """라이다 원본 구독(한 번만). 같은 장비(NOS) 안이라 BEST_EFFORT·최신 1개로 받는다."""
        if self._lidar_subscribed or np is None:
            return
        self._lidar_subscribed = True
        self.create_subscription(PointCloud2, "/LIDAR/POINTS", self.on_lidar,
                                 QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT,
                                            durability=DurabilityPolicy.VOLATILE, depth=1))
        self.get_logger().info("측위 스캔 수신 — /LIDAR/POINTS 구독 시작(위치 재조정 스캔 원본)")

    def on_lidar(self, msg: PointCloud2):
        """라이다 원본 → 위치 재조정 스캔(type=body, source=lidar). LOC_BODY 와 같은 자리에 넣는다."""
        now = time.monotonic()
        if now - self._lidar_scan_at < LIDAR_SCAN_MIN_SEC:
            return
        self._lidar_scan_at = now
        offsets = {f.name: f.offset for f in msg.fields}
        points = lidar_scan_points(bytes(msg.data), offsets, msg.point_step, msg.width * msg.height)
        if not points:
            return
        data = json.dumps({"type": "body", "source": "lidar", "count": len(points), "points": points})
        was_none = self.latest_loc_body_json is None
        with self.lock:
            self.latest_loc_body_json = data
            self._lidar_ok_at = now
        if was_none:
            self.get_logger().info(f"LIDAR 원본 재조정 스캔 첫 계산: {len(points)}점")

    def on_loc_body(self, msg: PointCloud2):
        """측위 body 스캔(/LOC_BODY_POINTS) → 위치 재조정 오버레이용(body 프레임).

        body 프레임(로봇 기준) x=전방, y=좌측 점을 그대로 전달한다. 프론트가 로봇 pose 로
        map 프레임 변환해 저장맵에 얹는다(rviz+TF 와 동일). type=body 로 표시해 프론트가 구분.
        """
        x_offset = y_offset = None
        for field in msg.fields:
            if field.name == "x":
                x_offset = field.offset
            elif field.name == "y":
                y_offset = field.offset
        if x_offset is None or y_offset is None:
            return

        self._subscribe_lidar()
        # 라이다 원본 스캔이 살아 있으면 그쪽을 쓴다(이 토픽은 앞쪽 3.4m 만 담길 때가 있다, 11-05)
        if self._lidar_ok_at and time.monotonic() - self._lidar_ok_at < LIDAR_SCAN_FRESH_SEC:
            return

        points = []
        point_step = msg.point_step
        raw = bytes(msg.data)
        num_points = len(raw) // point_step
        # 60KB UDP 상한 대비 — 최대 ~1500점으로 적응적 다운샘플(조밀해도 payload 안정).
        stride = max(2, num_points // 1500)
        for i in range(0, num_points, stride):
            offset = i * point_step
            x = struct.unpack_from("<f", raw, offset + x_offset)[0]
            y = struct.unpack_from("<f", raw, offset + y_offset)[0]
            if math.isfinite(x) and math.isfinite(y):
                points.append([round(x, 3), round(y, 3)])

        data = json.dumps({
            "type": "body",
            "count": len(points),
            "points": points,
        })
        was_none = self.latest_loc_body_json is None
        with self.lock:
            self.latest_loc_body_json = data
        if was_none:
            self.get_logger().info(f"LOC_BODY 첫 수신: {len(points)}점")


def local_udp_server(node: MappingRelay):
    """로컬 UDP 서버: receiver.py로부터 요청 받아 최신 데이터 응답.

    ⚠️ 루프 조건이 `rclpy.ok()` 인 이유:
    SIGTERM 을 받으면 rclpy 가 컨텍스트를 내려 spin 스레드만 죽고, 이 루프가
    `while True` 면 프로세스가 살아남는다. 그 **좀비**는 ROS 구독이 끊긴 채
    모든 요청에 빈 `{}` 를 답하고, 서버 쪽 pgrep 점검에는 "실행 중"으로 잡혀
    재기동되지 않는다 → 매핑 캔버스가 조용히 빈 화면이 된다(실제 사고 사례).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", LOCAL_PORT))
    sock.settimeout(1.0)
    print(f"📡 로컬 UDP 서버 시작 (127.0.0.1:{LOCAL_PORT})")

    while rclpy.ok():
        try:
            data, addr = sock.recvfrom(1024)
            msg = json.loads(data.decode("utf-8"))
            action = msg.get("action")

            with node.lock:
                if action == "MAPPING_ODOM" and node.latest_odom_json:
                    sock.sendto(node.latest_odom_json.encode("utf-8"), addr)
                elif action == "MAPPING_CLOUD" and node.latest_cloud_json:
                    payload = node.latest_cloud_json.encode("utf-8")
                    if len(payload) > 60000:
                        sock.sendto(b'{"type":"cloud","count":0,"points":[]}', addr)
                    else:
                        sock.sendto(payload, addr)
                elif action == "MAPPING_ALIGNED" and node.latest_aligned_json:
                    payload = node.latest_aligned_json.encode("utf-8")
                    if len(payload) > 60000:
                        sock.sendto(b'{"type":"aligned","count":0,"points":[]}', addr)
                    else:
                        sock.sendto(payload, addr)
                elif action == "LIO_ALIGNED" and node.latest_lio_aligned_json:
                    payload = node.latest_lio_aligned_json.encode("utf-8")
                    if len(payload) > 60000:
                        sock.sendto(b'{"type":"aligned","count":0,"points":[]}', addr)
                    else:
                        sock.sendto(payload, addr)
                elif action == "LOC_BODY" and node.latest_loc_body_json:
                    payload = node.latest_loc_body_json.encode("utf-8")
                    if len(payload) > 60000:
                        sock.sendto(b'{"type":"body","count":0,"points":[]}', addr)
                    else:
                        sock.sendto(payload, addr)
                else:
                    sock.sendto(b'{}', addr)

        except socket.timeout:
            continue
        except Exception as e:
            print(f"[LOCAL UDP ERR] {e}")

    try:
        sock.close()
    except Exception:
        pass
    print("📴 로컬 UDP 서버 종료 (rclpy 컨텍스트 종료)")


# DDS 멀티캐스트 인터페이스를 고르는 기준 주소(NOS). NOS 밖(Jetson)에서는 NOS 로 가는
# 유선(eno1)이 잡히고, NOS 자신에서는 local 로 나오므로 NOS 내부 ROS 망(eth0)을 그대로 쓴다.
NOS_LAN_IP = os.environ.get("NOS_LAN_IP", "10.21.31.106")
NOS_MCAST_IFACE = "eth0"


def _mcast_iface() -> str:
    """멀티캐스트를 내보낼 인터페이스. 장비마다 이름이 달라(NOS=eth0, Jetson=eno1) 경로로 찾는다."""
    forced = os.environ.get("RELAY_MCAST_IFACE", "").strip()
    if forced:
        return forced
    result = subprocess.run(
        ["ip", "route", "get", NOS_LAN_IP], capture_output=True, text=True
    )
    tokens = result.stdout.split()
    if "dev" not in tokens or tokens[0] == "local":
        return NOS_MCAST_IFACE
    return tokens[tokens.index("dev") + 1]


def ensure_environment():
    """root 권한 확인 및 멀티캐스트 라우트 설정"""
    if os.geteuid() != 0:
        print("[ERROR] root 권한이 필요합니다.")
        print("  su")
        print("  source /opt/robot/scripts/setup_ros2.sh")
        print("  python3 relay_map.py")
        sys.exit(1)

    result = subprocess.run(
        ["ip", "route", "show", "224.0.0.0/4"],
        capture_output=True, text=True
    )
    if result.stdout.strip():
        # 로봇 제조사가 잡아 둔 NOS 라우트일 수 있으므로 덮어쓰지 않는다.
        print(f"[SETUP] 멀티캐스트 라우트 이미 존재: {result.stdout.strip()}")
        return

    # 라우트가 없으면 DDS 멀티캐스트가 기본 경로(Jetson 은 Wi-Fi)로 새어 무선을 막는다.
    iface = _mcast_iface()
    added = subprocess.run(
        ["ip", "route", "add", "224.0.0.0/4", "dev", iface],
        capture_output=True, text=True
    )
    if added.returncode == 0:
        print(f"[SETUP] 멀티캐스트 라우트 추가: 224.0.0.0/4 dev {iface}")
    else:
        print(f"[SETUP][WARN] 멀티캐스트 라우트 추가 실패(dev {iface}): {added.stderr.strip()}")


def main():
    ensure_environment()

    rclpy.init()
    node = MappingRelay()

    spin_thread = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin_thread.start()

    try:
        local_udp_server(node)
    finally:
        # 종료 경로를 확실히 밟아 좀비를 남기지 않는다 — 껍데기만 살아 있으면
        # 서버가 "이미 실행 중" 으로 오판해 다시 띄우지 않는다.
        try:
            node.destroy_node()
        except Exception:
            pass
        if rclpy.ok():
            try:
                rclpy.shutdown()
            except Exception:
                pass
        print("👋 relay_map 종료")


if __name__ == "__main__":
    main()
