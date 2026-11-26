"""'곧 도착' 시점 — 끝 구간에서 느려질 때(2026-10-30 A).

  sim\\run.bat sim\\scenarios\\soon_timing_1030.py --cand sim\\out\\cand_soonA
실기(10-30 16:44 3F 로비, 보통 명령): '곧 도착' 4.3m 앞 → 로봇 멈춤 8.6초 → 도착 판정 10.1초. 앞서 빨랐던 걸음(최근 10초
최고값)으로 거리를 잡았다. 안내창구는 3.1m 앞 → 멈춤 5.0초.
변경: '곧 도착'은 지금 걸음(최고값 유지 없음)·도착 5초 앞(2.5+전달 2.5)으로. 진행 멘트 남은 시간도 같은 속도로 잰다.
가짜 로봇은 속도 명령을 따르지 않으므로 걸음을 정해 준다. 측정: '곧 도착'을 낸 뒤 로봇이 목표에 닿을 때까지 초.
목표: 4~6초(Jetson 이 받는 데 약 1초 → 멈추기 3~5초 전에 들림).
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("csf_base_st", os.path.join(os.path.dirname(__file__), "cue_speed_floor.py"))
csf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(csf)
del REGISTRY[_n:]
pc = csf.pc
GOAL = (4.0, 0.0)


def _slow_near_goal(sim, fast, slow, within):
    """목표 within m 안에 들어오면 걸음을 slow 로 — 끝 구간 감속 재현."""
    def tick(dt):
        d = math.hypot(sim.robot.x - GOAL[0], sim.robot.y - GOAL[1])
        sim.robot.p.speed = slow if d <= within else fast
    sim.clock.add_ticker(tick)


@scenario("T1 [재현] 로비 닮음 — 0.66 m/s 로 오다 끝 4.5m 를 0.5 m/s · 곧 도착이 닿기 4~6초 전(현행 약 8~9초)",
          expect=lambda r: (r.get("heard") == [pc.RIGHT, pc.SOON] and 4.0 <= (r.get("soon_before_s") or -1) <= 6.0)
          or f"곧 도착 → 도착 {r.get('soon_before_s')}초 들은 것={r.get('heard')}")
def t1(sim):
    _slow_near_goal(sim, 0.66, 0.5, 4.5)
    return csf._run(sim, "normal", 0.66)


@scenario("T2 [회귀] 보통 1.1 m/s 일정 — 곧 도착 4초 이상 전, 방향 안내 3초 이상 전",
          expect=lambda r: (r.get("heard") == [pc.RIGHT, pc.SOON] and (r.get("soon_before_s") or 0) >= 4.0
                            and (r.get("turn_before_s") or 0) >= 3.0) or str({k: r.get(k) for k in ("heard", "soon_before_s", "turn_before_s")}))
def t2(sim):
    return csf._run(sim, "normal", 1.1)


@scenario("T3 [회귀] 0.46 m/s 일정(오늘 3F 평균) — 곧 도착 4~7초 전",
          expect=lambda r: (r.get("heard") == [pc.RIGHT, pc.SOON] and 4.0 <= (r.get("soon_before_s") or -1) <= 7.0)
          or str({k: r.get(k) for k in ("heard", "soon_before_s")}))
def t3(sim):
    return csf._run(sim, "normal", 0.46)


@scenario("T4 [회귀] 저속 0.33 m/s — 곧 도착 그대로(최소 2.5m)",
          expect=lambda r: r.get("heard") == [pc.RIGHT, pc.SOON] or str(r.get("heard")))
def t4(sim):
    return csf._run(sim, "slow", 0.33)
