#!/usr/bin/env python3
"""엘리베이터 안/밖 판정용 라이다 기록기 — NOS 에서 실행(읽기 전용·무발행·무모션).

층 이동 한 번(1F 홀 → 탑승 → 이동 → 하차 → 3F 홀)을 통째로 기록한다.
엘리베이터 안에서는 Wi-Fi 가 끊겨 SSH 가 죽으므로 백그라운드(nohup)로 돌린다.
운영자는 켜 두고 평소대로 이동만 한다(정답은 분석기가 IMU 로 자동 산출).

  # NOS: su → source /opt/robot/scripts/setup_ros2.sh 후
  nohup python3 ev_scan_recorder.py --label trip1 --duration 1200 > /tmp/ev_scan/trip1.log 2>&1 &
  # 한 지점 짧게(10프레임)
  python3 ev_scan_recorder.py --label corridor --frames 10

출력: <out>/scan_<label>_<시각>.jsonl.gz
  1줄째 meta {"meta": {...}}, 이후
  - 라이다 프레임 {"t": 경과초, "n": 점수, "pts": [[x,y,z],...], "ms": MotionState}
  - IMU       {"i": 경과초, "tp": 토픽, "a": [ax,ay,az], "w": [wx,wy,wz]}   (~20Hz)
  - 오도메트리 {"o": 경과초, "tp": 토픽, "p": [x,y,z], "v": [vx,vy,vz]}    (~5Hz)
  - MotionInfo {"m": 경과초, "v": {필드: 값}}                              (~5Hz, 숫자 필드 전부)
  - 토픽 목록 {"topics": [[이름, 타입], ...]}
IMU·오도메트리 토픽은 이름을 몰라도 타입(sensor_msgs/Imu, nav_msgs/Odometry)으로 찾아 구독한다.
분석기는 IMU 로 엘리베이터 출발·정지를, 흔들림으로 로봇 이동·정지를 찾아 '안' 구간을 자동으로 정한다
(운영자가 시각을 적을 필요 없음). 매 줄 flush 하므로 중간에 끊겨도 그때까지는 읽힌다.

이 파일은 표준 라이브러리 + rclpy 만 쓴다(elevator_align 패키지 없이 단독 복사 가능).
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import signal
import socket
import struct
import sys
import time

DEFAULT_TOPIC = "/LOC_BODY_POINTS"
MAX_POINTS = 3000           # 프레임당 저장 상한(초과 시 균등 다운샘플)
PROGRESS_SEC = 30.0
IMU_HZ = 20.0
SLOW_HZ = 5.0               # 오도메트리·MotionInfo 저장 주기
DISCOVER_SEC = 3.0          # 시작 후 토픽 탐색 시점(DDS 발견 대기)
IMU_TYPE = "sensor_msgs/msg/Imu"
ODOM_TYPE = "nav_msgs/msg/Odometry"


def field_offsets(msg) -> dict[str, int]:
    """PointCloud2 fields → {이름: 바이트 오프셋}. x,y,z 가 없으면 0,4,8 로 가정."""
    offs = {f.name: f.offset for f in getattr(msg, "fields", [])}
    offs.setdefault("x", 0)
    offs.setdefault("y", 4)
    if "z" not in offs and getattr(msg, "point_step", 0) >= 12:
        offs["z"] = 8
    return offs


def parse_points(msg, max_points: int = MAX_POINTS) -> list[list[float]]:
    """PointCloud2 → [[x,y,z], ...] (body 프레임, 소수 3자리). z 가 없으면 0."""
    raw = bytes(msg.data)
    step = msg.point_step
    n = len(raw) // step if step else 0
    offs = field_offsets(msg)
    has_z = "z" in offs
    ds = max(1, -(-n // max_points))  # 올림 나눗셈
    pts = []
    for i in range(0, n, ds):
        base = i * step
        x = struct.unpack_from("<f", raw, base + offs["x"])[0]
        y = struct.unpack_from("<f", raw, base + offs["y"])[0]
        z = struct.unpack_from("<f", raw, base + offs["z"])[0] if has_z else 0.0
        if x != x or y != y:  # NaN 제외
            continue
        pts.append([round(x, 3), round(y, 3), round(z, 3)])
    return pts


def flatten_numbers(obj, prefix: str = "", depth: int = 0) -> dict[str, float]:
    """ROS2 메시지 → {"a.b.c": 숫자}. 필드 이름을 몰라도 속도 등을 남기기 위함."""
    out: dict[str, float] = {}
    if depth > 4 or obj is None:
        return out
    if hasattr(obj, "get_fields_and_field_types"):
        names = list(obj.get_fields_and_field_types().keys())
    elif hasattr(obj, "__dict__"):
        names = list(vars(obj).keys())
    else:
        return out
    for name in names:
        v = getattr(obj, name, None)
        key = f"{prefix}{name}"
        if isinstance(v, bool):
            out[key] = int(v)
        elif isinstance(v, (int, float)):
            out[key] = round(float(v), 4)
        elif not isinstance(v, (str, bytes, list, tuple)) and v is not None:
            out.update(flatten_numbers(v, key + ".", depth + 1))
    return out


def output_path(out_dir: str, label: str) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(out_dir, f"scan_{label}_{stamp}.jsonl.gz")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="엘리베이터 안/밖 판정용 라이다 기록기(무모션)")
    ap.add_argument("--label", required=True, help="기록 이름(예: trip1, corridor)")
    ap.add_argument("--out", default="/tmp/ev_scan", help="저장 폴더")
    ap.add_argument("--duration", type=float, default=1200.0, help="기록 시간(초)")
    ap.add_argument("--frames", type=int, default=0, help="N 프레임만 받고 종료(0=시간 기준)")
    ap.add_argument("--hz", type=float, default=2.0, help="저장 주기")
    ap.add_argument("--topic", default=DEFAULT_TOPIC)
    args = ap.parse_args(argv)

    try:
        import rclpy
        from rclpy.node import Node
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import PointCloud2
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] ROS2 환경이 필요합니다(su 후 source /opt/robot/scripts/setup_ros2.sh): {e}")
        return 2

    os.makedirs(args.out, exist_ok=True)
    path = output_path(args.out, args.label)
    fout = gzip.open(path, "at", encoding="utf-8")
    start_wall, start_mono = time.time(), time.monotonic()
    meta = {"label": args.label, "topic": args.topic, "hz": args.hz, "host": socket.gethostname(),
            "start_wall": start_wall, "start_local": time.strftime("%Y-%m-%d %H:%M:%S")}
    fout.write(json.dumps({"meta": meta}, ensure_ascii=False) + "\n")
    fout.flush()
    print(f"[REC] 시작 {meta['start_local']} (NOS 시계) → {path}", flush=True)

    class Recorder(Node):
        def __init__(self):
            super().__init__("ev_scan_recorder")
            self.saved = 0
            self.last_save = -1e9
            self.last_progress = 0.0
            self.motion_state = None
            self.last_slow: dict[str, float] = {}
            self.last_imu: dict[str, float] = {}
            self.extra_topics: list[str] = []
            qos = QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT,
                             durability=DurabilityPolicy.VOLATILE, depth=5)
            self.create_subscription(PointCloud2, args.topic, self._on_scan, qos)
            try:  # 자세(1=Stand, 4=Sit 등) — 앉혔다 세우기 표식 확인용. 없어도 기록은 한다.
                from drdds.msg import MotionInfo
                self.create_subscription(MotionInfo, "/MOTION_INFO", self._on_motion, 10)
            except Exception as e:  # noqa: BLE001
                self.get_logger().warn(f"MotionInfo 구독 생략: {e}")
            self._discover_timer = self.create_timer(DISCOVER_SEC, self._discover)

        def _due(self, table: dict, key: str, hz: float) -> float | None:
            now = time.monotonic()
            if now - table.get(key, -1e9) < 1.0 / hz:
                return None
            table[key] = now
            return round(now - start_mono, 2)

        def _write(self, obj: dict) -> None:
            fout.write(json.dumps(obj) + "\n")
            fout.flush()

        def _discover(self):
            """IMU·오도메트리 토픽을 타입으로 찾아 구독한다(1회)."""
            self._discover_timer.cancel()
            topics = self.get_topic_names_and_types()
            self._write({"topics": [[n, list(t)] for n, t in topics]})
            sensor_qos = QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT,
                                    durability=DurabilityPolicy.VOLATILE, depth=20)
            for name, types in topics:
                try:
                    if IMU_TYPE in types:
                        from sensor_msgs.msg import Imu
                        self.create_subscription(Imu, name, lambda m, n=name: self._on_imu(n, m), sensor_qos)
                    elif ODOM_TYPE in types:
                        from nav_msgs.msg import Odometry
                        self.create_subscription(Odometry, name, lambda m, n=name: self._on_odom(n, m), sensor_qos)
                    else:
                        continue
                    self.extra_topics.append(name)
                except Exception as e:  # noqa: BLE001
                    self.get_logger().warn(f"{name} 구독 실패: {e}")
            print(f"[REC] IMU·오도메트리 구독: {self.extra_topics or '없음'}", flush=True)

        def _on_imu(self, name, msg):
            t = self._due(self.last_imu, name, IMU_HZ)
            if t is None:
                return
            a, w = msg.linear_acceleration, msg.angular_velocity
            self._write({"i": t, "tp": name, "a": [round(a.x, 3), round(a.y, 3), round(a.z, 3)],
                         "w": [round(w.x, 3), round(w.y, 3), round(w.z, 3)]})

        def _on_odom(self, name, msg):
            t = self._due(self.last_slow, name, SLOW_HZ)
            if t is None:
                return
            p, v = msg.pose.pose.position, msg.twist.twist.linear
            self._write({"o": t, "tp": name, "p": [round(p.x, 3), round(p.y, 3), round(p.z, 3)],
                         "v": [round(v.x, 3), round(v.y, 3), round(v.z, 3)]})

        def _on_motion(self, msg):
            try:
                self.motion_state = int(msg.data.motion_state.state)
            except Exception:  # noqa: BLE001
                self.motion_state = None
            t = self._due(self.last_slow, "/MOTION_INFO", SLOW_HZ)
            if t is not None:
                self._write({"m": t, "v": flatten_numbers(getattr(msg, "data", msg))})

        def _on_scan(self, msg):
            now = time.monotonic()
            if now - self.last_save < 1.0 / args.hz:
                return
            self.last_save = now
            pts = parse_points(msg)
            t = round(now - start_mono, 2)
            self._write({"t": t, "n": len(pts), "pts": pts, "ms": self.motion_state})
            self.saved += 1
            if t - self.last_progress >= PROGRESS_SEC:
                self.last_progress = t
                print(f"[REC] {t:.0f}s 프레임 {self.saved} (점 {len(pts)})", flush=True)

    rclpy.init()
    node = Recorder()
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        while not stop["flag"] and rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.2)
            if args.frames and node.saved >= args.frames:
                break
            if not args.frames and time.monotonic() - start_mono >= args.duration:
                break
    finally:
        print(f"[REC] 종료 — 프레임 {node.saved}, {time.monotonic() - start_mono:.0f}s → {path}", flush=True)
        fout.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0 if node.saved else 1


if __name__ == "__main__":
    sys.exit(main())
