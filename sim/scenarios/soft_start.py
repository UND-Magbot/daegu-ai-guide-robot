"""출발 구간 저속(2026-10-28) — 장소에 서 있다가 새 이동을 시작할 때 급발진처럼 내달리는 출발을 줄인다.

현행: 새 이동의 첫 전송도 보통 속도(1.2 m/s)로 바로 출발한다.
변경: 새 이동의 첫 전송(current_wp_index 0)에서 첫 구간(로봇 → 첫 점)이 3m 이하이고 원래 속도가 보통이면
      첫 점만 저속(0.5 m/s)으로 보낸다. 첫 점이 노드·경유점이면 본체가 그 점에서 어차피 감속(약 2.5s,
      10-20 실측)하므로 정지 수는 늘지 않는다. 나뉜 묶음의 다음 묶음·재전송·저속/고속 지정은 그대로.

가짜 로봇은 속도 단계를 주행 속도에 반영하지 않는다(가정) — 속도는 보낸 명령으로만 비교한다.
"""
from harness import scenario

ON = {"NAV_MULTIPOINT_ENABLED": "true", "NAV_KEEP_GAIT_ON_ARRIVAL": "true"}
OFF = dict(ON, NAV_SOFT_START="false")
ROUTE3 = [(2, 0, True), (2, 3, True), (5, 3)]                 # 첫 구간 2m
ROUTE_LONG_FIRST = [(5, 0, True), (5, 3, True), (8, 3)]       # 첫 구간 5m
ROUTE7 = [(2, 0, True), (4, 0, True), (6, 0, True), (6, 2, True), (6, 4, True), (8, 4, True), (8, 6)]


def _capture(sim):
    """보낸 이동 명령의 점별 속도를 기록한다 — [("multi"|"single", [속도...]), ...]."""
    sent = []
    s = sim.sender
    multi, single = s.send_nav_multi_to_robot, s.send_nav_to_robot

    def m(points, map_id=None, continuing=False):
        sent.append(("multi", [p.get("speed") or "normal" for p in points]))
        return multi(points, map_id=map_id, continuing=continuing)

    def one(*a, **k):
        sent.append(("single", [k.get("speed") or "normal"]))
        return single(*a, **k)
    s.send_nav_multi_to_robot, s.send_nav_to_robot = m, one
    return sent


def _move(sim, route, speed=None, keep_gait=False, turn_first=False):
    """sim.move 와 같되 웨이포인트에 속도를 붙인다."""
    import math
    sm = sim.sm
    wps = []
    for i, p in enumerate(route):
        nxt = route[i + 1] if i + 1 < len(route) else None
        w = {"x": p[0], "y": p[1], "yaw": math.atan2(nxt[1] - p[1], nxt[0] - p[0]) if nxt else 0.0,
             "name": f"P{i}", "map_id": None}
        if len(p) > 2 and p[2]:
            w["pass_through"] = True
        if speed:
            w["speed"] = speed
        wps.append(w)
    if keep_gait:
        wps[-1]["keep_gait"] = True
    if turn_first:
        # 출발 제자리 회전 점(_depart_turn 과 같은 꼴) — 지금 위치, 첫 진행 방향으로 돈다
        wps.insert(0, {"x": sim.robot.x, "y": sim.robot.y, "yaw": math.pi / 2, "name": "출발 회전",
                       "map_id": None, "node": True, "multi_pass": True, "pass_through": True,
                       "fixed_yaw": True})
    final = (route[-1][0], route[-1][1])
    n_arr = len(sim.robot.arrivals)
    sm.waypoints_list, sm.current_wp_index, sm.is_navigating = wps, 0, True
    sm.charge_on_arrival = False
    sm._signal_nav_reset(full=True)

    def go():
        sm.navigation_send_next()
        sim.pl.nav_thread()
    sim.run(go, until=lambda: any((a[1], a[2]) == final for a in sim.robot.arrivals[n_arr:])
            and not sm.is_navigating)


def _res(sim, sent, t0=0.0):
    return {"sent": sent, "done_sec": round(sim.clock.elapsed() - t0, 1)}


def _expect(want_sent, arrive_at):
    def ok(r):
        good = r["sent"] == want_sent and r["pos"] == list(arrive_at) and r["errors"] == 0
        return good or f"보낸 속도={r['sent']} 위치={r['pos']} 오류={r['errors']}"
    return ok


@scenario("S1 [재현] 서 있다가 3점 이동(첫 구간 2m) → 첫 점만 저속, 나머지 보통", env=ON,
          expect=_expect([("multi", ["slow", "normal", "normal"])], (5, 3)))
def s1(sim):
    sent = _capture(sim)
    _move(sim, ROUTE3)
    return _res(sim, sent)


@scenario("S2 안내 도착(내비 보행 유지) 뒤 다음 장소로 출발 → 두 번째 이동도 첫 점 저속", env=ON,
          expect=_expect([("multi", ["slow", "normal", "normal"]), ("multi", ["slow", "normal", "normal"])],
                         (5, 6)))
def s2(sim):
    sent = _capture(sim)
    _move(sim, ROUTE3, keep_gait=True)
    _move(sim, [(5, 5, True), (2, 5, True), (5, 6)])
    return _res(sim, sent)


@scenario("S3 [회귀] 7점 경로(5점+2점, 10-29 전 4+3) → 첫 묶음 첫 점만 저속, 나뉜 다음 묶음은 보통", env=ON,
          expect=_expect([("multi", ["slow", "normal", "normal", "normal", "normal"]),
                          ("multi", ["normal", "normal"])], (8, 6)))
def s3(sim):
    sent = _capture(sim)
    _move(sim, ROUTE7)
    return _res(sim, sent)


@scenario("S4 [회귀] 첫 구간 5m(3m 초과) → 그대로 보통", env=ON,
          expect=_expect([("multi", ["normal", "normal", "normal"])], (8, 3)))
def s4(sim):
    sent = _capture(sim)
    _move(sim, ROUTE_LONG_FIRST)
    return _res(sim, sent)


@scenario("S5 [회귀] 시나리오 속도 고속 → 그대로 고속", env=ON,
          expect=_expect([("multi", ["fast", "fast", "fast"])], (5, 3)))
def s5(sim):
    sent = _capture(sim)
    _move(sim, ROUTE3, speed="fast")
    return _res(sim, sent)


@scenario("S6 [회귀] 시나리오 속도 저속 → 그대로 저속", env=ON,
          expect=_expect([("multi", ["slow", "slow", "slow"])], (5, 3)))
def s6(sim):
    sent = _capture(sim)
    _move(sim, ROUTE3, speed="slow")
    return _res(sim, sent)


@scenario("S7 단일 장소 이동 2m → 저속 / 6m → 보통", env=ON,
          expect=_expect([("single", ["slow"]), ("single", ["normal"])], (2, 6)))
def s7(sim):
    sent = _capture(sim)
    _move(sim, [(2, 0)])
    _move(sim, [(2, 6)])
    return _res(sim, sent)


@scenario("S8 [회귀] 스위치 끔(NAV_SOFT_START=false) → 현행과 같음", env=OFF,
          expect=_expect([("multi", ["normal", "normal", "normal"])], (5, 3)))
def s8(sim):
    sent = _capture(sim)
    _move(sim, ROUTE3)
    return _res(sim, sent)


@scenario("S9 [회귀] 첫 구간 중 통신 두절 8s → 재전송은 원래 속도, 끝까지 도착(관찰+판정)", env=ON,
          expect=lambda r: (r["pos"] == [5, 3] and r["errors"] == 0 and r["sent"][0][1][0] == "slow")
          or f"보낸 속도={r['sent']} 위치={r['pos']}")
def s9(sim):
    sent = _capture(sim)
    sim.at(3.0, lambda: sim.robot.drop_comm(8.0))
    _move(sim, ROUTE3)
    return _res(sim, sent)


@scenario("S10 출발 제자리 회전 점이 앞에 있으면 → 회전 점과 실제 첫 구간까지 저속, 나머지 보통", env=ON,
          expect=_expect([("multi", ["slow", "slow", "normal", "normal"])], (5, 3)))
def s10(sim):
    sent = _capture(sim)
    _move(sim, [(0, 2, True), (3, 2, True), (5, 3)], turn_first=True)
    return _res(sim, sent)
