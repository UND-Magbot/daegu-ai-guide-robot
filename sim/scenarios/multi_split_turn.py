"""다지점 묶음 5점·꺾는 점에서 끊기(2026-10-29) — 1회 주행이 경유지 중간에서 멈췄다 가는 것을 줄인다.

  sim\\run.bat sim\\scenarios\\multi_split_turn.py --cand sim\\out\\cand_edgefirst
배경(10-29 16:54 관제 로그): 5점 경로가 4점 묶음 + 1점으로 나뉘어 4번째 점에서 섰다. 묶음 끝은 작업점이라 늘 선다.
자율 모드(운영 NODE_NAV_MODE=auto)는 NODE_AUTO_TURN_STOP_DEG(45°) 이상 꺾는 점에서만 서서 돌고 나머지는 걸으며 지난다.
변경: 묶음 최대 4 → 5점(중계기 압축 형식, 976B 안), 한도로 끊을 때는 45° 이상 꺾는 점에서 끊는다.
판정 지표 '직진 중 정지' = 묶음 끝인데 그 점에서 45° 미만으로 꺾는(서서 돌 필요가 없는) 곳의 수.
가짜 로봇은 속도·감속을 흉내 내지 않는다 — 보낸 묶음(점 수·끝 위치)만 본다.
"""
import math

from harness import scenario

ENV = {"NAV_MULTIPOINT_ENABLED": "true", "NAV_KEEP_GAIT_ON_ARRIVAL": "true", "NAV_CONTINUOUS_PASS": "false",
       "NODE_ROUTING_ENABLED": "true", "NODE_NAV_MODE": "auto", "NODE_AUTO_TURN_STOP_DEG": "45",
       "NAV_SOFT_START": "true"}
STRAIGHT = {**ENV, "NODE_NAV_MODE": "straight"}
# 1F 원무과수납 → 1F_엘리베이터입구 (10-29 노드망 계산: 노드 6개 + 목적지)
ELEV_START = (0.52, 6.27)
ELEV = [(-3.14, 5.9), (-3.14, 0.46), (-0.97, 0.46), (-0.99, -2.73), (3.47, -3.77), (3.8, -7.04), (5.42, -10.31)]
# 긴 복도 — 직진 노드 4개 뒤 90° 꺾임, 다시 직진 노드 3개 뒤 90° (점 9개)
LONG_START = (0.0, 0.0)
LONG = [(2, 0.3), (4, 0), (6, 0.3), (8, 0), (10, 0), (10.3, 2), (10, 4), (10.3, 6), (10, 8)]


def _turn(a, b, c):
    h1 = math.atan2(b[1] - a[1], b[0] - a[0])
    h2 = math.atan2(c[1] - b[1], c[0] - b[0])
    return abs(math.degrees((h2 - h1 + math.pi) % (2 * math.pi) - math.pi))


def _run(sim, start, route):
    orig = sim.sender.send_nav_multi_to_robot
    sent = []

    def send(points, map_id=None, continuing=False):
        sent.append([(round(p["x"], 2), round(p["y"], 2)) for p in points])
        return orig(points, map_id=map_id, continuing=continuing)
    sim.sender.send_nav_multi_to_robot = send
    orig1 = sim.sender.send_nav_to_robot

    def send1(idx, x, y, yaw, *a, **k):
        sent.append([(round(x, 2), round(y, 2))])
        return orig1(idx, x, y, yaw, *a, **k)
    sim.sender.send_nav_to_robot = send1
    sim.robot.place(*start, 0.0)
    sm = sim.sm
    wps = []
    for i, p in enumerate(route):
        nxt = route[i + 1] if i + 1 < len(route) else None
        w = {"x": p[0], "y": p[1], "yaw": math.atan2(nxt[1] - p[1], nxt[0] - p[0]) if nxt else 0.0,
             "name": f"N{i}", "map_id": None, "routed": True}
        if nxt:
            w["node"] = True
            w["pass_through"] = True
        wps.append(w)
    final = route[-1]
    sm.waypoints_list, sm.current_wp_index, sm.is_navigating = wps, 0, True
    sm._signal_nav_reset(full=True)

    def done():
        return (any((round(a[1], 2), round(a[2], 2)) == final for a in sim.robot.arrivals)
                and not sm.is_navigating) or sim.clock.elapsed() > 400

    def go():
        sm.navigation_send_next()
        sim.pl.nav_thread()
    sim.run(go, until=done)
    pts = [start] + route
    straight_stops = []
    for b in sent[:-1]:                     # 마지막 묶음 끝은 목적지 — 원래 선다
        end = b[-1]
        k = next((i for i, p in enumerate(route) if (round(p[0], 2), round(p[1], 2)) == end), None)
        if k is not None and k + 1 < len(route):
            ang = _turn(pts[k], pts[k + 1], pts[k + 2])
            if ang < 45:
                straight_stops.append((end, round(ang)))
    return {"bundles": [len(b) for b in sent], "straight_stops": straight_stops, "final": list(final),
            "bundle_ends": [b[-1] for b in sent]}


def _ok(r, sizes=None):
    good = r["pos"] == r["final"] and r["errors"] == 0 and not r["straight_stops"]
    if sizes is not None:
        good = good and r["bundles"] == sizes
    return good or f"묶음={r['bundles']} 직진중정지={r['straight_stops']} 위치={r['pos']} 오류={r['errors']}"


@scenario("S1 [재현] 원무과수납 → 엘리베이터입구(7점, 자율) — 직진 중 정지 0, 묶음 끝은 꺾는 점", env=ENV,
          expect=lambda r: _ok(r))
def s1(sim):
    return _run(sim, ELEV_START, ELEV)


@scenario("S2 [재현] 긴 복도 9점(직진 노드 사이 꺾임 2곳, 자율) — 묶음 끝이 직진 노드에 오지 않음", env=ENV,
          expect=lambda r: _ok(r))
def s2(sim):
    return _run(sim, LONG_START, LONG)


@scenario("S3 [회귀] 짧은 경로 3점 — 한 묶음 그대로", env=ENV, expect=lambda r: _ok(r, [3]))
def s3(sim):
    return _run(sim, (0.0, 0.0), [(2, 0), (2, 2), (4, 2)])


@scenario("S4 [회귀] 직선 모드 — 꺾는 점 끊기는 쓰지 않음(점마다 서서 돈다), 묶음 크기만", env=STRAIGHT,
          expect=lambda r: (r["pos"] == r["final"] and r["errors"] == 0) or f"{r['bundles']} {r['pos']}")
def s4(sim):
    return _run(sim, LONG_START, LONG)
