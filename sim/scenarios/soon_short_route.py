"""짧은 구간 "곧 도착" 생략(2026-10-30) — 3F 관공서 실기에서 곧 도착 뒤 몇 초 만에 도착 멘트가 붙어 들렸다.

  sim\run.bat sim\scenarios\soon_short_route.py --cand sim\out\cand_soonshort
실기(10-30 11:32~11:36, 걸음 약 0.3~0.45 m/s): 안내창구 → 종합민원실 3.5m 는 출발 회전이 끝나자마자(2.5m 앞) 곧 도착,
약 6초 뒤 도착 멘트. 음성 합성 대기까지 겹쳐 곧 도착이 끝나고 약 3초 만에 도착 멘트.
변경: 경로가 ARRIVE_SOON_MIN_ROUTE_M(5m, 진행 멘트와 같은 기준) 보다 짧으면 곧 도착을 내지 않는다.
무대: progress_cue 병원 무대, 원무과 접수 (4,0) 로 가는 이동(경유지 1개 + 목적지 = 다지점이 아닌 2점 이동), 로봇 0.4 m/s.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("pc_base_soon", os.path.join(os.path.dirname(__file__), "progress_cue.py"))
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)
del REGISTRY[_n:]


def _run(sim, robot_at, via):
    st = pc._stage(sim, vias=[via], progress="-", robot_at=robot_at)
    sim.robot.p.speed = 0.4
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
            heard.append(c["text"])
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
    return {"heard": heard, "arrived": abs(sim.robot.x - 4.0) < 0.05 and abs(sim.robot.y) < 0.05}


@scenario("S1 [재현] 경로 3.5m(직진) — 곧 도착을 내지 않음",
          expect=lambda r: (r["heard"] == [] and r["arrived"] and r["errors"] == 0) or f"들은 것={r['heard']}")
def s1(sim):
    return _run(sim, (0.5, 0.0), (2.0, 0.0))


@scenario("S2 [경계] 경로 5.5m(직진) — 곧 도착 그대로",
          expect=lambda r: (r["heard"] == [pc.SOON] and r["arrived"] and r["errors"] == 0) or f"들은 것={r['heard']}")
def s2(sim):
    return _run(sim, (-1.5, 0.0), (1.0, 0.0))


@scenario("S3 [회귀] 경로 4.2m 에 꺾임 — 방향 안내는 그대로 냄(곧 도착은 짧아 생략 또는 방향 안내에 붙음)",
          expect=lambda r: (r["heard"][:1] and r["heard"][0].startswith("잠시 후") and r["arrived"] and r["errors"] == 0)
          or f"들은 것={r['heard']}")
def s3(sim):
    return _run(sim, (2.0, 3.0), (4.0, 2.0))


@scenario("S4 [회귀] 긴 경로 15m 꺾임 — 방향 안내·곧 도착 둘 다",
          expect=lambda r: (r["heard"] == [pc.RIGHT, pc.SOON] and r["arrived"] and r["errors"] == 0) or f"들은 것={r['heard']}")
def s4(sim):
    return _run(sim, (-10.0, 0.0), (0.0, 3.0))
