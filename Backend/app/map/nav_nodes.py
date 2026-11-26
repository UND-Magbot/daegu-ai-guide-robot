"""노드망 편집 API (2026-10-19) — 맵 편집 화면의 노드 레이어가 쓴다.

GET  /map/nav-nodes/{map_id}          노드·간선·점검 결과(이동 가능 여부, 문제 목록)
PUT  /map/nav-nodes/{map_id}          노드·수동/문/제외 간선을 통째로 저장하고 자동 간선을 다시 계산한다
POST /map/nav-nodes/{map_id}/preview  두 지점 사이 노드 경로 미리보기
POST /map/nav-nodes/{map_id}/validate 저장하지 않고 검사만(편집 중 화면이 자동 간선·점검을 바로 다시 그린다)
POST /map/nav-nodes/{map_id}/attach-check  (x, y) 에 장소를 만들면 노드에 붙는가

저장 규칙
- 자동 간선은 저장 때 계산한다: NODE_AUTO_EDGE_MAX_M 안의 두 노드를 벽 여유(0.15m)를 두고 이을 수 있으면
  잇는다. 사이에 다른 노드가 있으면 그 노드를 거치는 짧은 간선으로 충분하므로 잇지 않는다.
- 수동 간선이 벽을 가로지르면 거절한다. 문 간선은 벽 검사를 하지 않는 대신, 등록된 자동문의 양쪽 문 앞
  장소에서 DOOR_NEAR_M 안의 두 노드만 이을 수 있다.
- 저장 전에 노드망에 붙어 있던 충전소 접근점·엘리베이터 로비가 저장 뒤 끊기면 거절한다(새 맵에서 아직
  붙인 적이 없으면 저장은 받는다 — 맵은 계속 '이동 불가' 로 남는다).
- 간선 방향(2026-10-20): 기본 양방향. 한쪽 방향 때문에 충전소에서 갈 수 없거나 돌아올 수 없는 장소가 생기면,
  또 충전소 접근점 노드에 붙은 간선이 한쪽이면 거절한다. 같은 두 노드에 사용자 간선(수동·문)이 있으면 자동
  간선은 만들지 않는다 — 남겨 두면 한쪽 수동 간선 옆의 양방향 자동 간선 때문에 방향이 무시된다.
"""

from __future__ import annotations

import math
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.audit import get_client_ip, write_audit
from app.auth.dependencies import get_business_map_ids, get_current_user, is_admin, require_permission
from app.database.database import get_db
from app.database.models import DoorInfo, LocationInfo, NavEdge, NavNode, RobotMapInfo, UserInfo
from app.navigation import node_route

router = APIRouter()

# 노드끼리 이 거리(m)보다 가까우면 하나로 본다(맵 편집 목업과 같은 기준)
MIN_NODE_GAP_M = 0.4
# 자동 간선 판정 때 '사이에 다른 노드가 있다' 로 볼 거리(m)
BETWEEN_NODE_M = 0.12


class NodeIn(BaseModel):
    key: str = Field(..., description="화면에서 쓰는 노드 키(기존 노드는 id 문자열, 새 노드는 임시 키)")
    x: float
    y: float


class EdgeIn(BaseModel):
    a: str
    b: str
    kind: Literal["manual", "door", "excluded"]
    direction: Literal["both", "forward"] = "both"
    door_id: Optional[int] = None


class SaveReq(BaseModel):
    nodes: list[NodeIn]
    edges: list[EdgeIn] = []


class PreviewReq(BaseModel):
    from_x: float
    from_y: float
    to_place_id: int


def _get_map(db: Session, map_id: int, user: UserInfo) -> RobotMapInfo:
    m = db.query(RobotMapInfo).filter(RobotMapInfo.id == map_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="맵을 찾을 수 없습니다.")
    if not is_admin(user) and user.BusinessId and map_id not in get_business_map_ids(db, user.BusinessId):
        raise HTTPException(status_code=403, detail="이 사업장의 맵이 아닙니다.")
    return m


def _wall_clear(map_id: int, a: tuple[float, float], b: tuple[float, float]) -> bool:
    """노드 사이를 벽 여유(0.15m)를 두고 이을 수 있는가. 지도를 못 읽으면 False."""
    from app.navigation.via_route import _shortcut_grid

    grid = _shortcut_grid(map_id)
    return grid is not None and not grid.line_blocked(a, b)


def compute_auto_edges(map_id: int, nodes: dict[int, tuple[float, float]]) -> list[tuple[int, int]]:
    from app.robot_io.config import NODE_AUTO_EDGE_MAX_M

    ids = sorted(nodes)
    out = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            pa, pb = nodes[a], nodes[b]
            if math.dist(pa, pb) > NODE_AUTO_EDGE_MAX_M:
                continue
            between = any(node_route_pt_seg(nodes[c], pa, pb) < BETWEEN_NODE_M
                          for c in ids if c not in (a, b))
            if not between and _wall_clear(map_id, pa, pb):
                out.append((a, b))
    return out


def node_route_pt_seg(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2)) if length2 else 0.0
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _inspect(db: Session, map_id: int) -> dict:
    """이동 가능 여부와 문제 목록 — 화면의 '이 맵에서 이동하려면' 체크리스트·점검 목록에 쓴다."""
    g = node_route.load_graph(db, map_id)
    ready, reason = node_route.check_ready(db, map_id) if g.nodes else (False, "노드가 없습니다.")
    issues = []
    for place, what in node_route.required_places(db, map_id):
        if node_route.attach_any(g, (float(place.LocationX), float(place.LocationY))) is None:
            issues.append({"level": "block", "kind": "required_unattached",
                           "text": f"{what} '{place.LacationName}'이(가) 노드망에 연결되어 있지 않습니다.",
                           "placeId": place.id})
    comps = node_route.components(g)
    if len(comps) > 1:
        issues.append({"level": "warn", "kind": "split",
                       "text": f"노드망이 {len(comps)}개로 나뉘어 있습니다. 서로 다른 덩어리 사이는 이동할 수 없습니다."})
    linked = set().union(*comps) if comps else set()
    for nid in g.nodes:
        if nid not in linked:
            issues.append({"level": "warn", "kind": "isolated", "nodeId": nid,
                           "text": f"{node_route.label(nid)} — 다른 노드와 연결되지 않았습니다."})
    places = db.query(LocationInfo).filter(LocationInfo.MapId == map_id).all()
    unlinked = []
    for p in places:
        if p.LocationX is None:
            continue
        nid = node_route.attach_any(g, (float(p.LocationX), float(p.LocationY))) if g.nodes else None
        if nid is None:
            unlinked.append(p.id)
    for prob in node_route.direction_problems(db, map_id, g) if g.nodes else []:
        issues.append({"level": "block", **prob})
    charger_nodes = []
    for chain in node_route.dock_chains(db, map_id):
        n = node_route.attach_any(g, (float(chain[0].LocationX), float(chain[0].LocationY))) if g.nodes else None
        if n is not None:
            charger_nodes.append(n)
    attach_of = {str(p.id): (node_route.attach_any(g, (float(p.LocationX), float(p.LocationY))) if g.nodes else None)
                 for p in places if p.LocationX is not None}
    return {"ready": bool(ready), "reason": reason, "issues": issues, "unlinkedPlaceIds": unlinked,
            "placeAttach": attach_of, "chargerNodes": charger_nodes}


@router.get("/nav-nodes/{map_id}")
def get_nav_nodes(map_id: int, db: Session = Depends(get_db),
                  current_user: UserInfo = Depends(get_current_user)):
    _get_map(db, map_id, current_user)
    nodes = db.query(NavNode).filter(NavNode.MapId == map_id).all()
    edges = db.query(NavEdge).filter(NavEdge.MapId == map_id).all()
    return {
        "mapId": map_id,
        "nodes": [{"id": n.id, "x": n.X, "y": n.Y} for n in nodes],
        "edges": [{"id": e.id, "a": e.NodeA, "b": e.NodeB, "kind": e.Kind,
                   "direction": e.Direction, "doorId": e.DoorId} for e in edges],
        "check": (chk := _inspect(db, map_id)),
        "chargerNodes": chk.pop("chargerNodes"),
        # 노드망 경로가 켜져 있는가 — 꺼져 있으면 화면은 노드망을 보여 주기만 하고 이동을 막지 않는다
        "enabled": _node_routing_enabled(),
    }


def _node_routing_enabled() -> bool:
    from app.robot_io.config import NODE_ROUTING_ENABLED
    return bool(NODE_ROUTING_ENABLED)


def _validate_door_edge(db: Session, map_id: int, e: EdgeIn, pa, pb) -> None:
    if e.door_id is None:
        raise HTTPException(status_code=400, detail="문 간선에는 연결할 자동문을 골라야 합니다.")
    door = db.query(DoorInfo).filter(DoorInfo.id == e.door_id).first()
    if not door or (door.MapId is not None and door.MapId != map_id):
        raise HTTPException(status_code=400, detail="이 맵에 등록된 자동문이 아닙니다. 시설 장치 탭에서 먼저 등록하세요.")
    ends = []
    for lid in (door.NearLocationId, door.FarLocationId):
        loc = db.query(LocationInfo).filter(LocationInfo.id == lid).first() if lid else None
        if loc is None or loc.LocationX is None:
            raise HTTPException(status_code=400, detail=f"자동문 '{door.Name}'의 문 앞 장소가 지정되지 않았습니다.")
        ends.append((float(loc.LocationX), float(loc.LocationY)))
    near = node_route.DOOR_NEAR_M

    def close(p, q):
        return math.dist(p, q) <= near

    if not ((close(pa, ends[0]) and close(pb, ends[1])) or (close(pa, ends[1]) and close(pb, ends[0]))):
        raise HTTPException(status_code=400,
                            detail=f"이 선은 자동문 '{door.Name}'을(를) 지나지 않습니다 — 문 양쪽 노드는 "
                                   f"각 문 앞 장소에서 {near}m 안이어야 합니다.")


DIRECTION_BLOCKS = ("oneway_trap", "charger_oneway")


def _write(db: Session, map_id: int, req: SaveReq) -> tuple[dict[str, int], list[str]]:
    """노드·간선을 통째로 바꾸고 flush 한다(커밋·되돌리기는 호출자).

    Returns: (화면 key → 노드 id, 저장을 막는 규칙 위반 문구). 형식 오류(겹친 노드·벽 관통·문 검증)는
    HTTPException 400 — 호출자가 rollback 한다.
    """
    # 저장 전 — 붙어 있던 필수 장소(충전소 접근점·엘리베이터 로비)
    before_graph = node_route.load_graph(db, map_id)
    required = node_route.required_places(db, map_id)
    was_attached = {p.id for p, _ in required
                    if before_graph.nodes and node_route.attach_any(before_graph, (float(p.LocationX), float(p.LocationY)))}

    pts = {n.key: (n.x, n.y) for n in req.nodes}
    # 새로 생기거나 옮긴 노드는 흰 바닥·벽에서 떨어진 곳이어야 한다(저장된 노드는 그대로 둔다)
    saved = {str(n.id): (n.X, n.Y) for n in db.query(NavNode).filter(NavNode.MapId == map_id).all()}
    for n in req.nodes:
        old = saved.get(n.key)
        if old is not None and math.dist(old, (n.x, n.y)) < 1e-6:
            continue
        why = node_route.node_spot_problem(map_id, (n.x, n.y))
        if why:
            raise HTTPException(status_code=400, detail=f"노드 ({n.x:.2f}, {n.y:.2f}) — {why}")
    keys = list(pts)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if math.dist(pts[a], pts[b]) < MIN_NODE_GAP_M:
                raise HTTPException(status_code=400, detail=f"노드가 {MIN_NODE_GAP_M}m 안에 겹쳐 있습니다.")
    for e in req.edges:
        if e.a not in pts or e.b not in pts or e.a == e.b:
            raise HTTPException(status_code=400, detail="간선 양끝 노드를 찾을 수 없습니다.")
        if e.kind == "door":
            _validate_door_edge(db, map_id, e, pts[e.a], pts[e.b])
        elif e.kind == "manual" and not _wall_clear(map_id, pts[e.a], pts[e.b]):
            raise HTTPException(status_code=400, detail="두 노드 사이에 벽이 있어 이을 수 없습니다. 자동문이면 문 간선을 쓰세요.")

    # 통째로 바꾼다 — 한 트랜잭션
    old_ids = {str(n.id): n for n in db.query(NavNode).filter(NavNode.MapId == map_id).all()}
    db.query(NavEdge).filter(NavEdge.MapId == map_id).delete(synchronize_session=False)
    id_of: dict[str, int] = {}
    keep = set()
    for n in req.nodes:
        row = old_ids.get(n.key)
        if row is None:
            row = NavNode(MapId=map_id, X=n.x, Y=n.y)
            db.add(row)
            db.flush()
        else:
            row.X, row.Y = n.x, n.y
            keep.add(n.key)
        id_of[n.key] = row.id
    for k, row in old_ids.items():
        if k not in keep:
            db.delete(row)
    db.flush()

    nodes_xy = {id_of[k]: pts[k] for k in keys}
    # 수동·문·막은 연결이 있는 쌍에는 자동 간선을 두지 않는다 — 막은 연결 쌍에 자동 간선이 남으면 화면에 점선이 남는다
    user_pairs = {frozenset((id_of[e.a], id_of[e.b])) for e in req.edges}
    for a, b in compute_auto_edges(map_id, nodes_xy):
        if frozenset((a, b)) not in user_pairs:
            db.add(NavEdge(MapId=map_id, NodeA=a, NodeB=b, Kind="auto", Direction="both"))
    for e in req.edges:
        db.add(NavEdge(MapId=map_id, NodeA=id_of[e.a], NodeB=id_of[e.b], Kind=e.kind,
                       Direction=e.direction, DoorId=e.door_id if e.kind == "door" else None))
    db.flush()

    after_graph = node_route.load_graph(db, map_id)
    violations = [f"{what} '{p.LacationName}'이(가) 노드망에서 끊기는 변경은 저장할 수 없습니다."
                  for p, what in required
                  if p.id in was_attached
                  and node_route.attach_any(after_graph, (float(p.LocationX), float(p.LocationY))) is None]
    # 직접 그린 간선이 연결 안 된 노드 위를 지나거나 다른 간선과 겹치면 저장하지 않는다(10-29)
    violations += node_route.overlap_problems(after_graph, {frozenset((id_of[e.a], id_of[e.b])) for e in req.edges
                                                            if e.kind != "excluded"})
    bad = [i for i in node_route.direction_problems(db, map_id, after_graph) if i["kind"] in DIRECTION_BLOCKS]
    if bad:
        violations.append("방향 설정 때문에 저장할 수 없습니다.\n" + "\n".join(f"· {i['text']}" for i in bad))
    return id_of, violations


@router.put("/nav-nodes/{map_id}")
def save_nav_nodes(map_id: int, req: SaveReq, request: Request, db: Session = Depends(get_db),
                   current_user: UserInfo = Depends(require_permission("map-edit"))):
    _get_map(db, map_id, current_user)
    try:
        _, violations = _write(db, map_id, req)
    except HTTPException:
        db.rollback()
        raise
    if violations:
        db.rollback()
        raise HTTPException(status_code=409, detail=violations[0])
    db.commit()
    write_audit(db, current_user.id, "nav_nodes_saved", "map", map_id,
                f"노드망 저장: 노드 {len(req.nodes)}개, 수동·문·제외 간선 {len(req.edges)}개",
                get_client_ip(request))
    return get_nav_nodes(map_id, db, current_user)


@router.post("/nav-nodes/{map_id}/validate")
def validate_nav_nodes(map_id: int, req: SaveReq, db: Session = Depends(get_db),
                       current_user: UserInfo = Depends(require_permission("map-edit"))):
    """저장하지 않고 검사만 — 같은 규칙으로 써 본 뒤 되돌린다. 응답의 노드 key 는 화면이 보낸 key."""
    _get_map(db, map_id, current_user)
    try:
        id_of, _ = _write(db, map_id, req)     # 규칙 위반은 거절 대신 점검 결과(issues)로 보인다
        key_of = {v: k for k, v in id_of.items()}
        cur = get_nav_nodes(map_id, db, current_user)
    except HTTPException:
        db.rollback()
        raise
    db.rollback()

    def k(n):
        return key_of.get(n, str(n)) if n is not None else None
    chk = cur["check"]
    return {
        "mapId": map_id,
        "nodes": [{"key": k(n["id"]), "x": n["x"], "y": n["y"]} for n in cur["nodes"]],
        "edges": [{**e, "a": k(e["a"]), "b": k(e["b"])} for e in cur["edges"]],
        "chargerNodes": [k(n) for n in cur["chargerNodes"]],
        "check": {**chk, "placeAttach": {pid: k(n) for pid, n in chk["placeAttach"].items()},
                  "issues": [{**i, **({"nodeKey": k(i["nodeId"])} if "nodeId" in i else {}),
                              **({"edge": [k(i["edge"][0]), k(i["edge"][1])]} if i.get("edge") else {})}
                             for i in chk["issues"]]},
    }


class SpotReq(BaseModel):
    x: float
    y: float


@router.post("/nav-nodes/{map_id}/spot-check")
def spot_check(map_id: int, req: SpotReq, db: Session = Depends(get_db),
               current_user: UserInfo = Depends(get_current_user)):
    """(x, y) 에 노드를 둘 수 있는가 — 맵 편집이 노드를 추가·옮길 때 묻는다."""
    _get_map(db, map_id, current_user)
    why = node_route.node_spot_problem(map_id, (req.x, req.y))
    return {"ok": why is None, "reason": why}


class AttachReq(BaseModel):
    x: float
    y: float
    nodes: list[NodeIn] = []


@router.post("/nav-nodes/{map_id}/attach-check")
def attach_check(map_id: int, req: AttachReq, db: Session = Depends(get_db),
                 current_user: UserInfo = Depends(get_current_user)):
    """(x, y) 에 장소를 만들면 노드에 붙는가 — 편집 중 노드 목록(없으면 저장된 노드)으로 판정한다."""
    _get_map(db, map_id, current_user)
    g = node_route.Graph(map_id=map_id)
    keys: dict[int, str] = {}
    if req.nodes:
        for i, n in enumerate(req.nodes):
            g.nodes[i] = (n.x, n.y)
            keys[i] = n.key
        # 편집 중 간선은 저장 때 만들 자동 간선으로 본다(수동·문 간선은 화면에만 있어 여기서는 모른다)
        for a, b in compute_auto_edges(map_id, g.nodes):
            g.adj.setdefault(a, []).append(b)
            g.adj.setdefault(b, []).append(a)
    else:
        g = node_route.load_graph(db, map_id)
        keys = {nid: str(nid) for nid in g.nodes}
    pt = (req.x, req.y)
    nid, edge = node_route.attach_point(g, pt)       # 노드 바로 위 → 간선 위 → 2m 안 노드(실제 경로 계산과 같은 순서)
    if nid is not None:
        return {"nodeKey": keys[nid], "distM": round(math.dist(pt, g.nodes[nid]), 2)}
    if edge is not None:
        # 간선 옆 — 가까운 끝 노드를 대표로, 거리는 간선까지
        a, b = edge
        near_end = min(edge, key=lambda k: math.dist(pt, g.nodes[k]))
        return {"nodeKey": keys[near_end], "distM": round(node_route_pt_seg(pt, g.nodes[a], g.nodes[b]), 2),
                "edge": [keys[a], keys[b]]}
    near = min(g.nodes, key=lambda i: math.dist(pt, g.nodes[i]), default=None)
    d = math.dist(pt, g.nodes[near]) if near is not None else None
    return {"nodeKey": None, "nearestKey": keys.get(near) if near is not None else None,
            "nearestM": round(d, 2) if d is not None else None,
            "reason": ("2 m 안에 노드·간선이 없습니다" if d is None or d > node_route.ATTACH_MAX_M
                       else "가까운 노드가 모두 벽 너머에 있습니다")}


@router.post("/nav-nodes/{map_id}/preview")
def preview_route(map_id: int, req: PreviewReq, db: Session = Depends(get_db),
                  current_user: UserInfo = Depends(get_current_user)):
    _get_map(db, map_id, current_user)
    place = db.query(LocationInfo).filter(LocationInfo.id == req.to_place_id,
                                          LocationInfo.MapId == map_id).first()
    if not place:
        raise HTTPException(status_code=404, detail="이 맵의 장소가 아닙니다.")
    goal = {"x": place.LocationX, "y": place.LocationY, "name": place.LacationName}
    start = (req.from_x, req.from_y)
    plan = node_route.plan_leg(db, map_id, start, goal)
    if plan.error:
        return {"ok": False, "msg": plan.error}
    length = node_route.leg_length(start, plan.waypoints, goal)
    direct = math.dist(start, (goal["x"], goal["y"]))
    return {
        "ok": True,
        "nodePath": plan.node_path,
        "waypoints": [{"x": w["x"], "y": w["y"], "name": w["name"], "doorId": w.get("door_id")}
                      for w in plan.waypoints],
        "entry": plan.entry,
        "lengthM": round(length, 2),
        "ratio": round(length / direct, 2) if direct > 1e-6 else None,
    }
