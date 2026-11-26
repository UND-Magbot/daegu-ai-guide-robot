"""노드망 다지점 주행 모드 스위치 NODE_NAV_MODE (2026-10-29).

실주행(10-29 669, NOS planner 로그): 노드망 다지점은 직선(NavMode 0)이라 계획기가 제자리 회전 뒤 0.1초 만에
0 → 1.20 m/s 로 뛴다(보통 16회, 앞바퀴 들림·직후 장애물 정지 2회). 자율(NavMode 1)은 보통 최고 1.00, 걸으면서 돈다.

변경: config.NODE_NAV_MODE(straight|auto, 기본 straight) — 노드망이 켜졌을 때 다지점 점의 mode 를 이 값으로.
노드망이 꺼져 있으면 지금처럼 늘 auto. 잘못된 값은 straight.

가짜 로봇은 mode 를 받지 않으므로(harness robot.nav_multi) 송신 함수를 감싸 실제로 나간 mode 를 기록한다.
점 사이 길·가속은 흉내 내지 않는다 — 이 시뮬은 '관제가 무엇을 보내는가' 와 묶음·도착 회귀만 본다.
"""
import math

from harness import scenario

BASE = {"NAV_MULTIPOINT_ENABLED": "true", "NAV_KEEP_GAIT_ON_ARRIVAL": "true", "NAV_CONTINUOUS_PASS": "false"}
# 노드 5개 + 목적지 — 5점 한도로 5점 묶음 + 단일 1점(10-29 전에는 4점 한도로 4+2)
ROUTE = [(2, 0), (4, 0), (4, 2), (6, 2), (6, 4), (8, 4)]


def _capture(sim):
    orig = sim.sender.send_nav_multi_to_robot
    modes, speeds = [], []

    def send(points, map_id=None, continuing=False):
        modes.append(sorted({p.get("mode") for p in points}))
        speeds.append([p.get("speed") for p in points])
        return orig(points, map_id=map_id, continuing=continuing)
    sim.sender.send_nav_multi_to_robot = send
    return modes, speeds


def _run(sim):
    modes, speeds = _capture(sim)
    sm = sim.sm
    wps = []
    for i, p in enumerate(ROUTE):
        nxt = ROUTE[i + 1] if i + 1 < len(ROUTE) else None
        w = {"x": p[0], "y": p[1], "yaw": math.atan2(nxt[1] - p[1], nxt[0] - p[0]) if nxt else 0.0,
             "name": f"N{i}", "map_id": None, "routed": True}
        if nxt:
            w["node"] = True
        wps.append(w)
    final = ROUTE[-1]
    sm.waypoints_list, sm.current_wp_index, sm.is_navigating = wps, 0, True
    sm._signal_nav_reset(full=True)

    def done():
        arrived = any((a[1], a[2]) == final for a in sim.robot.arrivals) and not sm.is_navigating
        return arrived or sim.clock.elapsed() > 200

    def go():
        sm.navigation_send_next()
        sim.pl.nav_thread()
    sim.run(go, until=done)
    return {"modes": modes, "speeds": speeds, "multi_sent": list(sim.robot.multi_sent)}


def _expect(mode):
    def ok(r):
        # 10-29 부터 묶음 최대 5점 — 5점 묶음 + 마지막 1점 단일 NAV(단일은 receiver 가 늘 자율)
        good = (r["modes"] == [[mode]] and r["multi_sent"] == [5]
                and r["pos"] == list(ROUTE[-1]) and r["errors"] == 0)
        return good or f"mode={r['modes']} 묶음={r['multi_sent']} 위치={r['pos']} 오류={r['errors']}"
    return ok


@scenario("M1 노드망 켬·설정 없음 → 직선(지금과 같음)", expect=_expect("straight"),
          env={**BASE, "NODE_ROUTING_ENABLED": "true"})
def m1(sim):
    return _run(sim)


@scenario("M2 [변경] 노드망 켬·NODE_NAV_MODE=auto → 자율", expect=_expect("auto"),
          env={**BASE, "NODE_ROUTING_ENABLED": "true", "NODE_NAV_MODE": "auto"})
def m2(sim):
    return _run(sim)


@scenario("M3 노드망 켬·NODE_NAV_MODE=straight → 직선", expect=_expect("straight"),
          env={**BASE, "NODE_ROUTING_ENABLED": "true", "NODE_NAV_MODE": "straight"})
def m3(sim):
    return _run(sim)


@scenario("M4 노드망 끔·NODE_NAV_MODE=straight → 늘 자율(지금과 같음)", expect=_expect("auto"),
          env={**BASE, "NODE_ROUTING_ENABLED": "false", "NODE_NAV_MODE": "straight"})
def m4(sim):
    return _run(sim)


@scenario("M5 [경계] 노드망 켬·잘못된 값(fast) → 직선, 오류 없음", expect=_expect("straight"),
          env={**BASE, "NODE_ROUTING_ENABLED": "true", "NODE_NAV_MODE": "fast"})
def m5(sim):
    return _run(sim)


@scenario("M6 [경계] 노드망 켬·대문자 AUTO → 자율", expect=_expect("auto"),
          env={**BASE, "NODE_ROUTING_ENABLED": "true", "NODE_NAV_MODE": "AUTO"})
def m6(sim):
    return _run(sim)
