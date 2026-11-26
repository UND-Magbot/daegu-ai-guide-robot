"""간선 옆 장소 붙이기(2026-10-28) — 긴 간선 한가운데 옆 장소가 양 끝 노드에서 2m 넘게 떨어져 '미연결'이 된다.

실측(10-28 맵49 3F): 종합민원실이 간선에서 0.30m 인데 가장 가까운 노드가 3.23m → 노드망 미연결 → 이동 거부.
변경: node_route.attach_edge — 가까운 노드가 없어도 간선 옆(발이 두 끝 사이, 2m 안, 발까지 벽 없음)이면 그 간선에
붙고, 드나들 수 있는 끝 노드(한쪽 방향이면 그 방향) 중 전체 길이가 가장 짧은 쪽을 쓴다. 점검(미연결 목록·준비
점검·저장 검사)도 같은 규칙(attach_any).

무대: node_route.py 병원 1층에서 복도 가운데 노드 N4·N5(와 거기만 붙던 N10·N13)를 빼고, N3(2.65)–N6(7.85)
을 5.2m 수동 간선으로 잇는다. 장소 '복도 가운데'(5.2, 3.3)는 간선에서 0.2m, N3·N6 에서 2.5m 넘게 떨어져 있다.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "node_route_base_ea", os.path.join(os.path.dirname(__file__), "node_route.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
Stage, ON, PLACES, NODES, MAP = _base.Stage, _base.ON, _base.PLACES, _base.NODES, _base.MAP

MID = (5.2, 3.3)
MID2 = (4.0, 3.0)
BEHIND = (5.2, 2.2)          # 간선에서 0.9m 지만 복도 벽(y=2.6) 너머 방 안
MY_NODES = {k: v for k, v in NODES.items() if k not in (4, 5, 10, 13)}
MY_PLACES = {**PLACES, "복도 가운데": (*MID, "work"), "복도 가운데2": (*MID2, "work"),
             "벽 너머": (*BEHIND, "work")}


def stage(sim, direction="both"):
    st = Stage(sim, nodes=MY_NODES, places=MY_PLACES)
    from app.database.database import SessionLocal
    from app.database import models as m
    db = SessionLocal()
    try:
        db.add(m.NavEdge(MapId=MAP, NodeA=3, NodeB=6, Kind="manual", Direction=direction))
        db.commit()
    finally:
        db.close()
    return st


def go(sim, st, name):
    sm = sim.sm
    from app.database.database import SessionLocal
    out = {}

    def run():
        db = SessionLocal()
        try:
            out.update(sm.start_place_move(st.place_ids[name], db))
        finally:
            db.close()
        if out.get("status") == "ok":
            sim.pl.nav_thread()
    n0, s0 = len(sim.robot.arrivals), len(st.samples)
    # 접수 뒤 보낼 때 경로 실패(node_route_failed)로 끝나도 멈춘다 — 도착만 기다리면 시뮬 상한까지 돈다
    sim.run(run, until=lambda: out and (out.get("status") != "ok" or not sm.is_navigating))
    path = st.samples[s0:] or [(sim.robot.x, sim.robot.y)]
    x, y, _ = MY_PLACES[name]
    arrived = abs(sim.robot.x - x) < 0.05 and abs(sim.robot.y - y) < 0.05
    return st.result(arrived=arrived, msg=out.get("msg"), max_x=round(max(p[0] for p in path), 2),
                     min_x=round(min(p[0] for p in path), 2),
                     nodes=[w["name"] for w in sm.waypoints_list if w.get("node")])


def place(sim, st, xy):
    sim.robot.place(*xy)
    st.samples.append(xy)


def ok(r, cond):
    return (r["arrived"] and r["wall_cross"] == 0 and r["errors"] == 0 and cond) or \
        f"도착={r['arrived']} 메시지={r.get('msg')} 노드={r.get('nodes')} x범위={r.get('min_x')}~{r.get('max_x')} 벽={r['wall_cross']}"


@scenario("E1 [재현] 로비 서쪽 → '복도 가운데'(긴 간선 옆) 도착, 동쪽 끝 N6 까지 지나치지 않음", env=ON,
          expect=lambda r: ok(r, r["max_x"] <= MID[0] + 0.15))
def e1(sim):
    st = stage(sim)
    place(sim, st, (0.8, 3.1))
    return go(sim, st, "복도 가운데")


@scenario("E2 '복도 가운데'에서 동쪽 원무과 접수 → 서쪽 N3 로 먼저 가지 않음", env=ON,
          expect=lambda r: ok(r, r["min_x"] >= MID[0] - 0.15))
def e2(sim):
    st = stage(sim)
    place(sim, st, MID)
    return go(sim, st, "원무과 접수")


@scenario("E3 '복도 가운데'에서 서쪽 진료실 1 → 동쪽 N6 로 먼저 가지 않음", env=ON,
          expect=lambda r: ok(r, r["max_x"] <= MID[0] + 0.15))
def e3(sim):
    st = stage(sim)
    place(sim, st, MID)
    return go(sim, st, "진료실 1")


@scenario("E4 같은 간선 옆끼리('복도 가운데' → '복도 가운데2') → 노드 거치지 않고 곧장", env=ON,
          expect=lambda r: ok(r, r["nodes"] == [] and r["max_x"] <= MID[0] + 0.15))
def e4(sim):
    st = stage(sim)
    place(sim, st, MID)
    return go(sim, st, "복도 가운데2")


@scenario("E5 [방향] 간선 N3→N6 한쪽: 서쪽에서 오면 도착", env=ON,
          expect=lambda r: ok(r, r["max_x"] <= MID[0] + 0.15))
def e5(sim):
    st = stage(sim, direction="forward")
    place(sim, st, (0.8, 3.1))
    return go(sim, st, "복도 가운데")


@scenario("E6 [방향] 간선 N3→N6 한쪽: 동쪽(N7)에서는 거꾸로 못 들어감 → 움직이지 않음(거절 또는 경로 실패)", env=ON,
          expect=lambda r: (not r["arrived"] and r["arrivals"] == 0 and r["wall_cross"] == 0 and r["cmds"] == [])
          or f"도착={r['arrived']} 메시지={r.get('msg')} 노드={r.get('nodes')}")
def e6(sim):
    st = stage(sim, direction="forward")
    place(sim, st, (8.65, 3.1))
    return go(sim, st, "복도 가운데")


@scenario("E7 [경계] 간선에서 0.9m 지만 벽 너머(방 안) → 붙지 않음, 거절", env=ON,
          expect=lambda r: (not r["arrived"] and r["arrivals"] == 0 and "연결되지 않은" in (r.get("msg") or ""))
          or f"도착={r['arrived']} 메시지={r.get('msg')}")
def e7(sim):
    st = stage(sim)
    place(sim, st, (0.8, 3.1))
    return go(sim, st, "벽 너머")


@scenario("E8 [점검] 맵 편집 점검: '복도 가운데'는 연결됨(N3·N6 대표), '벽 너머'는 미연결", env=ON,
          expect=lambda r: (r["mid_attach"] in ("3", "6") and r["mid_unlinked"] is False and r["behind_unlinked"] is True)
          or f"복도 가운데 대표={r['mid_attach']} 미연결={r['mid_unlinked']} · 벽 너머 미연결={r['behind_unlinked']}")
def e8(sim):
    st = stage(sim)
    from app.database.database import SessionLocal
    from app.map import nav_nodes
    db = SessionLocal()
    try:
        chk = nav_nodes._inspect(db, MAP)
    finally:
        db.close()
    pid, bid = st.place_ids["복도 가운데"], st.place_ids["벽 너머"]
    a = chk["placeAttach"].get(str(pid))
    return st.result(mid_attach=str(a) if a is not None else None, mid_unlinked=pid in chk["unlinkedPlaceIds"],
                     behind_unlinked=bid in chk["unlinkedPlaceIds"])


@scenario("E9 [회귀] 노드 2m 안 장소(원무과 접수)는 지금과 같은 경로", env=ON,
          expect=lambda r: ok(r, True))
def e9(sim):
    st = stage(sim)
    place(sim, st, (0.8, 3.1))
    return go(sim, st, "원무과 접수")
