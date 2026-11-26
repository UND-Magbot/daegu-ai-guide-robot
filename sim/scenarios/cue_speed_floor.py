"""안내 시점 속도 하한(2026-10-29 오후) — 구간에 보낸 속도에 맞춰 방향 안내·"곧 도착합니다" 시점을 정한다.

  sim\\run.bat sim\\scenarios\\cue_speed_floor.py --cand sim\\out\\cand_speedcue
배경: 출발 저속 4m 한도로 보통 속도 구간이 늘었다. 방향 안내는 잰 속도 × 5.5초 앞(최소 2m)인데 잰 속도는 늦게 따라오고,
"곧 도착합니다" 는 2.5m 고정(0.3 m/s 기준)이라 보통 1.0~1.2 m/s 에서는 도착 약 2초 전이다.
변경: 보낸 속도의 하한(저속 0.35·보통 1.0 m/s)을 함께 보고, 곧 도착은 4초+전달 지연 2.5초 앞(최소 2.5m).
가짜 로봇은 속도 명령을 따르지 않으므로 조건마다 로봇 속도를 정해 준다. 기록: 안내를 낸 뒤 그 점(꺾는 곳·도착)까지 걸린 초.
판정: 저속은 현행과 같아야 하고, 보통은 꺾기 3초 이상·도착 4초 이상 전에 나와야 한다(전달 지연 약 2.5초를 빼도 들리게).
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("progress_cue_base",
                                               os.path.join(os.path.dirname(__file__), "progress_cue.py"))
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)
del REGISTRY[_n:]

VIA = (0.0, 3.0)        # (-10,0) → (0,3) 10.4m → 오른쪽 꺾어 (4,0) 5m


def _run(sim, guide_speed, mps):
    st = pc._stage(sim, vias=[VIA], progress="-", robot_at=(-10.0, 0.0))
    sim.robot.p.speed = mps
    db = st.Session()
    try:
        db.query(st.M.VoiceScenario).filter(st.M.VoiceScenario.id == 1).first().GuideSpeed = guide_speed
        db.commit()
    finally:
        db.close()
    marks = {"via": None, "arrive": None}

    def watch():
        now = sim.clock.elapsed()
        if marks["via"] is None and math.hypot(sim.robot.x - VIA[0], sim.robot.y - VIA[1]) < 1.0:   # 통과 지점은 1m 안에서 다음 점으로 넘어간다
            marks["via"] = now
        if marks["arrive"] is None and math.hypot(sim.robot.x - 4.0, sim.robot.y) < 0.05:
            marks["arrive"] = now
        if sim.sm.is_navigating:
            sim.at(0.1, watch)
    sim.at(0.1, watch)
    res = st.open_guide()
    sid = res["session"]["id"]
    db = st.Session()
    seq0 = sim.sm.get_nav_cue()["seq"]
    heard = []

    def listen():
        nonlocal seq0
        c = sim.sm.get_nav_cue()
        if c["seq"] != seq0:
            seq0 = c["seq"]
            heard.append((c["text"], sim.clock.elapsed()))
        if sim.sm.is_navigating:
            sim.at(0.2, listen)
    try:
        s = db.query(st.M.ServiceSession).filter(st.M.ServiceSession.id == sid).first()
        s.CurrentStepCode = "ticket_move"
        db.commit()
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
    finally:
        db.close()
    sim.at(0.2, listen)
    st.drive(until=lambda: not sim.sm.is_navigating)
    turn = next((t for h, t in heard if h == pc.RIGHT), None)
    soon = next((t for h, t in heard if h == pc.SOON), None)
    return {"heard": [h for h, _ in heard],
            "turn_before_s": round(marks["via"] - turn, 1) if turn is not None and marks["via"] else None,
            "soon_before_s": round(marks["arrive"] - soon, 1) if soon is not None and marks["arrive"] else None}


@scenario("[저속 명령·0.33 m/s] 방향 안내·곧 도착 시점이 현행과 같음(관찰: 꺾기·도착 몇 초 전)",
          expect=lambda r: r.get("heard") == [pc.RIGHT, pc.SOON] and r["errors"] == 0)
def slow_same(sim):
    return _run(sim, "slow", 0.33)


@scenario("[보통 명령·1.1 m/s] 방향 안내 꺾기 3초 이상 전, 곧 도착 도착 4초 이상 전",
          expect=lambda r: r.get("heard") == [pc.RIGHT, pc.SOON] and (r.get("turn_before_s") or 0) >= 3.0
          and (r.get("soon_before_s") or 0) >= 4.0 and r["errors"] == 0)
def normal_earlier(sim):
    return _run(sim, "normal", 1.1)
