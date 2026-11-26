"""5~6m 구간 진행 멘트(2026-10-30) — 곧 도착이 없는 이동이면 그 몫을 빼지 않는다.

  sim\\run.bat sim\\scenarios\\progress_short_1030.py --cand sim\\out\\cand_prog5
현행: 노드 없는 한 점 이동에는 '곧 도착'이 없는데도 남은 시간에서 그 몫(최소 2.5m)을 빼 5~6m 직선은 늘 '여유 없음'.
변경: send_move._soon_share_m — 이번 이동에 곧 도착이 남아 있을 때만 뺀다. 곧 도착이 있는 구간은 그대로(사용자: 곧 도착 우선).
무대: progress_cue.py 병원 무대, 가짜 로봇 0.45 m/s(10-30 3F 걸음), 보통 명령. 측정: 진행 멘트 시각과 목표 도착 시각(걷기 시작 기준 초).
판정: 진행 멘트가 도착 4초 이상 전(멘트 약 3초 + 여유)에 나오고, 곧 도착이 있는 구간은 곧 도착과 5초 넘게 떨어지거나 진행 멘트 생략.
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("pc_base_ps", os.path.join(os.path.dirname(__file__), "progress_cue.py"))
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)
del REGISTRY[_n:]
GOAL = (4.0, 0.0)
SPEED = 0.45


def _run(sim, length, vias=()):
    start = (GOAL[0] - length, 0.0) if not vias else None
    st = pc._stage(sim, vias=list(vias), progress=pc.P, robot_at=start or (-1.0, 0.0))
    sim.robot.p.speed = SPEED
    t0 = {"depart": None, "arrive": None}
    s0 = (sim.robot.x, sim.robot.y)

    def watch():
        now = sim.clock.elapsed()
        if t0["depart"] is None and math.hypot(sim.robot.x - s0[0], sim.robot.y - s0[1]) >= 0.5:
            t0["depart"] = now
        if t0["arrive"] is None and math.hypot(sim.robot.x - GOAL[0], sim.robot.y - GOAL[1]) < 0.05:
            t0["arrive"] = now
        if t0["arrive"] is None:
            sim.at(0.1, watch)
    sim.at(0.1, watch)
    r = pc._guide_move(st, sim)
    if t0["depart"] is not None and t0["arrive"] is not None:
        r["arrive_t"] = round(t0["arrive"] - t0["depart"], 1)
        pt = [t for h, t in zip(r["heard"], r["t"]) if h == pc.P]
        r["progress_before_arrive"] = round(r["arrive_t"] - pt[0], 1) if pt else None
    return r


def _ok_straight(r):
    return (r.get("heard") == [pc.P] and (r.get("progress_before_arrive") or 0) >= 4.0 and r["errors"] == 0) \
        or f"들은 것={r.get('heard')} 진행 멘트→도착 {r.get('progress_before_arrive')}초 도착 {r.get('arrive_t')}초"


@scenario("P1 [재현] 직선 5.0m(곧 도착 없음) — 진행 멘트 1번, 도착 4초 이상 전", expect=_ok_straight)
def p1(sim):
    return _run(sim, 5.0)


@scenario("P2 [재현] 직선 5.5m — 진행 멘트 1번, 도착 4초 이상 전", expect=_ok_straight)
def p2(sim):
    return _run(sim, 5.5)


@scenario("P3 [재현] 직선 6.0m — 진행 멘트 1번, 도착 4초 이상 전", expect=_ok_straight)
def p3(sim):
    return _run(sim, 6.0)


@scenario("P4 [회귀] 직선 3.5m — 최소 거리(4m, 10-30 5m→4m) 미만이라 진행 멘트 없음",
          expect=lambda r: r.get("heard") == [] or str(r.get("heard")))
def p4(sim):
    return _run(sim, 3.5)


@scenario("P5 [회귀] 경유점 있는 5.2m(안내창구 닮음) — 진행 멘트와 곧 도착 중 하나만, 둘 다면 5초 넘게 떨어짐"
          "(11-02 부터 둘 다 안 들어가면 진행 멘트를 남긴다)",
          expect=lambda r: (len(r.get("heard", [])) == 1 or (r.get("min_gap") is None or r["min_gap"] >= 5.0))
          and bool(r.get("heard")) or str({k: r.get(k) for k in ("heard", "t", "min_gap")}))
def p5(sim):
    return _run(sim, 0.0, vias=[(1.6, 0.6)])
