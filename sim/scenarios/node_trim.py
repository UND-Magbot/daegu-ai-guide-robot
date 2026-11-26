"""노드망 끝 노드 지나침(2026-10-27) — 목적지가 간선 옆인데 가장 가까운 노드까지 갔다가 되돌아온다.

실기(10-27 14:55 사전 주행, 맵47): 편의점입구(-1.18,-2.00)는 N236(-0.54,0.46)–N238(-0.47,-2.75) 통로 옆인데
가장 가까운 노드 N238 에 붙어 0.75m 지나쳤다 복귀, 다음 구간 출발도 N238 로 먼저 내려갔다가 올라옴.

변경: node_route._trim_ends — 경로를 찾은 뒤 목표(시작)가 마지막(첫) 간선 옆(발이 두 끝 사이, 2m 안)이고
지름길이 벽에서 충분히 떨어지면 그 끝 노드를 건너뛴다. 문 간선은 건너뛰지 않는다.

무대: node_route.py 병원 1층 배치. 복도(y=3.1) 노드 N3(2.65)–N4(4.55) 사이 옆에 장소 '복도 옆'(3.9, 3.25).
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "node_route_base_nt", os.path.join(os.path.dirname(__file__), "node_route.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
Stage, ON, PLACES, NODES = _base.Stage, _base.ON, _base.PLACES, _base.NODES

BESIDE = (3.9, 3.25)
MY_PLACES = {**PLACES, "복도 옆": (*BESIDE, "work"), "복도 끝": (8.65, 3.1, "work")}


def _go(sim, st, name):
    sm = sim.sm
    from app.database.database import SessionLocal
    out = {}
    x, y, _ = MY_PLACES[name]

    def go():
        db = SessionLocal()
        try:
            out.update(sm.start_place_move(st.place_ids[name], db))
        finally:
            db.close()
        if out.get("status") == "ok":
            sim.pl.nav_thread()
    n0 = len(sim.robot.arrivals)
    s0 = len(st.samples)
    sim.run(go, until=lambda: out and (out.get("status") != "ok" or (
        not sm.is_navigating and len(sim.robot.arrivals) > n0)))
    return out, st.samples[s0:]


def _arrived(sim, name):
    x, y, _ = MY_PLACES[name]
    return abs(sim.robot.x - x) < 0.05 and abs(sim.robot.y - y) < 0.05


@scenario("T1 [재현] 복도 서쪽 끝 → '복도 옆'(N3–N4 사이) → N4 까지 지나치지 않고 바로", env=ON,
          expect=lambda r: (r["arrived"] and r["max_x"] <= BESIDE[0] + 0.15 and r["wall_cross"] == 0)
          or f"도착={r['arrived']} 최대x={r['max_x']} (목표 x {BESIDE[0]}) 노드={r['nodes']}")
def t1(sim):
    st = Stage(sim, places=MY_PLACES)
    sim.robot.place(0.8, 3.1)
    st.samples.append((0.8, 3.1))
    out, path = _go(sim, st, "복도 옆")
    return st.result(arrived=_arrived(sim, "복도 옆"), max_x=round(max(p[0] for p in path), 2),
                     nodes=[w["name"] for w in sim.sm.waypoints_list if w.get("node")])


@scenario("T2 [재현] '복도 옆'에서 출발 → 진료실 1(서쪽) → 동쪽 N4 로 먼저 가지 않음", env=ON,
          expect=lambda r: (r["arrived"] and r["max_x"] <= BESIDE[0] + 0.15 and r["wall_cross"] == 0)
          or f"도착={r['arrived']} 최대x={r['max_x']} 노드={r['nodes']}")
def t2(sim):
    st = Stage(sim, places=MY_PLACES)
    sim.robot.place(*BESIDE)
    st.samples.append(BESIDE)
    out, path = _go(sim, st, "진료실 1")
    return st.result(arrived=_arrived(sim, "진료실 1"), max_x=round(max(p[0] for p in path), 2),
                     nodes=[w["name"] for w in sim.sm.waypoints_list if w.get("node")])


@scenario("T3 [회귀] 목적지가 끝 노드 바로 옆(복도 끝 = N7 위치) → 끝까지 가는 것 그대로", env=ON,
          expect=lambda r: (r["arrived"] and r["wall_cross"] == 0) or f"도착={r['arrived']}")
def t3(sim):
    st = Stage(sim, places=MY_PLACES)
    sim.robot.place(0.8, 3.1)
    out, path = _go(sim, st, "복도 끝")
    return st.result(arrived=_arrived(sim, "복도 끝"))
