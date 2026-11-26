"""배터리 부족 안내 중단 알림 — 멈춘 곳과 가장 가까운 장소 이름을 싣는다 (현행 vs 변경).

화면 알림창(GuideBatteryAbortAlertWatcher)이 이 알림의 본문·상세를 읽어 직원에게 보여 준다.
무대는 battery_policy.py 의 BatStage(로봇 10, 로비 대기 (6,0), 내과 (6,6)).
"""
import importlib.util
import os
import re

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "battery_policy_base_alert", os.path.join(os.path.dirname(__file__), "battery_policy.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
BatStage, _no_err = _base.BatStage, _base._no_err

# 화면 parseBatteryAbort 와 같은 규칙
_WHERE = re.compile(r"멈춘 곳=(.+?)\s+(?:가까운 곳=|step=)")
_NEAR = re.compile(r"가까운 곳=(.+?)\s+step=")
_DEST = re.compile(r"못 간 목적지=(.+)$")


def _capture(st):
    """하네스 log_event 는 본문만 남긴다 — 이 알림의 상세(detail)까지 받아 둔다."""
    import app.scheduler.loop as lp
    inner = lp.log_event
    st.alerts = []

    def rec(*a, **k):
        if len(a) > 1 and a[1] == "guide_battery_aborted":
            st.alerts.append((a[2] if len(a) > 2 else k.get("message"), k.get("detail")))
        return inner(*a, **k)
    lp.log_event = rec


def _alert(st):
    return st.alerts[-1] if st.alerts else (None, None)


def _parsed(msg, detail):
    d = (detail or "").strip()
    g = lambda rx: (m.group(1) if (m := rx.search(d)) else None)
    return {"msg": msg, "detail": detail, "where": g(_WHERE), "near": g(_NEAR), "dest": g(_DEST)}


def _guide_abort(sim, place_id):
    st = BatStage(sim)
    _capture(st)
    st.arrive_standby()
    sid = st.operator_start()
    db = st.Session()
    try:
        st.vs.session_move(sid, place_id=place_id, db=db, _="sim")
        # 목적지 이름은 Jetson 이 세션에 채운다 — 하네스에는 Jetson 이 없어 직접 넣는다
        db.query(st.M.ServiceSession).filter(st.M.ServiceSession.id == sid).update(
            {st.M.ServiceSession.DestinationName: "내과"})
        db.commit()
    finally:
        db.close()
    for k in range(3):
        sim.at(1.5 + k, lambda: st.low(17))
    st.drive(until=lambda: not st.sm.is_navigating, max_sec=30)
    st.settle()
    return st


@scenario("A1 내과로 가다 배터리 중단 → 알림에 가까운 장소 이름·거리, 멈춘 곳 좌표·목적지는 그대로",
          expect=lambda r: _no_err(r) and r["near"] and re.match(r".+에서 \d+\.\dm$", r["near"])
          and r["where"].startswith("(") and r["dest"] == "내과" and r["near"] in r["msg"])
def a1(sim):
    st = _guide_abort(sim, 2)
    return st.out(**_parsed(*_alert(st)))


@scenario("A2 로봇 맵을 모르면 가까운 곳은 '-' (좌표는 그대로)",
          expect=lambda r: _no_err(r) and r["near"] in (None, "-") and r["where"].startswith("(")
          and r["dest"] == "내과")
def a2(sim):
    st = BatStage(sim)
    _capture(st)
    db = st.Session()
    try:
        db.query(st.M.RobotInfo).update({st.M.RobotInfo.CurrentMapId: None})
        db.commit()
    finally:
        db.close()
    st.arrive_standby()
    sid = st.operator_start()
    db = st.Session()
    try:
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
        # 목적지 이름은 Jetson 이 세션에 채운다 — 하네스에는 Jetson 이 없어 직접 넣는다
        db.query(st.M.ServiceSession).filter(st.M.ServiceSession.id == sid).update(
            {st.M.ServiceSession.DestinationName: "내과"})
        db.commit()
    finally:
        db.close()
    for k in range(3):
        sim.at(1.5 + k, lambda: st.low(17))
    st.drive(until=lambda: not st.sm.is_navigating, max_sec=30)
    st.settle()
    return st.out(**_parsed(*_alert(st)))
