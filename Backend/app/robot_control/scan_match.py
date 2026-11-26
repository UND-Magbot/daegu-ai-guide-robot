"""위치 재조정 — 스캔이 지도 벽과 얼마나 맞는지(점수)와 자동 맞춤(11-05).

로봇이 보고하는 측위 '정상'(Location=0)만으로는 틀린 위치를 못 거른다 — 669 는 매칭 오차 경고를
남기지 않고, 지도와 어긋나도 0 을 보냈다(10-30·11-05). 그래서 관제가 따로 잰다.

- 스캔: relay_map 이 보내는 /LOC_BODY_POINTS 의 x·y(로봇 기준, z 없음, 최대 약 1500점).
  바닥·천장 점이 섞여 있어 방향(1°)마다 가장 먼 점만 남긴다 — 그 방향에서 가장 먼 것이 벽이다.
- 지도: 운영 맵 PGM 의 실제 벽(via_route._load_grid 와 같은 판정) → 벽까지 거리표(m).
- 점수: 남긴 점 중 벽 _INLIER_M 안에 드는 비율(%).
- 자동 맞춤: 찍은 자리 주변(±_SEARCH_XY_M, ±_SEARCH_YAW_DEG)을 거칠게 훑고 가장 좋은 곳 둘레를 촘촘히.
  다른 자리(_AMBIG_SEP_M 이상 떨어짐)가 거의 같은 점수면 '모호함' — 긴 통로처럼 앞뒤 구분이 안 되는 곳.
로봇에는 아무것도 보내지 않는다. 주입은 운영자가 '적용'을 눌렀을 때 기존 /robot/initpose 로.
"""
from __future__ import annotations

import math
import threading
from typing import Optional

import cv2
import numpy as np

_BIN_DEG = 1.0            # 방향 묶음 폭 — 묶음마다 가장 먼 점 하나
_MIN_RANGE_M = 0.5        # 몸·다리에 맞은 점 제외
_MAX_RANGE_M = 25.0
_INLIER_M = 0.15          # 벽에서 이 안이면 '맞음'
_CLIP_M = 0.5             # 비용 계산 때 거리 상한 — 지도에 없는 물건 하나가 전체를 끌지 않게
_SEARCH_XY_M = 1.0
_SEARCH_YAW_DEG = 30.0
_COARSE_XY_M = 0.1
_COARSE_YAW_DEG = 2.0
_FINE_XY_M = 0.02
_FINE_YAW_DEG = 0.5
_AMBIG_SEP_M = 0.5
_AMBIG_SCORE_GAP = 3.0    # 점수(%) 차이가 이 안이면 모호
_MIN_POINTS = 40          # 이보다 적으면 판단하지 않는다
_EDGE_ROUNDS = 3          # 창 가장자리에서 다시 찾는 최대 횟수(시드에서 최대 약 3m·90°까지 따라감)
# 층 도착 시드(방향 자동) — 엘리베이터 앞에 세운 오차 ±0.5m, 다른 방향으로 볼 차이 20°
_SEED_XY_M = 0.5
_SEED_ALT_DEG = 20.0
# 이 점수 미만이면 스캔으로 정한 방향을 쓰지 않고 저장 방향으로 넣는다(화면 '잘 맞음' 기준과 같음)
SEED_MIN_SCORE = 75.0

_field_cache: dict[int, tuple[int, "_Field"]] = {}
_field_lock = threading.Lock()


class _Field:
    """벽까지 거리표. 지도 밖은 _CLIP_M 으로 본다."""

    def __init__(self, walls: np.ndarray, resolution: float, origin: tuple[float, float]):
        free = (~walls).astype(np.uint8)
        self.dist = cv2.distanceTransform(free, cv2.DIST_L2, 5).astype(np.float32) * resolution
        self.h, self.w = walls.shape
        self.res = resolution
        self.ox, self.oy = origin

    def sample(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        col = np.floor((xs - self.ox) / self.res).astype(np.int64)
        row = self.h - 1 - np.floor((ys - self.oy) / self.res).astype(np.int64)
        inside = (col >= 0) & (col < self.w) & (row >= 0) & (row < self.h)
        out = np.full(xs.shape, _CLIP_M, np.float32)
        out[inside] = self.dist[row[inside], col[inside]]
        return out


def field_for_grid(map_id: int, grid) -> _Field:
    """via_route 의 격자 지도로 거리표를 만든다(지도 객체가 바뀔 때만 다시)."""
    with _field_lock:
        hit = _field_cache.get(map_id)
        if hit and hit[0] == id(grid):
            return hit[1]
    f = _Field(grid.raw, grid.resolution, grid.origin)
    f.unknown = getattr(grid, "unknown", None)   # 전역 탐색 후보에서 회색(미탐색) 칸을 뺀다
    with _field_lock:
        _field_cache[map_id] = (id(grid), f)
    return f


def to_laser(points: list | np.ndarray) -> np.ndarray:
    """로봇 기준 점들 → 방향마다 가장 먼 점(2D 레이저처럼). (N,2)."""
    p = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(p) == 0:
        return p
    r = np.hypot(p[:, 0], p[:, 1])
    keep = np.isfinite(r) & (r >= _MIN_RANGE_M) & (r <= _MAX_RANGE_M)
    p, r = p[keep], r[keep]
    if len(p) == 0:
        return p
    b = np.floor((np.degrees(np.arctan2(p[:, 1], p[:, 0])) + 180.0) / _BIN_DEG).astype(np.int64)
    order = np.lexsort((-r, b))            # 묶음별로 먼 점이 앞
    first = np.ones(len(order), bool)
    first[1:] = b[order][1:] != b[order][:-1]
    return p[order[first]]


def _pose_dists(f: _Field, laser: np.ndarray, xs: np.ndarray, ys: np.ndarray, yaw: float) -> np.ndarray:
    """한 방향(yaw)에서 여러 위치(xs, ys) 후보의 점별 벽 거리. (후보 수, 점 수)."""
    c, s = math.cos(yaw), math.sin(yaw)
    rx = laser[:, 0] * c - laser[:, 1] * s
    ry = laser[:, 0] * s + laser[:, 1] * c
    return f.sample(xs[:, None] + rx[None, :], ys[:, None] + ry[None, :])


def _score(d: np.ndarray) -> np.ndarray:
    return (d <= _INLIER_M).mean(axis=-1) * 100.0


def score_pose(f: _Field, laser: np.ndarray, x: float, y: float, yaw: float) -> dict:
    d = _pose_dists(f, laser, np.array([x]), np.array([y]), yaw)[0]
    return {"score": round(float(_score(d)), 1), "mean_err": round(float(np.minimum(d, _CLIP_M).mean()), 3)}


def _search(f: _Field, laser: np.ndarray, x: float, y: float, yaw: float,
            xy_half: float, xy_step: float, yaw_half: float, yaw_step: float):
    """격자 탐색 — 후보마다 비용(벽 거리 평균, 상한 _CLIP_M). 낮을수록 좋다.
    돌려주는 것: (비용, x, y, yaw) 배열 — 모든 후보."""
    offs = np.arange(-xy_half, xy_half + 1e-9, xy_step)
    gx, gy = np.meshgrid(offs, offs)
    xs, ys = x + gx.ravel(), y + gy.ravel()
    rows = []
    for dy in np.arange(-yaw_half, yaw_half + 1e-9, yaw_step):
        a = yaw + dy
        cost = np.minimum(_pose_dists(f, laser, xs, ys, a), _CLIP_M).mean(axis=1)
        rows.append(np.stack([cost, xs, ys, np.full(len(xs), a)], 1))
    return np.concatenate(rows)


def _coarse_from(f: _Field, laser: np.ndarray, x: float, y: float, yaw: float) -> np.ndarray:
    """(x, y, yaw) 에서 거칠게 훑기. 최선이 탐색 창 가장자리면 그 자리에서 한 번 더
    (시드가 창보다 멀리 어긋난 경우 — 시뮬 1.5m 어긋남 1/20 → 20/20)."""
    cx, cy, ca = x, y, yaw
    for _ in range(_EDGE_ROUNDS):
        coarse = _search(f, laser, cx, cy, ca, _SEARCH_XY_M, _COARSE_XY_M,
                         math.radians(_SEARCH_YAW_DEG), math.radians(_COARSE_YAW_DEG))
        best = coarse[np.argmin(coarse[:, 0])]
        edge = (max(abs(best[1] - cx), abs(best[2] - cy)) >= _SEARCH_XY_M - _COARSE_XY_M / 2
                or abs(best[3] - ca) >= math.radians(_SEARCH_YAW_DEG - _COARSE_YAW_DEG / 2))
        if not edge:
            break
        cx, cy, ca = best[1], best[2], best[3]
    return coarse


def _finish(f: _Field, laser: np.ndarray, coarse: np.ndarray, far_mask_fn) -> dict:
    """거친 후보들 → 최선 둘레 촘촘히 + 모호함(far_mask_fn 이 '다른 자리'로 보는 후보 중 최고와 비교)."""
    best = coarse[np.argmin(coarse[:, 0])]
    fine = _search(f, laser, best[1], best[2], best[3], _COARSE_XY_M, _FINE_XY_M,
                   math.radians(_COARSE_YAW_DEG), math.radians(_FINE_YAW_DEG))
    bx, by, ba = fine[np.argmin(fine[:, 0])][1:]
    ba = math.atan2(math.sin(ba), math.cos(ba))
    out = {"x": round(float(bx), 3), "y": round(float(by), 3), "yaw": round(float(ba), 4),
           **score_pose(f, laser, bx, by, ba)}
    far = far_mask_fn(coarse, bx, by, ba)
    if far.any():
        alt = coarse[far][np.argmin(coarse[far][:, 0])]
        alt_s = score_pose(f, laser, alt[1], alt[2], alt[3])["score"]
        out["alt"] = {"x": round(float(alt[1]), 3), "y": round(float(alt[2]), 3),
                      "yaw": round(float(alt[3]), 4), "score": alt_s}
        out["ambiguous"] = out["score"] - alt_s <= _AMBIG_SCORE_GAP
    else:
        out["ambiguous"] = False
    return out


def _yaw_diff_deg(a: np.ndarray, b: float) -> np.ndarray:
    return np.abs(np.degrees(np.arctan2(np.sin(a - b), np.cos(a - b))))


def refine_pose(f: _Field, laser: np.ndarray, x: float, y: float, yaw: float) -> dict:
    """찍은 자리 주변에서 가장 잘 맞는 위치·방향. 모호하면 다른 후보를 함께 돌려준다.

    방향은 끈 방향 ±30° 와 그 반대(+180°) ±30° 를 둘 다 본다 — 10-30 1층 도착 때 저장 방향과 실제가
    약 160° 달랐다(분석보고 11-05 §6). 반대쪽이 더 잘 맞으면 그쪽을 고른다.
    """
    coarse = np.concatenate([_coarse_from(f, laser, x, y, yaw),
                             _coarse_from(f, laser, x, y, yaw + math.pi)])
    return _finish(f, laser, coarse,
                   lambda c, bx, by, ba: (np.hypot(c[:, 1] - bx, c[:, 2] - by) >= _AMBIG_SEP_M)
                   | (_yaw_diff_deg(c[:, 3], ba) >= _SEED_ALT_DEG))


def heading_free_pose(f: _Field, laser: np.ndarray, x: float, y: float) -> dict:
    """방향을 모를 때(층 도착 시드) — 좌표 ±_SEED_XY_M 에서 방향 360° 를 다 훑는다.

    모호함은 '다른 방향(_SEED_ALT_DEG 이상 차이)' 후보와 비교한다. 시뮬(맵 47·49, 아무 방향·0.5m 어긋남)
    30/30 성공, 다른 방향 후보와 점수 차 최소 38%p.
    """
    coarse = _search(f, laser, x, y, 0.0, _SEED_XY_M, _COARSE_XY_M, math.pi, math.radians(_COARSE_YAW_DEG))
    return _finish(f, laser, coarse,
                   lambda c, bx, by, ba: _yaw_diff_deg(c[:, 3], ba) >= _SEED_ALT_DEG)


def seed_from_scan(f: _Field, points: list, x: float, y: float) -> dict:
    """층 도착 시드 결정 — 쓸 만하면 ok=True 와 위치·방향, 아니면 ok=False 와 사유."""
    laser = to_laser(points)
    if len(laser) < _MIN_POINTS:
        return {"ok": False, "reason": f"스캔 점이 적음({len(laser)}개)", "n": int(len(laser))}
    fit = heading_free_pose(f, laser, x, y)
    fit["n"] = int(len(laser))
    if fit["ambiguous"]:
        return {"ok": False, "reason": "비슷하게 맞는 방향이 또 있음", "fit": fit}
    if fit["score"] < SEED_MIN_SCORE:
        return {"ok": False, "reason": f"지도와 맞음 {fit['score']:.0f}% (기준 {SEED_MIN_SCORE:.0f}%)", "fit": fit}
    return {"ok": True, "fit": fit}


# ── 지도 전체 탐색(11-06) — 측위 상실 뒤 시드 없이 위치 찾기 ──
# 11-06 맵 47 실제 스캔 6곳 30장: 전부 맞게 찾음(정지 시 보고 위치와 ≤0.14m), 장당 1.4~2.8초,
# 1·2등 점수 차 최소 17(원무과 접수 — 2.2m 옆이 75~78%). _자료/2026-11-06_전역재측위
_GLOBAL_STEP_M = 0.3        # 후보 위치 간격
_GLOBAL_YAW_DEG = 6.0       # 후보 방향 간격
_GLOBAL_WALL_M = 0.3        # 벽에서 이보다 가까운 칸은 로봇이 설 수 없어 후보에서 뺀다
_GLOBAL_SUB_POINTS = 90     # 거친 훑기에 쓰는 점 수(속도)
_GLOBAL_TOP = 8             # 거친 훑기 상위 몇 곳을 다듬을지
_GLOBAL_SEP_M = 1.0         # '다른 자리'로 보는 거리
GLOBAL_MIN_GAP = 10.0       # 1등이 다른 자리 2등보다 이만큼(점수 %p) 높아야 채택


def _global_cands(f: _Field) -> tuple[np.ndarray, np.ndarray]:
    hit = getattr(f, "_gcands", None)
    if hit is not None:
        return hit
    ok = f.dist >= _GLOBAL_WALL_M
    unknown = getattr(f, "unknown", None)
    if unknown is not None and unknown.shape == ok.shape:
        ok &= ~unknown
    step = max(1, int(round(_GLOBAL_STEP_M / f.res)))
    rows, cols = np.nonzero(ok[::step, ::step])
    xs = f.ox + (cols * step + 0.5) * f.res
    ys = f.oy + (f.h - 1 - rows * step + 0.5) * f.res
    f._gcands = (xs, ys)
    return f._gcands


def global_locate(f: _Field, points: list) -> dict:
    """시드 없이 지도 전체에서 스캔이 가장 잘 맞는 위치·방향. ok 면 넣어도 되는 결과.

    거친 훑기(빈칸 0.3m × 방향 6°) → 서로 1m 이상 떨어진 상위 8곳을 refine_pose 로 다듬기 →
    1등 점수 SEED_MIN_SCORE 이상, 다른 자리 2등보다 GLOBAL_MIN_GAP 이상 높을 때만 ok.
    """
    laser = to_laser(points)
    n = int(len(laser))
    if n < _MIN_POINTS:
        return {"ok": False, "reason": f"스캔 점이 적음({n}개)", "n": n}
    xs, ys = _global_cands(f)
    if len(xs) == 0:
        return {"ok": False, "reason": "지도에 후보 칸이 없음", "n": n}
    sub = laser[:: max(1, n // _GLOBAL_SUB_POINTS)]
    rows = []
    for yd in np.arange(0.0, 360.0, _GLOBAL_YAW_DEG):
        a = math.radians(yd)
        cost = np.minimum(_pose_dists(f, sub, xs, ys, a), _CLIP_M).mean(axis=1)
        rows.append(np.stack([cost, xs, ys, np.full(len(xs), a)], 1))
    allc = np.concatenate(rows)
    picks: list = []
    for i in np.argsort(allc[:, 0]):
        c = allc[i]
        if all(math.hypot(c[1] - p[1], c[2] - p[2]) >= _GLOBAL_SEP_M for p in picks):
            picks.append(c)
            if len(picks) >= _GLOBAL_TOP:
                break
    fits = sorted((refine_pose(f, laser, c[1], c[2], c[3]) for c in picks), key=lambda r: -r["score"])
    top = fits[0]
    second = next((r for r in fits[1:]
                   if math.hypot(r["x"] - top["x"], r["y"] - top["y"]) >= _GLOBAL_SEP_M), None)
    gap = top["score"] - (second["score"] if second else 0.0)
    out = {"fit": top, "second": second, "gap": round(float(gap), 1), "n": n}
    if top["score"] < SEED_MIN_SCORE:
        return {"ok": False, "reason": f"지도와 맞음 {top['score']:.0f}% (기준 {SEED_MIN_SCORE:.0f}%)", **out}
    if gap < GLOBAL_MIN_GAP:
        return {"ok": False, "reason": f"비슷하게 맞는 다른 자리 있음(점수 차 {gap:.0f})", **out}
    return {"ok": True, **out}


def match(f: _Field, points: list, x: float, y: float, yaw: float, refine: bool) -> dict:
    """화면 요청 한 번 — 점수, 그리고 refine 이면 자동 맞춤 결과."""
    laser = to_laser(points)
    if len(laser) < _MIN_POINTS:
        return {"status": "few_points", "n": int(len(laser)),
                "msg": f"스캔 점이 적어({len(laser)}개) 판단하지 않습니다."}
    res = {"status": "ok", "n": int(len(laser)), "at": score_pose(f, laser, x, y, yaw)}
    if refine:
        res["refined"] = refine_pose(f, laser, x, y, yaw)
    return res
