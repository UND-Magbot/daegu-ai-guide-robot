"""안내 시작 위치 생략 반경(2026-10-30) — 1.5m → 1.0m.

  sim\\run.bat sim\\scenarios\\call_near_1030.py --cand sim\\out\\cand_callnear
실기(10-30 17:24, 3F 맵49): 로봇이 로비입구(1.88,-1.69)에 서 있고 시작 장소 로봇대기위치(1.93,-0.20)까지 1.49m —
GUIDE_CALL_NEAR_M 1.5m 안이라 이동 없이 '이미 근처'로 시작했다(세션 516·517).
변경: 기본값 1.0m. 층 이동 뒤 손으로 세운 오차(0.5~1m, 10-11 실기)는 계속 생략.
무대: guide_speed_start.py Stage(시작 장소 (2,0)). 측정: 시작 위치 이동 NAV 수·끝 위치.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("gss_base_cn", os.path.join(os.path.dirname(__file__), "guide_speed_start.py"))
gss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gss)
del REGISTRY[_n:]


def _start_from(sim, dy):
    st = gss.Stage(sim)
    sim.robot.place(2.0, dy)
    sid, first = st.open(start_id=gss.START)
    st.move(sid, gss.START)
    return {"first_step": first, "navs": len(st.speeds)}


def _moved(r):
    return (r["navs"] >= 1 and r["pos"] == [2, 0] and r["errors"] == 0) or f"NAV {r['navs']}개 위치 {r['pos']}"


def _skipped(r):
    return (r["navs"] == 0 and r["errors"] == 0) or f"NAV {r['navs']}개 위치 {r['pos']}"


@scenario("C1 [재현] 시작 장소에서 1.49m(3F 로비입구) — 시작 장소로 이동", expect=_moved)
def c1(sim):
    return _start_from(sim, -1.49)


@scenario("C2 [변경] 1.2m — 이동", expect=_moved)
def c2(sim):
    return _start_from(sim, -1.2)


@scenario("C3 [회귀] 0.9m(손으로 세운 오차) — 생략", expect=_skipped)
def c3(sim):
    return _start_from(sim, -0.9)


@scenario("C4 [회귀] 0.4m — 생략", expect=_skipped)
def c4(sim):
    return _start_from(sim, -0.4)


@scenario("C5 [회귀] 멀리(7m) — 이동", expect=_moved)
def c5(sim):
    st = gss.Stage(sim)
    sim.robot.place(9, 0)
    sid, _ = st.open(start_id=gss.START)
    st.move(sid, gss.START)
    return {"navs": len(st.speeds)}


@scenario("C6 [회귀] 안내 단계(guide_move)는 0.5m 기준 그대로 — 0.8m 앞이면 이동", expect=_moved)
def c6(sim):
    st = gss.Stage(sim)
    sim.robot.place(2.0, -0.8)
    sid, _ = st.open()
    st.set_step(sid, "guide_move")
    st.move(sid, gss.START)
    return {"navs": len(st.speeds)}
