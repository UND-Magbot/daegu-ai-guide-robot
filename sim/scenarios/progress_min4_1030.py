"""진행 멘트 최소 거리 5m → 4m(2026-10-30).

  sim\\run.bat sim\\scenarios\\progress_min4_1030.py --cand sim\\out\\cand_progmin4
실기(10-30 17:31, 3F 세션 518): 안내데스크 4.8m '진행 멘트 생략 — 경로 4.8m < 5m', 걷기 약 9초.
종합민원실 3.6m(약 8초)는 그대로 생략. 무대·측정은 progress_short_1030 과 같다(0.45 m/s, 보통 명령).
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("ps_base_m4", os.path.join(os.path.dirname(__file__), "progress_short_1030.py"))
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)
del REGISTRY[_n:]
pc = ps.pc


@scenario("M1 [재현] 직선 4.8m(안내데스크) — 진행 멘트 1번, 도착 4초 이상 전", expect=ps._ok_straight)
def m1(sim):
    return ps._run(sim, 4.8)


@scenario("M2 [변경] 직선 4.2m — 진행 멘트 1번, 도착 4초 이상 전", expect=ps._ok_straight)
def m2(sim):
    return ps._run(sim, 4.2)


@scenario("M3 [회귀] 직선 3.6m(종합민원실) — 생략", expect=lambda r: r.get("heard") == [] or str(r.get("heard")))
def m3(sim):
    return ps._run(sim, 3.6)


@scenario("M4 [회귀] 직선 6.0m — 진행 멘트 1번", expect=ps._ok_straight)
def m4(sim):
    return ps._run(sim, 6.0)
