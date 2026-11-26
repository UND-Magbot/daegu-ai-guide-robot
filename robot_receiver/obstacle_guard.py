#!/usr/bin/env python3
"""안내 중 전방 장애물 감지 노드 — NOS 에서 상시 실행 (2026-10-29).

관제가 안내 이동 중 매 주기(약 1초) GUARD 요청을 보내 감지를 켜 둔다. 켜져 있는 동안 /LOC_BODY_POINTS(약 10Hz)로
앞이 막혔는지 보고(obstacle_core.Detector), 막히면 receiver 에 CANCEL_NAV 를 바로 보낸다 — 관제 폴링(1초)을 기다리면
멈추는 거리가 줄어든다(10-18 시뮬). 재출발은 관제가 한다(같은 목표 재전송).

    관제 → 이 노드 (UDP OBSTACLE_GUARD_PORT, 기본 40100)
        {"action": "GUARD", "arm": true, "record": false, "suppress": false, "stops": [[x, y], ...], "ttl": 3.0}
        관제는 안내 시나리오 세션의 이동에서만 보낸다. record=true 면 판단·기록만 하고 취소하지 않는다.
    이 노드 → 관제
        {"armed", "state": unknown|blocked|clear, "stopped", "episode", "lastDist", "scanAge", "poseAge", "map"}

안전 규칙
    - 관제 신호가 ttl 초 끊기면 감지를 끈다(관제가 죽거나 Wi-Fi 가 끊겨도 로봇을 계속 세우지 않는다).
    - 위치가 오래됐거나(1.5초) 측위 상실(Location≠0)이면 판정하지 않는다 — 벽 판정이 틀려 엉뚱하게 선다.
    - 스캔·지도가 없으면 state=unknown 으로 답한다. 관제는 unknown 이면 출발시키지 않고 기다린다.
지도는 NOS 의 지금 활성 맵(/var/opt/robot/data/maps/active/occ_grid.*)을 읽고 바뀌면 다시 읽는다.
무시 구역(OBSTACLE_IGNORE_FILE, 기본 obstacle_ignore.json): {"지도 폴더 이름": [{"name", "x", "y", "r"}]} — 활성 지도 이름이
맞는 구역만 쓴다(지도를 새로 만들면 따라가지 않는다). 파일이 바뀌면 지도와 함께 다시 읽는다.

실기 확인용 '기록만' (관제 OBSTACLE_GUARD_MODE=record, 또는 이 노드의 OBSTACLE_RECORD_ONLY=1 — 둘 중 하나면)
    판단은 관제가 켤 때(안내 시나리오 이동)만 하고, CANCEL_NAV 는 보내지 않는다. 답에 recordOnly 를 싣는다.
    OBSTACLE_RECORD_ONLY=1 은 NOS 쪽 안전장치다 — 관제가 on 으로 보내도 취소하지 않는다.
    OBSTACLE_RECORD_DIR 를 주면 프레임마다 요약(위치·점 수·앞 상자 점 수·높이 분포·판정·처리 시간)을 jsonl 로 남기고,
    OBSTACLE_RAW_EVERY 프레임마다 앞 넓은 구역(앞 0.5~2.5m, 좌우 ±1.0m) 원본 점을 함께 남긴다.
    파일이 OBSTACLE_RECORD_FILE_MB(100)를 넘으면 새 파일로 넘기고, 폴더 합계가 OBSTACLE_RECORD_TOTAL_MB(2000)를
    넘으면 오래된 파일부터 지운다(11-02: 예전에는 300MB 에서 기록이 멈춰 16:50 이후 정지 기록이 없었다).
    정지가 나면 앞 OBSTACLE_EVENT_PRE_SEC(5)·뒤 OBSTACLE_EVENT_POST_SEC(5)초를 기록 폴더 옆 <폴더>_events/ 에 따로 남긴다 — 매 프레임 원본 점
    (최대 OBSTACLE_EVENT_RAW_PTS 1500점) 포함. OBSTACLE_EVENT_KEEP_DAYS(30)·OBSTACLE_EVENT_TOTAL_MB(500)를 넘으면 오래된 것부터 지운다.
    OBSTACLE_EVERY_N 프레임에 1번만 처리한다(NOS 부하).

실행: source /opt/robot/scripts/setup_ros2.sh && python3 obstacle_guard.py
"""
from __future__ import annotations

import glob
import json
import math
import os
import queue
import socket
import struct
import threading
import time
from collections import deque

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import PointCloud2

from obstacle_core import CameraView, CamParams, Detector, Params, PoseTracker, WallMap, near_any

GUARD_PORT = int(os.environ.get("OBSTACLE_GUARD_PORT", "40100"))
RECEIVER_ADDR = ("127.0.0.1", int(os.environ.get("RECEIVER_PORT", "40000")))
MAP_DIR = os.environ.get("OBSTACLE_MAP_DIR", "/var/opt/robot/data/maps/active")
IGNORE_FILE = os.environ.get("OBSTACLE_IGNORE_FILE",
                             os.path.join(os.path.dirname(os.path.abspath(__file__)), "obstacle_ignore.json"))
SCAN_TOPIC = os.environ.get("OBSTACLE_SCAN_TOPIC", "/LOC_BODY_POINTS")
RAW_BOX = (0.5, 2.5, 1.0)          # 기록용 넓은 구역: 앞 x0~x1, 좌우 ±w
RAW_MAX_PTS = 4000                  # 넘으면 고르게 뽑는다(앞에서 자르면 라이다 빔 순서 때문에 바닥 빔이 빠진다 — 10-29)
POSE_POLL_SEC = 0.1
# 본체 위치(1007/2)를 직접 묻는다 — 본체는 0.1초마다 새 값을 내는데 receiver 는 0.5초마다만 물어(POS_POLL_MS=500)
# 출발·회전 직후 위치가 최대 0.6m 뒤처져 지도 물건을 새 물체로 봤다(11-03 정지 2·4·5·6번). 'off' 면 receiver 만 쓴다.
_BODY = os.environ.get("OBSTACLE_BODY_ADDR", "10.21.31.103:30000").strip()
BODY_ADDR = None if _BODY.lower() in ("", "off", "none") else (_BODY.rsplit(":", 1)[0], int(_BODY.rsplit(":", 1)[1]))
BODY_TIMEOUT = 0.2
POSE_MAX_AGE = 1.5
SCAN_MAX_AGE = 0.5
MAP_CHECK_SEC = 5.0


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


MB = 1024 * 1024


def _pts_list(pts) -> list:
    """원본 점 → 소수 둘째 자리 목록. 32비트 실수를 그대로 반올림하면 1.590000033378601 처럼 길게 찍혀
    기록이 2배 넘게 커진다(11-02 측정) — 64비트로 바꾼 뒤 반올림한다."""
    return np.round(np.asarray(pts, dtype=np.float64), 2).tolist()


def pick_event_pts(pts, limit: int, box=None):
    """사건 파일에 남길 원본 점 — 앞 상자(x0, x1, w) 안 점은 모두, 나머지는 고르게 골라 limit 까지 채운다.
    상자 안 점만으로 limit 를 넘으면 상자 안 점을 모두 남긴다(판정 근거가 먼저다)."""
    if len(pts) <= limit:
        return pts
    if box is None:
        return pts[np.linspace(0, len(pts) - 1, limit).astype(int)]
    x0, x1, w = box
    inb = (pts[:, 0] >= x0) & (pts[:, 0] <= x1) & (np.abs(pts[:, 1]) <= w)
    keep, rest = pts[inb], pts[~inb]
    room = max(0, limit - len(keep))
    if room and len(rest):
        rest = rest[np.linspace(0, len(rest) - 1, min(room, len(rest))).astype(int)]
        return np.concatenate([keep, rest])
    return keep


def _dumps(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"


def prune(folder: str, pattern: str, max_bytes: int, keep: str = "", max_age_sec: float = 0.0,
          now: float = None) -> list:
    """오래된 파일부터 지운다 — 합계가 max_bytes 를 넘거나 max_age_sec 보다 오래된 것. keep 은 지우지 않는다(쓰는 중).
    반환: 지운 파일 이름."""
    now = time.time() if now is None else now
    files = []
    for p in glob.glob(os.path.join(folder, pattern)):
        try:
            st = os.stat(p)
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, p))
    files.sort()                                # 오래된 것부터
    total = sum(s for _, s, _ in files)
    gone = []
    for mt, size, p in files:
        if os.path.abspath(p) == os.path.abspath(keep):
            continue
        if total <= max_bytes and not (max_age_sec and now - mt > max_age_sec):
            continue
        try:
            os.remove(p)
        except OSError:
            continue
        total -= size
        gone.append(os.path.basename(p))
    return gone


def body_packet() -> bytes:
    """본체 위치 조회(1007/2) 패킷 — receiver.position_poll_loop 와 같은 모양."""
    asdu = json.dumps({"PatrolDevice": {"Type": 1007, "Command": 2, "Time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                        "Items": {}}}).encode("utf-8")
    return struct.pack("<4BHHB7B", 0xEB, 0x91, 0xEB, 0x90, len(asdu), 1, 0x01, *(0x00,) * 7) + asdu


def query_body(sock, addr):
    """본체에 위치를 한 번 묻는다. 반환 (받은 monotonic, x, y, yaw, location) 또는 None(시간 초과·다른 답)."""
    try:
        sock.sendto(body_packet(), addr)
        end = time.monotonic() + BODY_TIMEOUT
        while time.monotonic() < end:
            data, _ = sock.recvfrom(4096)
            t = time.monotonic()
            try:
                msg = json.loads(data.decode())
            except ValueError:
                msg = json.loads(data[16:].decode())
            pd = msg.get("PatrolDevice", {})
            if pd.get("Type") == 1007 and pd.get("Command") == 2:
                it = pd.get("Items", {})
                return t, float(it["PosX"]), float(it["PosY"]), float(it["Yaw"]), it.get("Location")
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
        pass
    return None


def query_receiver(sock):
    """receiver POSITION 을 한 번 묻는다(본체 답이 없을 때). 반환은 query_body 와 같다."""
    try:
        sock.sendto(json.dumps({"action": "POSITION"}).encode(), RECEIVER_ADDR)
        pos = json.loads(sock.recvfrom(4096)[0].decode())
        age = pos.get("age")
        if age is None or not pos.get("timestamp"):
            return None
        return (time.monotonic() - float(age), float(pos["x"]), float(pos["y"]), float(pos["yaw"]),
                pos.get("location"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


class Recorder:
    """프레임 요약을 jsonl 로 남긴다.

    연속 기록: file_mb 마다 새 파일, 폴더 합계 total_mb 를 넘으면 오래된 파일부터 지운다.
    정지 사건: 정지가 나면(start_event) 앞 pre_sec 를 메모리에서 꺼내고 뒤 post_sec 까지 <폴더>_events/ 에 따로 쓴다.
              원본 점은 사건 파일에만 매 프레임 넣는다(평소에는 배열로만 들고 있다 — 글자로 바꾸는 비용을 아낀다).
              글자로 바꾸고 쓰는 일은 쓰기 스레드가 한다 — 판정 스레드에서 하면 정지 순간 약 1.1초 멈췄다(11-03 실측 6건).
    """

    def __init__(self, folder: str, log, file_mb: float = 100, total_mb: float = 2000,
                 event_keep_days: float = 30, event_total_mb: float = 500,
                 pre_sec: float = 5.0, post_sec: float = 5.0, event_raw_pts: int = 1500,
                 event_box=None) -> None:
        os.makedirs(folder, exist_ok=True)
        self.folder = folder
        # 기록 폴더 옆에 둔다(…/obstacle_rec_events) — 기록 폴더 안에는 rec_*.jsonl 만 있다고 보는 도구·시험이 있다
        self.ev_dir = os.path.normpath(folder) + "_events"
        os.makedirs(self.ev_dir, exist_ok=True)
        self.log = log
        self.file_bytes = int(file_mb * MB)
        self.total_bytes = int(total_mb * MB)
        self.ev_keep_sec = event_keep_days * 86400.0
        self.ev_total_bytes = int(event_total_mb * MB)
        self.pre_sec, self.post_sec, self.event_raw_pts = pre_sec, post_sec, event_raw_pts
        # (x0, x1, w) — 판정하는 앞 상자. 사건 파일에는 이 안의 점을 모두 남기고 나머지만 골라 채운다
        # (11-03 13:23 정지: 넓은 구역 1500점 표본에 판정 상자 점이 0~1개만 남아 원인을 못 봤다)
        self.event_box = event_box
        self.buf: deque = deque()               # (t, row, 원본 점 배열 또는 None) — 직전 pre_sec
        self.ev_f = None                        # 쓰기 스레드만 만진다
        self.ev_on = False                      # 판정 스레드 쪽 '사건 기록 중'
        self.ev_path = ""
        self.ev_until = 0.0
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._ev_loop, daemon=True).start()
        self.f = None
        self.path = ""
        self.size = 0
        self._open()
        log.info(f"기록 시작: {self.path} (파일 {file_mb:.0f}MB · 합계 {total_mb:.0f}MB, "
                 f"정지 사건 {self.ev_dir} 앞뒤 {pre_sec:.0f}/{post_sec:.0f}초 · {event_keep_days:.0f}일 · {event_total_mb:.0f}MB)")
        self._prune_events()

    def _open(self) -> None:
        base = os.path.join(self.folder, time.strftime("rec_%Y%m%d_%H%M%S"))
        path, n = base + ".jsonl", 1
        while os.path.exists(path):             # 같은 초에 다시 열면 덮어쓰지 않는다
            path, n = f"{base}_{n:02d}.jsonl", n + 1
        self.path = path
        self.f = open(self.path, "a", encoding="utf-8")
        self.size = 0
        gone = prune(self.folder, "rec_*.jsonl", self.total_bytes, keep=self.path)
        if gone:
            self.log.info(f"기록 합계 상한 — 오래된 기록 {len(gone)}개 지움: {', '.join(gone)}")

    def write(self, row: dict, raw=None) -> None:
        """한 줄. raw: 사건 파일에 넣을 원본 점(N×3 배열, 이미 고른 것) — 연속 파일에는 row 에 있는 것만 쓴다."""
        line = _dumps(row)
        if self.size + len(line) > self.file_bytes and self.size > 0:
            self.f.close()
            self.log.info(f"기록 파일 상한 — 새 파일로: {self.path}")
            self._open()
        self.f.write(line)
        self.f.flush()
        self.size += len(line)
        t = float(row.get("t") or time.time())
        if self.ev_on:
            self.q.put(("row", row, raw))
            if t > self.ev_until:
                self.ev_on = False
                self.q.put(("close",))
        self.buf.append((t, row, raw))
        while self.buf and self.buf[0][0] < t - self.pre_sec:
            self.buf.popleft()

    def start_event(self, episode: int, info: dict, now: float = None) -> None:
        """정지 시작 — 직전 pre_sec 를 사건 파일에 먼저 쓴다. 뒤 post_sec 는 write 가 이어 쓴다. 사건 중 새 정지면 늘린다.
        now: 기록 줄의 t 와 같은 시계(time.time). 시험에서만 넣는다."""
        now = time.time() if now is None else now
        self.ev_until = now + self.post_sec
        if self.ev_on:
            self.q.put(("line", {"event": episode, "start": round(now, 2), **info}))
            return
        self.ev_on = True
        path = os.path.join(self.ev_dir, time.strftime("evt_%Y%m%d_%H%M%S") + f"_ep{episode}.jsonl")
        head = {"event": episode, "start": round(now, 2), "preSec": self.pre_sec, "postSec": self.post_sec, **info}
        self.q.put(("open", path, head, list(self.buf)))   # 앞 pre_sec 는 목록만 넘긴다(바꾸고 쓰는 건 쓰기 스레드)

    def flush(self, timeout: float = 30.0) -> None:
        """쓰기 스레드가 밀린 일을 다 할 때까지 기다린다(시험용)."""
        end = time.monotonic() + timeout
        while self.q.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.01)

    def _ev_loop(self) -> None:
        while True:
            job = self.q.get()
            try:
                kind = job[0]
                if kind == "open":
                    _, path, head, rows = job
                    self.ev_path = path
                    try:
                        self.ev_f = open(path, "a", encoding="utf-8")
                    except OSError as e:
                        self.log.warn(f"정지 사건 파일을 못 열었다: {e}")
                        self.ev_f = None
                        continue
                    self.ev_f.write(_dumps(head))
                    for _t, row, raw in rows:
                        self._ev_write(row, raw)
                elif self.ev_f is None:
                    continue
                elif kind == "row":
                    self._ev_write(job[1], job[2])
                elif kind == "line":
                    self.ev_f.write(_dumps(job[1]))
                elif kind == "close":
                    self._close_event()
            except Exception as e:  # noqa: BLE001 — 기록 실패로 판정 노드를 죽이지 않는다
                self.log.warn(f"정지 사건 기록 실패: {e}")
            finally:
                self.q.task_done()

    def _ev_write(self, row: dict, raw) -> None:
        if self.ev_f is None:
            return
        if raw is not None and "raw" not in row:
            pts = pick_event_pts(raw, self.event_raw_pts, self.event_box)
            row = {**row, "raw": _pts_list(pts)}
        try:
            self.ev_f.write(_dumps(row))
        except (OSError, ValueError):
            self._close_event()

    def _close_event(self) -> None:
        if self.ev_f is None:
            return
        try:
            self.ev_f.close()
        except OSError:
            pass
        self.ev_f = None
        self.log.info(f"정지 사건 기록: {self.ev_path}")
        self._prune_events()

    def _prune_events(self) -> None:
        gone = prune(self.ev_dir, "evt_*.jsonl", self.ev_total_bytes, keep=self.ev_path if self.ev_f else "",
                     max_age_sec=self.ev_keep_sec)
        if gone:
            self.log.info(f"정지 사건 보관 기한·상한 — {len(gone)}개 지움")


def _quantiles(z: np.ndarray):
    if z.size == 0:
        return None
    return [round(float(v), 3) for v in np.percentile(z, (0, 10, 50, 90, 100))]


def parse_cloud(msg, crop=None):
    """PointCloud2 → ((M,3) float32 (x 앞, y 왼쪽, z 위), 전체 점 수). 필드 오프셋을 메시지에서 읽는다.

    crop=(x0, x1, w) 면 앞 x0~x1, 좌우 ±w 안의 점만 꺼낸다 — 10만 점을 통째로 복사하지 않는다(10-29 처리 63ms).
    """
    off = {f.name: f.offset for f in msg.fields}
    n = msg.width * msg.height
    if n == 0 or "x" not in off or "y" not in off:
        return np.zeros((0, 3), np.float32), 0
    names = ["x", "y"] + (["z"] if "z" in off else [])
    dt = np.dtype({"names": names, "formats": ["<f4"] * len(names),
                   "offsets": [off[k] for k in names], "itemsize": msg.point_step})
    rec = np.frombuffer(msg.data, dt, count=n)          # 복사 없이 보기만 한다
    x, y = rec["x"], rec["y"]
    keep = np.isfinite(x) & np.isfinite(y)
    if crop is not None:
        keep &= (x >= crop[0]) & (x <= crop[1]) & (np.abs(y) <= crop[2])
    idx = np.nonzero(keep)[0]
    z = rec["z"][idx] if "z" in off else np.zeros(len(idx), np.float32)
    pts = np.stack([x[idx], y[idx], z], 1).astype(np.float32)
    return pts[np.isfinite(pts[:, 2])], n


class Guard(Node):
    def __init__(self) -> None:
        super().__init__("obstacle_guard")
        self.p = Params.from_env()
        self.det = Detector(self.p)
        self.cam = CameraView(CamParams.from_env())   # 관제 vision 서비스가 보내는 앞 사람(C3)
        self.kind = None                              # 이번 정지의 구분(person|moving|object|unknown)
        self.move_mps = float(os.environ.get("OBSTACLE_MOVING_MPS", "0.25"))   # 이보다 빠르면 움직이는 물건
        self.stop_t = 0.0
        self.pose = PoseTracker()
        self.lock = threading.Lock()
        self.armed_until = 0.0
        self.req_record = False
        self.suppress = False
        self.stops: list = []
        self.stopped = False
        self.episode = 0
        self.state = "unknown"
        self.last_dist = None
        self.scan_t = 0.0
        self.pose_t = 0.0
        self.pose_ok = False
        self.walls: WallMap | None = None
        self.map_key = None
        self.map_checked = 0.0
        self.ignore_n = 0
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.port = int(os.environ.get("OBSTACLE_GUARD_PORT", str(GUARD_PORT)))
        self.record_only = _env_on("OBSTACLE_RECORD_ONLY")
        self.every_n = max(1, int(os.environ.get("OBSTACLE_EVERY_N", "1") or 1))
        self.raw_every = max(1, int(os.environ.get("OBSTACLE_RAW_EVERY", "10") or 10))
        self.scan_no = 0
        self.judged = 0
        folder = os.environ.get("OBSTACLE_RECORD_DIR", "").strip()

        def env_f(name, default):
            try:
                return float(os.environ.get(name, "").strip() or default)
            except ValueError:
                return default
        # 감지가 꺼져 있을 때(서 있음·충전 중)도 위치를 이 간격으로 남긴다 — 서 있는 동안 측위가 밀리는지 보려고(11-05). 0 = 끔
        self.idle_sec = env_f("OBSTACLE_IDLE_POSE_SEC", 1.0)
        self.idle_last = 0.0
        # OBSTACLE_RECORD_MAX_MB(예전: 이 크기에서 기록을 멈춤)는 쓰지 않는다 — 파일 크기는 FILE_MB, 합계는 TOTAL_MB
        self.rec = (Recorder(folder, self.get_logger(),
                             file_mb=env_f("OBSTACLE_RECORD_FILE_MB", 100), total_mb=env_f("OBSTACLE_RECORD_TOTAL_MB", 2000),
                             event_keep_days=env_f("OBSTACLE_EVENT_KEEP_DAYS", 30),
                             event_total_mb=env_f("OBSTACLE_EVENT_TOTAL_MB", 500),
                             pre_sec=env_f("OBSTACLE_EVENT_PRE_SEC", 5), post_sec=env_f("OBSTACLE_EVENT_POST_SEC", 5),
                             event_raw_pts=int(env_f("OBSTACLE_EVENT_RAW_PTS", 1500)),
                             event_box=(self.p.x0, self.p.x1, self.p.w))
                    if folder else None)

        qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE, depth=1)
        self.create_subscription(PointCloud2, SCAN_TOPIC, self.on_scan, qos)
        threading.Thread(target=self.serve, daemon=True).start()
        threading.Thread(target=self.poll_pose, daemon=True).start()
        self.get_logger().info(f"장애물 감지 시작: {SCAN_TOPIC}, 관제 UDP {self.port}, 지도 {MAP_DIR}, "
                               f"{'기록만(정지 안 함) · ' if self.record_only else ''}{self.every_n}프레임마다, {self.p}")

    # ── 지도 ──
    def _check_map(self) -> None:
        now = time.monotonic()
        if now - self.map_checked < MAP_CHECK_SEC:
            return
        self.map_checked = now
        pgm, yml = os.path.join(MAP_DIR, "occ_grid.pgm"), os.path.join(MAP_DIR, "occ_grid.yaml")
        try:
            ign_mt = os.path.getmtime(IGNORE_FILE) if os.path.exists(IGNORE_FILE) else None
            key = (os.path.realpath(MAP_DIR), os.path.getmtime(pgm), os.path.getmtime(yml), ign_mt)
        except OSError:
            if self.walls is not None:
                self.get_logger().warn("활성 지도 파일이 없다 — 판정을 멈춘다")
            self.walls, self.map_key = None, None
            return
        if key == self.map_key:
            return
        try:
            zones = self._ignore_zones(os.path.basename(key[0]))
            self.walls = WallMap.load(pgm, yml, self.p.tol, zones, wide_tol=self.p.amb_m)
            self.map_key = key
            self.ignore_n = len(zones)
            self.get_logger().info(f"지도 읽음: {key[0]} (무시 구역 {len(zones)}곳"
                                   + (": " + ", ".join(str(z.get("name", "?")) for z in zones) if zones else "") + ")")
        except Exception as e:  # noqa: BLE001 — 지도가 깨졌으면 판정하지 않는다
            self.walls, self.map_key = None, None
            self.get_logger().error(f"지도 읽기 실패: {e}")

    def _ignore_zones(self, map_name: str) -> list:
        """무시 구역 파일에서 이 지도 이름의 구역. 파일이 없거나 깨졌으면 빈 목록."""
        try:
            with open(IGNORE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            self.get_logger().error(f"무시 구역 파일 읽기 실패(무시 구역 없이 판단): {e}")
            return []
        zones = data.get(map_name) or []
        return [z for z in zones if isinstance(z, dict)]

    # ── 위치 ──
    def poll_pose(self) -> None:
        """위치를 0.1초마다 쌓는다 — 본체(1007/2)에 직접 묻고, 답이 없으면 receiver POSITION(0.5초마다 바뀜)."""
        body = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        body.settimeout(BODY_TIMEOUT)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.3)
        src = None
        while rclpy.ok():
            got = query_body(body, BODY_ADDR) if BODY_ADDR else None
            now_src = "본체 0.1초" if got else "receiver 0.5초"
            if got is None:
                got = query_receiver(sock)
            if got is not None:
                t, x, y, yaw, loc = got
                with self.lock:
                    self.pose.update(t, x, y, yaw)
                    self.pose_t = t
                    self.pose_ok = loc in (0, None)
                if now_src != src:
                    self.get_logger().info(f"위치 받는 곳: {now_src}")
                    src = now_src
            time.sleep(POSE_POLL_SEC)

    # ── 관제 ──
    def serve(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", self.port))
        while rclpy.ok():
            try:
                data, addr = sock.recvfrom(8192)
                req = json.loads(data.decode())
            except (OSError, ValueError):
                continue
            if req.get("action") == "CAMERA":
                # 관제 카메라: age = 찍힌 뒤 지난 시간(영상 지연 포함). 받은 시각에서 빼 찍힌 시각으로 되돌린다
                with self.lock:
                    self.cam.push(time.monotonic() - float(req.get("age", 0.5)), req.get("persons") or [])
                continue
            if req.get("action") != "GUARD":
                continue
            with self.lock:
                if req.get("arm"):
                    if time.monotonic() >= self.armed_until:
                        self.get_logger().info("감지 켬(관제 안내 이동)")
                        self._disarm_state()
                    self.armed_until = time.monotonic() + float(req.get("ttl", 3.0))
                    self.req_record = bool(req.get("record"))
                    self.suppress = bool(req.get("suppress"))
                    self.stops = req.get("stops") or []
                else:
                    self.armed_until = 0.0
                    self._disarm_state()
                now = time.monotonic()
                quiet = self.record_only or self.req_record
                reply = {"armed": now < self.armed_until, "recordOnly": quiet,
                         "state": self.state, "stopped": self.stopped and not quiet,
                         "episode": self.episode, "lastDist": self.last_dist,
                         "scanAge": round(now - self.scan_t, 2) if self.scan_t else None,
                         "poseAge": round(now - self.pose_t, 2) if self.pose_t else None,
                         "map": self.map_key[0] if self.map_key else None, "ignoreZones": self.ignore_n,
                         "kind": self.kind, "camFresh": self.cam.fresh(now),
                         "camAge": round(now - self.cam.t, 2) if self.cam.t is not None else None}
            sock.sendto(json.dumps(reply).encode(), addr)

    def _disarm_state(self) -> None:
        self.det.reset()
        self.state, self.stopped, self.last_dist = "unknown", False, None

    # ── 판정 ──
    def on_scan(self, msg) -> None:
        now = time.monotonic()
        self.scan_no += 1
        if self.scan_no % self.every_n:
            return
        self._check_map()
        with self.lock:
            self.scan_t = now
            if now >= self.armed_until:
                if self.state != "unknown":
                    self.get_logger().info("감지 끔(관제 신호 끊김)")
                    self._disarm_state()
                self._record_idle(now)
                return
            pose = self.pose.at(now)
            pose_age = round(now - self.pose_t, 2) if self.pose_t else None
            if self.walls is None or pose is None or now - self.pose_t > POSE_MAX_AGE or not self.pose_ok:
                why = ("지도·위치 없음" if self.walls is None or pose is None
                       else "측위 상실" if not self.pose_ok else "위치 오래됨")
                self._record(now, pose, pose_age, None, None, why)
                return                      # 위치·지도를 못 믿는다 — 판정을 멈추고 앞 상태를 둔다
            t0 = time.perf_counter()
            raw, n_all = parse_cloud(msg, crop=(0.0, RAW_BOX[1], RAW_BOX[2]))   # 판단·기록에 쓰는 앞쪽만
            pts = self.det.filter_z(raw)
            near = self.suppress or near_any(pose[:2], self.stops, self.p.goal_m)
            yaw_rate = math.degrees(self.pose.vel[2])      # receiver 위치 두 개로 낸 회전 속도(지금까지 정보만)
            turning = self.det.turning(yaw_rate, now)
            cam_r = self.cam.step(now, pose, self.walls, turning, near)
            blocked = self.det.update(pts[:, 0], pts[:, 1], pose, self.walls, now, near, yaw_rate, cam_r,
                                      pose_t=self.pose_t)
            ms = round((time.perf_counter() - t0) * 1000, 1)
            self.judged += 1
            self.last_dist = None if math.isinf(self.det.last_rng) else round(self.det.last_rng, 2)
            prev = self.state
            if blocked:
                self.state = "blocked"
            elif self.det.clear_for >= self.p.t_clear - 1e-9:
                self.state = "clear"            # 판정 시작 뒤 t_clear 동안 빈 것을 봐야 clear(켠 직후 unknown)
            if blocked and not self.stopped:
                self.stopped, self.episode = True, self.episode + 1
                self.stop_t = now
                self.kind = "person" if self.det.last_src == "camera" else None
                # 취소부터 보낸다 — 기록은 그 뒤(11-03: 기록을 먼저 해 취소가 약 1.1초 늦었다)
                if self.record_only or self.req_record:
                    self.get_logger().warn(f"[기록만] 앞 막힘 — 정지했을 것 (#{self.episode}, 거리 {self.last_dist}m)")
                else:
                    self.tx.sendto(json.dumps({"action": "CANCEL_NAV"}).encode(), RECEIVER_ADDR)
                    self.get_logger().warn(f"앞 막힘 → 이동 취소 (#{self.episode}, 거리 {self.last_dist}m)")
                if self.rec is not None:
                    self.rec.start_event(self.episode, {"dist": self.last_dist, "src": self.det.last_src,
                                                        "recordOnly": bool(self.record_only or self.req_record)})
            elif self.state == "clear" and self.stopped:
                self.stopped = False
                self.kind = None
                self.get_logger().info(f"앞 풀림 (#{self.episode})")
            if self.stopped and self.kind not in ("person", "moving") and now - self.stop_t <= 2.0:
                # 영상이 0.4초 늦다 — 멈춘 뒤 2초까지 다시 본다. 사람·움직임은 한 번 나오면 그대로 둔다
                k = self.cam.kind(now, self.last_dist)
                v = self.det.speed(now)
                if k != "person" and v is not None and v >= self.move_mps:
                    k = "moving"
                if k != self.kind:
                    self.get_logger().info(f"정지 #{self.episode} 구분: {k}")
                self.kind = k
            if prev != self.state and self.state != "blocked":
                self.get_logger().info(f"상태 {prev} → {self.state}")
            self._record(now, pose, pose_age, raw, ms, None, n_all)

    def _record_idle(self, now) -> None:
        """감지가 꺼진 동안 idle_sec 마다 위치 한 줄(점·판정 없이). 판정·정지에는 관여하지 않는다."""
        if self.rec is None or self.idle_sec <= 0 or now - self.idle_last < self.idle_sec:
            return
        self.idle_last = now
        pose = self.pose.at(now) if self.pose_t else None
        pose_age = round(now - self.pose_t, 2) if self.pose_t else None
        why = ("감지 꺼짐·위치 없음" if pose is None
               else "감지 꺼짐·측위 상실" if not self.pose_ok
               else "감지 꺼짐·위치 오래됨" if pose_age > POSE_MAX_AGE else "감지 꺼짐")
        self._record(now, pose, pose_age, None, None, why)

    def _record(self, now, pose, pose_age, raw, ms, skip, n_all=None) -> None:
        """기록 한 줄(기록 폴더를 줬을 때만). raw 는 높이(z) 거르기 전 점."""
        if self.rec is None:
            return
        row = {"t": round(time.time(), 2), "pose": None if pose is None else [round(v, 3) for v in pose],
               "poseAge": pose_age, "recordOnly": self.record_only or self.req_record}
        if skip:
            row["skip"] = skip
            self.rec.write(row)
            return
        p = self.p
        x, y, z = raw[:, 0], raw[:, 1], raw[:, 2]
        box = (x >= p.x0) & (x <= p.x1) & (np.abs(y) <= p.w)
        wide = (x >= RAW_BOX[0]) & (x <= RAW_BOX[1]) & (np.abs(y) <= RAW_BOX[2])
        row.update(n=int(n_all if n_all is not None else len(raw)), box=int(box.sum()),
                   zBox=_quantiles(z[box]), zWide=_quantiles(z[wide]),
                   blocked=self.state == "blocked", minR=self.last_dist, ms=ms, episode=self.episode,
                   yawRate=round(math.degrees(self.pose.vel[2]), 1), turnSkip=now < self.det.turn_until,
                   ambWait=self.det.amb_wait,
                   src=self.det.last_src if self.state == "blocked" else None, kind=self.kind,
                   camFresh=self.cam.fresh(now), camN=len(self.cam.persons),
                   mps=None if self.state != "blocked" or self.det.speed(now) is None else round(self.det.speed(now), 2))
        w = raw[wide]
        if len(w) > RAW_MAX_PTS:                # 고르게 뽑는다 — 앞에서 자르면 바닥 빔이 빠진다
            w = w[np.linspace(0, len(w) - 1, RAW_MAX_PTS).astype(int)]
        if self.judged % self.raw_every == 0:
            row["raw"] = _pts_list(w)
        self.rec.write(row, raw=w)              # 사건 파일은 매 프레임 원본 점을 쓴다(연속 파일은 raw_every 마다)


def main() -> None:
    rclpy.init()
    node = Guard()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
