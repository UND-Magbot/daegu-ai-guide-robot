"""사전 시뮬레이션 하네스.

시나리오 파일은 이 패키지만 import 하고, Backend(app.*)는 시나리오 함수 안에서 sim 으로 쓴다.

    from harness import scenario

    @scenario("일반 이동 도착", expect=lambda r: r["arrivals"] == 3 and r["errors"] == 0)
    def plain(sim):
        sim.move([(3, 0, True), (3, 3, True), (6, 3)])
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .robot import RobotParams


@dataclass
class Scenario:
    name: str
    fn: Callable[[Any], Any]
    expect: Callable[[dict], Any] | None = None
    params: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    limit_sec: float = 600.0


REGISTRY: list[Scenario] = []


def scenario(name: str, expect: Callable[[dict], Any] | None = None,
             params: dict | None = None, env: dict | None = None,
             limit_sec: float = 600.0):
    """시나리오 등록.

    expect: 결과 dict 를 받아 참/거짓(또는 실패 사유 문자열)을 돌려준다. 현행·변경 모두에 적용한다.
    params: RobotParams 덮어쓰기(예: {"speed": 0.3}).
    env:    Backend import 전에 넣을 환경 변수(예: {"NAV_CONTINUOUS_PASS": "true"}).
    """
    def deco(fn):
        REGISTRY.append(Scenario(name, fn, expect, params or {}, env or {}, limit_sec))
        return fn
    return deco


__all__ = ["scenario", "Scenario", "REGISTRY", "RobotParams"]
