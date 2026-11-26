"""S4 노드망 백엔드 결합(2026-10-22) — 현행(노드망+다지점 사본) vs 변경.

변경:
- 띠 안 건너뛰기(D3 0.4 m): 건너뛴 노드가 지름길 직선에서 0.4 m 안일 때만 건너뛴다.
- 다음 목표 미리 펼치기: 통과 목표(경유지)의 노드를 앞 지점 자리에서 미리 펼쳐 한 묶음(NAV_MULTI 1건)으로.
- 문 간선 노드·층 경계·멈추는 목표에서는 지금처럼 묶음을 끊는다(회귀 확인).

무대는 node_route.py 의 Stage(병원 1층 배치, 벽 선분 판정). 다지점 켬, 노드망 직선.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "node_route_base_s4", os.path.join(os.path.dirname(__file__), "node_route.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
Stage, PLACES, MAP, ON, arrived_at = _base.Stage, _base.PLACES, _base.MAP, _base.ON, _base.arrived_at

ENV = {**ON, "NAV_MULTIPOINT_ENABLED": "true"}


def _route(sim, st, names, pass_flags):
    """경로 이동(여러 목표) — 앞 목표는 통과(pass_through), 마지막은 정지."""
    sm = sim.sm
    wps = []
    for name, passing in zip(names, pass_flags):
        x, y, _ = PLACES[name]
        w = {"x": x, "y": y, "yaw": 0.0, "name": name, "map_id": MAP}
        if passing:
            w["pass_through"] = True
        wps.append(w)
    sm.waypoints_list = wps
    sm.current_wp_index, sm.is_navigating = 0, True
    sm._signal_nav_reset(full=True)
    final = (wps[-1]["x"], wps[-1]["y"])
    n0 = len(sim.robot.arrivals)
    sim.run(lambda: (sm.navigation_send_next(), sim.pl.nav_thread()),
            until=lambda: not sm.is_navigating and any((a[1], a[2]) == final for a in sim.robot.arrivals[n0:]))


@scenario("S4-1 띠: 넓은 곳 ㄱ자 꺾임(꼭짓점 3.5m 벗어남) → 꼭짓점 노드 남김 / 거의 일직선(0.1m) → 건너뜀", env=ENV,
          expect=lambda r: r["corner"] == [0, 1, 2] and r["straight"] == [0, 2])
def s4_1(sim):
    st = Stage(sim)
    nr = st.nr
    nr._shortcut_clear = lambda mid, a, b, **_: True          # 벽 없는 넓은 곳
    corner = nr._simplify(MAP, [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0)], set())
    straight = nr._simplify(MAP, [(0.0, 0.0), (2.0, 0.1), (4.0, 0.0)], set())
    return st.result(corner=corner, straight=straight)


@scenario("S4-2 경로 이동(진료실 1 통과 → 원무과): 경유지 노드를 미리 펼쳐 다지점 1건, 벽 안 넘음", env=ENV,
          # 10-27 부터 묶음 최대 4점, 10-29 부터 5점 — 5점 이하 묶음들로 이어지면 된다(경유지 노드는 미리 펼침)
          expect=lambda r: arrived_at(r, "원무과 접수") and r["wall_cross"] == 0 and r["multi_sent"]
          and max(r["multi_sent"]) <= 5 and "진료실 1" in r["targets"] and r["errors"] == 0)
def s4_2(sim):
    st = Stage(sim)
    sim.robot.place(2.0, 5.0)
    _route(sim, st, ["진료실 1", "원무과 접수"], [True, False])
    return st.result()


@scenario("S4-3 [회귀] 문 간선: 간호실→약제실, 문 양쪽 노드(N15·N16)는 묶음에 넣지 않고 하나씩 — 문 앞에서 선다", env=ENV,
          expect=lambda r: arrived_at(r, "약제실") and r["wall_cross"] == 0
          and r["targets"][:2] == ["N15", "N16"] and r["cmds"][:2] == ["NAV", "NAV"])
def s4_3(sim):
    st = Stage(sim, door=True)
    sim.robot.place(6.5, 5.0)
    st.go_place("약제실")
    return st.result()


@scenario("S4-4 [회귀] 경로 이동 중 멈추는 목표(정지 경유지)에서는 묶음을 끊음", env=ENV,
          expect=lambda r: arrived_at(r, "원무과 접수") and r["wall_cross"] == 0 and len(r["multi_sent"]) >= 2)
def s4_4(sim):
    st = Stage(sim)
    sim.robot.place(2.0, 5.0)
    _route(sim, st, ["진료실 1", "원무과 접수"], [False, False])
    return st.result()


@scenario("S4-5 장소 이동(로비→원무과) 보낸 점이 모두 노드 띠 안 — 지름길 대각선 없음", env=ENV,
          expect=lambda r: arrived_at(r, "원무과 접수") and r["wall_cross"] == 0 and r["off_lane"] == [])
def s4_5(sim):
    st = Stage(sim)
    sim.robot.place(2.0, 5.0)
    st.go_place("원무과 접수")
    # 노드 → 노드 구간(보낸 점 둘 다 노드)의 중간점이 노드망 간선에서 0.4m 안이어야 한다 — 간선 위로만 다닌다
    edges = [(_base.NODES[a], _base.NODES[b]) for a, b in _base.auto_edges(_base.NODES)]
    # 출발 제자리 회전 점(10-28, fixed_yaw)은 로봇이 서 있던 자리라 간선 위가 아니다 — 구간 검사에서 뺀다
    sent = [w for w in sim.sm.waypoints_list if w.get("node") and not w.get("fixed_yaw")]
    off = []
    for a, b in zip(sent, sent[1:]):
        mid = ((a["x"] + b["x"]) / 2, (a["y"] + b["y"]) / 2)
        if min(_base._pt_seg(mid, p, q) for p, q in edges) > 0.4:
            off.append((a["name"], b["name"]))
    return st.result(off_lane=off)


# ── S5: 화면이 쓰는 진행 상태(묶음·좌표·남은 거리)·노드망 켬 여부 ──

@scenario("S5-1 이동 중 /nav/progress: 묶음 범위·점 좌표·노드 표시·남은 거리, 노드망 켬 여부(enabled)", env=ENV,
          # 10-27 부터 묶음 최대 4점, 10-29 부터 5점 — 첫 묶음 범위는 1~5
          expect=lambda r: r["bundle"] == {"from": 1, "to": 5} and r["has_xy"] and r["node_flags"] >= 3
          and r["remain_ok"] and r["enabled"] is True)
def s5_1(sim):
    st = Stage(sim)
    sim.robot.place(2.0, 5.0)
    snaps = []
    sim.at(8.0, lambda: snaps.append(sim.sm.get_nav_progress()))
    sim.at(30.0, lambda: snaps.append(sim.sm.get_nav_progress()))
    _route(sim, st, ["진료실 1", "원무과 접수"], [True, False])
    a, b = (snaps + [{}, {}])[:2]
    try:
        import app.map.nav_nodes as nn
        enabled = nn._node_routing_enabled()
    except (ImportError, AttributeError):
        enabled = None
    wps = a.get("waypoints") or []
    return st.result(bundle=a.get("bundle"), has_xy=bool(wps) and all(w.get("x") is not None for w in wps),
                     node_flags=sum(1 for w in wps if w.get("node")),
                     remain_ok=(a.get("remainM") or 0) > (b.get("remainM") or 0) > 0, enabled=enabled,
                     remain=[a.get("remainM"), b.get("remainM")])


# ── S6: 충전소가 없는 맵(2F 주차장 등) ──

@scenario("S6-1 충전소가 없는 맵 → 노드망을 켜도 이동 가능(엘리베이터 로비 연결만 봄), 장소 이동 도착", env=ENV,
          expect=lambda r: r["ready"] is True and arrived_at(r, "원무과 접수") and r["wall_cross"] == 0)
def s6_1(sim):
    no_charge = {k: v for k, v in PLACES.items() if not k.startswith("충전소")}
    st = Stage(sim, places=no_charge)
    sim.robot.place(2.0, 5.0)
    from app.database.database import SessionLocal
    db = SessionLocal()
    try:
        ready, why = st.nr.check_ready(db, MAP)
    finally:
        db.close()
    st.go_place("원무과 접수")
    return st.result(ready=ready, why=why)


@scenario("S6-2 [회귀] 충전소는 있는데 도킹 포인트(…-1)가 없는 맵 → 이동 거절", env=ENV,
          expect=lambda r: r["ready"] is False and "도킹 포인트" in r["why"])
def s6_2(sim):
    no_dock = {k: v for k, v in PLACES.items() if k not in ("충전소-1", "충전소-2")}
    st = Stage(sim, places=no_dock)
    from app.database.database import SessionLocal
    db = SessionLocal()
    try:
        ready, why = st.nr.check_ready(db, MAP)
    finally:
        db.close()
    return st.result(ready=ready, why=why)


@scenario("S6-3 막은 연결 저장 → 같은 쌍의 자동 간선을 저장하지 않는다(화면에 자동 점선이 남지 않게), 경로 그래프에서도 빠짐", env=ENV,
          expect=lambda r: r["kinds"] == ["excluded"] and r["in_graph"] is False)
def s6_3(sim):
    st = Stage(sim)
    import app.map.nav_nodes as nn
    nn._wall_clear = lambda mid, a, b: _base.clear(a, b, 0.08, _base.MAP_WALLS)
    a, b = _base.auto_edges(_base.NODES)[0]
    from app.database.database import SessionLocal
    from app.database import models as m
    db = SessionLocal()
    try:
        req = nn.SaveReq(nodes=[nn.NodeIn(key=str(k), x=x, y=y) for k, (x, y) in _base.NODES.items()],
                         edges=[nn.EdgeIn(a=str(a), b=str(b), kind="excluded")])
        id_of, _ = nn._write(db, MAP, req)
        ia, ib = id_of[str(a)], id_of[str(b)]
        kinds = sorted(e.Kind for e in db.query(m.NavEdge).filter(m.NavEdge.MapId == MAP).all()
                       if {e.NodeA, e.NodeB} == {ia, ib})
        g = st.nr.load_graph(db, MAP)
        in_graph = ib in g.adj.get(ia, [])
    finally:
        db.rollback()
        db.close()
    return st.result(kinds=kinds, in_graph=in_graph)
