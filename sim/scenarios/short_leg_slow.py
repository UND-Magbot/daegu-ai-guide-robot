"""자율 모드 — 서서 돈 뒤 짧은 구간 출발 저속 (2026-11-03 memo "3F N363↔N360 짧은데 전속력").

본체 로그(11-03, 669, 맵 49): N360→N363(첫 구간 1.3m)은 출발 저속(Speed 1)으로 0.25 m/s.
반대로 6m 를 달려와 N363 에서 서서 돈 뒤 로비입구까지 1.31m 는 Speed 0(보통)으로 나가 0.44~0.47 m/s.
출발 저속(_soft_start_speed)이 이동의 첫 구간만 보기 때문이다.

변경: 자율 모드 묶음에서 앞 점이 서서 도는 점(turn_stop)이거나 앞 묶음 끝에 서 있다가 다시 출발하는 구간이
NAV_SOFT_START_MAX_M(3m) 이하이면 slow. 직선 모드(꺾인 뒤 저속이 따로 있음)·긴 구간·NAV_SOFT_START 끔은 그대로.

판정은 관제가 보낸 점별 속도다. 본체가 Speed 1 을 따르는 것은 11-03 본체 로그로 확인(0.24~0.26 m/s).
무대: node_edge_attach 병원 복도(auto_turn_stop 과 같음).
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "auto_turn_stop_base_sl", os.path.join(os.path.dirname(__file__), "auto_turn_stop.py"))
ats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ats)
del REGISTRY[_n:]
ea = ats.ea

STOP = {**ats.STOP, "NODE_AUTO_TURN_STOP_DEG": "5"}      # 운영값(NODE_AUTO_TURN_STOP_DEG=5, 11-03 컨테이너)
MAX_M = 3.0


def legs(sim, start):
    """이동이 끝난 뒤 웨이포인트별 (이름, 앞 구간 길이, 앞 점에서 서서 돎, 보낸 속도)."""
    wl = sim.sm.waypoints_list
    out = []
    for k, w in enumerate(wl):
        if w.get("fixed_yaw"):
            continue
        a = (wl[k - 1]["x"], wl[k - 1]["y"]) if k > 0 else start
        out.append((w.get("name"), round(math.dist(a, (w["x"], w["y"])), 2),
                    bool(k > 0 and wl[k - 1].get("turn_stop")), w.get("sent_speed") or "normal"))
    return out


def run(sim, env_note, start=(1.0, 1.1), yaw=ats.NORTH, name="원무과 접수"):
    r = ats.run(sim, start, yaw, name)
    r["legs"] = legs(sim, start)
    r["note"] = env_note
    return r


def restart_short(r):
    """서서 돈 뒤 3m 이하 구간들의 속도"""
    return [(n, d, s) for n, d, t, s in r["legs"] if t and d <= MAX_M]


def ok(r, cond):
    return (r["arrived"] and r["errors"] == 0 and cond(r)) or f"도착={r['arrived']} 구간={r['legs']}"


@scenario("K1 [재현/변경] 자율·서서 돌기 — 서서 돈 뒤 3m 이하 구간은 모두 저속 (현행: 보통)", env=STOP,
          expect=lambda r: ok(r, lambda r: restart_short(r) and all(s == "slow" for _, _, s in restart_short(r))))
def k1(sim):
    return run(sim, "auto")


@scenario("K2 [회귀] 자율 — 서서 돈 뒤라도 3m 넘는 구간은 원래 속도", env=STOP,
          expect=lambda r: ok(r, lambda r: all(s != "slow" for n, d, t, s in r["legs"][1:] if t and d > MAX_M)))
def k2(sim):
    return run(sim, "auto")


@scenario("K3 [회귀] 자율 — 걸으며 도는(서지 않는) 점 뒤 구간은 원래 속도", env=STOP,
          expect=lambda r: ok(r, lambda r: all(s != "slow" for n, d, t, s in r["legs"][1:] if not t)))
def k3(sim):
    return run(sim, "auto")


@scenario("K4 [회귀] NAV_SOFT_START 끔 — 저속 하나도 없음", env={**STOP, "NAV_SOFT_START": "false"},
          expect=lambda r: ok(r, lambda r: "slow" not in r["speeds"]))
def k4(sim):
    return run(sim, "auto, 출발 저속 끔")


@scenario("K5 [회귀] 직선 모드 — 지금과 같은 속도(꺾인 뒤 저속 규칙만)", env={**STOP, "NODE_NAV_MODE": "straight"},
          expect=lambda r: ok(r, lambda r: r["modes"] == ["straight"]))
def k5(sim):
    return run(sim, "straight")


@scenario("K6 [회귀] 서서 돌기 끔(NODE_AUTO_TURN_STOP_DEG=0) — 첫 구간 말고는 저속 없음",
          env={**STOP, "NODE_AUTO_TURN_STOP_DEG": "0"},
          expect=lambda r: ok(r, lambda r: all(s != "slow" for *_, s in r["legs"][1:])))
def k6(sim):
    return run(sim, "auto, 서서 돌기 끔")
