"""출발 구간 저속 거리 한도 — 직선 다지점 첫 구간에도 적용(2026-10-29 오후, 사용자 결정).

  sim\run.bat sim\scenarios\soft_start4.py
10-29 실기(14:05~14:10): 직선 다지점 저속 보정(NAV_TURN_SLOW_DEG=30)이 첫 걷는 구간을 거리와 상관없이 저속으로 보내
수납 5.4m·엘리베이터 3.7m 가 저속, "보통인데 저속 모드로 고른 것처럼" 보였다. 변경: 출발 구간 저속 거리 한도
(NAV_SOFT_START_MAX_M)를 직선 다지점의 첫 구간에도 적용. 한도는 한때 4m 로 올렸다가 15시 사용자 결정으로 3m 로 되돌렸다
(f2 세션 config.py) — 이 시나리오도 3m 기준. 꺾인 뒤 구간 저속(B2)은 그대로. 송신 기록 방식은 turn_slow.py 와 같다.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("turn_slow_base", os.path.join(os.path.dirname(__file__), "turn_slow.py"))
ts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ts)
del REGISTRY[_n:]

S, N = ts.S, ts.N
B = ts.B                                              # 직선 + 꺾임 저속 30°(지금 운영 값)
BASE = ts.BASE                                        # 꺾임 저속 끔
AUTO = {**ts.B, "NODE_NAV_MODE": "auto"}


@scenario("F1 직선 다지점 첫 구간 2.5m → 저속", expect=ts._expect([[S, N]]), env=B)
def f1(sim):
    return ts._run(sim, [(2.5, 0), (6, 0)])


@scenario("F2 [경계] 첫 구간 3.0m → 저속", expect=ts._expect([[S, N]]), env=B)
def f2(sim):
    return ts._run(sim, [(3.0, 0), (6, 0)])


@scenario("F3 [변경] 첫 구간 3.5m → 보통으로 출발(전에는 거리 무관 저속)", expect=ts._expect([[N, N]]), env=B)
def f3(sim):
    return ts._run(sim, [(3.5, 0), (7, 0)])


@scenario("F4 [변경] 수납처럼 첫 구간 5.4m 뒤 90° → 첫 구간 보통, 꺾인 뒤는 그대로 저속",
          expect=ts._expect([[N, S]]), env=B)
def f4(sim):
    return ts._run(sim, [(5.4, 0), (5.4, 5.4)])


@scenario("F5 [회귀] 자율 모드 첫 구간 2.5m → 저속(현행과 같음)", expect=ts._expect([[S, N]]), env=AUTO)
def f5(sim):
    return ts._run(sim, [(2.5, 0), (6, 0)])


@scenario("F6 [회귀] 꺾임 저속 끔 · 첫 구간 3.5m → 보통(현행과 같음)", expect=ts._expect([[N, N]]), env=BASE)
def f6(sim):
    return ts._run(sim, [(3.5, 0), (6, 0)])


@scenario("F7 [회귀] 꺾임 저속 끔 · 첫 구간 5m → 보통(현행과 같음)", expect=ts._expect([[N, N]]), env=BASE)
def f7(sim):
    return ts._run(sim, [(5.0, 0), (7, 0)])
