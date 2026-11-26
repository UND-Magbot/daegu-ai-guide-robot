"""다지점 묶음 최대 4점(2026-10-27) — 본체가 5점 이상 묶음을 해석하지 않는다.

실기(10-27 14:18 본체 시계): 수납 → 1F_엘리베이터입구 노드 7점 묶음 4회 모두 본체가 TCP 연결만 받고 버림
(Basic_server 에 多点任务下发 없음). 같은 세션 3점은 정상, 10월 본체 로그 최대 4점(976B), 7점은 1612B.

변경: config.NAV_MULTI_MAX_POINTS 64 → 4. 넘으면 나눠 보낸다. 나뉜 끝 점은 통과 지점이라 도착 시 내비 보행을
유지하고(mark_pass_through_kept), 다음 묶음은 이어가기(continue)로 리셋 없이 출발한다(10-27 배포분).

10-29: 중계기가 다지점을 공백 없는 형식·소수 3자리로 보내 5점이 약 845B(본체가 받은 976B 안) → 관제 한도 5점.
가짜 본체 규칙(이 시나리오 안): 한도(LIMIT)를 넘는 묶음은 받았다고 답하지만(receiver ACK) 실행하지 않는다.
압축 중계기 기준 LIMIT = 5. X4 는 옛 중계기(4점 한도)인데 관제만 5점으로 먼저 배포한 경우 — 배포 순서 위험.
다지점 이어가기 = 단일 이어가기 NAV(하네스 가정, 실측 아님).
"""
from harness import scenario

ON = {"NAV_MULTIPOINT_ENABLED": "true", "NAV_KEEP_GAIT_ON_ARRIVAL": "true"}
LIMIT = 5          # 압축 중계기(10-29) 기준. 옛 중계기는 4
# 노드 6개(통과) + 목적지 — 엘리베이터행과 같은 7점
ROUTE7 = [(2, 0, True), (4, 0, True), (6, 0, True), (6, 2, True), (6, 4, True), (8, 4, True), (8, 6)]
ROUTE9 = [(2, 0, True), (4, 0, True), (6, 0, True), (6, 2, True), (6, 4, True), (8, 4, True),
          (8, 6, True), (10, 6, True), (10, 8)]
ROUTE3 = [(3, 0, True), (3, 3, True), (6, 3)]


def _size_limited_body(sim, limit=None):
    limit = LIMIT if limit is None else limit
    robot = sim.robot
    orig = sim.sender.send_nav_multi_to_robot
    ignored = []

    def send(points, map_id=None, continuing=False):
        if len(points) > limit:
            ignored.append(len(points))
            robot._log(f"NAV_MULTI({len(points)}점, 본체 무시)")
            return True
        return orig(points, map_id=map_id, continuing=continuing)
    sim.sender.send_nav_multi_to_robot = send
    return ignored


def _run(sim, route, stop_after=200):
    """끝까지 가거나(도착) 운영자 대기·정지로 멈출 때까지."""
    sm = sim.sm
    import math
    wps = []
    for i, p in enumerate(route):
        nxt = route[i + 1] if i + 1 < len(route) else None
        w = {"x": p[0], "y": p[1], "yaw": math.atan2(nxt[1] - p[1], nxt[0] - p[0]) if nxt else 0.0,
             "name": f"P{i}", "map_id": None}
        if len(p) > 2 and p[2]:
            w["pass_through"] = True
        wps.append(w)
    final = (route[-1][0], route[-1][1])
    n_arr = len(sim.robot.arrivals)
    sm.waypoints_list, sm.current_wp_index, sm.is_navigating = wps, 0, True
    sm._signal_nav_reset(full=True)

    def done():
        arrived = any((a[1], a[2]) == final for a in sim.robot.arrivals[n_arr:]) and not sm.is_navigating
        held = any(k == "nav_unreached_hold" for _, k, _m in sim.events)
        return arrived or held or sim.clock.elapsed() > stop_after

    def go():
        sm.navigation_send_next()
        sim.pl.nav_thread()
    sim.run(go, until=done)


def _res(sim, ignored, sw0=0):
    return {"multi_sent": list(sim.robot.multi_sent), "ignored": ignored,
            "switches_route": sim.robot.switches - sw0,
            "held": any(k == "nav_unreached_hold" for _, k, _m in sim.events)}


def _expect(multi, arrive_at, held=False, max_switch=None):
    def ok(r):
        good = r["multi_sent"] == multi and r["held"] == held and r["errors"] == 0
        if arrive_at is not None:
            good &= r["pos"] == list(arrive_at)
        if max_switch is not None:
            good &= r["switches_route"] <= max_switch
        return good or (f"다지점={r['multi_sent']} 무시={r['ignored']} 대기창={r['held']} "
                        f"위치={r['pos']} 전환={r['switches_route']}")
    return ok


@scenario("X1 [재현] 7점 경로(노드 6+목적지) → 5점·2점으로 나눠 끝까지(10-29 전 4+3), 나뉜 곳 보행 전환 없음", env=ON,
          expect=_expect([5, 2], (8, 6), max_switch=2))
def x1(sim):
    ig = _size_limited_body(sim)
    _run(sim, ROUTE7)
    return _res(sim, ig)


@scenario("X2 [회귀] 3점 경로 → 한 묶음 그대로", env=ON, expect=_expect([3], (6, 3), max_switch=2))
def x2(sim):
    ig = _size_limited_body(sim)
    _run(sim, ROUTE3)
    return _res(sim, ig)


@scenario("X3 9점 경로 → 5점·4점으로 끝까지(10-29 전 4·4·단일 1)", env=ON, expect=_expect([5, 4], (10, 8), max_switch=2))
def x3(sim):
    ig = _size_limited_body(sim)
    _run(sim, ROUTE9)
    return _res(sim, ig)


@scenario("X4 [비교] 3점 경로 보행 전환 수(기준값, 관찰용)", env=ON)
def x4(sim):
    ig = _size_limited_body(sim)
    _run(sim, ROUTE3)
    return _res(sim, ig)


@scenario("X4 [위험] 옛 중계기(본체 4점까지) + 관제만 5점 한도로 배포 → 5점 묶음 무시·못 감(배포 순서: 중계기 먼저)", env=ON,
          expect=lambda r: (r["held"] and r["multi_sent"] == [] and r["ignored"]) or f"{r}", limit_sec=400)
def x4(sim):
    ig = _size_limited_body(sim, limit=4)
    _run(sim, ROUTE7, stop_after=350)
    return _res(sim, ig)
