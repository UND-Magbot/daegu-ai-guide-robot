"""경유지 방향 안내를 '도착 N초 전'으로 — 거리(2m) 기준이면 빨리 걸을수록 늦게 들린다.

현행: 경유지 2m 안에 들어오면 안내를 낸다(NAV_CUE_LEAD_M).
변경: 지금 속도 × (NAV_CUE_LEAD_SEC 3초 + 전달 지연 2.5초) 앞에서 낸다(최소 2m).

하네스는 위치 지연·음성 엔진 확인 주기를 재현하지 않는다. 그래서 '들린 시각' = 낸 시각 + PIPE_DELAY 로 따로 더한다.
PIPE_DELAY 는 위치 약 1초 늦음 + 음성 엔진 1초마다 확인(평균 0.5초) + 재생 시작 약 0.3초를 합친 추정값이다(실측 아님).
판정은 '들린 뒤 경유지에 닿기까지 남은 초'로 한다. 통과 지점은 반경 안에서 넘기므로 가장 가까이 간 시각을 쓴다.
"""
from harness import scenario

MULTI = {"NAV_MULTIPOINT_ENABLED": "true"}
PIPE_DELAY = 1.8
# 꺾는 경유지 2곳(왼쪽 → 오른쪽) + 목적지. 구간 8m.
ROUTE = [(8, 0, True), (8, 8, True), (16, 8, False)]
# 경유지끼리 1.5m — 앞 경유지를 지나자마자 다음 안내 거리 안이다
CLOSE = [(8, 0, True), (8, 1.5, True), (6.5, 1.5, True), (6.5, 9, False)]


def drive(sim, pts):
    """방향 안내를 계산해 둔 장소 이동. 반환: [{no, text, emit, reach, lead_sec, emit_m}]"""
    sm, robot = sim.sm, sim.robot
    wps = [{"x": x, "y": y, "yaw": 0.0, "name": f"P{i}", "map_id": None, **({"pass_through": True} if p else {})}
           for i, (x, y, p) in enumerate(pts)]
    sm.waypoints_list = wps
    sm._set_route_cues((robot.x, robot.y), wps, 0)
    plan = dict(sm._route_cues)
    emits, best = [], {}
    state = {"seq": sm.get_nav_cue()["seq"]}

    def tick():
        c = sm.get_nav_cue()
        now = sim.clock.elapsed()
        if c["seq"] != state["seq"]:
            state["seq"] = c["seq"]
            no = next((k for k, v in plan.items() if v == c["text"] and k not in [e["no"] for e in emits]), None)
            if no is not None:
                w = wps[no - 1]
                emits.append({"no": no, "text": c["text"], "emit": now,
                              "emit_m": round(((robot.x - w["x"]) ** 2 + (robot.y - w["y"]) ** 2) ** 0.5, 2)})
        for k, w in enumerate(wps, 1):
            d = ((robot.x - w["x"]) ** 2 + (robot.y - w["y"]) ** 2) ** 0.5
            if k not in best or d < best[k][0] - 1e-6:
                best[k] = (d, now)
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
    reach = {k: t for k, (d, t) in best.items() if d < 1.2}
    out = []
    for e in emits:
        r = reach.get(e["no"])
        out.append({**e, "emit": round(e["emit"], 1), "reach": round(r, 1) if r is not None else None,
                    "lead_sec": round(r - (e["emit"] + PIPE_DELAY), 1) if r is not None else None})
    return {"cues": out, "plan": plan}


def turns(r):
    return [c for c in r.get("cues", []) if "방향" in c["text"]]


def leads(r):
    return [c["lead_sec"] for c in turns(r)]


def ok_lead(r, lo=2.5):
    t = turns(r)
    return (len(t) == 2 and all(c["lead_sec"] is not None and c["lead_sec"] >= lo for c in t)
            and r["errors"] == 0) or f"꺾기 안내={[(c['text'][6:9], c['emit_m'], c['lead_sec']) for c in t]}"


def order_ok(r):
    texts = [c["text"] for c in r.get("cues", [])]
    return texts == ["잠시 후 왼쪽으로 방향을 바꾸겠습니다.", "잠시 후 오른쪽으로 방향을 바꾸겠습니다.", "곧 도착합니다."]


@scenario("C1 저속 0.3m/s — 꺾기 안내가 경유지 2.5초 이상 전에 들림", env=MULTI, params={"speed": 0.3},
          expect=ok_lead)
def c1(sim):
    return drive(sim, ROUTE)


@scenario("C2 보통 0.5m/s — 꺾기 안내가 경유지 2.5초 이상 전에 들림", env=MULTI, params={"speed": 0.5},
          expect=ok_lead)
def c2(sim):
    return drive(sim, ROUTE)


@scenario("C3 고속 0.8m/s — 꺾기 안내가 경유지 2.5초 이상 전에 들림", env=MULTI, params={"speed": 0.8},
          expect=ok_lead)
def c3(sim):
    return drive(sim, ROUTE)


@scenario("C4 [회귀] 안내 순서·문구 그대로(왼쪽 → 오른쪽 → 곧 도착), 오류 없음", env=MULTI, params={"speed": 0.5},
          expect=lambda r: (order_ok(r) and r["pos"] == [16, 8] and r["errors"] == 0)
          or f"안내={[c['text'] for c in r.get('cues', [])]} 위치={r['pos']}")
def c4(sim):
    return drive(sim, ROUTE)


# 10-29 오후부터 '곧 도착' 은 도착 4초+전달 지연 2.5초 앞(최소 2.5m), 속도는 보낸 명령의 하한(보통 1.0 m/s) — 약 6.5m
@scenario("C5 '곧 도착합니다'는 지금 걸음 × 5초 앞(0.8 m/s 약 2.5~4.4m. 10-29 오후 전 2.5m 고정·10-30 오전 6.5초·오후 5초)",
          env=MULTI, params={"speed": 0.8},
          expect=lambda r: (any(c["text"] == "곧 도착합니다." and 2.5 <= c["emit_m"] <= 4.4 for c in r.get("cues", [])))
          or f"곧 도착={[(c['emit_m']) for c in r.get('cues', []) if '도착' in c['text']]}")
def c5(sim):
    return drive(sim, ROUTE)


@scenario("C6 [경계] 경유지끼리 1.5m — 안내가 빠지지 않고 꺾는 순서대로 모두 나옴", env=MULTI, params={"speed": 0.5},
          expect=lambda r: (len(turns(r)) == 3 and [c["no"] for c in turns(r)] == [1, 2, 3] and r["errors"] == 0)
          or f"꺾기 안내={[(c['no'], c['emit_m'], c['lead_sec']) for c in turns(r)]}")
def c6(sim):
    return drive(sim, CLOSE)


@scenario("C7 [회귀] NAV_CUE_LEAD_SEC=0 이면 예전처럼 2m 앞(관찰: 현행은 이 env 를 모름)",
          env={**MULTI, "NAV_CUE_LEAD_SEC": "0"}, params={"speed": 0.8},
          expect=lambda r: all(1.0 <= c["emit_m"] <= 2.1 for c in turns(r)) or f"낸 거리={[c['emit_m'] for c in turns(r)]}")
def c7(sim):
    return drive(sim, ROUTE)


@scenario("C8 [경계] 단일 NAV(다지점 꺼짐) 고속 0.8m/s — 같은 효과", params={"speed": 0.8},
          expect=ok_lead)
def c8(sim):
    return drive(sim, ROUTE)
