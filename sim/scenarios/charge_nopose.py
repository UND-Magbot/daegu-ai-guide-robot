"""위치 미확정 충전 복귀(2026-10-23) — 노드망 경로가 켜져 있으면 위치를 모르는 채로 충전소로 떠나지 않는다.

변경:
- charge._return_to_charge_internal: NODE_ROUTING_ENABLED 이고 위치 미확정이면 거절(reason=no_pose, 위치 재조정 안내).
  노드망 미완성 맵의 배터리 직행도 같다. 노드망이 꺼져 있으면 지금처럼 시도한다.
- send_move._expand_node_leg: 위치 미확정이면 충전 통로 목표도 노드 없이 보내지 않고 멈춘다.
- auto_return: 배터리 기준 이하인데 위치 때문에 못 떠났으면 알림을 남기고, 위치가 다시 잡히면 쿨다운(10분)을
  기다리지 않고 바로 다시 시도한다. 못 떠났을 때는 "충전하러 갑니다" 멘트를 하지 않는다.

무대는 node_route.py 의 Stage(병원 1층 배치).
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "node_route_base_np", os.path.join(os.path.dirname(__file__), "node_route.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
Stage, ON, RID, ev, _battery = _base.Stage, _base.ON, _base.RID, _base.ev, _base._battery

OFF = {**ON, "NODE_ROUTING_ENABLED": "false"}


def _lose_pose(sim):
    """측위 상실 주입. 하네스의 get_position 은 가짜 로봇 좌표만 돌려주므로 미확정 표시를 덧붙인다."""
    rt = sim.rt
    with rt._lock:   # 빈 dict 면 set_initpose_pending 이 무시한다
        rt._runtime.setdefault(RID, {}).update({"robot_id": RID, "robot_name": "SIM"})
    if not getattr(rt.get_position, "_np", False):
        base = rt.get_position

        def get_position(rid):
            return {**base(rid), "initpose_pending": rt.is_initpose_pending(rid)}
        get_position._np = True
        rt.get_position = get_position
    rt.set_initpose_pending(RID, True, rt.LOCALIZATION_LOST_REASON)


def _manual_return(sim):
    out = {}

    def go():
        out.update(sim.ch._return_to_charge_internal(cancel_running=False))
        sim.pl.nav_thread()
    sim.run(go, until=lambda: sim.robot.charging or sim.clock.elapsed() > 120)
    return out


def _spoke(r, text):
    return any(text in str(c) for c in r["cmds"])


@scenario("N1 [변경] 노드망 켬 · 위치 미확정 · 충전 복귀 버튼 → 떠나지 않고 '위치 재조정' 안내", env=ON,
          expect=lambda r: r["accepted"] is False and "위치" in (r["msg"] or "") and not r["charging"] and not r["targets"])
def n1(sim):
    st = Stage(sim)
    sim.robot.place(8.8, 1.2)
    _lose_pose(sim)
    out = _manual_return(sim)
    return st.result(accepted=out.get("ok"), msg=out.get("msg"))


@scenario("N2 [변경] 노드망 켬 · 이동 중 위치 상실 · 배터리 기준 이하 → 멈추고 알림, '충전하러 갑니다' 멘트 없음", env=ON,
          expect=lambda r: not r["charging"] and ev(r, "battery_low_no_pose") and not r["spoke_go"]
          and not r["after_low"] and not r["moving"])
def n2(sim):
    st = Stage(sim)
    sim.robot.place(2.0, 5.0)
    _base._start_route(sim, st, "원무과 접수", lambda: sim.clock.elapsed() >= 12)
    k = len(st.targets)
    _lose_pose(sim)
    import app.navigation.arrival_gate as ag
    spoken = []
    orig = ag.speak_only
    ag.speak_only = lambda rid, text, *a, **kw: (spoken.append(text), orig(rid, text, *a, **kw))[1]

    def trigger():
        _battery(sim, 12, n=3)
        sim.pl.nav_thread()
    try:
        sim.run(trigger, until=lambda: sim.robot.charging or sim.clock.elapsed() > 60)
    finally:
        ag.speak_only = orig
    return st.result(spoke_go=any("충전하러 갑니다" in t for t in spoken), spoken=spoken,
                     after_low=st.targets[k:], moving=bool(sim.sm.is_navigating))


@scenario("N3 [변경] 위치 미확정으로 못 떠난 뒤 운영자가 위치 재조정 → 다음 heartbeat 에 바로 충전소로(10분 쿨다운 안 기다림)",
          env=ON, expect=lambda r: not r["charging_while_lost"] and r["charging"] and r["wall_cross"] == 0
          and r["after_fix_sec"] < 200)   # 쿨다운이면 600s+, 주행 시간 포함
def n3(sim):
    st = Stage(sim)
    sim.robot.place(8.8, 1.2)
    _lose_pose(sim)

    def trigger():
        _battery(sim, 12, n=3)
        sim.pl.nav_thread()
    sim.run(trigger, until=lambda: sim.robot.charging or sim.clock.elapsed() > 30)
    lost = sim.robot.charging
    sim.rt.set_initpose_pending(RID, False)
    t0 = sim.clock.elapsed()

    def again():
        _battery(sim, 12, n=1)
        sim.pl.nav_thread()
    sim.run(again, until=lambda: sim.robot.charging or sim.clock.elapsed() - t0 > 900)
    return st.result(charging_while_lost=lost, after_fix_sec=sim.clock.elapsed() - t0)


@scenario("N4 [변경] 노드 없는 맵 · 위치 미확정 · 배터리 기준 이하 → 직행도 하지 않고 멈춤", env=ON,
          expect=lambda r: not r["charging"] and not r["targets"] and ev(r, "battery_low_no_pose"))
def n4(sim):
    st = Stage(sim, nodes={})
    sim.robot.place(2.0, 5.0)
    _lose_pose(sim)

    def trigger():
        _battery(sim, 12, n=3)
        sim.pl.nav_thread()
    sim.run(trigger, until=lambda: sim.robot.charging or sim.clock.elapsed() > 60)
    return st.result()


@scenario("N5 [회귀] 노드망 켬 · 위치 확정 · 충전 복귀 → 노드망으로 접근점까지, 통로로 도킹", env=ON,
          expect=lambda r: r["charging"] and r["wall_cross"] == 0 and r["targets"][-2:] == ["충전소-2", "충전소-1"])
def n5(sim):
    st = Stage(sim)
    sim.robot.place(8.8, 1.2)
    _manual_return(sim)
    return st.result()


@scenario("N6 [회귀] 노드 없는 맵 · 위치 확정 · 배터리 기준 이하 → 직행으로 충전소", env=ON,
          expect=lambda r: r["charging"] and ev(r, "auto_return_triggered"))
def n6(sim):
    st = Stage(sim, nodes={})
    sim.robot.place(2.0, 5.0)

    def trigger():
        _battery(sim, 12, n=3)
        sim.pl.nav_thread()
    sim.run(trigger, until=lambda: sim.robot.charging or sim.clock.elapsed() > 120)
    return st.result()


@scenario("N7 [회귀] 노드망 꺼짐 · 위치 미확정 · 충전 복귀 → 지금처럼 시도해서 충전", env=OFF,
          expect=lambda r: r["charging"])
def n7(sim):
    st = Stage(sim)
    sim.robot.place(8.8, 1.2)
    _lose_pose(sim)
    _manual_return(sim)
    return st.result()


@scenario("N8 [변경] 안내 종료 뒤 배터리 복귀(scheduler._go_charge) · 위치 미확정 → 알림, 재조정되면 바로 충전소로",
          env=ON, expect=lambda r: r["pose_wait"] and ev(r, "battery_low_no_pose") and not r["charging_while_lost"]
          and r["charging"] and r["after_fix_sec"] < 200)
def n8(sim):
    st = Stage(sim)
    sim.robot.place(8.8, 1.2)
    _lose_pose(sim)
    import app.robot_control.auto_return as ar
    import app.scheduler.loop as lp
    sim.run(lambda: lp._go_charge("배터리 기준 이하", low_battery=True),
            until=lambda: sim.robot.charging or sim.clock.elapsed() > 20)
    lost = sim.robot.charging
    pose_wait = bool(ar._state.get(RID, {}).get("pose_wait"))
    sim.rt.set_initpose_pending(RID, False)
    t0 = sim.clock.elapsed()

    def again():
        _battery(sim, 12, n=3)
        sim.pl.nav_thread()
    sim.run(again, until=lambda: sim.robot.charging or sim.clock.elapsed() - t0 > 900)
    return st.result(pose_wait=pose_wait, charging_while_lost=lost, after_fix_sec=sim.clock.elapsed() - t0)
