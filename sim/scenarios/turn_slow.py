"""직선 모드 출발·꺾인 뒤 구간 저속 (2026-10-29, B안).

실주행(10-29 669, NOS planner): 직선(NavMode 0) 다지점은 꺾이는 점에서 서서 돈 뒤 0.1초 만에 0 → 1.20 m/s 로 뛴다
(보통 16회, 앞바퀴 들림·직후 장애물 정지 2회). 저속(0.5) 1회 주행에서는 들림·정지가 없었다. 자율 모드는 걸으면서
크게 돌아 노드망과 안 맞았다(11:25 1회 주행, (1.00, ±0.60, ±0.80) 명령).

변경:
- NAV_TURN_SLOW_DEG(기본 0=끔)가 0 보다 크면 직선 다지점 묶음에서
  B1 첫 걷는 구간을 거리와 상관없이 저속, B2 앞 점에서 이 각도 이상 꺾인 구간 저속
- 원래 속도가 보통·고속일 때만 저속으로 바꾼다(저속은 그대로)
- 단일 이동(노드 없이 바로)·자율 모드는 기존 출발 구간 저속(보통·3m 이하) 그대로

가짜 로봇은 속도를 받지 않으므로 송신 함수를 감싸 점마다 나간 speed 를 기록한다. 속도별 주행 시간은 흉내 내지 않는다.
"""
import math

from harness import scenario

# 10-29 부터 묶음 최대 5점(중계기 압축 형식) — L_ROUTE 6점은 5점 묶음 + 마지막 1점 단일 NAV.
# 단일 NAV 는 receiver 가 늘 자율 모드(NavMode 1)로 보내 꺾임 저속(직선 모드 대책)을 붙이지 않는다.

BASE = {"NAV_MULTIPOINT_ENABLED": "true", "NAV_KEEP_GAIT_ON_ARRIVAL": "true", "NAV_CONTINUOUS_PASS": "false",
        "NODE_ROUTING_ENABLED": "true", "NODE_NAV_MODE": "straight", "NAV_SOFT_START": "true"}
B = {**BASE, "NAV_TURN_SLOW_DEG": "30"}
# 로봇 (0,0) 동쪽 → 2m 직진 → 직진 → (4,0) 에서 90° → 직진 → (4,4) 에서 90° → (6,4) 에서 90°
L_ROUTE = [(2, 0), (4, 0), (4, 2), (4, 4), (6, 4), (6, 6)]
LONG = [(6, 0), (8, 0)]            # 첫 구간 6m(> 3m) 뒤 직진
GENTLE = [(2, 0), (4, 0.7), (6, 1.4)]   # 약 19° 꺾임(30° 미만)


def _run(sim, route, speed=None):
    orig = sim.sender.send_nav_multi_to_robot
    sent = []

    def send(points, map_id=None, continuing=False):
        sent.append([p.get("speed") or "normal" for p in points])
        return orig(points, map_id=map_id, continuing=continuing)
    sim.sender.send_nav_multi_to_robot = send
    orig1 = sim.sender.send_nav_to_robot

    def send1(*a, **k):
        sent.append(["single:" + (k.get("speed") or "normal")])
        return orig1(*a, **k)
    sim.sender.send_nav_to_robot = send1
    sim.robot.place(0, 0, 0.0)
    sm = sim.sm
    wps = []
    for i, p in enumerate(route):
        nxt = route[i + 1] if i + 1 < len(route) else None
        w = {"x": p[0], "y": p[1], "yaw": math.atan2(nxt[1] - p[1], nxt[0] - p[0]) if nxt else 0.0,
             "name": f"N{i}", "map_id": None, "routed": True}
        if nxt:
            w["node"] = True
        if speed:
            w["speed"] = speed
        wps.append(w)
    final = route[-1]
    sm.waypoints_list, sm.current_wp_index, sm.is_navigating = wps, 0, True
    sm._signal_nav_reset(full=True)

    def done():
        arrived = any((a[1], a[2]) == final for a in sim.robot.arrivals) and not sm.is_navigating
        return arrived or sim.clock.elapsed() > 200

    def go():
        sm.navigation_send_next()
        sim.pl.nav_thread()
    sim.run(go, until=done)
    return {"speeds": sent, "final": list(final)}


def _expect(speeds):
    def ok(r):
        good = r["speeds"] == speeds and r["pos"] == r["final"] and r["errors"] == 0
        return good or f"속도={r['speeds']} 위치={r['pos']} 오류={r['errors']}"
    return ok


S, N, F = "slow", "normal", "fast"


@scenario("T1 설정 없음(지금 운영 값) — 첫 구간 2m 만 저속", expect=_expect([[S, N, N, N, N], ["single:normal"]]), env=BASE)
def t1(sim):
    return _run(sim, L_ROUTE)


@scenario("T2 [변경] 꺾임 30° — (4,0)·(4,4)·(6,4) 뒤 구간 저속", expect=_expect([[S, N, S, N, S], ["single:normal"]]), env=B)
def t2(sim):
    return _run(sim, L_ROUTE)


@scenario("T3 첫 구간 6m(> 4m) — 보통으로 출발(10-29 오후 출발 저속 4m 한도, 전에는 저속)", expect=_expect([[N, N]]), env=B)
def t3(sim):
    return _run(sim, LONG)


@scenario("T4 [변경] 고속 시나리오 — 첫 구간·꺾인 뒤만 저속, 나머지 고속", expect=_expect([[S, F, S, F, S], ["single:fast"]]), env=B)
def t4(sim):
    return _run(sim, L_ROUTE, speed=F)


@scenario("T5 저속 시나리오 — 모두 저속(같음)", expect=_expect([[S, S, S, S, S], ["single:slow"]]), env=B)
def t5(sim):
    return _run(sim, L_ROUTE, speed=S)


@scenario("T6 자율 모드 — 꺾임 저속 안 붙음, 첫 구간만", expect=_expect([[S, N, N, N, N], ["single:normal"]]),
          env={**B, "NODE_NAV_MODE": "auto"})
def t6(sim):
    return _run(sim, L_ROUTE)


@scenario("T7 [경계] 19° 완만한 꺾임 — 저속 안 붙음", expect=_expect([[S, N, N]]), env=B)
def t7(sim):
    return _run(sim, GENTLE)


@scenario("T8 [경계] 꺾임 0(끔) — 지금과 같음", expect=_expect([[S, N, N, N, N], ["single:normal"]]),
          env={**BASE, "NAV_TURN_SLOW_DEG": "0"})
def t8(sim):
    return _run(sim, L_ROUTE)


@scenario("T9 단일 이동 6m(노드 없이 바로) — 보통 그대로", expect=_expect([["single:normal"]]), env=B)
def t9(sim):
    return _run(sim, [(6, 0)])


@scenario("T10 단일 이동 2m — 기존 출발 구간 저속 그대로", expect=_expect([["single:slow"]]), env=B)
def t10(sim):
    return _run(sim, [(2, 0)])
