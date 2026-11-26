"""노드망 경로(2026-10-19) 사전 시뮬 — A1~A5·B1~B3.

맵은 노드 목업과 같은 병원 1층 배치(단위 m). 가짜 로봇은 목표를 향해 직선으로 걷는다 — 실제 로봇은 벤더
플래너가 길을 정하지만, 관제가 보낸 목표 순서가 벽을 넘는 직선인지가 이 시뮬의 관심사다.

지표(결과 dict 에 더한다)
- wall_cross: 로봇 궤적이 벽(문 제외)을 넘은 횟수 — 순간 이동(측위 점프 재현)은 세지 않는다
- targets:    로봇이 받은 NAV 목표 이름(관제 웨이포인트 이름)
- node_on:    변경안(노드망 모듈 있음)인지

현행 열: 노드망 모듈이 없으므로 목표만 보낸다(sim.move). 현행의 경유지 자동 우회(via_route)는 격자 맵(PGM)과
numpy 가 필요해 이 하네스에서 재현하지 않는다 — 현행 열은 '목표만 보낼 때' 로 읽는다.
"""
from __future__ import annotations

import math

from harness import scenario

# 운영 .env 와 같은 주행 설정(NAV_CONTINUOUS_PASS=false, NAV_KEEP_GAIT_ON_ARRIVAL=true) + 노드망 켬
ON = {"NODE_ROUTING_ENABLED": "true", "NAV_CONTINUOUS_PASS": "false", "NAV_KEEP_GAIT_ON_ARRIVAL": "true"}
MAP, FLOOR, RID = 1, 1, 10

# 벽(m). DOOR 는 간호실–약제실 자동문 — 맵에는 벽, 실제로는 열린다(궤적 판정에서는 뺀다)
WALLS = [(0.4, 0.4, 9.6, 0.4), (9.6, 0.4, 9.6, 6.0), (9.6, 6.0, 0.4, 6.0), (0.4, 6.0, 0.4, 0.4),
         (0.4, 2.6, 1.5, 2.6), (2.0, 2.6, 4.3, 2.6), (4.8, 2.6, 7.6, 2.6), (8.1, 2.6, 9.6, 2.6),
         (0.4, 3.6, 2.4, 3.6), (2.9, 3.6, 5.6, 3.6), (6.1, 3.6, 8.4, 3.6), (8.9, 3.6, 9.6, 3.6),
         (3.2, 0.4, 3.2, 2.6), (6.2, 0.4, 6.2, 2.6), (4.2, 3.6, 4.2, 6.0),
         (7.2, 3.6, 7.2, 4.7), (7.2, 5.3, 7.2, 6.0)]
DOOR = (7.2, 4.7, 7.2, 5.3)
NODES = {1: (0.8, 3.1), 2: (1.75, 3.1), 3: (2.65, 3.1), 4: (4.55, 3.1), 5: (5.85, 3.1), 6: (7.85, 3.1),
         7: (8.65, 3.1), 8: (9.3, 3.1), 9: (1.75, 2.05), 10: (4.55, 2.05), 11: (7.85, 2.05),
         12: (2.65, 4.15), 13: (5.85, 4.15), 14: (8.65, 4.15)}
PLACES = {   # 이름: (x, y, 카테고리)
    "충전소": (1.4, 5.45, "charge"), "충전소-1": (0.8, 5.6, "waypoint"), "충전소-2": (1.4, 5.0, "waypoint"),
    "원무과 접수": (8.8, 1.2, "work"), "진료실 1": (1.0, 1.1, "work"), "간호실": (6.5, 5.0, "work"),
    "대기 의자": (3.4, 5.2, "standby"), "약제실": (9.0, 5.2, "work"), "창고": (7.0, 5.85, "work"),
}


# ── 기하 ──
def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _hit(a, b, c, d):
    d1, d2, d3, d4 = _cross(c, d, a), _cross(c, d, b), _cross(a, b, c), _cross(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and 0 not in (d1, d2, d3, d4)


def _pt_seg(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2)) if l2 else 0.0
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _seg_dist(a, b, c, d):
    if _hit(a, b, c, d):
        return 0.0
    return min(_pt_seg(a, c, d), _pt_seg(b, c, d), _pt_seg(c, a, b), _pt_seg(d, a, b))


def clear(a, b, clr, walls):
    return all(_seg_dist(a, b, (w[0], w[1]), (w[2], w[3])) >= clr for w in walls)


MAP_WALLS = WALLS + [DOOR]          # 맵(격자)에 찍힌 벽 — 닫힌 문 포함


def auto_edges(nodes):
    ids = sorted(nodes)
    out = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            pa, pb = nodes[a], nodes[b]
            if math.dist(pa, pb) > 2.1 or not clear(pa, pb, 0.18, MAP_WALLS):
                continue
            if any(_pt_seg(nodes[c], pa, pb) < 0.12 for c in ids if c not in (a, b)):
                continue
            out.append((a, b))
    return out


# ── 무대 ──
class Stage:
    def __init__(self, sim, nodes=None, places=None, door=False, lobby=None):
        self.sim = sim
        self.targets: list[str] = []
        self.samples: list[tuple[float, float]] = []
        self.wall_cross = 0
        self.cross_at: list = []
        self.skip_next = False
        try:
            import app.navigation.node_route as nr
            self.nr = nr
        except ImportError:
            self.nr = None
        self.place_ids: dict[str, int] = {}
        self._seed(nodes if nodes is not None else NODES, places if places is not None else PLACES, door, lobby)
        self._wire()

    @property
    def on(self) -> bool:
        return self.nr is not None

    def _seed(self, nodes, places, door, lobby):
        from app.database.database import SessionLocal
        from app.database import models as m
        db = SessionLocal()
        try:
            db.add(m.RobotInfo(id=RID, RobotName="SIM", CurrentMapId=MAP, CurrentFloorId=FLOOR,
                               LimitBattery=30))
            allp = dict(places)
            if lobby:
                allp["엘리베이터 입구"] = (*lobby, "waypoint")
            for name, (x, y, cat) in allp.items():
                row = m.LocationInfo(LacationName=name, LocationX=x, LocationY=y, Yaw=0.0, MapId=MAP,
                                     FloorId=FLOOR, Category=cat)
                db.add(row)
                db.flush()
                self.place_ids[name] = row.id
            if self.on:
                ns = dict(nodes)
                if door:
                    ns.update({15: (6.95, 5.0), 16: (7.45, 5.0)})
                for nid, (x, y) in ns.items():
                    db.add(m.NavNode(id=nid, MapId=MAP, X=x, Y=y))
                for a, b in auto_edges(ns):
                    db.add(m.NavEdge(MapId=MAP, NodeA=a, NodeB=b, Kind="auto"))
                if door:
                    db.add(m.NavEdge(MapId=MAP, NodeA=15, NodeB=16, Kind="door", DoorId=1))
                    # 운영은 문 간선 저장 때 등록된 자동문을 요구한다(nav_nodes._validate_door_edge) — 같은 조건으로 둔다.
                    # 센서식·판독 없음 = 문 앞 3초 유예 뒤 통과(11-06 door_gate). 자동문 정보가 없는 경우는 door_gate.py D8.
                    db.add(m.DoorInfo(id=1, BusinessId=1, Name="간호실-약제실", FloorId=FLOOR, MapId=MAP,
                                      NearLocationId=self.place_ids.get("간호실"), FarLocationId=self.place_ids.get("약제실"),
                                      ControlType="auto_sensor", DetectType="none", OpenTimeoutSec=20, PassTimeoutSec=30))
            db.commit()
        finally:
            db.close()

    def _wire(self):
        sim, robot = self.sim, self.sim.robot
        sim.rt.get_current_map_id = lambda rid: MAP
        if self.on:
            self.nr._seg_clear = lambda mid, a, b: clear(a, b, 0.08, MAP_WALLS)
            self.nr._shortcut_clear = lambda mid, a, b, **_: clear(a, b, 0.22, MAP_WALLS)
        # 통과 지점 조기 넘김의 지름길 검사(polling → via_route.shortcut_is_clear) — 격자 맵 대신 벽 선분으로.
        # 실제는 벽 여유 0.45m(목표 0.5m 앞부터는 실제 벽만) — 여기서는 벽 선분 0.3m 여유로 근사한다.
        import app.navigation.via_route as vr
        vr.shortcut_is_clear = lambda mid, a, b, **_: clear(a, b, 0.3, MAP_WALLS)
        orig_nav = robot.nav

        def nav(x, y, yaw, continuing=False):
            self.targets.append(self._name_of(x, y))
            return orig_nav(x, y, yaw, continuing)
        robot.nav = nav
        orig_multi = robot.nav_multi

        def nav_multi(points, *a, **k):        # continuing(10-27 이어가기) 등 새 인자는 그대로 넘긴다
            # 첫 점은 nav_multi 안의 robot.nav(위 기록)로 남는다 — 나머지 점만 더한다
            ok = orig_multi(points, *a, **k)
            if ok:
                self.targets.extend(self._name_of(x, y) for x, y, _ in points[1:])
            return ok
        robot.nav_multi = nav_multi
        self.samples.append((robot.x, robot.y))

        def tick(dt):
            p = (robot.x, robot.y)
            q = self.samples[-1]
            if p != q:
                # 한 틱에 0.5m 넘게 움직이면 걸은 것이 아니라 옮긴 것(시작 위치 지정·측위 점프 재현)이다
                if self.skip_next or math.dist(p, q) > 0.5:
                    self.skip_next = False
                elif any(_hit(q, p, (w[0], w[1]), (w[2], w[3])) for w in WALLS):
                    self.wall_cross += 1
                    self.cross_at.append((round(q[0], 2), round(q[1], 2), round(p[0], 2), round(p[1], 2)))
                self.samples.append(p)
        sim.clock.add_ticker(tick)

    def _name_of(self, x, y):
        sm = self.sim.sm
        for w in sm.waypoints_list:
            if abs(w["x"] - x) < 1e-6 and abs(w["y"] - y) < 1e-6:
                return w.get("name", "?")
        return f"({x:.1f},{y:.1f})"

    def teleport(self, x, y):
        """측위 점프 재현 — 로봇 위치를 옮긴다(이 이동은 벽 넘기로 세지 않는다)."""
        self.skip_next = True
        self.sim.robot.place(x, y)

    def go_place(self, name):
        """장소 이동 — 변경안은 실제 진입점(start_place_move), 현행은 목표만 보낸다."""
        sim, sm = self.sim, self.sim.sm
        x, y, _ = {**PLACES, **{}}.get(name, (None, None, None))
        if not self.on:
            sim.move([(x, y)])
            return {"status": "ok"}
        from app.database.database import SessionLocal
        out = {}

        def go():
            db = SessionLocal()
            try:
                out.update(sm.start_place_move(self.place_ids[name], db))
            finally:
                db.close()
            if out.get("status") == "ok":
                sim.pl.nav_thread()
        n0 = len(sim.robot.arrivals)
        sim.run(go, until=lambda: out and (out.get("status") != "ok" or (
            not sm.is_navigating and len(sim.robot.arrivals) > n0)))
        return out

    def result(self, **extra):
        return {"wall_cross": self.wall_cross, "cross_at": self.cross_at, "targets": self.targets,
                "node_on": self.on, **extra}


def arrived_at(r, name):
    x, y, _ = PLACES[name]
    return abs(r["pos"][0] - x) < 0.05 and abs(r["pos"][1] - y) < 0.05


def ev(r, kind):
    return any(f" {kind}:" in e for e in r["events"])


# ── 시나리오 ──
@scenario("1. 장소 이동: 벽 너머 원무과(로비→복도→원무과)", env=ON,
          expect=lambda r: arrived_at(r, "원무과 접수") and r["wall_cross"] == 0 and r["errors"] == 0)
def s1(sim):
    st = Stage(sim)
    sim.robot.place(2.0, 5.0)
    out = st.go_place("원무과 접수")
    return st.result(move=out.get("msg"))


@scenario("2. 단순화: 복도 직선 중간 노드(N04·N05) 생략", env=ON,
          expect=lambda r: r["node_on"] and "N04" not in r["targets"] and "N05" not in r["targets"]
          and r["targets"][-1] == "원무과 접수")
def s2(sim):
    st = Stage(sim)
    sim.robot.place(2.0, 5.0)
    st.go_place("원무과 접수")
    return st.result()


@scenario("3. 노드 미연결 장소(창고) → 출발 전 거절", env=ON,
          expect=lambda r: r["node_on"] and r["arrivals"] == 0 and "노드망에 연결되지 않은" in (r.get("move") or ""))
def s3(sim):
    st = Stage(sim)
    sim.robot.place(6.5, 5.0)
    out = st.go_place("창고")
    return st.result(move=out.get("msg"))


@scenario("4. 노드망 밖 로봇(2.1m) → 가장 가까운 노드로 진입 후 이동", env=ON,
          expect=lambda r: arrived_at(r, "간호실") and r["wall_cross"] == 0 and r["targets"][:1] == ["N12"])
def s4(sim):
    st = Stage(sim)
    sim.robot.place(3.9, 5.8)          # 로비 구석 — N12 에서 2.07m(붙는 거리 2m 밖), 도킹 포인트와 멀다
    st.go_place("간호실")
    return st.result()


@scenario("5. 충전소 접근점 미연결 맵 → 이동 거절(A1·B2)", env=ON,
          expect=lambda r: r["node_on"] and r["arrivals"] == 0 and "충전소" in (r.get("move") or ""))
def s5(sim):
    nodes = {k: v for k, v in NODES.items() if k != 12}       # 로비 노드를 빼면 충전소-2 가 떨어진다
    st = Stage(sim, nodes=nodes)
    sim.robot.place(8.8, 1.2)
    out = st.go_place("진료실 1")
    return st.result(move=out.get("msg"))


@scenario("6. 엘리베이터 로비: 있으면 필수, 없으면 요구 안 함(B2)", env=ON,
          expect=lambda r: r["node_on"] and r["ready_no_lobby"] and not r["ready_lobby_far"]
          and r["ready_lobby_near"])
def s6(sim):
    from app.database.database import SessionLocal
    st = Stage(sim)
    if not st.on:
        return st.result(ready_no_lobby=None, ready_lobby_far=None, ready_lobby_near=None)
    from app.database import models as m
    db = SessionLocal()
    try:
        ok0, _ = st.nr.check_ready(db, MAP)
        lob = m.LocationInfo(LacationName="엘리베이터 입구", LocationX=9.45, LocationY=0.55, MapId=MAP,
                             FloorId=FLOOR, Category="waypoint")     # 원무과 구석 — 가장 가까운 N11 에서 2.2m
        db.add(lob)
        db.commit()
        ok1, why1 = st.nr.check_ready(db, MAP)
        lob.LocationX, lob.LocationY = 9.3, 3.4                      # 복도 끝 N08 옆
        db.commit()
        ok2, _ = st.nr.check_ready(db, MAP)
    finally:
        db.close()
    return st.result(ready_no_lobby=ok0, ready_lobby_far=ok1, ready_lobby_near=ok2, why=why1)


@scenario("7. 문 간선: 닫힌 자동문(맵상 벽)으로 간호실→약제실, 문 양쪽 노드 생략 안 함(A2)", env=ON,
          expect=lambda r: arrived_at(r, "약제실") and r["wall_cross"] == 0
          and "N15" in r["targets"] and "N16" in r["targets"])
def s7(sim):
    st = Stage(sim, door=True)
    sim.robot.place(6.5, 5.0)
    st.go_place("약제실")
    return st.result()


@scenario("8. 충전 복귀: 접근점(충전소-2)까지 노드망, 그 뒤 고정 통로로 도킹", env=ON,
          expect=lambda r: r["charging"] and r["wall_cross"] == 0 and r["targets"][-2:] == ["충전소-2", "충전소-1"])
def s8(sim):
    st = Stage(sim)
    sim.robot.place(8.8, 1.2)
    out = {}

    def go():
        out.update(sim.ch._return_to_charge_internal(cancel_running=False))
        sim.pl.nav_thread()
    sim.run(go, until=lambda: sim.robot.charging)
    return st.result(msg=out.get("msg"))


@scenario("9. 언도킹 직후 출발: 통로 역순(충전소-2) 거쳐 노드망으로(A4)", env=ON,
          expect=lambda r: arrived_at(r, "원무과 접수") and r["wall_cross"] == 0 and r["targets"][:1] == ["충전소-2"])
def s9(sim):
    st = Stage(sim)
    sim.robot.place(0.8, 5.6)
    st.go_place("원무과 접수")
    return st.result()


def _start_route(sim, st, name, run_until):
    """원무과까지 가는 중 run_until 이 참이 될 때까지만 돌린다(이후 사건을 끼우려고)."""
    sm = sim.sm
    if st.on:
        from app.database.database import SessionLocal
        out = {}

        def go():
            db = SessionLocal()
            try:
                out.update(sm.start_place_move(st.place_ids[name], db))
            finally:
                db.close()
            sim.pl.nav_thread()
        sim.run(go, until=run_until, grace=0.0)
    else:
        x, y, _ = PLACES[name]
        sm.waypoints_list = [{"x": x, "y": y, "yaw": 0.0, "name": name, "map_id": None}]
        sm.current_wp_index, sm.is_navigating = 0, True
        sm._signal_nav_reset(full=True)
        sim.run(lambda: (sm.navigation_send_next(), sim.pl.nav_thread()), until=run_until, grace=0.0)


def _battery(sim, pct, n=1, limit=30):
    """heartbeat n 번 — 배터리 pct% 로 auto_return 판정을 부른다(현행·변경 공통 입구)."""
    import app.robot_control.auto_return as ar
    import app.database.routes.voice_scenario  # noqa: F401  배터리 처리 스레드가 부른다
    sim.rewire_late_modules()     # 늦게 불러온 모듈도 가상 시계·스레드 위에서
    e = sim.rt._runtime.setdefault(RID, {})
    e.update({"battery": {"SOC": pct}, "robot_type": "", "limit_battery": limit, "critical_battery": 15,
              "basic_status": {"Sleep": 0, "MotionState": 1}, "_is_charging": False})
    for _ in range(n):
        ar.check_battery_and_return(RID)


def _charge_ev(r):
    """충전소로 떠났다는 기록 — 기준 하나(10-22) auto_return_triggered, 위급 직행(10-19) battery_critical_return."""
    return ev(r, "auto_return_triggered") or ev(r, "battery_critical_return")


@scenario("10. 배터리 기준 이하(3회 확인) → 하던 이동 즉시 중단, 노드망 따라 충전소(벽 안 넘음)", env=ON,
          expect=lambda r: r["charging"] and _charge_ev(r) and r["wall_cross"] == 0
          and any(t.startswith("N") for t in r["after_low"]))
def s10(sim):
    st = Stage(sim)
    sim.robot.place(2.0, 5.0)
    _start_route(sim, st, "원무과 접수", lambda: sim.clock.elapsed() >= 12)
    k = len(st.targets)

    def trigger():
        _battery(sim, 12, n=3)
        sim.pl.nav_thread()
    sim.run(trigger, until=lambda: sim.robot.charging)
    return st.result(after_low=st.targets[k:])


@scenario("10b. 배터리 기준 이하, 노드망 미완성 맵(노드 없음) → 직행으로라도 충전소", env=ON,
          expect=lambda r: r["charging"] and _charge_ev(r))
def s10b(sim):
    st = Stage(sim, nodes={})
    sim.robot.place(2.0, 5.0)

    def trigger():
        _battery(sim, 12, n=3)
        sim.pl.nav_thread()
    sim.run(trigger, until=lambda: sim.robot.charging or sim.clock.elapsed() > 120)
    return st.result()


@scenario("11. 배터리 기준 이하 2회 뒤 회복(오독) → 판정 안 함", env=ON,
          expect=lambda r: r["node_on"] and not r["triggered"] and not _charge_ev(r))
def s11(sim):
    st = Stage(sim)
    _battery(sim, 12, n=2)
    _battery(sim, 80)
    return st.result(triggered=sim.sm.is_navigating)


@scenario("12. 배터리 기준 이하: 층 전환 중이면 미루고 끝나면 충전소", env=ON,
          expect=lambda r: r["node_on"] and r["deferred"]
          and (ev(r, "battery_low_deferred") or ev(r, "battery_critical_deferred")) and _charge_ev(r))
def s12(sim):
    st = Stage(sim)
    sim.robot.place(8.8, 1.2)
    sim.ft.is_transitioning = lambda rid: True
    _battery(sim, 12, n=3)
    deferred = not sim.sm.is_navigating
    sim.ft.is_transitioning = lambda rid: False

    def go():
        _battery(sim, 12)
        sim.pl.nav_thread()
    sim.run(go, until=lambda: sim.robot.charging)
    return st.result(deferred=deferred)


@scenario("13. 측위 복구 후 작은 차이(0.3m) → 가던 목표 유지(재전송 원칙)", env=ON,
          expect=lambda r: arrived_at(r, "원무과 접수") and r["wall_cross"] == 0 and not ev(r, "node_route_replan"))
def s13(sim):
    st = Stage(sim)
    sim.robot.place(2.0, 5.0)
    _start_route(sim, st, "원무과 접수", lambda: sim.clock.elapsed() >= 20)
    before = {"x": sim.robot.x, "y": sim.robot.y}
    st.teleport(sim.robot.x + 0.3, sim.robot.y)
    after = {"x": sim.robot.x, "y": sim.robot.y}

    def go():
        if hasattr(sim.sm, "replan_after_relocalization"):
            sim.sm.replan_after_relocalization(RID, before, after)
        sim.pl.nav_thread()
    sim.run(go, until=lambda: not sim.sm.is_navigating)
    return st.result()


@scenario("14. 측위 복구 후 큰 점프(복도→검사실 안) → 현재 위치에서 다시 계산(A3)", env=ON,
          expect=lambda r: arrived_at(r, "원무과 접수") and r["wall_cross"] == 0 and ev(r, "node_route_replan"))
def s14(sim):
    st = Stage(sim)
    sim.robot.place(2.0, 5.0)
    _start_route(sim, st, "원무과 접수", lambda: sim.clock.elapsed() >= 20)
    before = {"x": sim.robot.x, "y": sim.robot.y}
    st.teleport(4.0, 1.5)                    # 검사실 안으로 튐
    after = {"x": 4.0, "y": 1.5}

    def go():
        if hasattr(sim.sm, "replan_after_relocalization"):
            sim.sm.replan_after_relocalization(RID, before, after)
        sim.pl.nav_thread()
    sim.run(go, until=lambda: not sim.sm.is_navigating)
    return st.result(before=before)


@scenario("15. 장애물 정지 15s 후 재개 → 같은 목표로 계속(재계산 없음)", env=ON,
          expect=lambda r: arrived_at(r, "원무과 접수") and r["wall_cross"] == 0 and not ev(r, "node_route_replan"))
def s15(sim):
    st = Stage(sim)
    sim.robot.place(2.0, 5.0)
    sim.at(14.0, lambda: sim.robot.hold(15.0))
    st.go_place("원무과 접수")
    return st.result()


@scenario("16. 반복 2회(원무과↔대기 의자): 회차마다 다시 펼치고 노드가 쌓이지 않음", env=ON,
          expect=lambda r: r["wall_cross"] == 0 and r["round_targets_equal"] and r["list_len_ok"])
def s16(sim):
    st = Stage(sim)
    sm = sim.sm
    sim.robot.place(3.4, 5.2)
    pts = [PLACES["원무과 접수"], PLACES["대기 의자"]]
    sm.waypoints_list = [{"x": x, "y": y, "yaw": 0.0, "name": n, "map_id": None}
                         for n, (x, y, _) in zip(["원무과 접수", "대기 의자"], pts)]
    sm.current_wp_index, sm.is_navigating = 0, True
    sm.nav_loop_remaining, sm.nav_loop_total = 1, 2
    sm._signal_nav_reset(full=True)
    sim.run(lambda: (sm.navigation_send_next(), sim.pl.nav_thread()), until=lambda: not sm.is_navigating)
    t = st.targets
    half = len(t) // 2
    return st.result(round_targets_equal=(t[:half] == t[half:]) if st.on else None,
                     list_len_ok=len(sm.waypoints_list) <= 2 + half)


def _relocalize(sim, st, dx, info):
    sim.robot.place(2.0, 5.0)
    _start_route(sim, st, "원무과 접수", lambda: sim.clock.elapsed() >= 20)
    before = {"x": sim.robot.x, "y": sim.robot.y}
    st.teleport(sim.robot.x + dx, sim.robot.y)
    after = {"x": sim.robot.x, "y": sim.robot.y}

    def go():
        if hasattr(sim.sm, "replan_after_relocalization"):
            sim.sm.replan_after_relocalization(RID, before, after, info)
        sim.pl.nav_thread()
    sim.run(go, until=lambda: not sim.sm.is_navigating)


@scenario("17. 측위 복구: 차이 0.3m 지만 상실 40초 → 다시 계산(B3 보조 규칙)", env=ON,
          expect=lambda r: arrived_at(r, "원무과 접수") and r["wall_cross"] == 0 and ev(r, "node_route_replan"))
def s17(sim):
    st = Stage(sim)
    _relocalize(sim, st, 0.3, {"lost_sec": 40.0, "map_changed": False, "reset": False})
    return st.result()


@scenario("18. 측위 복구: 차이 0.3m 지만 운영자 위치 재설정으로 풀림 → 다시 계산(B3 보조 규칙)", env=ON,
          expect=lambda r: arrived_at(r, "원무과 접수") and r["wall_cross"] == 0 and ev(r, "node_route_replan"))
def s18(sim):
    st = Stage(sim)
    _relocalize(sim, st, 0.3, {"lost_sec": 5.0, "map_changed": False, "reset": True})
    return st.result()


@scenario("19. 노드 저장 API: 자동 간선 재계산·충전소 끊김 거절·벽 관통/문 간선 검증", env=ON,
          expect=lambda r: r["node_on"] and r["ready0"] and r["cut_charger"] == 409 and r["wall_manual"] == 400
          and r["door_unregistered"] == 400 and r["warehouse_linked"] and r["auto_edges"] > 0)
def s19(sim):
    st = Stage(sim)
    if not st.on:
        return st.result(ready0=None, cut_charger=None, wall_manual=None, door_unregistered=None,
                         warehouse_linked=None, auto_edges=None)
    from fastapi import HTTPException
    import app.map.nav_nodes as nn
    from app.database.database import SessionLocal
    from app.database.models import UserInfo
    nn._wall_clear = lambda mid, a, b: clear(a, b, 0.15, MAP_WALLS)

    class Req:
        headers: dict = {}
        client = type("C", (), {"host": "127.0.0.1"})()
    user = UserInfo(id=1, Permission=1, BusinessId=None)
    db = SessionLocal()
    try:
        from app.database.models import RobotMapInfo
        db.add(RobotMapInfo(id=MAP, MapName="SIM", BusinessId=1, FloorId=FLOOR))
        db.commit()
        cur = nn.get_nav_nodes(MAP, db, user)
        ready0 = cur["check"]["ready"]
        base = [nn.NodeIn(key=str(n["id"]), x=n["x"], y=n["y"]) for n in cur["nodes"]]

        def save(nodes, edges=()):
            try:
                return nn.save_nav_nodes(MAP, nn.SaveReq(nodes=nodes, edges=list(edges)), Req(), db, user), 200
            except HTTPException as e:
                db.rollback()
                return None, e.status_code
        _, cut = save([n for n in base if n.key != "12"])
        _, wall = save(base, [nn.EdgeIn(a="9", b="10", kind="manual")])
        _, door = save(base, [nn.EdgeIn(a="13", b="14", kind="door", door_id=99)])
        out, ok = save(base + [nn.NodeIn(key="new1", x=6.6, y=5.4)])
        linked = ok == 200 and st.place_ids["창고"] not in out["check"]["unlinkedPlaceIds"]
        autos = sum(1 for e in (out or {}).get("edges", []) if e["kind"] == "auto")
    finally:
        db.close()
    return st.result(ready0=ready0, cut_charger=cut, wall_manual=wall, door_unregistered=door,
                     warehouse_linked=linked, auto_edges=autos)


# ── 간선 방향(2026-10-20) ──
def _api(st):
    """노드 저장 API 를 시뮬 DB 로 부르는 도구 — s19 와 같은 준비."""
    import app.map.nav_nodes as nn
    from fastapi import HTTPException
    from app.database.database import SessionLocal
    from app.database.models import RobotMapInfo, UserInfo
    nn._wall_clear = lambda mid, a, b: clear(a, b, 0.15, MAP_WALLS)

    class Req:
        headers: dict = {}
        client = type("C", (), {"host": "127.0.0.1"})()
    user = UserInfo(id=1, Permission=1, BusinessId=None)
    db = SessionLocal()
    db.add(RobotMapInfo(id=MAP, MapName="SIM", BusinessId=1, FloorId=FLOOR))
    db.commit()
    base = [nn.NodeIn(key=str(n["id"]), x=n["x"], y=n["y"]) for n in nn.get_nav_nodes(MAP, db, user)["nodes"]]

    def save(edges, nodes=None):
        try:
            return nn.save_nav_nodes(MAP, nn.SaveReq(nodes=nodes or base, edges=list(edges)), Req(), db, user), 200, ""
        except HTTPException as e:
            db.rollback()
            return None, e.status_code, str(e.detail)

    def validate(edges):
        return nn.validate_nav_nodes(MAP, nn.SaveReq(nodes=base, edges=list(edges)), db, user)
    return nn, db, user, save, validate


@scenario("20. 한쪽 방향 막다른 길(복도 N05→N06만) → 저장 거절, 검사는 점검 목록으로 보임", env=ON,
          expect=lambda r: r["node_on"] and r["save"] == 409 and "돌아올 수 없" in r["msg"]
          and r["validate_ok"] and not r["persisted"])
def s20(sim):
    st = Stage(sim)
    if not st.on:
        return st.result(save=None, msg="", validate_ok=None, persisted=None)
    nn, db, user, save, validate = _api(st)
    try:
        one = [nn.EdgeIn(a="5", b="6", kind="manual", direction="forward")]
        _, code, msg = save(one)
        v = validate(one)
        traps = [i for i in v["check"]["issues"] if i["kind"] == "oneway_trap"]
        cur = nn.get_nav_nodes(MAP, db, user)
        persisted = any(e["direction"] == "forward" for e in cur["edges"])
    finally:
        db.close()
    return st.result(save=code, msg=msg, validate_ok=bool(traps),
                     traps=[i["text"] for i in traps], persisted=persisted)


@scenario("21. 충전소 노드(N12)에 붙은 연결을 한쪽으로 → 저장 거절(출도킹·복귀 통로)", env=ON,
          expect=lambda r: r["node_on"] and r["save"] == 409 and "충전소 노드" in r["msg"])
def s21(sim):
    st = Stage(sim)
    if not st.on:
        return st.result(save=None, msg="")
    nn, db, user, save, validate = _api(st)
    try:
        _, code, msg = save([nn.EdgeIn(a="12", b="3", kind="manual", direction="forward")])
    finally:
        db.close()
    return st.result(save=code, msg=msg)


@scenario("22. 순환 통로 한쪽 방향(가→나→다→가) → 저장 성공, 경로는 정해진 방향으로만", env=ON,
          expect=lambda r: r["node_on"] and r["save"] == 200 and r["ready"]
          and r["path_fwd"] == [21, 22, 23] and "N22" in r["sent_fwd"] and r["path_back"] == [23, 21, 22])
def s22(sim):
    # 원무과 쪽 복도 끝에 작은 순환 통로(N21·N22·N23)를 붙인다 — 충전소와 먼 곳
    nodes = {**NODES, 21: (8.65, 1.2), 22: (9.3, 2.0), 23: (8.0, 1.2)}
    st = Stage(sim, nodes=nodes)
    if not st.on:
        return st.result(save=None, ready=None, path_fwd=None, path_back=None)
    nn, db, user, save, validate = _api(st)
    try:
        loop = [nn.EdgeIn(a="21", b="22", kind="manual", direction="forward"),
                nn.EdgeIn(a="22", b="23", kind="manual", direction="forward"),
                nn.EdgeIn(a="23", b="21", kind="manual", direction="forward"),
                nn.EdgeIn(a="11", b="21", kind="manual", direction="both"),
                # 순환 통로 밖 지름길(N11–N22·N11–N23 자동 간선)은 막아 둔다 — 방향만 보려고
                nn.EdgeIn(a="11", b="22", kind="excluded"), nn.EdgeIn(a="11", b="23", kind="excluded")]
        out, code, msg = save(loop)
        ready = bool(out and out["check"]["ready"])
        fwd = st.nr.plan_leg(db, MAP, (8.65, 1.2), {"x": 8.0, "y": 1.2, "name": "가"})
        back = st.nr.plan_leg(db, MAP, (8.0, 1.2), {"x": 9.3, "y": 2.0, "name": "나"})
    finally:
        db.close()
    return st.result(save=code, msg=msg, ready=ready, path_fwd=fwd.node_path, path_back=back.node_path,
                     sent_fwd=[w["name"] for w in fwd.waypoints],
                     err=fwd.error or back.error)


@scenario("23. 사용자 한쪽 간선이 같은 자리 자동 간선을 대신(방향이 무시되지 않음)", env=ON,
          expect=lambda r: r["node_on"] and r["auto_same_pair"] == 0 and r["forward_kept"])
def s23(sim):
    nodes = {**NODES, 21: (8.65, 1.2), 22: (9.3, 2.0), 23: (8.0, 1.2)}
    st = Stage(sim, nodes=nodes)
    if not st.on:
        return st.result(auto_same_pair=None, forward_kept=None)
    nn, db, user, save, validate = _api(st)
    try:
        loop = [nn.EdgeIn(a="21", b="22", kind="manual", direction="forward"),
                nn.EdgeIn(a="22", b="23", kind="manual", direction="forward"),
                nn.EdgeIn(a="23", b="21", kind="manual", direction="forward"),
                nn.EdgeIn(a="11", b="21", kind="manual", direction="both")]
        out, code, _ = save(loop)
        es = (out or {}).get("edges", [])
        pair = {21, 22}
        auto_same = sum(1 for e in es if e["kind"] == "auto" and {e["a"], e["b"]} == pair)
        kept = any(e["kind"] == "manual" and e["a"] == 21 and e["b"] == 22 and e["direction"] == "forward" for e in es)
        g = st.nr.load_graph(db, MAP)
        no_back = 21 not in g.neighbors(22)
    finally:
        db.close()
    return st.result(auto_same_pair=auto_same, forward_kept=kept and no_back)
