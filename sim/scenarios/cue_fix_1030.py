"""안내 시점 맞추기(2026-10-30) — 3F 관공서 실기에서 "곧 도착합니다"가 어긋났다.

  sim\run.bat sim\scenarios\cue_fix_1030.py --cand sim\out\cand_cuefix
실기(세션 505, 10-30 11:00, 자율 모드·보통 명령, 구간 평균 0.35~0.46 m/s):
- 안내창구·로비: 도착 11~16초 전에 "곧 도착" — 보통 명령 속도 하한 1.0 m/s × 6.5초 = 6.5m 앞에서 냄.
- 대기석: 도착 0.3m(4초) 앞 — 대기석 0.09m 옆 노드 N362 를 경유점으로 보내, 그 점에 닿아야 마지막 구간이 됨.
변경: ① 보통 하한 1.0 → 0.5 m/s(고속 1.2 → 0.8) ② 목적지 0.3m 안 노드는 경유점에서 뺌(문 노드 제외).
가짜 로봇은 속도 명령을 따르지 않으므로 속도를 정해 준다.
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_here = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location("csf_base_1030", os.path.join(_here, "cue_speed_floor.py"))
csf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(csf)
_spec2 = importlib.util.spec_from_file_location("nr_base_1030", os.path.join(_here, "node_route.py"))
nrb = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(nrb)
del REGISTRY[_n:]
pc = csf.pc


def _band(lo, hi, key):
    return lambda r: (r.get("heard") == [pc.RIGHT, pc.SOON] and lo <= (r.get(key) or -1) <= hi
                      and r["errors"] == 0) or f"{key}={r.get(key)} (기대 {lo}~{hi}초) 들은 것={r.get('heard')}"


@scenario("Q1 [재현] 보통 명령·0.46 m/s — 곧 도착이 도착 4~9초 전(현행 약 14초 전)",
          expect=_band(4.0, 9.0, "soon_before_s"))
def q1(sim):
    return csf._run(sim, "normal", 0.46)


@scenario("Q2 [재현] 보통 명령·0.46 m/s — 방향 안내가 꺾기 3~9초 전(현행 약 12초 전)",
          expect=_band(3.0, 9.0, "turn_before_s"))
def q2(sim):
    return csf._run(sim, "normal", 0.46)


@scenario("Q3 [회귀] 보통 명령·1.1 m/s — 꺾기 3초·도착 4초 이상 전(잰 속도가 하한보다 크면 그대로)",
          expect=lambda r: r.get("heard") == [pc.RIGHT, pc.SOON] and (r.get("turn_before_s") or 0) >= 3.0
          and (r.get("soon_before_s") or 0) >= 4.0 and r["errors"] == 0)
def q3(sim):
    return csf._run(sim, "normal", 1.1)


@scenario("Q4 [회귀] 저속 명령·0.33 m/s — 현행과 같음",
          expect=lambda r: r.get("heard") == [pc.RIGHT, pc.SOON] and r["errors"] == 0)
def q4(sim):
    return csf._run(sim, "slow", 0.33)


# ── 목적지 바로 옆 노드 ──
N14 = nrb.NODES[14]
BESIDE_NODE = (N14[0], N14[1] - 0.09)          # 대기석처럼 노드에서 0.09m
PLACES_Q = {**nrb.PLACES, "노드 옆": (*BESIDE_NODE, "work")}


def _plan(sim, start, name):
    st = nrb.Stage(sim, places=PLACES_Q)
    if not st.on:
        return st, None
    from app.database.database import SessionLocal
    db = SessionLocal()
    try:
        x, y, _ = PLACES_Q[name]
        plan = st.nr.plan_leg(db, nrb.MAP, start, {"x": x, "y": y, "floor_id": nrb.FLOOR})
    finally:
        db.close()
    return st, plan


@scenario("Q5 [회귀·시뮬 지도에선 단순화가 이미 뺌] 복도 서쪽 → '노드 옆'(N14 에서 0.09m) — 0.3m 안 노드는 경유점에 없음", env=nrb.ON,
          expect=lambda r: (r["near_goal_wp"] == 0 and r["wps"] and r["errors"] == 0)
          or f"경유점={r['wps']} 목적지 0.3m 안 경유점={r['near_goal_wp']}")
def q5(sim):
    st, plan = _plan(sim, nrb.NODES[1], "노드 옆")
    wps = plan.waypoints if plan is not None else []
    return {"wps": [w["name"] for w in wps],
            "near_goal_wp": sum(1 for w in wps if math.dist((w["x"], w["y"]), BESIDE_NODE) <= 0.3)}


@scenario("Q6 [회귀] '노드 옆' 실제 이동 — 도착·벽 넘음 0", env=nrb.ON,
          expect=lambda r: (r["arrived"] and r["wall_cross"] == 0 and r["errors"] == 0)
          or f"도착={r['arrived']} 벽={r['wall_cross']} 목표={r['targets']}")
def q6(sim):
    st = nrb.Stage(sim, places=PLACES_Q)
    sim.robot.place(*nrb.NODES[1])
    st.samples.append(nrb.NODES[1])
    sm = sim.sm
    from app.database.database import SessionLocal
    out = {}

    def go():
        db = SessionLocal()
        try:
            out.update(sm.start_place_move(st.place_ids["노드 옆"], db))
        finally:
            db.close()
        if out.get("status") == "ok":
            sim.pl.nav_thread()
    n0 = len(sim.robot.arrivals)
    sim.run(go, until=lambda: out and (out.get("status") != "ok" or (
        not sm.is_navigating and len(sim.robot.arrivals) > n0)))
    return {"arrived": math.dist((sim.robot.x, sim.robot.y), BESIDE_NODE) < 0.05,
            "wall_cross": st.wall_cross, "targets": st.targets}
