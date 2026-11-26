"""장애물 정지 중 방향·도착·진행 안내 막기(10-30 C) — 현행 vs 변경.

  sim\\run.bat sim\\scenarios\\obstacle_cue_hold.py --cand sim\\out\\cand_cuehold
무대는 progress_cue.py(병원 시나리오, 가짜 로봇 0.5 m/s), 감지 노드는 obstacle_hold.py 의 FakeGuard.
현행: 서 있는 동안에도 send_move.check_approach_cue 가 시간·거리로 안내를 낸다(진행 멘트는 출발 2초 뒤 시간으로 나간다).
변경: obstacle_hold 가 정지 중(출발 대기 포함)이면 안내를 내지 않고, 다시 출발한 뒤 거리·시간으로 다시 판단한다.
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_here = os.path.dirname(__file__)
_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("progress_cue_base_cuehold", os.path.join(_here, "progress_cue.py"))
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)
_spec2 = importlib.util.spec_from_file_location("obstacle_hold_base_cuehold", os.path.join(_here, "obstacle_hold.py"))
oh = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(oh)
del REGISTRY[_n:]

ON = {"OBSTACLE_GUARD_ENABLED": "1", "OBSTACLE_ANNOUNCE_SEC": "0", "OBSTACLE_ASK_AFTER_SEC": "7"}
P = pc.P


def _run(sim, block_sec, vias=(), robot_at=(-12.0, 0.0), start_after_m=0.6):
    st = pc._stage(sim, vias=vias, progress=P, robot_at=robot_at)
    start = (sim.robot.x, sim.robot.y)
    blk = oh.Block(sim, lambda: math.hypot(sim.robot.x - start[0], sim.robot.y - start[1]) >= start_after_m, block_sec)
    guard = oh.FakeGuard(sim, blk)
    oh.FakeVoice(sim, ["yes"])
    res = st.open_guide()
    sid = res["session"]["id"]
    db = st.Session()
    try:
        s = db.query(st.M.ServiceSession).filter(st.M.ServiceSession.id == sid).first()
        s.CurrentStepCode = "ticket_move"
        db.commit()
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
    finally:
        db.close()
    out = pc._listen(sim, start)
    st.drive(until=lambda: not sim.sm.is_navigating)
    r = pc._result(out, sim)
    stop_t = next((t for t, x in guard.notice_t if x == oh.STOP), None)
    go_t = next((t for t, x in guard.notice_t if x == oh.RESUME), None)
    during = [x for x, at in out["heard"] if stop_t is not None and go_t is not None and stop_t - 0.3 <= at <= go_t]
    return {**r, "notices": [x[:12] for x in guard.notices], "cancels": guard.cancels, "during_hold": during,
            "hold": [stop_t, go_t]}


@scenario("[C1] 출발 직후(0.6m) 8초 막힘 — 진행 멘트 시각(출발 2초 뒤)이 정지 중 → 변경: 정지 중엔 안 나오고 출발 뒤에",
          env=ON,
          expect=lambda r: r["errors"] == 0 and r["cancels"] == 1 and r["during_hold"] == [] and r.get("pos") == [4, 0])
def c1(sim):
    return _run(sim, 8.0)


@scenario("[C2] 꺾는 점 앞에서 12초 막힘(멈춘 동안 방향 안내 거리 안) → 변경: 정지 중 방향 안내 없음, 출발 뒤 거리로 다시",
          env=ON,
          expect=lambda r: r["errors"] == 0 and r["during_hold"] == [] and pc.RIGHT in r.get("heard", []))
def c2(sim):
    # 꺾는 점(-4, 0)까지 8m — 6.5m 간 뒤(꺾는 점 1.5m 앞, 안내 거리 2m 안) 막는다
    return _run(sim, 12.0, vias=[(-4.0, 0.0), (-4.0, 6.0)], robot_at=(-12.0, 0.0), start_after_m=6.5)


@scenario("[회귀] 막힘 없음 · 직선 16m → 진행 멘트 한 번(progress_cue straight_long 과 같음)",
          env=ON,
          expect=lambda r: r["errors"] == 0 and r.get("heard") == [P] and r["cancels"] == 0)
def reg(sim):
    return _run(sim, 0.0, start_after_m=999.0)
