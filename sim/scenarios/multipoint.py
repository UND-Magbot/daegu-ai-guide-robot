"""다지점 NAV(S1~S2) — 통과 지점이 이어지는 구간을 멈출 곳까지 묶어 1건으로 보낸다.

현행: 통과 지점마다 단일 NAV(1.0m 반경에서 넘김 → receiver 리셋 공백).
변경: NAV_MULTIPOINT_ENABLED 면 묶음 1건, 점 1개·도킹 통로·옛 receiver 는 단일 그대로.
가짜 로봇 다지점 규칙은 harness/robot.py 머리말(10-20 1F 실기 7회).
"""
from harness import scenario

ON = {"NAV_MULTIPOINT_ENABLED": "true"}
ROUTE = [(3, 0, True), (3, 3, True), (6, 3)]          # 통과점 2 + 목적지


def nav_cmds(r):
    return [c for c in r["cmds"] if c.startswith("NAV")]


def move_flags(sim, wps_extra: list[dict], **kw):
    """sim.move 와 같되 웨이포인트에 플래그(charge_chain 등)를 붙인다."""
    sm, robot = sim.sm, sim.robot
    wps = [dict({"yaw": 0.0, "name": f"P{i}", "map_id": None}, **w) for i, w in enumerate(wps_extra)]
    final = (wps[-1]["x"], wps[-1]["y"])
    n_arr = len(robot.arrivals)
    sm.waypoints_list = wps
    sm.current_wp_index = 0
    sm.is_navigating = True
    sm.charge_on_arrival = False
    sm._signal_nav_reset(full=True)

    def go():
        sm.navigation_send_next()
        sim.pl.nav_thread()

    sim.run(go, until=lambda: any((a[1], a[2]) == final for a in robot.arrivals[n_arr:]) and not sm.is_navigating)


@scenario("M1 통과점 2 + 목적지 → 다지점 1건(3점), 끝까지 도착",
          env=ON,
          expect=lambda r: (r["multi_sent"] == [3] and "NAV" not in nav_cmds(r) and r["pos"] == [6, 3]
                            and r["errors"] == 0) or f"명령={nav_cmds(r)} 위치={r['pos']}")
def m1(sim):
    sim.move(ROUTE)
    return {"done_sec": round(sim.clock.elapsed(), 1)}


@scenario("M2 [회귀] 점 1개(단일 장소 이동) → 단일 NAV 그대로",
          env=ON,
          expect=lambda r: nav_cmds(r) == ["NAV"] and r["pos"] == [6, 3] and r["errors"] == 0)
def m2(sim):
    sim.move([(6, 3)])


@scenario("M3 작업지에서 묶음을 자름 — 통과·작업지·통과·작업지 → 다지점 2건(2점·2점)",
          env=ON,
          expect=lambda r: (r["multi_sent"] == [2, 2] and r["pos"] == [6, 6] and r["errors"] == 0)
          or f"다지점={r['multi_sent']} 명령={nav_cmds(r)} 위치={r['pos']}")
def m3(sim):
    sim.move([(3, 0, True), (3, 3), (6, 3, True), (6, 6)])


@scenario("M4 통과점 앞 통신 두절 8s(6~14s) — 두절 중에도 다음 구간을 가고, 복구 뒤 처음부터 재전송 안 함",
          env=ON,
          expect=lambda r: (r.get("y_at_restore", 0) > 0.5 and r["pos"] == [6, 3] and r["multi_sent"] == [3]
                            and r.get("back_to_wp1", 0) == 0)
          or f"복구 시점=({r.get('x_at_restore')}, {r.get('y_at_restore')}) 다지점={r['multi_sent']} "
             f"지난 통과점 되돌아감={r.get('back_to_wp1')}")
def m4(sim):
    seen = {}
    sim.at(6.0, lambda: sim.robot.drop_comm(8.0))
    sim.at(14.0, lambda: seen.update(x=round(sim.robot.x, 2), y=round(sim.robot.y, 2)))
    sim.move(ROUTE)
    back = sum(1 for t, x, y in sim.robot.arrivals if (x, y) == (3, 0)) - 1
    return {"x_at_restore": seen.get("x"), "y_at_restore": seen.get("y"), "back_to_wp1": back,
            "done_sec": round(sim.clock.elapsed(), 1)}


@scenario("M5 [회귀] 묶음 주행 중 사용자 정지(10s) → 취소, 더 안 감",
          env=ON,
          expect=lambda r: not r["executing"] and r["pos"] != [6, 3] and "CANCEL_NAV" in r["cmds"]
          and r["errors"] == 0)
def m5(sim):
    sim.move(ROUTE, stop_at=10.0)
    sim.wait(15.0)


@scenario("M6 묶음 중 재전송(2번째 점으로 가는 중) → 남은 점만 다시(3점 → 2점)",
          env=ON,
          expect=lambda r: (r["multi_sent"] == [3, 2] and r["pos"] == [6, 3] and r["errors"] == 0)
          or f"다지점={r['multi_sent']} 명령={nav_cmds(r)}")
def m6(sim):
    def resend():
        if sim.robot.value == 2:
            sim.sm.navigation_resend_current()
    sim.at(14.0, resend)
    sim.move(ROUTE)


@scenario("M7 옛 receiver(다지점 모름) → 거부 뒤 단일 NAV 로 끝까지",
          env=ON, params={"multi_supported": False},
          # 10-27 부터 다지점을 끄지 않는다 — 묶음마다 다시 시도하고 거부되면 그 구간만 단일(점 수 = 묶음 시도 수 이하)
          expect=lambda r: (r["pos"] == [6, 3] and r["errors"] == 0 and nav_cmds(r).count("NAV") >= 3
                            and sum(c.startswith("NAV_MULTI") for c in nav_cmds(r)) <= len(ROUTE))
          or f"명령={nav_cmds(r)}")
def m7(sim):
    sim.move(ROUTE)


@scenario("M8 [회귀] 도킹 통로(charge_chain)는 통과점이어도 단일 NAV",
          env=ON,
          expect=lambda r: r["multi_sent"] == [] and r["pos"] == [2, 0] and r["errors"] == 0
          or f"다지점={r['multi_sent']}")
def m8(sim):
    move_flags(sim, [{"x": 4, "y": 0, "pass_through": True, "charge_chain": True},
                     {"x": 2, "y": 0, "charge_chain": True}])


@scenario("M9 진행 표시 — 2번째 점으로 가는 중 화면의 현재 경유지가 2",
          env=ON,
          expect=lambda r: r.get("progress_at_14s") == 2 or f"현재 표시={r.get('progress_at_14s')}")
def m9(sim):
    got = {}
    sim.at(14.0, lambda: got.update(i=sim.sm.get_nav_progress()["currentIndex"]))
    sim.move(ROUTE)
    return {"progress_at_14s": got.get("i")}


@scenario("M10 긴 묶음(통과점 9 + 목적지, 87s) — hang(60s) 오판 재전송 없음",
          env=ON, limit_sec=400,
          # 묶음 최대 점 수(10-27 부터 4)로 나뉘어도 된다 — 합 10점·hang 0·끝까지가 판정 기준
          expect=lambda r: (sum(r["multi_sent"]) == 10 and r.get("hang", 0) == 0 and r["pos"] == [30, 0])
          or f"다지점={r['multi_sent']} hang={r.get('hang')}")
def m10(sim):
    sim.move([(3 * i, 0, True) for i in range(1, 10)] + [(30, 0)])
    return {"hang": sim.log().count("hang 감지"), "done_sec": round(sim.clock.elapsed(), 1)}


@scenario("M11 [회귀] 다지점 꺼짐(env 없음) → 현행과 같은 명령",
          expect=lambda r: r["multi_sent"] == [] and r["pos"] == [6, 3] and r["errors"] == 0)
def m11(sim):
    sim.move(ROUTE)


def _cue_route(sim, pts):
    """방향 안내를 계산해 둔 장소 이동(start_place_move 와 같은 준비)."""
    sm = sim.sm
    wps = [{"x": x, "y": y, "yaw": 0.0, "name": f"P{i}", "map_id": None, **({"pass_through": True} if p else {})}
           for i, (x, y, p) in enumerate(pts)]
    sm.waypoints_list = wps
    sm._set_route_cues((sim.robot.x, sim.robot.y), wps, 0)
    heard = []

    def tick():
        c = sm.get_nav_cue()
        if c.get("seq") and (not heard or heard[-1][1] != c["seq"]):
            # 안내가 나온 순간 다음 점까지 남은 거리
            heard.append((round(sim.clock.elapsed(), 1), c["seq"], c["text"],
                          round(sim.robot.x, 1), round(sim.robot.y, 1)))
        sim.at(0.5, tick)
    sim.at(0.5, tick)
    final = (wps[-1]["x"], wps[-1]["y"])
    sm.current_wp_index = 0
    sm.is_navigating = True
    sm._signal_nav_reset(full=True)

    def go():
        sm.navigation_send_next()
        sim.pl.nav_thread()
    sim.run(go, until=lambda: any((a[1], a[2]) == final for a in sim.robot.arrivals) and not sm.is_navigating)
    return heard


# 방향 안내는 max(잰 속도, 보낸 속도 하한) × 5.5초 앞. 하한 보통 0.5 m/s(10-30, 앞서 1.0) — 가짜 로봇 0.5 m/s 면 약 2.75m,
# 관제 1초 간격 확인으로 0.5m 늦을 수 있다.
@scenario("M12 방향 안내 — 다지점이어도 꺾기 앞에서 왼쪽·오른쪽·곧 도착이 차례로 나옴(0.5 m/s 약 2~3m 앞)",
          env=ON,
          expect=lambda r: (len(r.get("cues", [])) == 3 and all(2.0 <= d <= 3.0 for d in r.get("lead_m", [])))
          or f"안내={r.get('cues')} 앞거리={r.get('lead_m')}")
def m12(sim):
    pts = [(6, 0, True), (6, 6, True), (12, 6, False)]
    heard = _cue_route(sim, pts)
    lead = []
    for (_, _, text, x, y), nxt in zip(heard[:2], pts[:2]):    # 첫 안내 = 첫 꺾는 점, 둘째 = 둘째 꺾는 점
        lead.append(round(((nxt[0] - x) ** 2 + (nxt[1] - y) ** 2) ** 0.5, 1))
    return {"cues": [h[2] for h in heard], "lead_m": lead}


@scenario("M13 묶음 중 '작업 복귀(역주행)' — 아직 안 간 앞쪽 점을 되밟지 않음",
          env=ON,
          expect=lambda r: (r.get("retrace") == [[3, 3], [3, 0]]) or f"역주행 경로={r.get('retrace')}")
def m13(sim):
    import app.robot_control.return_to_work as rtw
    sim.rewire_late_modules()
    got = {}

    def back():
        rtw.return_to_work(mode="retrace")
        got["wps"] = [[w["x"], w["y"]] for w in sim.sm.waypoints_list]
        sim.sm.stop_navigation_internal("시험 끝")
    sim.at(14.0, back)
    sim.move(ROUTE)
    return {"retrace": got.get("wps")}
