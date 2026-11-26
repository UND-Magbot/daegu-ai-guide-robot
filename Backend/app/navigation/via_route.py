"""자동 우회 — 로봇이 매핑 벽을 넘어 질러가는 구간을 관제가 경유지로 돌려 보낸다.

벤더 플래너는 저장된 격자 지도의 벽을 절대 기준으로 쓰지 않는다. 실시간 점군으로 '넘을 수
있다'고 보면 매핑 때 벽(확률 1.0)으로 찍힌 선도 넘는다(2026-09-30 실측: UND_병원 x3.0,y2.5).
관제는 목표점 하나만 보내므로 길목을 대신 정해 준다.

규칙: 출발점→목표 직선이 지도의 벽을 가로지르면(벽에 _CLEARANCE_M 이내로 붙어 지나가도),
같은 맵의 **경유지 카테고리 장소**를 노드로 삼아 벽을 넘지 않고 이어지는 가장 짧은 길을 찾아
그 경유지들을 거쳐 간다(중간은 멈추지 않는다). 직선이 깨끗하면 바로 간다. 그런 길이 없거나
우회가 직선의 _MAX_DETOUR_RATIO 배를 넘으면 이동하지 않는다(호출부가 거부). AUTO_DETOUR_DISABLED_MAPS
에 든 맵은 판정하지 않고 바로 간다. ROUTE_GRAPH_MAPS 에 든 맵은 자동 우회보다 먼저, 맵 관리에서 그린 구간을
따라가는 길을 찾는다(구간이 없으면 위 규칙).
장소마다 우회 지점을 지정하지 않아도, 길목에 경유지 장소만 찍어 두면 필요한 이동에만 쓰인다.
"""

from __future__ import annotations

import heapq
import math
import os
import re
import threading
from typing import Optional

import cv2
import numpy as np
from sqlalchemy.orm import Session

from app.database.models import LocationInfo, RobotMapInfo, RouteInfo
from app.navigation.waypoints import is_pass_through_category

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SAMPLE_STEP_M = 0.05      # 직선 검사 간격 — 격자(0.1m)보다 촘촘하게 봐야 한 칸짜리 벽을 놓치지 않는다
_DEFAULT_OCC_THRESH = 0.65
# 직선이 벽에서 이 거리 안으로 지나가면 막힌 것으로 본다. 벽 끝을 아슬아슬하게 비껴가는 직선도
# 로봇은 벽을 넘어 질러가므로, 정확히 벽 칸을 지날 때만 막혔다고 보면 경유지가 빠진다.
# UND_병원 실측: 벽 건너편 장소 쌍은 0.00~0.02m. 같은 쪽 쌍은 작은 장애물 옆을 0.25m 로 지나는
# 경우가 있어(대기의자 이동 후) 0.3m 면 불필요한 우회가 생긴다. 출발점 ±0.3m 흔들림 시험에서
# 0.15m 가 누락 0/240 · 불필요 우회 0/160 (0.2m 는 불필요 8~12건, 0.3m 는 36~40건) — 2026-10-01.
_CLEARANCE_M = 0.15
# 이보다 작은 외딴 점유 덩어리(가로·세로 모두)는 벽이 아니라 기둥·의자 같은 작은 장애물로 보고 뺀다.
# 로봇 플래너는 이런 것은 스스로 비켜 가지만, 벽으로 치면 엉뚱하게 먼 경유지로 돌아간다
# (2026-10-01 실측: 충전 대기 자리→호출 1.6m 사이 0.2m 덩어리 때문에 3.6m 떨어진 경유지로 우회 지시,
# 로봇은 그 경유지 NAV 를 거부(0xa343)). 0.3m 는 전 맵 장소 쌍 시험에서 벽 건너편 판정이 그대로였다.
_MIN_WALL_EXTENT_M = 0.3
# 출발점에서 나가는 구간·목표로 들어가는 구간의 양끝은 이 거리까지만 실제 벽만 본다(나머지는 경유지
# 사이와 같은 여유 검사). 예전엔 이 두 구간을 실제 벽 칸만 봐서, 벽에 붙는다고 버린 직선보다 더 벽에
# 붙는 우회(0.10m)를 골랐다. 장소가 벽 옆인 경우(3F 안내데스크 0.10m)를 감안해 0.30m — 병원 장소 쌍
# 시험에서 0.15·0.30·0.50 결과가 같았고 우회 구간 최소 벽거리가 0.10→0.32m 로 늘었다(2026-10-08).
_ENDPOINT_SKIP_M = 0.30
# 우회 경로가 직선보다 이 배수 넘게 길면 쓰지 않는다 — 멀리 있는 경유지를 들렀다 오는 경로를 막는다.
# 2026-10-07 3F 안내데스크→신청서 작성 2.5m 를 005 경유 16.7m(6.6배)로 보냈다. 병원 시나리오 구간은 1.1~1.4배.
_MAX_DETOUR_RATIO = 1.5
# 로봇이 등록 장소에서 이 거리 안이면 그 장소에서 출발한 것으로 보고 판정한다 — 몇십 cm 차이로 경로가
# 바뀌지 않게(3F 안내데스크 ±0.6m 에서 3가지 경로). 바로 옆 경유지를 첫 경유지로 넣지 않는 기준이기도
# 하다(10-07 언도킹 직후 0.08m 옆 CH_001-1 로 NAV). polling.NEAR_SKIP_DISTANCE(도착 간주)와 같은 값.
_NEAR_PLACE_M = 0.5
# 구간(route_info)을 따라가는 길이 직선보다 이 배수 넘게 길면 구간을 쓰지 않고 목표만 보낸다. 운영자가 그린
# 길이라 자동 우회(1.5배)보다 넉넉히 둔다 — 3F 안내데스크→신청서 작성(01→02 경유)이 1.9배(2026-10-08).
_ROUTE_GRAPH_MAX_RATIO = 2.0

# map_server 기본 free_thresh — 이보다 밝은 칸만 빈 곳(흰색)이다
_FREE_THRESH = 0.196

_cache: dict[int, tuple[float, "_OccGrid"]] = {}
_cache_lock = threading.Lock()


def _drop_small_blobs(occupied: np.ndarray, resolution: float) -> np.ndarray:
    """가로·세로가 모두 _MIN_WALL_EXTENT_M 보다 작은 외딴 점유 덩어리를 지운다(8방향 연결)."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(occupied.astype(np.uint8), connectivity=8)
    extent = np.maximum(stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]) * resolution
    keep = extent >= _MIN_WALL_EXTENT_M
    keep[0] = False     # 0 = 빈 칸
    return keep[labels]


def _inflate(occupied: np.ndarray, radius_cells: int) -> np.ndarray:
    """점유 칸을 반경만큼 부풀린다(원형). 벽에서 그 거리 안의 칸도 막힌 것으로 본다."""
    if radius_cells <= 0:
        return occupied
    h, w = occupied.shape
    padded = np.pad(occupied, radius_cells)
    out = np.zeros_like(occupied)
    for dy in range(-radius_cells, radius_cells + 1):
        for dx in range(-radius_cells, radius_cells + 1):
            if dx * dx + dy * dy <= radius_cells * radius_cells:
                out |= padded[radius_cells + dy:radius_cells + dy + h,
                              radius_cells + dx:radius_cells + dx + w]
    return out


class _OccGrid:
    """격자 지도(PGM + YAML) 의 점유 판정. raw 는 실제 벽, inflated 는 여유 거리만큼 부풀린 벽."""

    def __init__(self, occupied: np.ndarray, resolution: float, origin: tuple[float, float],
                 unknown: Optional[np.ndarray] = None):
        self.raw = occupied
        # 로봇이 지도에 기록하지 않은 칸(회색) — 노드를 둘 수 없다. 모르면 None
        self.unknown = unknown
        self.inflated = _inflate(occupied, round(_CLEARANCE_M / resolution))
        self.resolution = resolution
        self.origin = origin
        self._wall_dist: Optional[np.ndarray] = None

    def wall_distance(self, x: float, y: float) -> Optional[float]:
        """(x, y) 에서 가장 가까운 실제 벽까지 거리(m). 지도 밖이면 None."""
        if self._wall_dist is None:
            free = (~self.raw).astype(np.uint8)
            self._wall_dist = cv2.distanceTransform(free, cv2.DIST_L2, 5) * self.resolution
        h, w = self.raw.shape
        col = int((x - self.origin[0]) / self.resolution)
        row = h - 1 - int((y - self.origin[1]) / self.resolution)
        if not (0 <= col < w and 0 <= row < h):
            return None
        return float(self._wall_dist[row, col])

    def is_unknown(self, x: float, y: float) -> bool:
        """(x, y) 가 미탐색(회색) 칸인가. 지도 밖도 미탐색으로 본다."""
        h, w = self.raw.shape
        col = int((x - self.origin[0]) / self.resolution)
        row = h - 1 - int((y - self.origin[1]) / self.resolution)
        if not (0 <= col < w and 0 <= row < h):
            return True
        return bool(self.unknown[row, col]) if self.unknown is not None else False

    def _hit(self, grid: np.ndarray, x: float, y: float) -> bool:
        h, w = grid.shape
        col = int((x - self.origin[0]) / self.resolution)
        row = h - 1 - int((y - self.origin[1]) / self.resolution)
        if not (0 <= col < w and 0 <= row < h):
            return False       # 지도 밖은 판단하지 않는다
        return bool(grid[row, col])

    def line_blocked(self, a: tuple[float, float], b: tuple[float, float],
                     end_skip: float = _CLEARANCE_M) -> bool:
        """직선이 벽을 가로지르거나 벽에 붙어 지나가는가.

        양끝 end_skip 구간은 실제 벽만 본다 — 출발·도착 지점 바로 옆의 벽은 로봇이 이미
        서 있는(서게 될) 자리라 '질러가기' 가 아니다. 벽 가까운 장소에서 출발할 때 같은 쪽
        목적지에도 경유지를 쓰는 오판을 막는다.
        """
        dist = float(np.hypot(b[0] - a[0], b[1] - a[1]))
        n = max(2, int(dist / _SAMPLE_STEP_M) + 1)
        for t in np.linspace(0.0, 1.0, n):
            x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            near_end = min(t, 1.0 - t) * dist < end_skip
            if self._hit(self.raw if near_end else self.inflated, x, y):
                return True
        return False


def _read_pgm(path: str) -> np.ndarray:
    """P5(바이너리) PGM 을 2차원 배열로. 헤더 주석(#)을 건너뛴다."""
    with open(path, "rb") as f:
        data = f.read()
    tokens: list[bytes] = []
    pos = 0
    while len(tokens) < 4:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        start = pos
        while not data[pos:pos + 1].isspace():
            pos += 1
        tokens.append(data[start:pos])
    if tokens[0] != b"P5":
        raise ValueError(f"P5 PGM 이 아닙니다: {path}")
    width, height, maxval = int(tokens[1]), int(tokens[2]), int(tokens[3])
    dtype = np.uint8 if maxval < 256 else np.dtype(">u2")
    pixels = np.frombuffer(data, dtype=dtype, count=width * height, offset=pos + 1)
    return pixels.reshape(height, width).astype(np.float32) / maxval


def _parse_yaml(path: str) -> tuple[float, tuple[float, float], bool, float]:
    with open(path, "r") as f:
        content = f.read()
    res = re.search(r"resolution:\s*([\d.]+)", content)
    org = re.search(r"origin:\s*\[\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)", content)
    neg = re.search(r"negate:\s*(\d)", content)
    occ = re.search(r"occupied_thresh:\s*([\d.]+)", content)
    return (
        float(res.group(1)) if res else 0.1,
        (float(org.group(1)), float(org.group(2))) if org else (0.0, 0.0),
        bool(int(neg.group(1))) if neg else False,
        float(occ.group(1)) if occ else _DEFAULT_OCC_THRESH,
    )


def _load_grid(db: Session, map_id: int) -> Optional[_OccGrid]:
    m = db.query(RobotMapInfo).filter(RobotMapInfo.id == map_id).first()
    if not m or not m.PgmFilePath or not m.YamlFilePath:
        return None
    try:
        from app.map.map_file_store import ensure_local
        ensure_local(db, m)     # 다른 관제 PC 면 공유 DB 에서 복원
    except Exception as e:  # noqa: BLE001 — 복원 실패는 아래 존재 검사에서 걸러진다
        print(f"[VIA] 맵 파일 복원 실패 map={map_id}: {e}")

    pgm = os.path.join(_BASE_DIR, m.PgmFilePath.replace("./", ""))
    yml = os.path.join(_BASE_DIR, m.YamlFilePath.replace("./", ""))
    if not (os.path.exists(pgm) and os.path.exists(yml)):
        return None

    mtime = os.path.getmtime(pgm)
    with _cache_lock:
        hit = _cache.get(map_id)
        if hit and hit[0] == mtime:
            return hit[1]

    try:
        resolution, origin, negate, occ_thresh = _parse_yaml(yml)
        pixels = _read_pgm(pgm)
    except Exception as e:  # noqa: BLE001 — 지도를 못 읽으면 우회 판단만 건너뛰고 이동은 막지 않는다
        print(f"[VIA] 맵 파일 읽기 실패 map={map_id}: {e} — 자동 우회 없이 이동")
        return None
    # map_server 규약: negate=0 이면 어두울수록 점유 — p = (1 - 밝기)
    occupancy = pixels if negate else 1.0 - pixels
    # 회색(미탐색): 빈 곳 기준(free_thresh)보다 어둡고 벽 기준보다 밝은 칸 — map_server 규약
    unknown = (occupancy > _FREE_THRESH) & (occupancy <= occ_thresh)
    grid = _OccGrid(_drop_small_blobs(occupancy > occ_thresh, resolution), resolution, origin, unknown)
    with _cache_lock:
        _cache[map_id] = (mtime, grid)
    return grid


def _map_places(db: Session, map_id: int) -> list[LocationInfo]:
    """같은 맵의 좌표가 있는 장소 전부."""
    rows = db.query(LocationInfo).filter(LocationInfo.MapId == map_id).all()
    return [p for p in rows if p.LocationX is not None and p.LocationY is not None]


def _xy(p: LocationInfo) -> tuple[float, float]:
    return (p.LocationX, p.LocationY)


def _place_at(start: tuple[float, float], places: list[LocationInfo]) -> Optional[LocationInfo]:
    """로봇이 _NEAR_PLACE_M 안에 서 있는 등록 장소(가장 가까운 것). 없으면 None.

    있으면 그 장소에서 출발한 것으로 보고 그 좌표로 판정한다 — 몇십 cm 차이로 경로가 바뀌지 않게.
    """
    near = [p for p in places if math.dist(start, _xy(p)) < _NEAR_PLACE_M]
    return min(near, key=lambda p: math.dist(start, _xy(p))) if near else None


def _route_graph_path(db: Session, map_id: int, origin: str, goal: str) -> Optional[list[str]]:
    """맵 관리에서 그린 구간(route_info)을 방향대로 이어 origin→goal 최단 길(장소 이름). 없으면 None."""
    names = {p.LacationName: _xy(p) for p in _map_places(db, map_id)}
    edges: dict[str, list[str]] = {}
    for r in db.query(RouteInfo).filter(RouteInfo.MapId == map_id).all():
        if r.Direction in ("forward", "bidirectional"):
            edges.setdefault(r.StartPlaceName, []).append(r.EndPlaceName)
        if r.Direction in ("reverse", "bidirectional"):
            edges.setdefault(r.EndPlaceName, []).append(r.StartPlaceName)
    if origin not in names or goal not in names:
        return None
    dist, prev, pq = {origin: 0.0}, {}, [(0.0, origin)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == goal:
            break
        if d > dist.get(u, math.inf):
            continue
        for v in edges.get(u, []):
            if v in names and (nd := d + math.dist(names[u], names[v])) < dist.get(v, math.inf):
                dist[v], prev[v] = nd, u
                heapq.heappush(pq, (nd, v))
    if goal not in dist or goal == origin:
        return None
    path = [goal]
    while path[-1] != origin:
        path.append(prev[path[-1]])
    return path[::-1]


def _route_graph_plan(db: Session, places: list[LocationInfo], place: LocationInfo,
                      origin: Optional[LocationInfo]) -> Optional[tuple[list[dict], str]]:
    """구간 우선 판정(직선이 막혔을 때). 결정하지 못하면 None — 맵의 다른 규칙으로 넘긴다.

    - 출발 장소→목표가 구간으로 이어지고 직선의 _ROUTE_GRAPH_MAX_RATIO 배 이하: 중간 장소를 거쳐 간다
    - 구간이 바로 이어지거나(중간 없음) 너무 길면: 목표만 보낸다(길은 로봇이 정한다)
    - 로봇이 등록 장소에 있지 않거나 구간 길이 없으면: None
    """
    if origin is None:
        return None
    path = _route_graph_path(db, int(place.MapId), origin.LacationName, place.LacationName)
    if not path:
        return None
    if len(path) == 2:
        return [], "route"
    by_name = {p.LacationName: p for p in places}
    chain = [by_name[n] for n in path[1:-1]]
    pts = [_xy(origin)] + [_xy(v) for v in chain] + [_xy(place)]
    length = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
    direct = math.dist(_xy(origin), _xy(place))
    if length > _ROUTE_GRAPH_MAX_RATIO * direct:
        print(f"[VIA] {place.LacationName}: 구간 길이 직선의 {length / max(direct, 1e-6):.1f}배 — 목표만 보냄")
        return [], "route_too_long"
    print(f"[VIA] {place.LacationName}: 구간 따라 — 경유 " + " → ".join(v.LacationName for v in chain))
    return _pass_waypoints(chain, place), "route"


def _pass_waypoints(chain: list[LocationInfo], place: LocationInfo) -> list[dict]:
    """목표 앞에 끼울 통과 지점 웨이포인트.

    방향은 장소에 저장된 yaw 가 아니라 다음 지점을 향하게 한다 — 저장 yaw 로 서면 지점마다 제자리에서
    돌았다가 다시 도는 동작이 생긴다. pass_through: 길목일 뿐이라 멈추지 않는다 — 반경 안에 들어오면
    nav_thread 가 바로 다음 지점을 보낸다.
    """
    points = [_xy(v) for v in chain] + [_xy(place)]
    return [
        {"x": v.LocationX, "y": v.LocationY,
         "yaw": math.atan2(points[i + 1][1] - v.LocationY, points[i + 1][0] - v.LocationX),
         "name": v.LacationName, "map_id": place.MapId,
         "floor_id": place.FloorId, "pass_through": True}
        for i, v in enumerate(chain)
    ]


def _detour_nodes(places: list[LocationInfo], place: LocationInfo,
                  start: tuple[float, float]) -> list[LocationInfo]:
    """우회 노드 후보 — 같은 맵의 경유지 카테고리 장소. 목표 자신과 출발점 바로 옆(이미 서 있는 곳)은 뺀다."""
    return [p for p in places
            if p.id != place.id and is_pass_through_category(p.Category)
            and math.dist(start, _xy(p)) >= _NEAR_PLACE_M]


def _shortest_detour(grid: "_OccGrid", start: tuple[float, float], target: tuple[float, float],
                     nodes: list[LocationInfo]) -> Optional[list[LocationInfo]]:
    """벽을 넘지 않는 구간으로만 이어지는 start→(경유지들)→target 최단 경로의 경유지 목록.

    노드 수가 적어(맵당 경유지 수 개~수십 개) 간선을 필요할 때 검사하는 다익스트라로 충분하다.
    모든 구간을 같은 여유 거리로 본다. 출발점에서 나가는 구간과 목표로 들어가는 구간만 양끝
    _ENDPOINT_SKIP_M 을 실제 벽만 본다 — 로봇이 데스크 모서리에 붙어 서 있거나 목표가 벽 옆이면
    여유 검사로는 첫 구간부터 막힌다. 출발→목표 직행은 이미 막혔다고 판정했으므로 후보에서 뺀다.
    """
    pts = [start, target] + [_xy(n) for n in nodes]

    def blocked(u: int, v: int) -> bool:
        if u == 0 or v == 1:
            return grid.line_blocked(pts[u], pts[v], end_skip=_ENDPOINT_SKIP_M)
        return grid.line_blocked(pts[u], pts[v])

    dist = {0: 0.0}
    prev: dict[int, int] = {}
    pq = [(0.0, 0)]
    done: set[int] = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in done:
            continue
        if u == 1:
            break
        done.add(u)
        for v in range(1, len(pts)):
            if v in done or v == u or (u == 0 and v == 1):
                continue
            nd = d + math.dist(pts[u], pts[v])
            if nd < dist.get(v, math.inf) and not blocked(u, v):
                dist[v], prev[v] = nd, u
                heapq.heappush(pq, (nd, v))
    if 1 not in dist:
        return None
    chain = []
    cur = prev[1]
    while cur != 0:
        chain.append(nodes[cur - 2])
        cur = prev[cur]
    return chain[::-1]


def build_via_waypoints(db: Session, place: LocationInfo,
                        start: Optional[tuple[float, float]]) -> list[dict]:
    """목표 장소 앞에 끼울 우회 경유지 웨이포인트. 필요 없으면 [].

    start 가 None(위치 미상)이면 판단할 수 없으므로 바로 간다 — 위치 미확정 상태의 자율주행은
    start_place_move 가 이미 막는다.
    """
    via, reason = plan_via_waypoints(db, place, start)
    if via is None:
        print(f"[VIA] {place.LacationName}: 우회 경로를 확인할 수 없음({reason})")
        return []
    return via


def plan_via_waypoints(db: Session, place: LocationInfo,
                       start: Optional[tuple[float, float]]) -> tuple[Optional[list[dict]], str]:
    """직선이 벽을 통과하면 안전한 우회 waypoint를 반환한다.

    Returns:
        (waypoints, reason)
        - [] / "clear": 직선 이동 가능
        - [] / "disabled": 이 맵은 자동 우회를 끔(AUTO_DETOUR_DISABLED_MAPS) — 목표만 보낸다
        - [waypoints] / "route": 구간(route_info)을 따라 간다(ROUTE_GRAPH_MAPS). [] 면 구간이 바로 이어짐
        - [] / "route_too_long": 구간 길이 직선의 2배를 넘어 목표만 보낸다
        - [waypoints] / "detour": 우회 경로 생성
        - None / 사유: 맵 누락, 우회 경로 없음(no_safe_detour), 우회가 너무 김(detour_too_long)
    """
    from app.robot_io.config import AUTO_DETOUR_DISABLED_MAPS, ROUTE_GRAPH_ALWAYS_MAPS, ROUTE_GRAPH_MAPS

    if start is None or place.MapId is None:
        return [], "not_checkable"
    map_id = int(place.MapId)
    disabled = map_id in AUTO_DETOUR_DISABLED_MAPS
    use_graph = map_id in ROUTE_GRAPH_MAPS
    if disabled and not use_graph:
        return [], "disabled"
    grid = _load_grid(db, map_id)
    if grid is None:
        return ([], "disabled") if disabled else (None, "map_unavailable")
    places = _map_places(db, map_id)
    origin = _place_at(start, places)
    if origin is not None:
        start = _xy(origin)
    target = (place.LocationX, place.LocationY)
    if use_graph and map_id in ROUTE_GRAPH_ALWAYS_MAPS:
        # 직선이 비어 있어도 구간의 중간 장소를 거친다. 바로 이어지거나 너무 길면 아래 직선 판정으로
        routed = _route_graph_plan(db, places, place, origin)
        if routed is not None and routed[0]:
            return routed
    if not grid.line_blocked(start, target):
        return [], "clear"
    if use_graph:
        routed = _route_graph_plan(db, places, place, origin)
        if routed is not None:
            return routed
    if disabled:
        return [], "disabled"

    # 깨끗한 길이 없을 때 출발 구간 벽 검사를 끄고 다시 찾던 차선책은 뺐다 — 그렇게 찾은 경로는
    # 첫 구간이 실제 벽을 가로질렀다(2026-10-07 3F 10건 전부).
    chain = _shortest_detour(grid, start, target, _detour_nodes(places, place, start))
    if not chain:
        print(f"[VIA] {place.LacationName}: 직선이 벽을 가로지르지만 경유지로 돌아갈 길이 없음 — 이동하지 않음")
        return None, "no_safe_detour"

    # 로봇이 첫 경유지 바로 옆에 있으면 거기는 이미 지난 것으로 본다(제자리 NAV 방지).
    # 로봇 자리에서 그다음 지점까지 벽에 걸리면 그대로 둔다.
    while chain and math.dist(start, _xy(chain[0])) < _NEAR_PLACE_M:
        nxt = _xy(chain[1]) if len(chain) > 1 else target
        if grid.line_blocked(start, nxt):
            break
        chain = chain[1:]
    if not chain:
        return [], "clear"

    length = sum(math.dist(a, b) for a, b in zip([start] + [_xy(v) for v in chain],
                                                  [_xy(v) for v in chain] + [target]))
    direct = math.dist(start, target)
    if length > _MAX_DETOUR_RATIO * direct:
        print(f"[VIA] {place.LacationName}: 우회가 직선의 {length / max(direct, 1e-6):.1f}배 — 이동하지 않음 (경유 "
              + " → ".join(v.LacationName for v in chain) + ")")
        return None, "detour_too_long"

    print(f"[VIA] {place.LacationName}: 직선이 벽을 가로지름 — 경유 "
          + " → ".join(v.LacationName for v in chain))
    return _pass_waypoints(chain, place), "detour"


# ── 통과 지점 지름길 검사 ─────────────────────────────────
# 통과 지점 반경에 들어오면 관제는 다음 지점을 미리 보낸다. 로봇은 그 자리에서 다음 지점으로
# 곧장 틀어 가므로(코너 안쪽으로 최대 반경만큼 파고든다), 그 직선이 벽에 붙으면 아직 넘기지 않는다.
# 몸 반폭 0.3m + 여유 0.15m. 2026-10-06 본사 맵 시뮬레이션: 검사 없이 넘기면 3F 대기 자리 코너
# 벽 여유가 0.22m 까지 줄고, 이 기준이면 0.36m 이상 유지된다.
_SHORTCUT_CLEARANCE_M = 0.45
# 목표 끝 구간은 벽 옆에 서는 장소일 수 있어 실제 벽 칸만 피하면 된다(via 경로 검사와 같은 취지)
_SHORTCUT_END_SKIP_M = 0.5
_SHORTCUT_GRID_TTL_S = 60.0
_shortcut_grids: dict[int, tuple[float, Optional[_OccGrid]]] = {}


def _shortcut_grid(map_id: int) -> Optional[_OccGrid]:
    """주행 중 폴링(0.5s)마다 부르므로 DB 조회 없이 쓰도록 맵별로 잠시 들고 있는다."""
    import time

    now = time.monotonic()
    with _cache_lock:
        hit = _shortcut_grids.get(map_id)
    if hit and now - hit[0] < _SHORTCUT_GRID_TTL_S:
        return hit[1]
    from app.database.database import SessionLocal

    db = SessionLocal()
    try:
        grid = _load_grid(db, map_id)
    finally:
        db.close()
    with _cache_lock:
        _shortcut_grids[map_id] = (now, grid)
    return grid


def shortcut_is_clear(map_id: Optional[int], start: tuple[float, float],
                      target: tuple[float, float]) -> bool:
    """현재 위치에서 다음 지점으로 곧장 가도 벽에서 충분히 떨어져 지나가는가.

    지도를 못 읽으면 False — 판단할 수 없을 때는 일찍 넘기지 않는다(통과 지점 가까이에서 넘긴다).
    """
    if map_id is None:
        return False
    grid = _shortcut_grid(int(map_id))
    if grid is None:
        return False
    dist = math.dist(start, target)
    n = max(2, int(dist / _SAMPLE_STEP_M) + 1)
    for t in np.linspace(0.0, 1.0, n):
        x = start[0] + (target[0] - start[0]) * t
        y = start[1] + (target[1] - start[1]) * t
        wall = grid.wall_distance(x, y)
        if wall is None:
            continue
        need = 0.0 if (1.0 - t) * dist < _SHORTCUT_END_SKIP_M else _SHORTCUT_CLEARANCE_M
        if wall <= need:
            return False
    return True
