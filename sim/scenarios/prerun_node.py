"""안내 사전 주행 × 노드망(2026-10-27).

실기(10-27 14:43): 음성관리 1회 주행 시험(출발 로봇대기장소, 고속) → "'진료대기장소'까지 맵상 안전 우회 경로를
찾지 못했습니다" — voice_prerun._build_waypoints 가 노드망이 켜져 있어도 예전 자동 우회(plan_via_waypoints)로
구간을 계획했다. 장소 이동·안내 이동은 노드망이면 이 계산을 건너뛰고 보낼 때 노드를 펼친다.

변경:
- voice_prerun._build_waypoints: NODE_ROUTING_ENABLED 면 자동 우회 대신 node_entry_check 만 하고 목표만 넣는다.
- send_move: 노드를 끼울 때 목표의 사전 주행 표시(prerun_leg·halt_if_unreached·speed)를 노드에 물려 준다
  — 노드에서 못 가도 그 구간에서 멈추고 실패 구간을 맞게 표시한다.

무대: node_route.py 의 병원 1층 배치(벽·노드). 하네스에는 격자 맵이 없어 현행 자동 우회는 map_unavailable 로
거절된다(실기 사유 no_safe_detour 와 같은 '거절').
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "node_route_base_pn", os.path.join(os.path.dirname(__file__), "node_route.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
Stage, NR_ON, PLACES = _base.Stage, _base.ON, _base.PLACES

ON = {**NR_ON, "NAV_MULTIPOINT_ENABLED": "true"}
LEGS = ["원무과 접수", "간호실", "진료실 1"]        # 대기 의자에서 출발 — 구간마다 벽 너머


def _stage(sim):
    st = Stage(sim)
    robot = sim.robot
    inner = robot.nav_multi

    def nav_multi(points, *a, **k):              # Stage 래퍼는 points 만 받는다 — 새 인자는 버린다
        return inner(points)
    robot.nav_multi = nav_multi
    sim.robot.place(*PLACES["대기 의자"][:2])
    return st


def _legs(st):
    return [{"id": st.place_ids[n], "name": n, "x": PLACES[n][0], "y": PLACES[n][1]} for n in LEGS]


def _build(sim, st, legs):
    import app.database.voice_prerun as vp
    from app.database.database import SessionLocal
    sim.rewire_late_modules()
    db = SessionLocal()
    try:
        return vp._build_waypoints(db, legs, 0, (sim.robot.x, sim.robot.y), "fast")
    finally:
        db.close()


def _drive(sim, wps):
    sm = sim.sm
    final = (wps[-1]["x"], wps[-1]["y"])
    n0 = len(sim.robot.arrivals)
    sm.waypoints_list, sm.current_wp_index, sm.is_navigating = wps, 0, True
    sm._signal_nav_reset(full=True)

    def go():
        sm.navigation_send_next()
        sim.pl.nav_thread()
    sim.run(go, until=lambda: not sm.is_navigating and (
        any((a[1], a[2]) == final for a in sim.robot.arrivals[n0:]) or sim.clock.elapsed() > 30))


@scenario("P1 [재현] 대기 의자 → 원무과 → 간호실 → 진료실(구간마다 벽 너머) → 거절 없이 노드 따라 끝까지", env=ON,
          limit_sec=900,
          expect=lambda r: (r["refused"] is None and _base.arrived_at(r, "진료실 1") and r["wall_cross"] == 0
                            and r["nodes_flagged"] and r["errors"] == 0)
          or f"거절={r['refused']} 벽={r['wall_cross']} 노드표시={r['nodes_flagged']} 위치={r['pos']}")
def p1(sim):
    st = _stage(sim)
    wps, why = _build(sim, st, _legs(st))
    if wps is None:
        return st.result(refused=why, nodes_flagged=False)
    _drive(sim, wps)
    nodes = [w for w in sim.sm.waypoints_list if w.get("node")]
    flagged = bool(nodes) and all("prerun_leg" in w and w.get("halt_if_unreached") and w.get("speed") == "fast"
                                  for w in nodes)
    return st.result(refused=None, nodes_flagged=flagged, n_nodes=len(nodes))


@scenario("P2 노드망에 붙지 않는 장소가 섞이면 → 출발 전 거절(노드 사유)", env=ON,
          expect=lambda r: (r["refused"] is not None and "안전 우회" not in r["refused"])
          or f"거절={r['refused']}")
def p2(sim):
    st = _stage(sim)
    from app.database.database import SessionLocal
    from app.database import models as m
    db = SessionLocal()
    try:
        far = m.LocationInfo(LacationName="외딴곳", LocationX=30.0, LocationY=30.0, Yaw=0.0, MapId=1,
                             FloorId=1, Category="work")
        db.add(far)
        db.commit()
        fid = far.id
    finally:
        db.close()
    legs = _legs(st)[:1] + [{"id": fid, "name": "외딴곳", "x": 30.0, "y": 30.0}]
    wps, why = _build(sim, st, legs)
    return st.result(refused=why if wps is None else None)
