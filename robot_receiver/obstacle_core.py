"""안내 중 전방 장애물 감지 — 판정 핵심 (2026-10-29). ROS2 없이 시험할 수 있게 obstacle_guard.py 에서 뺐다.

10-18 가상 시뮬 권장안(docs/작업보고/2026-10-18/작업보고_2026-10-18_안내중_장애물정지·자동재개_가상시뮬.md 3장)을 옮겼다.
    막힘 프레임  몸 기준 앞 상자(X0~X1, 좌우 ±W) 안의 점 가운데 지도 벽에서 tol 넘게 떨어진 점이 n_min 개 이상
    막힘        k_on 프레임 연속(10Hz 에서 0.3초)
    해소        t_clear 초 연속 빔. 마지막으로 본 거리가 X0+near_m 안이면(몸 앞 사각지대로 들어왔을 수 있음)
                옆 구역(앞 0~X1, 옆 W~side_w)에서 빠져나가는 것을 본 뒤 side_exit 초 지나야 해소
                시간은 프레임 수로 센다(frame_dt 0.1초 = 10Hz) — 스캔이 늦게 오면 해소도 늦어진다(안전한 쪽)
    정지 지점    남은 정지 지점 goal_m 안에서는 보지 않는다(도착은 로봇 자체 회피에 맡긴다)
점 높이(z): 10-29 669 실기 기록(안내 반복 4바퀴) — 바닥이 몸 기준 -0.49~-0.47m, 천장 2.6~2.8m.
    기본 하한 -0.05(바닥 위 약 0.43m), 상한 1.7. 환경 변수로 바꿀 수 있다.
    -0.30 은 2차 기록(15:13:04)에서 빠르게(1.3m/s) 달리다 감속할 때 앞 바닥이 -0.19~-0.07 로 올라와 막힘이 났다 —
    그보다 낮은 물체(가방·상자)는 로봇 자체 회피에 맡긴다. 사람 다리·몸통은 그 위다(1차 -0.02~+1.19, 2차 -0.15~+0.26).
빠른 회전: 위치가 0.5초마다만 와서 도는 동안 방향을 앞당기는 오차로 벽이 새 물체로 보인다(2차 기록 15:16:06·15:16:39,
    시작 순간 57~92°/s). 회전이 turn_max(°/s)를 넘는 동안과 그 뒤 turn_hold 초는 판단하지 않는다(빈 프레임으로 센다).
무시 구역: 지도에 없지만 판단에서 뺄 둥근 구역(놓였다 치웠다 하는 의자 등). 그 안의 점은 벽처럼 본다.
회전 시작 오탐(11-02): 꺾는 점에서 돌기 시작한 순간은 다음 위치(0.5초 뒤)가 오기 전이라 회전 속도가 0 으로 남아
    turn_max 로 거르지 못한다. 그 사이 지도 벽·물체 점이 옆으로 밀려 새 물체로 보였다(16:39 1F_엘리베이터입구,
    16:53 원무과수납 — 둘 다 '사람 아닌 물체' 3초 정지). 새 점이 모두 지도 벽·물체 amb_m 안이면(애매) 막힘이
    시작된 뒤 새 위치를 한 번 더 받고도 막혀 있어야 막힘으로 본다(최대 약 0.5초 늦음). 새 위치가 회전을 드러내면
    판단 쉼으로 넘어가 멈추지 않는다. 벽에서 떨어진 점(통로 가운데 사람)은 지금처럼 바로 막힘이다.
"""
from __future__ import annotations

import math
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


def _env_f(name: str, default: Optional[float]) -> Optional[float]:
    v = os.environ.get(name, "").strip()
    if not v:
        return default
    if v.lower() == "none":
        return None
    try:
        return float(v)
    except ValueError:
        return default


@dataclass
class Params:
    x0: float = 0.90          # 몸 마스크(앞 0.85m) 바로 바깥부터
    x1: float = 1.50          # 1.8 이면 꺾는 점 정면 유리 자동문(1.57~1.59m)을 막힘으로 봤다(10-29 18:06·10-30 15:10).
                              # 판정 뒤 약 0.6m 더 가서 선다(10-30 15:14 실측 1.22m/s) — 사람 앞 약 0.9m
    w: float = 0.40           # 몸 반폭 0.30 + 0.10
    tol: float = 0.25         # 지도 벽에서 이만큼 안의 점은 벽으로 본다
    n_min: int = 3
    k_on: int = 3             # 연속 막힘 프레임 수
    t_clear: float = 2.0
    goal_m: float = 1.2
    side_w: float = 1.0
    near_m: float = 0.15
    side_exit: float = 1.0
    frame_dt: float = 0.1     # /LOC_BODY_POINTS 약 10Hz(EV_SCAN_COLLECT.md)
    turn_max: float = 30.0    # 이 회전 속도(°/s)를 넘으면 판단하지 않는다 — 2차 기록 회전 막힘 2건을 막고 사람 앞(최대 14°/s)은 그대로
    turn_hold: float = 0.5    # 회전이 줄어든 뒤 이만큼 더 쉰다(위치 앞당김이 따라올 때까지)
    z_min: Optional[float] = -0.05  # 몸 기준 높이(m). 바닥(실측 -0.48)·감속 때 올라오는 앞 바닥을 뺀다. 'none' 이면 거르지 않음
    z_max: Optional[float] = 1.7    # 천장(2.6~2.8m)·높은 표지 점을 뺀다
    amb_m: float = 0.50       # 새 점이 모두 지도 벽에서 이 안이면 애매 — 새 위치로 한 번 더 확인한다. 0 이면 끔
                              # 11-02 16:39 재현에서 밀린 지도 점은 벽에서 0.45m 안이었다

    @classmethod
    def from_env(cls) -> "Params":
        p = cls()
        for name in ("x0", "x1", "w", "tol", "t_clear", "goal_m", "side_w", "near_m", "side_exit", "frame_dt",
                     "z_min", "z_max", "turn_max", "turn_hold", "amb_m"):
            setattr(p, name, _env_f(f"OBSTACLE_{name.upper()}", getattr(p, name)))
        p.n_min = int(_env_f("OBSTACLE_N_MIN", p.n_min))
        p.k_on = int(_env_f("OBSTACLE_K_ON", p.k_on))
        return p


# ── 지도 ─────────────────────────────────────────────
def read_pgm(path: str) -> np.ndarray:
    """P5 pgm → 0~1 실수 배열(위쪽 행이 지도 y 최대)."""
    with open(path, "rb") as f:
        data = f.read()
    tok, pos = [], 0
    while len(tok) < 4:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        s = pos
        while not data[pos:pos + 1].isspace():
            pos += 1
        tok.append(data[s:pos])
    w, h, maxv = int(tok[1]), int(tok[2]), int(tok[3])
    return np.frombuffer(data, np.uint8, w * h, pos + 1).reshape(h, w).astype(np.float32) / float(maxv)


def read_map_yaml(path: str) -> dict:
    """map_server yaml — PyYAML 없이 필요한 키만 읽는다."""
    out = {"resolution": 0.05, "origin": [0.0, 0.0, 0.0], "negate": 0, "occupied_thresh": 0.65}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if ":" not in line or line.lstrip().startswith("#"):
                continue
            k, v = (s.strip() for s in line.split(":", 1))
            if k == "origin":
                out[k] = [float(t) for t in v.strip("[] ").split(",")]
            elif k in ("resolution", "occupied_thresh"):
                out[k] = float(v)
            elif k == "negate":
                out[k] = int(v)
            elif k == "image":
                out[k] = v
    return out


class WallMap:
    """칸마다 '벽에서 tol 안인가' 를 미리 칠해 둔다. 지도 밖은 벽으로 본다(점을 새 물체로 세지 않는다)."""

    def __init__(self, occ: np.ndarray, res: float, origin: tuple[float, float], tol: float,
                 wide_tol: float = 0.0):
        self.res, self.ox, self.oy = res, origin[0], origin[1]
        self.h, self.w = occ.shape
        self.near = self._dilate(occ, tol)
        # 애매 판정용 넓은 띠(amb_m) — 없으면 near 와 같다(애매 판정 끔)
        self.wide = self._dilate(occ, wide_tol) if wide_tol > tol else self.near

    def _dilate(self, occ: np.ndarray, tol: float) -> np.ndarray:
        r = int(math.ceil(tol / self.res))
        near = occ.copy()
        pad = np.pad(occ, r)
        for dr in range(-r, r + 1):
            for dc in range(-r, r + 1):
                if (dr * dr + dc * dc) * self.res * self.res <= tol * tol + 1e-9:
                    near |= pad[r + dr:r + dr + self.h, r + dc:r + dc + self.w]
        return near

    @classmethod
    def load(cls, pgm: str, yaml: str, tol: float, ignore=(), wide_tol: float = 0.0) -> "WallMap":
        meta = read_map_yaml(yaml)
        img = read_pgm(pgm)
        p = img if meta["negate"] else 1.0 - img        # 점유 확률
        occ = p > meta["occupied_thresh"]
        w = cls(occ, meta["resolution"], (meta["origin"][0], meta["origin"][1]), tol, wide_tol)
        w.add_ignore(ignore)
        return w

    def add_ignore(self, zones) -> int:
        """무시 구역 [{x, y, r}] 을 '벽 근처' 로 칠한다 — 그 안의 점은 새 물체로 세지 않는다. 반환: 칠한 구역 수."""
        n = 0
        rows, cols = np.mgrid[0:self.h, 0:self.w]
        cx = self.ox + (cols + 0.5) * self.res
        cy = self.oy + (self.h - 1 - rows + 0.5) * self.res
        for z in zones or ():
            try:
                x, y, r = float(z["x"]), float(z["y"]), float(z["r"])
            except (KeyError, TypeError, ValueError):
                continue
            zone = (cx - x) ** 2 + (cy - y) ** 2 <= r * r
            self.near |= zone
            if self.wide is not self.near:
                self.wide |= zone
            n += 1
        return n

    def near_wall(self, mx: np.ndarray, my: np.ndarray, wide: bool = False) -> np.ndarray:
        c = np.floor((mx - self.ox) / self.res).astype(int)
        r = self.h - 1 - np.floor((my - self.oy) / self.res).astype(int)
        ok = (r >= 0) & (r < self.h) & (c >= 0) & (c < self.w)
        out = np.ones(mx.shape, bool)
        out[ok] = (self.wide if wide else self.near)[r[ok], c[ok]]
        return out


# ── 위치 ─────────────────────────────────────────────
class PoseTracker:
    """receiver 위치(0.5초마다 갱신)를 지금 시각으로 앞당긴다 — 걷는 중 0.5초면 0.27m 어긋나 벽 판정이 틀린다."""

    MAX_AHEAD = 1.0

    def __init__(self) -> None:
        self.last: Optional[tuple[float, float, float, float]] = None   # (t, x, y, yaw)
        self.vel = (0.0, 0.0, 0.0)

    def update(self, t: float, x: float, y: float, yaw: float) -> None:
        if self.last is not None:
            t0, x0, y0, a0 = self.last
            if t - t0 < 1e-3:
                return
            dt = t - t0
            da = (yaw - a0 + math.pi) % (2 * math.pi) - math.pi
            self.vel = ((x - x0) / dt, (y - y0) / dt, da / dt) if dt <= 2.0 else (0.0, 0.0, 0.0)
        self.last = (t, x, y, yaw)

    def at(self, t: float) -> Optional[tuple[float, float, float]]:
        if self.last is None:
            return None
        t0, x, y, a = self.last
        ahead = min(max(0.0, t - t0), self.MAX_AHEAD)
        vx, vy, va = self.vel
        return x + vx * ahead, y + vy * ahead, a + va * ahead


# ── 판정 ─────────────────────────────────────────────
@dataclass
class Detector:
    p: Params = field(default_factory=Params)
    blocked: bool = False
    on: int = 0
    off: int = 0              # 연속 빈 프레임
    last_rng: float = math.inf
    side_seen: bool = False
    side_off: int = 0         # 옆 구역도 빈 연속 프레임
    frames: int = 0
    turn_until: float = -1e9  # 이 시각까지 회전 중이라 판단을 쉰다
    turn_skipped: int = 0
    last_src: str = "lidar"   # 마지막 막힘 근거 — camera 면 사각지대(옆 구역) 해소 조건을 쓰지 않는다
    track: deque = field(default_factory=deque)   # (시각, 지도 x, y) — 앞 상자 새 점 중심, 움직임 판정용
    amb_wait: int = 0         # 애매해서 새 위치를 기다리며 막힘을 미룬 프레임 수(기록·시험용)
    _amb: bool = False        # 이번 프레임 새 점이 모두 지도 벽 amb_m 안인가
    _run_pose_t: Optional[float] = None   # 이번 막힘 연속이 시작될 때의 위치 시각
    _run_amb: bool = True     # 이번 연속의 모든 프레임이 애매했나
    _run_fresh: bool = False  # 연속이 시작된 뒤 새 위치를 받은 프레임이 있었나

    def reset(self) -> None:
        self.blocked, self.on, self.off = False, 0, 0
        self.last_rng, self.side_seen, self.side_off, self.frames = math.inf, False, 0, 0
        self.track.clear()
        self._run_pose_t, self._run_amb, self._run_fresh = None, True, False

    @property
    def clear_for(self) -> float:
        """앞 상자가 빈 지 몇 초(프레임 수 × frame_dt)."""
        return self.off * self.p.frame_dt

    def _to_map(self, qx, qy, pose):
        ex, ey, eth = pose
        c, s = math.cos(eth), math.sin(eth)
        return ex + c * qx - s * qy, ey + s * qx + c * qy

    def count_front(self, bx, by, pose, walls: WallMap) -> tuple[int, float]:
        p = self.p
        box = (bx >= p.x0) & (bx <= p.x1) & (np.abs(by) <= p.w)
        if not box.any():
            return 0, math.inf
        qx, qy = bx[box], by[box]
        mx, my = self._to_map(qx, qy, pose)
        new = ~walls.near_wall(mx, my)
        n = int(new.sum())
        self._amb = False
        if n:
            self._centre = (float(np.median(mx[new])), float(np.median(my[new])))
            self._amb = self.p.amb_m > self.p.tol and bool(walls.near_wall(mx[new], my[new], wide=True).all())
        return n, (float(np.min(np.hypot(qx[new], qy[new]))) if n else math.inf)

    def count_side(self, bx, by, pose, walls: WallMap) -> int:
        p = self.p
        m = (bx >= 0.0) & (bx <= p.x1) & (np.abs(by) > p.w) & (np.abs(by) <= p.side_w)
        if not m.any():
            return 0
        return int((~walls.near_wall(*self._to_map(bx[m], by[m], pose))).sum())

    def filter_z(self, pts: np.ndarray) -> np.ndarray:
        """(N,2|3) → (N,2). z 가 있고 범위가 정해졌으면 바닥·천장 점을 뺀다."""
        if pts.shape[1] >= 3 and (self.p.z_min is not None or self.p.z_max is not None):
            z = pts[:, 2]
            keep = np.ones(len(pts), bool)
            if self.p.z_min is not None:
                keep &= z >= self.p.z_min
            if self.p.z_max is not None:
                keep &= z <= self.p.z_max
            pts = pts[keep]
        return pts[:, :2]

    def turning(self, yaw_rate_dps: Optional[float], now: float) -> bool:
        """빠르게 도는 중인가(끝난 뒤 turn_hold 초 포함)."""
        if yaw_rate_dps is not None and self.p.turn_max and abs(yaw_rate_dps) > self.p.turn_max:
            self.turn_until = now + self.p.turn_hold
        return now < self.turn_until

    def update(self, bx, by, pose, walls: WallMap, now: float, near_stop: bool,
               yaw_rate_dps: Optional[float] = None, cam_r: Optional[float] = None,
               pose_t: Optional[float] = None) -> bool:
        """한 프레임. 반환: 지금 막혀 있는가. now 는 회전 쉼 시각 계산에만 쓴다(막힘·해소는 프레임 수로 센다).
        pose_t: 지금 쓰는 위치를 받은 시각 — 애매한 막힘을 새 위치로 다시 확인하는 데 쓴다(없으면 확인 생략)."""
        p = self.p
        self.frames += 1
        self._centre = None
        n, mr = self.count_front(bx, by, pose, walls)
        if n >= p.n_min and self._centre is not None:
            self.track.append((now, *self._centre))
        while self.track and self.track[0][0] < now - 3.0:
            self.track.popleft()
        if self.turning(yaw_rate_dps, now):
            self.turn_skipped += 1
            n = 0
        if near_stop:
            n = 0
        if n < p.n_min and cam_r is not None and not near_stop:
            # 라이다는 못 봤지만(코앞·회전 중) 카메라가 앞길에서 사람을 이어서 봤다 — 카메라 거리로 막힘
            n, mr, src = p.n_min, cam_r, "camera"
        else:
            src = "lidar"
        if n >= p.n_min:
            if self.on == 0:
                self._run_pose_t, self._run_amb, self._run_fresh = pose_t, True, False
            if src == "camera" or not self._amb:
                self._run_amb = False
            if pose_t is None or self._run_pose_t is None or pose_t > self._run_pose_t + 1e-6:
                self._run_fresh = True
            self.on, self.off = self.on + 1, 0
            self.last_rng = mr
            self.last_src = src
            self.side_seen, self.side_off = False, 0
        else:
            self.on, self.off = 0, self.off + 1
            if self.blocked:
                # 앞 상자가 빈 순간부터 옆 구역을 본다 — 옆으로 빠져나가는 사람은 1초 안에 지나간다
                if self.count_side(bx, by, pose, walls) >= p.n_min:
                    self.side_seen, self.side_off = True, 0
                else:
                    self.side_off += 1
        if not self.blocked and self.on >= p.k_on:
            if self._run_amb and not self._run_fresh:
                self.amb_wait += 1          # 새 점이 모두 지도 벽 근처 — 새 위치로 한 번 더 본 뒤 정한다
            else:
                self.blocked = True
        elif self.blocked and self.off * p.frame_dt >= p.t_clear - 1e-9:
            near = self.last_rng < p.x0 + p.near_m and self.last_src == "lidar"   # 카메라는 사각지대를 본다
            if not near or (self.side_seen and self.side_off * p.frame_dt >= p.side_exit - 1e-9):
                self.blocked = False
                self.last_rng, self.side_seen, self.side_off = math.inf, False, 0
        return self.blocked

    def speed(self, now: float, window: float = 1.0) -> Optional[float]:
        """막은 물체의 지도 위 이동 속도(m/s) — 최근 window 초의 처음·끝 중심 거리. 0.5초치 미만이면 None.
        중심은 보이는 면이 바뀌어도 흔들린다(사람이 돌아서기 등) — 기준 속도는 기록으로 정한다."""
        pts = [q for q in self.track if q[0] >= now - window]
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < 0.5:
            return None
        (t0, x0, y0), (t1, x1, y1) = pts[0], pts[-1]
        return math.hypot(x1 - x0, y1 - y0) / (t1 - t0)


# ── 전방 카메라 (C3, 10-29) ─────────────────────────────
# 관제 vision 서비스(robot_vision/front_person_watch.py)가 사람 박스를 보정식(10-29 보정 기록 62쌍)으로
# 몸 기준 위치(앞 x, 왼쪽 y)로 바꿔 초당 약 10번 보낸다. 사람이 없어도 빈 목록을 보내 '최신' 임을 알린다.
@dataclass
class CamParams:
    x0: float = 0.5          # 라이다 사각지대(0.85m 안)도 카메라는 본다 — 1.06m 안은 발이 화면 밖이라 1.05 로 온다
    x1: float = 1.8
    w: float = 0.4           # 라이다 앞 상자와 같은 폭
    w_turn: float = 0.25     # 빠른 회전 중 — 영상 지연 0.4초 동안 방향이 밀려 옆 사람이 앞으로 들어온다
    conf: float = 0.6        # 피규어·먼 물체는 0.65 이하가 많았다(10-29 기록)
    max_age: float = 0.6     # 이보다 오래된 카메라 정보는 쓰지 않는다(Wi-Fi 끊김)
    k_on: int = 3            # 연속 프레임(라이다와 같은 0.3초)
    kind_window: float = 1.0 # 멈춘 뒤 이 시간 안에 본 사람으로 사람/물건을 가른다
    kind_r_tol: float = 0.6  # 라이다 거리와 카메라 거리 차 허용(카메라 거리 오차 1.5m ±0.1, 2.5m ±0.3)
    kind_w: float = 0.8      # 사람/물건 구분 때 좌우 폭(앞길보다 넓게 — 막은 사람이 옆으로 비키는 중일 수 있다)

    @classmethod
    def from_env(cls) -> "CamParams":
        p = cls()
        for name in ("x0", "x1", "w", "w_turn", "conf", "max_age", "kind_window", "kind_r_tol", "kind_w"):
            setattr(p, name, _env_f(f"OBSTACLE_CAM_{name.upper()}", getattr(p, name)))
        p.k_on = int(_env_f("OBSTACLE_CAM_K_ON", p.k_on))
        return p


class CameraView:
    """카메라 사람 목록(몸 기준) — 앞길 막힘과 사람/물건 구분. 시각은 모두 이 노드의 monotonic(찍힌 시각으로 되돌린 값)."""

    def __init__(self, p: Optional[CamParams] = None) -> None:
        self.p = p or CamParams()
        self.t: Optional[float] = None
        self.persons: list = []
        self.on = 0
        self.hist: deque = deque()

    def push(self, shot_t: float, persons) -> None:
        self.t, self.persons = shot_t, [q for q in persons or () if len(q) >= 3]
        self.hist.append((shot_t, self.persons))
        while self.hist and self.hist[0][0] < shot_t - 3.0:
            self.hist.popleft()

    def fresh(self, now: float) -> bool:
        return self.t is not None and now - self.t <= self.p.max_age

    def hit_now(self, now: float, pose, walls: "WallMap", turning: bool) -> Optional[float]:
        """지금 앞길에 사람이 있으면 그 거리(m), 없으면 None. 무시 구역·벽 근처(지도 좌표)는 뺀다."""
        if not self.fresh(now):
            return None
        w = self.p.w_turn if turning else self.p.w
        best = None
        for x, y, c in (q[:3] for q in self.persons):
            if c < self.p.conf or not (self.p.x0 <= x <= self.p.x1 and abs(y) <= w):
                continue
            if pose is not None:
                ex, ey, eth = pose
                mx = ex + math.cos(eth) * x - math.sin(eth) * y
                my = ey + math.sin(eth) * x + math.cos(eth) * y
                if walls is not None and walls.near_wall(np.array([mx]), np.array([my]))[0]:
                    continue
            r = math.hypot(x, y)
            best = r if best is None or r < best else best
        return best

    def step(self, now: float, pose, walls, turning: bool, off: bool) -> Optional[float]:
        """한 스캔. 연속 k_on 번 앞길에 사람이면 그 거리, 아니면 None. off(정지 지점 근처·감지 끔)면 늘 None."""
        r = None if off else self.hit_now(now, pose, walls, turning)
        self.on = self.on + 1 if r is not None else 0
        return r if self.on >= self.p.k_on else None

    def kind(self, now: float, lidar_r: Optional[float]) -> str:
        """person | object | unknown — 라이다로 멈춘 것을 카메라로 가린다."""
        if not self.fresh(now):
            return "unknown"
        for t, ps in self.hist:
            if t < now - self.p.kind_window:
                continue
            for x, y, c in (q[:3] for q in ps):
                if c < self.p.conf or abs(y) > self.p.kind_w:
                    continue
                if lidar_r is None or abs(math.hypot(x, y) - lidar_r) <= self.p.kind_r_tol:
                    return "person"
        return "object"


def near_any(xy: tuple[float, float], stops, radius: float) -> bool:
    return any(math.hypot(xy[0] - s[0], xy[1] - s[1]) < radius for s in stops or ())
