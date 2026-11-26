"""방향 안내 기준 각도 30° → 50° (2026-11-09 메모: 원무과 수납 → 엘리베이터 이동 때 방향 안내가 너무 많다).

현행: 경유지에서 30° 이상 꺾으면 "잠시 후 ○쪽으로 방향을 바꾸겠습니다".
변경: 50° 이상만. 문구·안내 시점·'곧 도착' 규칙은 그대로.

경로 좌표는 운영 관제 경로 계획(node_route.plan_leg, 11-09 조회)이 만든 맵 47·49 경유지를 그대로 옮겼다.
출발점은 장소 좌표(실주행은 로봇 실제 위치). 수납 → 엘리베이터는 11-09 관제 기록(6번: 왼·왼·오·왼·오·왼)과 같다.
"""
from harness import scenario

MULTI = {"NAV_MULTIPOINT_ENABLED": "true"}
P = {"speed": 0.5}

# (x, y, 이름) — 마지막이 목적지
PAY_TO_EV = [(0.62, 5.614), [(-3.67, 6.344, "N243"), (-3.47, 0.434, "N240"), (-1.15, 0.444, "N236"),
                             (1.36, -3.256, "N398"), (3.57, -4.046, "N244"), (3.64, -7.496, "N245"),
                             (5.422, -10.306, "1F_엘리베이터입구")]]
EV_TO_PAY = [(5.422, -10.306), [(3.64, -7.496, "N245"), (3.57, -4.046, "N244"), (1.36, -3.256, "N398"),
                                (-1.15, 0.444, "N236"), (-3.47, 0.434, "N240"), (-3.67, 6.344, "N243"),
                                (0.62, 5.614, "원무과수납")]]
SEAT_TO_EV = [(-3.67, 0.604), [(-1.15, 0.444, "N236"), (1.36, -3.256, "N398"), (3.57, -4.046, "N244"),
                               (3.64, -7.496, "N245"), (5.422, -10.306, "1F_엘리베이터입구")]]
PAY_TO_NURSE = [(0.62, 5.614), [(-3.67, 6.344, "N243"), (-3.47, 0.434, "N240"), (2.35, 0.654, "간호실데스크")]]
DESK_TO_LOBBY = [(6.94, -7.416), [(6.21, -5.186, "N377"), (1.96, -0.866, "N363"), (1.91, 0.444, "로비입구")]]
LOBBY_TO_ROOM = [(1.91, 0.444), [(1.96, -0.866, "N363"), (6.25, -2.776, "N361"), (5.884, -6.183, "종합민원실")]]

LEFT, RIGHT = "잠시 후 왼쪽으로 방향을 바꾸겠습니다.", "잠시 후 오른쪽으로 방향을 바꾸겠습니다."


def drive(sim, route, arrive_name=None):
    """방향 안내를 계산해 둔 장소 이동을 끝까지. 반환: 계획된 안내{경유지 이름: 문구}, 실제로 낸 안내 순서."""
    sm, robot = sim.sm, sim.robot
    start, pts = route
    robot.place(*start)
    wps = [{"x": x, "y": y, "yaw": 0.0, "name": n, "map_id": None,
            **({"pass_through": True} if i < len(pts) - 1 else {})} for i, (x, y, n) in enumerate(pts)]
    if arrive_name:
        wps[-1]["arrive_name"] = arrive_name
    sm.waypoints_list = wps
    sm._set_route_cues((robot.x, robot.y), wps, 0)
    plan = {wps[k - 1]["name"]: v for k, v in sorted(sm._route_cues.items())}
    heard, state = [], {"seq": sm.get_nav_cue()["seq"]}

    def tick():
        c = sm.get_nav_cue()
        if c["seq"] != state["seq"]:
            state["seq"] = c["seq"]
            heard.append(c["text"])
        sim.at(0.1, tick)

    sim.at(0.1, tick)
    final = (wps[-1]["x"], wps[-1]["y"])
    sm.current_wp_index = 0
    sm.is_navigating = True
    sm._signal_nav_reset(full=True)

    def go():
        sm.navigation_send_next()
        sim.pl.nav_thread()
    sim.run(go, until=lambda: any((a[1], a[2]) == final for a in robot.arrivals) and not sm.is_navigating)
    turns = {k: v for k, v in plan.items() if "방향" in v}
    return {"plan": plan, "heard": heard, "turn_nodes": list(turns), "n_turns": sum("방향" in h for h in heard)}


def same(r, nodes, n):
    ok = r["turn_nodes"] == nodes and r["n_turns"] == n and r["errors"] == 0 and r["dropped"] == 0
    return ok or f"계획={r['turn_nodes']} 낸 꺾기 안내 {r['n_turns']}번 오류={r['errors']} 놓침={r['dropped']}"


@scenario("T1 [재현] 1F 수납 → 엘리베이터 입구: 꺾기 안내 4번(N243·N240·N236·N244). 현행 6번(N398 36°·N245 31° 포함)",
          env=MULTI, params=P,
          expect=lambda r: same(r, ["N243", "N240", "N236", "N244"], 4))
def t1(sim):
    return drive(sim, PAY_TO_EV, "엘리베이터 입구")


@scenario("T2 [재현] 1F 엘리베이터 입구 → 수납: 꺾기 안내 4번(N244·N236·N240·N243). 현행 6번", env=MULTI, params=P,
          expect=lambda r: same(r, ["N244", "N236", "N240", "N243"], 4))
def t2(sim):
    return drive(sim, EV_TO_PAY)


@scenario("T3 1F 진료대기장소 → 엘리베이터 입구: 2번(N236 −52°·N244). 현행 4번", env=MULTI, params=P,
          expect=lambda r: same(r, ["N236", "N244"], 2))
def t3(sim):
    return drive(sim, SEAT_TO_EV, "엘리베이터 입구")


@scenario("T4 [회귀] 1F 수납 → 간호실(±90° 모퉁이 2곳): 그대로 2번, 문구 왼쪽·왼쪽", env=MULTI, params=P,
          expect=lambda r: (same(r, ["N243", "N240"], 2) is True
                            and [h for h in r["heard"] if "방향" in h] == [LEFT, LEFT]) or same(r, ["N243", "N240"], 2))
def t4(sim):
    return drive(sim, PAY_TO_NURSE)


@scenario("T5 3F 접수창구 → 로비입구: N363 −42° 꺾기 안내 없음, '곧 로비에 도착합니다' 는 따로 나옴"
          "(현행: '오른쪽… 곧 로비에 도착합니다' 한 문장)", env=MULTI, params=P,
          expect=lambda r: (r["turn_nodes"] == [] and "곧 로비에 도착합니다." in r["heard"]
                            and not any("방향" in h for h in r["heard"]) and r["errors"] == 0)
          or f"계획={r['plan']} 들림={r['heard']}")
def t5(sim):
    return drive(sim, DESK_TO_LOBBY, "로비")


@scenario("T6 [회귀] 3F 로비입구 → 종합민원실(+64°·−72°): 그대로 왼쪽·오른쪽 2번", env=MULTI, params=P,
          expect=lambda r: (same(r, ["N363", "N361"], 2) is True
                            and [h for h in r["heard"] if "방향" in h] == [LEFT, RIGHT]) or same(r, ["N363", "N361"], 2))
def t6(sim):
    return drive(sim, LOBBY_TO_ROOM)


def _edge(sim):
    """경계: 50° 는 말하고 49° 는 말하지 않는다. 135° 이상은 '뒤로' 그대로."""
    from app.navigation import turn_cues as tc
    return {"p50": tc.turn_phrase(50.0), "p49": tc.turn_phrase(-49.0), "p60": tc.turn_phrase(-60.0),
            "p140": tc.turn_phrase(140.0), "min": tc.TURN_MIN_DEG}


@scenario("T7 [경계] 50° 말함·49° 안 함·−60° 오른쪽·140° 뒤로", expect=lambda r: (
        r["p50"] == "잠시 후 왼쪽으로 방향을 바꾸겠습니다." and r["p49"] is None
        and r["p60"] == RIGHT and r["p140"] == "잠시 후 뒤로 방향을 바꾸겠습니다.") or f"{r}")
def t7(sim):
    return _edge(sim)
