"""노드망 경로 (2026-10-19) — 맵마다 운영자가 그린 노드·간선을 따라 목표까지 통과 지점을 만든다.

NODE_ROUTING_ENABLED 가 켜지면 send_move.navigation_send_next 가 목표를 보내기 직전에 이 모듈로
'로봇 위치 → 목표' 구간을 노드 경로로 펼친다. 장소 이동·경로·스케줄·대기·충전 복귀·안내 어디서 들어온
이동이든 같은 규칙을 따른다(구간 우선·자동 우회를 대신한다).

규칙
- 로봇과 목표는 ATTACH_MAX_M 안에서 벽에 막히지 않은 가장 가까운 노드에 붙는다. 그런 노드가 없어도
  간선 옆(간선에 내린 발이 두 끝 사이, ATTACH_MAX_M 안, 발까지 벽 없음)이면 그 간선에 붙는다 — 긴 간선 중간의
  장소도 노드를 더 그리지 않고 갈 수 있게(2026-10-28, 3F 맵49 종합민원실). 로봇이 둘 다 아니면
  ENTRY_MAX_M 안의 보이는 노드로 먼저 들어간다(노드망 밖 → 진입).
- 노드 사이는 저장된 간선만 쓴다(auto·manual·door). excluded 는 같은 쌍의 auto 를 지운다.
- 직선으로 가도 되는 중간 노드는 생략한다 — 통과 지점마다 생기는 정지 공백을 줄인다. 문 간선 양끝은
  남긴다. 자동문 트리거(door_trigger)가 문 앞 반경·진행 방향으로 판정하기 때문이다.
- 맵이 이동 가능하려면 노드가 있고, 충전소 바깥 접근점과(엘리베이터 로비가 있는 맵이면) 로비가
  노드망에 붙어 있어야 한다.
- 벽 판정은 via_route 의 격자 맵 검사를 빌린다. 시뮬레이션은 _seg_clear·_shortcut_clear 를 바꿔 끼운다.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.database.models import LocationInfo, NavEdge, NavNode

# 장소·로봇 → 노드 최대 거리(m). 목업·운영 합의값(2m) — 실제 맵 시뮬로 다시 본다.
ATTACH_MAX_M = 2.0
# 로봇이 노드망 밖일 때 진입 노드를 찾는 최대 거리(m). 이보다 멀면 이동을 거부한다.
ENTRY_MAX_M = 5.0
# 장소·로봇이 노드에서 이 안(m)이면 그 노드 위에 있는 것으로 본다. 그 밖이면 간선 위의 점으로 붙인다(attach_point).
ATTACH_ON_NODE_M = 0.3
# 직접 그린 간선이 연결 안 된 노드나 다른 간선과 이 거리(m) 안에서 겹치면 저장하지 않는다(overlap_problems).
EDGE_OVERLAP_M = 0.25
# 문 간선 양끝 노드는 문 앞 장소(door_info Near/Far)에서 이 안이어야 한다 — door_trigger 기본 반경과 같다.
DOOR_NEAR_M = 1.5
# 언도킹 직후 출발 판정 — 언도킹은 도킹 포인트(ch-1)로 재정렬하고 끝나므로 그 자리에 서 있을 때만 본다.
# (charge.AT_DOCK_THRESHOLD_M 2m 를 쓰면 충전소 근처 로비에 서 있기만 해도 통로로 돌아 나간다 — 10-19 시뮬)
AT_DOCK_M = 0.7
USABLE_KINDS = ("auto", "manual", "door")


def label(node_id: int) -> str:
    return f"N{node_id:02d}"


# ── 벽 판정 (시뮬레이션에서 바꿔 끼운다) ─────────────────
def _seg_clear(map_id: int, a: tuple[float, float], b: tuple[float, float]) -> bool:
    """장소·로봇과 노드를 이어도 되는가 — 양끝은 실제 벽만, 가운데는 여유(0.15m)까지 본다.

    지도를 못 읽으면 False: 판단할 수 없을 때 붙이지 않는다(이동이 거부되고 이유가 남는다).
    """
    from app.navigation.via_route import _ENDPOINT_SKIP_M, _shortcut_grid

    grid = _shortcut_grid(int(map_id))
    if grid is None:
        return False
    return not grid.line_blocked(a, b, end_skip=_ENDPOINT_SKIP_M)


def _shortcut_clear(map_id: int, a: tuple[float, float], b: tuple[float, float]) -> bool:
    """중간 노드를 건너뛰고 곧장 가도 되는가 — 통과 지점 조기 넘김과 같은 기준(벽 여유 0.45m)."""
    from app.navigation.via_route import shortcut_is_clear

    return shortcut_is_clear(map_id, a, b)


# 노드는 로봇이 서서 돌 수 있는 바닥에만 — 벽에서 이만큼(m) 떨어진 흰 바닥(자동 초안의 중심선 기준과 같다)
NODE_WALL_MIN_M = 0.35


def node_spot_problem(map_id: int, xy: tuple[float, float]) -> Optional[str]:
    """(x, y) 에 노드를 둘 수 없는 이유. 둘 수 있거나 지도를 못 읽으면 None(판단할 수 없을 때 막지 않는다)."""
    from app.navigation.via_route import _shortcut_grid

    grid = _shortcut_grid(int(map_id))
    if grid is None:
        return None
    d = grid.wall_distance(*xy)
    if d is None:
        return "맵 바깥입니다."
    if grid.is_unknown(*xy):
        return "로봇이 지도에 기록하지 않은 곳(회색)입니다."
    if d < NODE_WALL_MIN_M:
        return f"벽에 너무 가깝습니다(벽까지 {d:.2f} m — {NODE_WALL_MIN_M} m 이상 떨어져야 합니다)."
    return None


# ── 그래프 ──────────────────────────────────────────
@dataclass
class Graph:
    map_id: int
    nodes: dict[int, tuple[float, float]] = field(default_factory=dict)
    adj: dict[int, list[int]] = field(default_factory=dict)
    doors: dict[frozenset, Optional[int]] = field(default_factory=dict)   # 문 간선 양끝 → door_info.id
    oneway: set[tuple[int, int]] = field(default_factory=set)             # 한쪽 방향 간선(a→b 만)

    def neighbors(self, n: int) -> list[int]:
        return self.adj.get(n, [])


def load_graph(db: Session, map_id: int) -> Graph:
    g = Graph(map_id=int(map_id))
    for n in db.query(NavNode).filter(NavNode.MapId == map_id).all():
        g.nodes[n.id] = (float(n.X), float(n.Y))
    edges = db.query(NavEdge).filter(NavEdge.MapId == map_id).all()
    excluded = {frozenset((e.NodeA, e.NodeB)) for e in edges if e.Kind == "excluded"}
    for e in edges:
        if e.Kind not in USABLE_KINDS or e.NodeA not in g.nodes or e.NodeB not in g.nodes:
            continue
        pair = frozenset((e.NodeA, e.NodeB))
        if e.Kind == "auto" and pair in excluded:
            continue
        g.adj.setdefault(e.NodeA, []).append(e.NodeB)
        if e.Direction != "forward":
            g.adj.setdefault(e.NodeB, []).append(e.NodeA)
        else:
            g.oneway.add((e.NodeA, e.NodeB))
        if e.Kind == "door":
            g.doors[pair] = e.DoorId
    return g


def attach(g: Graph, pt: tuple[float, float], max_m: float = ATTACH_MAX_M) -> Optional[int]:
    """pt 에서 max_m 안, 벽에 막히지 않은 가장 가까운 노드."""
    near = sorted((math.dist(pt, xy), nid) for nid, xy in g.nodes.items() if math.dist(pt, xy) <= max_m)
    for _, nid in near:
        if _seg_clear(g.map_id, pt, g.nodes[nid]):
            return nid
    return None


def attach_edge(g: Graph, pt: tuple[float, float], max_m: float = ATTACH_MAX_M) -> Optional[tuple[int, int]]:
    """pt 가 간선 옆이면 그 간선 (a, b). 발이 두 끝 사이·max_m 안이고 발까지 벽에 막히지 않은 것 중 가장 가까운 것.

    한쪽 방향 간선은 저장된 방향 그대로(a → b) 돌려준다 — 드나드는 끝은 _edge_exits·_edge_entries 가 가린다.
    """
    best: Optional[tuple[float, tuple[int, int]]] = None
    seen: set[frozenset] = set()
    for a, nbs in g.adj.items():
        for b in nbs:
            pair = frozenset((a, b))
            if pair in seen:
                continue
            seen.add(pair)
            pa, pb = g.nodes[a], g.nodes[b]
            dx, dy = pb[0] - pa[0], pb[1] - pa[1]
            l2 = dx * dx + dy * dy
            if l2 == 0:
                continue
            t = ((pt[0] - pa[0]) * dx + (pt[1] - pa[1]) * dy) / l2
            if not 0.0 < t < 1.0:
                continue
            foot = (pa[0] + t * dx, pa[1] + t * dy)
            d = math.dist(pt, foot)
            if d <= max_m and (best is None or d < best[0]) and _seg_clear(g.map_id, pt, foot):
                best = (d, (a, b))
    return best[1] if best else None


def attach_point(g: Graph, pt: tuple[float, float]) -> tuple[Optional[int], Optional[tuple[int, int]]]:
    """장소·로봇을 노드망에 붙인다 — (노드, None) 또는 (None, 간선). 둘 다 None 이면 2m 안에 아무것도 없다.

    노드는 꺾는 점일 뿐이고 장소는 간선 위에 있다(10-29 사용자 정리). 그래서 ① 노드 바로 위(ATTACH_ON_NODE_M)면 그 노드,
    ② 2m 안 간선이 있으면 그 간선 위의 점, ③ 간선이 없을 때만 2m 안 가장 가까운 노드. 예전에는 ③ 을 먼저 봐서, 간선 위
    장소가 진행 반대쪽 노드에 붙어 물러났다 돌아왔다(10-29 3F 종합민원실 → 대기석, 0.76m 뒤 N377).
    """
    on = attach(g, pt, ATTACH_ON_NODE_M)
    if on is not None:
        return on, None
    e = attach_edge(g, pt)
    if e is not None:
        return None, e
    return attach(g, pt), None


def attach_any(g: Graph, pt: tuple[float, float]) -> Optional[int]:
    """점검용 — 붙는 노드, 간선 위면 그 간선의 가까운 끝 노드(덩어리·방향 점검의 대표 노드). 둘 다 아니면 None."""
    n, e = attach_point(g, pt)
    if n is not None:
        return n
    return min(e, key=lambda k: math.dist(pt, g.nodes[k])) if e else None


def overlap_problems(g: Graph, user_pairs: set[frozenset]) -> list[str]:
    """직접 그린 간선(user_pairs)이 연결 안 된 노드 위를 지나거나 다른 간선과 겹치는 곳.

    겹쳐 그리면 노드 위 장소가 어느 간선에 붙을지 흔들리고, 한쪽 방향 간선을 옆 간선으로 우회할 수 있다(10-29 3F N361–N362 가
    N370 위를 지나감). 자동 간선끼리는 보지 않는다 — 자동 간선은 저장 때마다 다시 만든다.
    """
    def foot(p, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        l2 = dx * dx + dy * dy
        t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2 if l2 else 0.0
        return t, math.dist(p, (a[0] + t * dx, a[1] + t * dy))

    pairs = sorted({tuple(sorted((a, b))) for a, nbs in g.adj.items() for b in nbs})
    out = []
    for a, b in pairs:
        if frozenset((a, b)) not in user_pairs:
            continue
        for n, xy in g.nodes.items():
            if n in (a, b):
                continue
            t, d = foot(xy, g.nodes[a], g.nodes[b])
            if 0.02 < t < 0.98 and d <= EDGE_OVERLAP_M:
                out.append(f"간선 {label(a)}–{label(b)} 이 연결되지 않은 노드 {label(n)} 위를 지나갑니다 — {label(n)} 에서 나눠 이어 주세요.")
        for c, d_ in pairs:
            if (c, d_) <= (a, b) and frozenset((c, d_)) in user_pairs or {a, b} & {c, d_}:
                continue
            t1, e1 = foot(g.nodes[c], g.nodes[a], g.nodes[b])
            t2, e2 = foot(g.nodes[d_], g.nodes[a], g.nodes[b])
            if e1 <= EDGE_OVERLAP_M and e2 <= EDGE_OVERLAP_M and max(t1, t2) > 0.02 and min(t1, t2) < 0.98:
                out.append(f"간선 {label(a)}–{label(b)} 과 {label(c)}–{label(d_)} 이 겹쳐 있습니다 — 하나만 남겨 주세요.")
    return list(dict.fromkeys(out))


def _edge_exits(g: Graph, e: tuple[int, int]) -> list[int]:
    """간선 중간에서 나갈 수 있는 끝 — 한쪽 방향이면 그 방향 끝만."""
    a, b = e
    if (a, b) in g.oneway:
        return [b]
    if (b, a) in g.oneway:
        return [a]
    return [a, b]


def _edge_entries(g: Graph, e: tuple[int, int]) -> list[int]:
    """간선 중간으로 들어올 수 있는 끝 — 한쪽 방향이면 그 방향 시작 끝만."""
    a, b = e
    if (a, b) in g.oneway:
        return [a]
    if (b, a) in g.oneway:
        return [b]
    return [a, b]


def _along_ok(g: Graph, e: tuple[int, int], p: tuple[float, float], q: tuple[float, float]) -> bool:
    """같은 간선 옆의 p 에서 q 로 간선을 따라 곧장 가도 되는가 — 한쪽 방향이면 그 방향일 때만."""
    a, b = e
    pa, pb = g.nodes[a], g.nodes[b]
    dx, dy = pb[0] - pa[0], pb[1] - pa[1]

    def along(pt):
        return (pt[0] - pa[0]) * dx + (pt[1] - pa[1]) * dy
    if (a, b) in g.oneway:
        return along(q) >= along(p)
    if (b, a) in g.oneway:
        return along(q) <= along(p)
    return True


def _path_len(g: Graph, path: list[int]) -> float:
    return sum(math.dist(g.nodes[u], g.nodes[v]) for u, v in zip(path, path[1:]))


def _shortest(g: Graph, s: int, t: int) -> Optional[list[int]]:
    dist, prev, pq = {s: 0.0}, {}, [(0.0, s)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == t:
            break
        if d > dist.get(u, math.inf):
            continue
        for v in g.neighbors(u):
            nd = d + math.dist(g.nodes[u], g.nodes[v])
            if nd < dist.get(v, math.inf):
                dist[v], prev[v] = nd, u
                heapq.heappush(pq, (nd, v))
    if t not in dist:
        return None
    path = [t]
    while path[-1] != s:
        path.append(prev[path[-1]])
    return path[::-1]


def components(g: Graph) -> list[set[int]]:
    """간선으로 이어진 노드 덩어리(간선 없는 노드는 빠진다)."""
    und: dict[int, set[int]] = {}
    for u, vs in g.adj.items():
        for v in vs:
            und.setdefault(u, set()).add(v)
            und.setdefault(v, set()).add(u)
    seen: set[int] = set()
    out = []
    for n in und:
        if n in seen:
            continue
        comp, stack = set(), [n]
        while stack:
            u = stack.pop()
            if u in comp:
                continue
            comp.add(u)
            stack.extend(und.get(u, ()))
        seen |= comp
        out.append(comp)
    return out


def reachable(g: Graph, start: int, reverse: bool = False) -> set[int]:
    """start 에서 간선 방향을 따라(reverse 면 거슬러) 갈 수 있는 노드."""
    nxt: dict[int, list[int]] = {}
    for u, vs in g.adj.items():
        for v in vs:
            if reverse:
                nxt.setdefault(v, []).append(u)
            else:
                nxt.setdefault(u, []).append(v)
    seen: set[int] = set()
    stack = [start]
    while stack:
        u = stack.pop()
        if u not in seen:
            seen.add(u)
            stack.extend(nxt.get(u, ()))
    return seen


def _pt_seg_dist(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2)) if l2 else 0.0
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _in_lane(pts: list[tuple[float, float]], i: int, j: int, tol: float) -> bool:
    """i→j 직선이 건너뛰는 점(i+1 … j-1)을 모두 tol 안으로 지나가는가."""
    return all(_pt_seg_dist(pts[m], pts[i], pts[j]) <= tol for m in range(i + 1, j))


def _simplify(map_id: int, pts: list[tuple[float, float]], locked: set[int]) -> list[int]:
    """남길 점의 인덱스(처음·끝 포함). locked 는 건너뛰지 않는다.

    건너뛰려면 지름길이 벽에서 충분히 떨어지고(_shortcut_clear) 건너뛴 노드가 띠(NODE_LANE_TOL_M) 안이어야 한다.
    """
    from app.robot_io.config import NODE_LANE_TOL_M

    keep, i, last = [0], 0, len(pts) - 1
    while i < last:
        stop = min([k for k in locked if k > i] + [last])
        j = stop
        while j > i + 1 and not (_in_lane(pts, i, j, NODE_LANE_TOL_M)
                                 and _shortcut_clear(map_id, pts[i], pts[j])):
            j -= 1
        keep.append(j)
        i = j
    return keep


# ── 경로 계산 ────────────────────────────────────────
@dataclass
class LegPlan:
    waypoints: list[dict]
    node_path: list[int]
    entry: bool = False          # 로봇이 노드망 밖이라 진입 구간을 거쳤다
    error: str = ""


def _beside_edge(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> bool:
    """p 를 간선 a–b 에 내린 발이 두 끝 사이에 있고, 붙을 수 있는 거리(ATTACH_MAX_M) 안인가."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return False
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2
    if not 0.0 < t < 1.0:
        return False
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy)) <= ATTACH_MAX_M


def _trim_ends(g: Graph, start: tuple[float, float], goal_xy: tuple[float, float],
               path: list[int], entry: bool) -> list[int]:
    """시작·목표가 첫·마지막 간선 옆에 있으면 그 끝 노드를 건너뛴다.

    가장 가까운 노드에 붙이면, 간선 중간 옆에 있는 목표를 지나쳐 끝 노드까지 갔다가 되돌아온다
    (10-27 실기: 편의점입구 → N238 까지 0.75m 지나쳤다 복귀, 출발할 때도 N238 로 먼저 내려감).
    문 간선은 건너뛰지 않고, 지름길이 벽에서 충분히 떨어질 때만(_shortcut_clear) 건너뛴다.
    """
    while len(path) >= 2:
        a, b = path[-2], path[-1]
        if (frozenset((a, b)) in g.doors or not _beside_edge(goal_xy, g.nodes[a], g.nodes[b])
                or not _shortcut_clear(g.map_id, g.nodes[a], goal_xy)):
            break
        path = path[:-1]
    while len(path) >= 2 and not entry:
        a, b = path[0], path[1]
        if (frozenset((a, b)) in g.doors or not _beside_edge(start, g.nodes[a], g.nodes[b])
                or not _shortcut_clear(g.map_id, start, g.nodes[b])):
            break
        path = path[1:]
    return path


def plan_leg(db: Session, map_id: int, start: tuple[float, float], goal: dict) -> LegPlan:
    """start → goal(웨이포인트 dict) 사이에 끼울 노드 통과 지점. error 가 있으면 이동하지 않는다."""
    g = load_graph(db, map_id)
    goal_xy = (float(goal["x"]), float(goal["y"]))
    name = goal.get("name") or "목표"
    if not g.nodes:
        return LegPlan([], [], error="이 맵에 노드망이 없습니다. 맵 편집에서 노드를 그려야 이동할 수 있습니다.")
    s, s_edge = attach_point(g, start)
    entry = False
    if s is None and s_edge is None:
        s = attach(g, start, ENTRY_MAX_M)
        entry = s is not None
    if s is None and s_edge is None:
        return LegPlan([], [], error=f"로봇이 노드망에서 {ENTRY_MAX_M:.0f}m 넘게 떨어져 있거나 벽 너머에 있어 경로를 만들 수 없습니다.")
    t, t_edge = attach_point(g, goal_xy)
    if t is None and t_edge is None:
        return LegPlan([], [], error=f"'{name}'은(는) 노드망에 연결되지 않은 장소라 갈 수 없습니다.")
    # 출발·목표가 같은 간선 옆이면(한쪽은 노드에 붙어 있어도) 끝 노드까지 갔다 오지 않고 간선을 따라 곧장
    shared = s_edge or t_edge
    if (shared and _beside_edge(start, *(g.nodes[k] for k in shared))
            and _beside_edge(goal_xy, *(g.nodes[k] for k in shared))
            and _along_ok(g, shared, start, goal_xy) and _shortcut_clear(g.map_id, start, goal_xy)):
        return LegPlan([], [])
    # 간선 옆이면 드나들 수 있는 끝 노드마다 경로를 보고 전체 길이가 가장 짧은 것을 고른다
    starts = [s] if s is not None else [n for n in _edge_exits(g, s_edge) if _seg_clear(g.map_id, start, g.nodes[n])]
    goals = [t] if t is not None else [n for n in _edge_entries(g, t_edge) if _seg_clear(g.map_id, g.nodes[n], goal_xy)]
    path = None
    best = math.inf
    for a in starts:
        for b in goals:
            p = _shortest(g, a, b)
            if p is None:
                continue
            cost = math.dist(start, g.nodes[a]) + _path_len(g, p) + math.dist(g.nodes[b], goal_xy)
            if cost < best:
                path, best = p, cost
    if path is None:
        return LegPlan([], [], error=f"노드망이 끊겨 있어 '{name}'까지 갈 수 없습니다.")
    path = _trim_ends(g, start, goal_xy, path, entry)

    pts = [start] + [g.nodes[n] for n in path] + [goal_xy]
    locked: set[int] = set()
    for k in range(len(path) - 1):
        # 문 간선·한쪽 방향 간선은 양끝을 건너뛰지 않는다 — 건너뛴 직선이 닫힌 문이나 금지 방향을 지날 수 있다
        if frozenset((path[k], path[k + 1])) in g.doors or (path[k], path[k + 1]) in g.oneway:
            locked |= {k + 1, k + 2}            # pts 인덱스(앞에 start 가 하나 있다)
    if entry:
        locked.add(1)                           # 진입 노드는 건너뛰지 않는다 — 진입 구간이 곧 그 노드까지다
    keep = _simplify(map_id, pts, locked)

    mids = keep[1:-1]
    wps = []
    for pos, k in enumerate(mids):
        nid = path[k - 1]
        x, y = pts[k]
        nx, ny = pts[keep[keep.index(k) + 1]]
        wp = {
            "x": x, "y": y, "yaw": math.atan2(ny - y, nx - x), "name": label(nid),
            "map_id": map_id, "floor_id": goal.get("floor_id"),
            "pass_through": True, "node": True, "node_id": nid,
        }
        if goal.get("speed"):
            wp["speed"] = goal["speed"]
        for other in (path[k - 2] if k >= 2 else None, path[k] if k < len(path) else None):
            pair = frozenset((nid, other))
            if other is not None and pair in g.doors:
                wp["door_id"] = g.doors[pair]
        wps.append(wp)
    # 목적지 바로 옆(ATTACH_ON_NODE_M) 노드는 보내지 않는다 — 사실상 같은 자리라 그 점에 닿아야 마지막 구간이 되고,
    # "곧 도착합니다"가 도착 0.3m 앞에서야 나왔다(10-30 3F 종합민원실 → 대기석, N362 가 대기석에서 0.09m). 문 노드는 남긴다.
    while wps and not wps[-1].get("door_id") and math.dist((wps[-1]["x"], wps[-1]["y"]), goal_xy) <= ATTACH_ON_NODE_M:
        wps.pop()
    return LegPlan(wps, path, entry=entry)


def leg_length(start: tuple[float, float], wps: list[dict], goal: dict) -> float:
    pts = [start] + [(w["x"], w["y"]) for w in wps] + [(goal["x"], goal["y"])]
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


# ── 충전소·엘리베이터 로비 ─────────────────────────────
def dock_chains(db: Session, map_id: int) -> list[list[LocationInfo]]:
    """맵의 충전소마다 접근 통로 [ch-N, …, ch-1] (바깥 → 도킹). 도킹 포인트(ch-1)가 없는 충전소는 뺀다."""
    out = []
    rows = db.query(LocationInfo).filter(LocationInfo.MapId == map_id).all()
    for st in (r for r in rows if r.Category == "charge"):
        prefix = f"{st.LacationName}-"
        numbered = sorted(
            ((int(r.LacationName[len(prefix):]), r) for r in rows
             if (r.LacationName or "").startswith(prefix) and r.LacationName[len(prefix):].isdigit()),
            key=lambda t: t[0], reverse=True,
        )
        if numbered and numbered[-1][0] == 1:
            out.append([r for _, r in numbered])
    return out


def has_charge_station(db: Session, map_id: int) -> bool:
    """이 맵에 충전소(Category charge)가 등록돼 있는가."""
    return db.query(LocationInfo).filter(LocationInfo.MapId == map_id, LocationInfo.Category == "charge").first() is not None


_ELEVATOR_NAME_KEYS = ("엘리베이터", "EV")


def elevator_lobby(db: Session, map_id: int) -> Optional[LocationInfo]:
    """맵의 엘리베이터 로비 장소. 없으면 None — 엘리베이터 없는 층은 로비 연결을 요구하지 않는다.

    robot_control.status._elevator_lobby_pose 의 1·2단계(층 설정 로비 → 이름에 엘리베이터/EV)와 같다.
    3단계 '로봇 대기 장소' 폴백은 엘리베이터가 없는 층의 도착 시드라 여기서는 쓰지 않는다.
    """
    from app.database.models import ElevatorFloorStop

    stop = db.query(ElevatorFloorStop).filter(
        ElevatorFloorStop.MapId == map_id, ElevatorFloorStop.LobbyLocationId.isnot(None)).first()
    if stop:
        loc = db.query(LocationInfo).filter(
            LocationInfo.id == stop.LobbyLocationId, LocationInfo.MapId == map_id).first()
        if loc is not None:
            return loc
    rows = db.query(LocationInfo).filter(LocationInfo.MapId == map_id).order_by(LocationInfo.id).all()
    named = [r for r in rows if any(k.lower() in (r.LacationName or "").lower() for k in _ELEVATOR_NAME_KEYS)]
    named.sort(key=lambda r: "입구" not in (r.LacationName or ""))
    return named[0] if named else None


def required_places(db: Session, map_id: int) -> list[tuple[LocationInfo, str]]:
    """노드망에 반드시 붙어 있어야 하는 장소 — 충전소 바깥 접근점(통로가 없으면 도킹 포인트), 엘리베이터 로비."""
    out = [(chain[0], "충전소 접근점") for chain in dock_chains(db, map_id)]
    lobby = elevator_lobby(db, map_id)
    if lobby is not None and lobby.LocationX is not None:
        out.append((lobby, "엘리베이터 로비"))
    return out


def direction_problems(db: Session, map_id: int, g: Graph, all_places: bool = True) -> list[dict]:
    """한쪽 방향 간선이 만든 막다른 길과 연결 끊김 — 충전소 접근점 노드(hub) 기준.

    - required_split: 필수 장소(엘리베이터 로비 등)가 충전소와 아예 다른 덩어리에 있다(차단)
    - oneway_trap:    같은 덩어리인데 한쪽 방향 때문에 충전소에서 갈 수 없거나 충전소로 돌아올 수 없다(차단)
    - charger_oneway: 충전소 접근점 노드에 붙은 간선이 한쪽 방향이다 — 출도킹·복귀 통로라 양방향만 된다(차단)
    all_places=False 면 필수 장소만 본다(check_ready). 다른 덩어리의 일반 장소는 여기서 다루지 않는다
    (이동 때 경로 없음으로 거절되고, 화면은 '노드망이 나뉨' 경고로 알린다).
    """
    hubs = []
    for chain in dock_chains(db, map_id):
        n = attach_any(g, (float(chain[0].LocationX), float(chain[0].LocationY)))
        if n is not None:
            hubs.append(n)
    if not hubs:
        return []
    hub = hubs[0]
    out = []
    for a, b in sorted(g.oneway):
        if a in hubs or b in hubs:
            out.append({"kind": "charger_oneway", "edge": [a, b],
                        "text": f"{label(a)}–{label(b)} 충전소 노드에 붙은 연결은 양방향이어야 합니다(출도킹·복귀 통로)."})
    comp_of = {n: i for i, c in enumerate(components(g)) for n in c}
    go, back = reachable(g, hub), reachable(g, hub, reverse=True)
    targets = list(required_places(db, map_id))
    if all_places:
        seen = {p.id for p, _ in targets}
        targets += [(p, "") for p in db.query(LocationInfo).filter(LocationInfo.MapId == map_id).all()
                    if p.id not in seen and p.LocationX is not None]
    for p, what in targets:
        n = attach_any(g, (float(p.LocationX), float(p.LocationY)))
        if n is None or n == hub:
            continue
        if comp_of.get(n, -1) != comp_of.get(hub, -2):
            if what:
                out.append({"kind": "required_split", "placeId": p.id,
                            "text": f"{what} '{p.LacationName}'이(가) 충전소와 노드망으로 이어져 있지 않습니다."})
            continue
        if n not in go or n not in back:
            why = "충전소에서 갈 수 없습니다" if n not in go else "충전소로 돌아올 수 없습니다"
            out.append({"kind": "oneway_trap", "placeId": p.id, "nodeId": n,
                        "text": f"'{p.LacationName}' — 한쪽 방향 연결 때문에 {why}."})
    return out


def check_ready(db: Session, map_id: Optional[int]) -> tuple[bool, str]:
    """이 맵에서 자율 이동이 가능한가. 노드망 경로가 꺼져 있으면 늘 가능(지금 동작)."""
    from app.robot_io.config import NODE_ROUTING_ENABLED

    if not NODE_ROUTING_ENABLED:
        return True, ""
    if map_id is None:
        return False, "로봇의 현재 맵을 알 수 없어 노드 경로를 계산할 수 없습니다."
    g = load_graph(db, int(map_id))
    if not g.nodes:
        return False, "이 맵은 노드망이 완성되지 않았습니다(노드 없음). 맵 편집에서 노드를 그려야 이동할 수 있습니다."
    if not dock_chains(db, int(map_id)) and has_charge_station(db, int(map_id)):
        # 충전소는 있는데 도킹 포인트(…-1)가 없다 — 복귀할 수 없으니 막는다.
        # 충전소가 아예 없는 맵(2F 주차장 등)은 이 조건을 보지 않는다(10-22 결정) — 엘리베이터 로비 연결만 본다.
        return False, "이 맵의 충전소에 도킹 포인트(…-1)가 없습니다. 충전소가 노드망에 연결되어야 이동할 수 있습니다."
    for place, what in required_places(db, int(map_id)):
        if attach_any(g, (float(place.LocationX), float(place.LocationY))) is None:
            return False, (f"이 맵은 노드망이 완성되지 않았습니다 — {what} '{place.LacationName}'이(가) "
                           f"노드망에 연결되어 있지 않습니다.")
    problems = direction_problems(db, int(map_id), g, all_places=False)
    if problems:
        return False, f"이 맵은 노드망이 완성되지 않았습니다 — {problems[0]['text']}"
    return True, ""


def outbound_prefix(db: Session, map_id: int, robot_xy: tuple[float, float],
                    goal: dict) -> tuple[list[dict], Optional[tuple[float, float]]]:
    """언도킹 직후 출발 — 도킹 포인트 근처면 접근 통로를 거꾸로(ch-2 … ch-N) 지난 뒤 노드망에 들어간다.

    Returns: (앞에 끼울 고정 통과 지점, 노드 경로의 새 출발점). 해당 없으면 ([], None).
    """
    for chain in dock_chains(db, map_id):
        if len(chain) < 2:
            continue
        dock = chain[-1]
        if math.dist(robot_xy, (dock.LocationX, dock.LocationY)) > AT_DOCK_M:
            continue
        if goal.get("name") in {r.LacationName for r in chain}:
            return [], None                         # 충전소로 가는 길이면 통로 그대로
        outward = list(reversed(chain[:-1]))        # ch-2 → … → ch-N
        wps = []
        for i, r in enumerate(outward):
            nxt = outward[i + 1] if i + 1 < len(outward) else None
            yaw = (math.atan2(nxt.LocationY - r.LocationY, nxt.LocationX - r.LocationX)
                   if nxt else (r.Yaw or 0.0))
            wps.append({"x": r.LocationX, "y": r.LocationY, "yaw": yaw, "name": r.LacationName,
                        "map_id": map_id, "floor_id": r.FloorId, "pass_through": True,
                        "node": True, "no_route": True, "dock_outbound": True})
        last = outward[-1]
        return wps, (float(last.LocationX), float(last.LocationY))
    return [], None
