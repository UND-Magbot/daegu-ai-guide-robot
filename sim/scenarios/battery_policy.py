"""배터리 정책 한 단계(2026-10-22) — 현행 vs 변경.

변경: 로봇별 최소 배터리(LimitBattery) 하나. 3회 연속 확인되면 안내·작업·대기 무엇이든 바로 멈추고 충전소로.
  안내 중이면 그 자리에 서서 사과·안내 데스크 멘트를 끝까지 한 뒤 닫고 떠난다.
  충전 후(10-16 규칙): 상시 대기는 근무를 남기고 80% 에 대기 장소로 복귀, 화면 경로 반복은 80% 에 남은 횟수 이어 가기,
  예약 작업은 회차를 battery_low 로 끝내고 다음 회차는 80% 이후. 층 전환·자동문 통과 중이면 끝난 뒤. 기준 0 = 끔.
현행: 기준 이하면 작업은 회차를 마치고 복귀, 안내 중이면 15% 미만일 때만 다음 이동 전 중단 요청(D10).

무대는 standby_scenario.py 의 Stage(로봇 10, 로비 대기 (6,0), 내과 (6,6), 충전소 이동은 기록만).
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "standby_scenario_base_bat", os.path.join(os.path.dirname(__file__), "standby_scenario.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
Stage, ROBOT, CLINIC, _no_err = _base.Stage, _base.ROBOT, _base.CLINIC, _base._no_err

FAR = (20.0, 0.0)


class BatStage(Stage):
    """Stage + 기준 값 지정·멘트 기록·충전 호출 시각/위치 기록."""

    def __init__(self, sim, limit=17, **kw):
        super().__init__(sim, **kw)
        self.rt._runtime[ROBOT]["limit_battery"] = limit
        self.speech: list[tuple[float, list, str]] = []
        self.charge_at: list[tuple[float, list]] = []
        import app.navigation.arrival_gate as ag
        ag.speak_only = lambda rid, text: self.speech.append((sim.clock.elapsed(), self.pos(), text))
        import app.robot_control.charge as ch
        inner = ch._return_to_charge_internal

        def rec(*a, **k):
            self.charge_at.append((sim.clock.elapsed(), self.pos()))
            return inner(*a, **k)
        ch._return_to_charge_internal = rec

    def low(self, pct, n=1):
        for _ in range(n):
            self.battery(pct)

    def settle(self, sec=20.0):
        """배터리 처리 스레드(멘트 대기 포함)가 끝날 시간."""
        self.sim.wait(sec)
        if self.sm.is_navigating:
            self.drive(max_sec=30)

    def far_move(self, max_sec=60):
        """근무 밖 일반 이동(경로·장소 이동) — 20m 앞 목적지까지 nav_thread 로 돌린다."""
        sim, sm = self.sim, self.sm
        sm.waypoints_list = [{"x": FAR[0], "y": FAR[1], "yaw": 0.0, "name": "먼 곳", "map_id": None}]
        sm.current_wp_index, sm.is_navigating = 0, True
        sm._signal_nav_reset(full=True)
        t0 = sim.clock.elapsed()
        sim.run(lambda: (sm.navigation_send_next(), sim.pl.nav_thread()),
                until=lambda: not sm.is_navigating or sim.clock.elapsed() - t0 > max_sec, grace=1.0)

    def run_result(self):
        db = self.Session()
        try:
            r = db.query(self.M.ScheduleRun).order_by(self.M.ScheduleRun.id.desc()).first()
            return r.Result if r else None
        finally:
            db.close()

    def res(self, **extra):
        sp = self.speech
        guide_sp = next((s for s in sp if "안내 데스크" in s[2]), None)
        ch = self.charge_at[0] if self.charge_at else None
        return self.out(speech=len(sp), guide_speech=bool(guide_sp),
                        # 안내 멘트 뒤 충전 출발까지 걸린 시간(멘트가 끝난 뒤 떠나는지)
                        speech_to_charge=round(ch[0] - guide_sp[0], 1) if (guide_sp and ch) else None,
                        moved_during_speech=bool(guide_sp and ch and guide_sp[1] != ch[1]),
                        charge_at=[c[1] for c in self.charge_at], moving=self.sm.is_navigating,
                        run=self.run_result(), **extra)


# ── 판정 ─────────────────────────────────────────────

@scenario("B1 대기 중 최소 기준(17%) 이하 3회 → 충전소 1회",
          expect=lambda r: _no_err(r) and r["charge_calls"] == 1)
def b1(sim):
    st = BatStage(sim)
    st.low(17, 3)
    st.settle(2)
    return st.res()


@scenario("B2 기준 이하 1회 뒤 80%(오독) → 충전소로 안 감",
          expect=lambda r: _no_err(r) and r["charge_calls"] == 0)
def b2(sim):
    st = BatStage(sim)
    st.low(15)
    st.low(80)
    st.settle(2)
    return st.res()


@scenario("B3 일반 이동 중(20m 앞) 기준 이하 → 회차 기다리지 않고 바로 멈추고 충전소, 멘트",
          expect=lambda r: _no_err(r) and r["charge_calls"] == 1 and r["charge_at"][0][0] < 10
          and r["speech"] == 1)
def b3(sim):
    st = BatStage(sim)
    for k in range(3):
        sim.at(4.0 + k, lambda: st.low(16))
    st.far_move()
    st.settle(2)
    return st.res()


@scenario("B4 안내 이동 중 17%(기준 17) → 그 자리 정지·사과+안내 데스크 멘트 끝난 뒤 안내 닫고 충전소, 근무는 '충전소로 이동 중'",
          expect=lambda r: _no_err(r) and r["sess"]["Result"] == "aborted_battery" and r["charge_calls"] == 1
          and r["guide_speech"] and r["speech_to_charge"] >= 9 and not r["moved_during_speech"]
          and r["shift_alive"] and "충전소로 이동 중" in (r["away"] or "") and r["pos"] != list(CLINIC))
def b4(sim):
    st = BatStage(sim)
    st.arrive_standby()
    sid = st.operator_start()
    db = st.Session()
    try:
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
    finally:
        db.close()
    for k in range(3):
        sim.at(1.5 + k, lambda: st.low(17))
    st.drive(until=lambda: not st.sm.is_navigating, max_sec=30)
    st.settle()
    return st.res(sess=st.session(sid))


@scenario("B5 안내 중 서 있을 때(운영자 입력 대기) 12% → 멘트 뒤 바로 닫고 충전소(3분 기다리지 않음)",
          expect=lambda r: _no_err(r) and r["sess"]["Result"] == "aborted_battery" and r["charge_calls"] == 1
          and r["guide_speech"] and r["sim_wait"] < 30 and r["shift_alive"])
def b5(sim):
    st = BatStage(sim, limit=30)
    st.arrive_standby()
    sid = st.operator_start()
    st.jetson_move(sid)
    t0 = sim.clock.elapsed()
    st.low(12, 3)
    st.settle()
    if st.session(sid)["Result"] != "aborted_battery":      # 현행: 3분 안전장치까지 기다린다
        st.stale(185)
        st.after_close()
        sim.wait(185)
    return st.res(sess=st.session(sid), sim_wait=round(sim.clock.elapsed() - t0, 1))


@scenario("B6 근무 중 대기 장소로 가는 중 기준 이하 → 근무 유지('충전소로 이동 중'), 충전소",
          expect=lambda r: _no_err(r) and r["shift_alive"] and "충전소로 이동 중" in (r["away"] or "")
          and r["charge_calls"] == 1 and r["charge_at"][0] != [6.0, 0.0])
def b6(sim):
    st = BatStage(sim)
    st.start_shift()
    for k in range(3):
        sim.at(1.5 + k, lambda: st.low(16))
    st.drive(max_sec=40)
    st.settle(2)
    return st.res()


@scenario("B7 층 전환 중 기준 이하 → 미룸(알림 1건), 끝난 뒤 충전소",
          expect=lambda r: _no_err(r) and r["during"] == 0 and r["charge_calls"] == 1
          and r["deferred_alerts"] == 1)
def b7(sim):
    st = BatStage(sim)
    sim.ft.is_transitioning = lambda rid: True
    st.low(16, 5)
    st.settle(2)
    during = st.charge_calls
    sim.ft.is_transitioning = lambda rid: False
    st.low(16)
    st.settle(2)
    return st.res(during=during, deferred_alerts=st.count("battery_low_deferred"))


@scenario("B8 한 번 떠난 뒤 계속 낮아도(쿨다운)·충전 중이어도 다시 안 보냄",
          expect=lambda r: _no_err(r) and r["charge_calls"] == 1)
def b8(sim):
    st = BatStage(sim)
    st.low(16, 3)
    st.low(15, 5)
    st.rt._runtime[ROBOT]["_is_charging"] = True
    st.low(14, 3)
    st.settle(2)
    return st.res()


@scenario("B9 최소 배터리 0(끔) → 12% 여도 충전소로 안 감",
          expect=lambda r: _no_err(r) and r["charge_calls"] == 0)
def b9(sim):
    st = BatStage(sim, limit=0)
    st.low(12, 3)
    st.settle(2)
    return st.res()


@scenario("B10 최소 배터리 비어 있음(DB 직접 입력) → 30% 로 판정",
          expect=lambda r: _no_err(r) and r["charge_calls"] == 1)
def b10(sim):
    st = BatStage(sim, limit=None)
    st.low(25, 3)
    st.settle(2)
    return st.res()


@scenario("B11 [회귀] 기준 위(50%) 안내·이동 → 아무 일 없음",
          expect=lambda r: _no_err(r) and r["charge_calls"] == 0 and r["sess"]["Result"] in ("running", "paused"))
def b11(sim):
    st = BatStage(sim)
    st.arrive_standby()
    sid = st.operator_start()
    st.jetson_move(sid)
    st.low(50, 5)
    st.settle(2)
    return st.res(sess=st.session(sid))


@scenario("B12 로봇 상태: 충전소로 가는 중이면 returning_to_charge, 일반 이동·정지면 아님",
          expect=lambda r: _no_err(r) and r["flags"] == [True, False, False])
def b12(sim):
    st = BatStage(sim)
    sm, rt = st.sm, st.rt
    flags = []
    for nav, charge in ((True, True), (True, False), (False, True)):
        sm.is_navigating, sm.charge_on_arrival = nav, charge
        flags.append(rt._build_status(rt._runtime[ROBOT] | {"robot_id": ROBOT, "robot_name": "SIM",
                                                           "last_heartbeat": 0, "position": {}})
                     .get("returning_to_charge"))
    sm.is_navigating, sm.charge_on_arrival = False, False
    return st.res(flags=flags)


# ── 작업(순찰·경로) — 근무와 같은 방향(3차) ──────────────

def _patrol(st, sid=3, mode="once"):
    """진행 중인 순찰 회차를 만든다(스케줄러가 시작한 것과 같은 전역 상태)."""
    from datetime import datetime, timedelta
    from app.scheduler import runs
    M, lp = st.M, st.lp
    now = datetime.now()
    kw = dict(id=sid, RobotName="SIM", WorkName="순찰", TaskType="patrol", TaskStatus="진행중",
              StartDate=now - timedelta(minutes=1), EndDate=now + timedelta(hours=8), ScheduleMode=mode,
              WayName="경로")
    if mode == "interval":
        kw.update(Repeat="Y", IntervalMinutes=60, ActiveStartTime="00:00", ActiveEndTime="23:59")
    db = st.Session()
    try:
        db.add(M.ScheduleInfo(**kw))
        db.commit()
        sched = db.query(M.ScheduleInfo).filter(M.ScheduleInfo.id == sid).first()
        run_id = runs.begin_run(sched, now - timedelta(minutes=1), now)
    finally:
        db.close()
    lp._active_schedule_id, lp._active_run_id, lp._active_started_at = sid, run_id, now


def _sched(st, sid):
    db = st.Session()
    try:
        s = st.db_sched = db.query(st.M.ScheduleInfo).filter(st.M.ScheduleInfo.id == sid).first()
        runs = db.query(st.M.ScheduleRun).filter(st.M.ScheduleRun.ScheduleId == sid).all()
        cancel_rows = db.query(st.M.ScheduleInfo).filter(st.M.ScheduleInfo.TaskStatus == "취소").count()
        return {"status": s.TaskStatus, "exceptions": s.SeriesExceptions, "paused": s.PausedUntil is not None,
                "runs": [r.Result for r in runs], "cancel_rows": cancel_rows}
    finally:
        db.close()


@scenario("B13 반복 순찰 중 기준 이하 → 회차 battery_low, 작업 '대기'(오늘 남은 회차 유지), 충전소",
          expect=lambda r: _no_err(r) and r["s"]["runs"] == ["battery_low"] and r["s"]["status"] == "대기"
          and not r["s"]["exceptions"] and r["s"]["cancel_rows"] == 0 and r["charge_calls"] == 1)
def b13(sim):
    st = BatStage(sim)
    _patrol(st, mode="interval")
    for k in range(3):
        sim.at(4.0 + k, lambda: st.low(16))
    st.far_move()
    st.settle(2)
    return st.res(s=_sched(st, 3))


@scenario("B14 단일 순찰 중 기준 이하 → 회차 battery_low, 작업 '완료'(다시 시작 안 함), 충전소",
          expect=lambda r: _no_err(r) and r["s"]["runs"] == ["battery_low"] and r["s"]["status"] == "완료"
          and r["charge_calls"] == 1)
def b14(sim):
    st = BatStage(sim)
    _patrol(st, mode="once")
    for k in range(3):
        sim.at(4.0 + k, lambda: st.low(16))
    st.far_move()
    st.settle(2)
    return st.res(s=_sched(st, 3))


@scenario("B15 시작 조건(기준 17%): 평소엔 17% 보류·18% 시작, 배터리 부족 복귀 뒤엔 79% 보류·80% 시작",
          expect=lambda r: _no_err(r) and r["at17"] == [] and r["at18"] != []
          and r["low79"] == [] and r["low80"] != [])
def b15(sim):
    from datetime import datetime, timedelta
    from app.scheduler import runs
    import app.robot_control.auto_return as ar
    st = BatStage(sim)
    db = st.Session()
    now = datetime.now()
    out = {}
    for sid, key, pct, low in ((4, "at17", 17, False), (4, "at18", 18, False),
                               (5, "low79", 79, True), (5, "low80", 80, True)):
        if not db.query(st.M.ScheduleInfo).filter(st.M.ScheduleInfo.id == sid).first():
            db.add(st.M.ScheduleInfo(id=sid, RobotName="SIM", WorkName="순찰", TaskType="patrol", TaskStatus="대기",
                                     StartDate=now - timedelta(seconds=20), EndDate=now + timedelta(hours=1),
                                     ScheduleMode="once", WayName="없는경로"))
            db.commit()
        if low and hasattr(ar, "mark_low_return"):
            ar.mark_low_return(ROBOT)
        st.rt._runtime[ROBOT]["battery"] = {"SOC": pct}
        sched = db.query(st.M.ScheduleInfo).filter(st.M.ScheduleInfo.id == sid).first()
        sim.run(lambda: st.lp._try_execute(db, sched, datetime.now(), runs.grace_minutes(sched)),
                until=lambda: True, grace=0.5)
        out[key] = _sched(st, sid)["runs"]
    db.close()
    return st.res(**out)


@scenario("B16 시작 조건에 막힌 채 허용 지연이 지나면 → 회차 battery_low(미실행 아님)",
          expect=lambda r: _no_err(r) and r["runs"] == ["battery_low"])
def b16(sim):
    from datetime import datetime, timedelta
    from app.scheduler import runs
    st = BatStage(sim)
    db = st.Session()
    now = datetime.now()
    db.add(st.M.ScheduleInfo(id=5, RobotName="SIM", WorkName="순찰", TaskType="patrol", TaskStatus="대기",
                             StartDate=now - timedelta(seconds=20), EndDate=now + timedelta(hours=1),
                             ScheduleMode="once", WayName="없는경로"))
    db.commit()
    st.lp._watch_since = now - timedelta(hours=1)
    st.rt._runtime[ROBOT]["battery"] = {"SOC": 16}
    sched = db.query(st.M.ScheduleInfo).filter(st.M.ScheduleInfo.id == 5).first()
    grace = runs.grace_minutes(sched)
    sim.run(lambda: st.lp._try_execute(db, sched, now, grace), until=lambda: True, grace=0.5)
    st.lp._record_expired(db, sched, now + timedelta(minutes=grace + 5), grace)
    db.close()
    return st.res(runs=_sched(st, 5)["runs"])


@scenario("B17 battery_low 3회 연속 → 자동 일시정지 안 함, 실행률엔 실패로 들어감(3건 중 0건 실행)",
          expect=lambda r: _no_err(r) and not r["paused"] and r["summary"]["planned"] == 3
          and r["summary"]["executed"] == 0)
def b17(sim):
    from datetime import datetime, timedelta
    from unittest.mock import MagicMock
    from app.scheduler import runs
    import app.database.routes.schedule_run as sr
    st = BatStage(sim)
    db = st.Session()
    now = datetime.now()
    db.add(st.M.ScheduleInfo(id=6, RobotName="SIM", WorkName="순찰", TaskType="patrol", TaskStatus="대기",
                             StartDate=now, EndDate=now + timedelta(hours=1), ScheduleMode="once", WayName="w"))
    for k in range(3):
        db.add(st.M.ScheduleRun(ScheduleId=6, WorkName="순찰", RobotName="SIM",
                                PlannedAt=now - timedelta(hours=3 - k), Result="battery_low"))
    db.commit()
    runs._check_circuit_breaker(db, 6, "순찰")
    sr.is_admin = lambda u: True
    out = sr.get_schedule_runs(days=1, robot=None, result=None, schedule_id=6, page=1, size=20,
                               db=db, current_user=MagicMock())
    paused = _sched(st, 6)["paused"]
    db.close()
    return st.res(paused=paused, summary={k: out["summary"][k] for k in ("planned", "executed", "rate")})


# ── 충전 후 이어 가기(10-16 규칙) ────────────────────────

def _charge(st, pct):
    """도킹해 충전 중 — pct% 로 heartbeat 한 번."""
    st.rt._runtime[ROBOT]["_is_charging"] = True
    st.low(pct)


@scenario("B18 근무 중 배터리 부족 → 도킹 후 '충전 중 — 80%…' → 80% 에 대기 장소로 돌아가 근무 이어 감",
          expect=lambda r: _no_err(r) and r["away_docked"] == "충전 중 — 80%가 되면 대기 장소로 복귀"
          and r["at79_moving"] is False and r["phase"] == "standby" and r["pos"] == [6.0, 0.0]
          and r["shift_alive"])
def b18(sim):
    st = BatStage(sim)
    st.arrive_standby()
    sim.robot.place(1.0, 0.0)          # 충전소 자리(가짜 충전 복귀는 로봇을 옮기지 않는다)
    st.low(16, 3)
    st.settle(2)
    _charge(st, 40)
    away_docked = (st.lp.get_standby_state() or {}).get("away_label")
    _charge(st, 79)
    st.settle(2)
    at79 = st.sm.is_navigating
    _charge(st, 80)
    st.settle(2)
    st.rt._runtime[ROBOT]["_is_charging"] = False
    st.drive(until=lambda: st.phase() == "standby", max_sec=40)
    return st.res(away_docked=away_docked, at79_moving=at79)


@scenario("B19 화면 경로 3회 반복 중 2바퀴째 배터리 부족 → 충전 → 80% 에 남은 2회 이어 가기",
          expect=lambda r: _no_err(r) and r["resumed"] == {"count": 2, "remaining": 1, "total": 3, "nav": True})
def b19(sim):
    st = BatStage(sim)
    sm = st.sm
    sm.waypoints_list = [{"x": 10.0, "y": 0.0, "yaw": 0.0, "name": "A", "map_id": None},
                         {"x": 10.0, "y": 10.0, "yaw": 0.0, "name": "B", "map_id": None}]
    sm.current_wp_index, sm.is_navigating = 0, True
    sm.nav_loop_infinite, sm.nav_loop_total, sm.nav_loop_remaining, sm.nav_loop_count = False, 3, 1, 2
    sm._signal_nav_reset(full=True)
    for k in range(3):
        sim.at(3.0 + k, lambda: st.low(16))
    sim.run(lambda: (sm.navigation_send_next(), sim.pl.nav_thread()),
            until=lambda: not sm.is_navigating and sim.clock.elapsed() > 6, grace=1.0)
    st.settle(2)
    _charge(st, 80)
    st.settle(2)
    return st.res(resumed={"count": sm.nav_loop_count, "remaining": sm.nav_loop_remaining,
                           "total": sm.nav_loop_total, "nav": sm.is_navigating})


def _route_loop(st, sim, total=3, remaining=1, count=2):
    """화면 경로 N회 반복 중 배터리 부족 → 충전소로(가짜 충전 복귀)."""
    sm = st.sm
    sm.waypoints_list = [{"x": 10.0, "y": 0.0, "yaw": 0.0, "name": "A", "map_id": None},
                         {"x": 10.0, "y": 10.0, "yaw": 0.0, "name": "B", "map_id": None}]
    sm.current_wp_index, sm.is_navigating = 0, True
    sm.nav_loop_infinite, sm.nav_loop_total, sm.nav_loop_remaining, sm.nav_loop_count = False, total, remaining, count
    sm._signal_nav_reset(full=True)
    for k in range(3):
        sim.at(3.0 + k, lambda: st.low(16))
    sim.run(lambda: (sm.navigation_send_next(), sim.pl.nav_thread()),
            until=lambda: not sm.is_navigating and sim.clock.elapsed() > 6, grace=1.0)
    st.settle(2)


def _br():
    try:
        import app.robot_control.battery_resume as br
        return br
    except ImportError:
        return None


def _decide(decision):
    br = _br()
    if not br:
        return "모듈 없음"
    from app.user_cache import get_robot_id
    p = br.current(get_robot_id())
    if not p:
        return "요청 없음"
    return br.decide(get_robot_id(), p["token"], decision)


@scenario("B20 충전 중 누가 도크에서 뺐다가 다시 충전 → 80% 에 출발하지 않고 관제 확인창, [이어 가기] 누르면 출발",
          expect=lambda r: _no_err(r) and r["nav_before_ok"] is False and r["confirm_alerts"] == 1
          and r["nav_after_ok"] is True)
def b20(sim):
    st = BatStage(sim)
    _route_loop(st, sim, total=3, remaining=2, count=1)
    _charge(st, 40)
    st.rt._runtime[ROBOT]["_is_charging"] = False      # 운영자가 도크에서 뺐다
    st.low(40)
    _charge(st, 80)
    _charge(st, 81)
    st.settle(2)
    before = st.sm.is_navigating
    _decide("resume")
    _charge(st, 82)
    st.settle(2)
    return st.res(nav_before_ok=before, confirm_alerts=st.count("battery_resume_confirm"),
                  nav_after_ok=st.sm.is_navigating)


@scenario("B21 충전소로 갈 때 관제 확인 요청 1건(근무·경로 반복·예약 작업 각각), 대기 중엔 묻지 않음",
          expect=lambda r: _no_err(r) and r["asks"] == {"standby": 1, "route": 1, "task": 1, "idle": 0})
def b21(sim):
    asks = {}
    st = BatStage(sim)
    for kind in ("standby", "route", "task", "idle"):
        st.lp._standby = None
        st.lp._active_schedule_id = None
        br = _br()
        if br:
            br._pending.clear()
        n0 = st.count("battery_resume_ask")
        st.ar._state.clear()
        st.rt._runtime[ROBOT]["_is_charging"] = False
        if kind == "standby":
            st.start_shift()
            st.low(16, 3)
            st.settle(2)
        elif kind == "route":
            _route_loop(st, sim)
        elif kind == "task":
            _patrol(st, sid=30 + len(asks))
            st.low(16, 3)
            st.settle(2)
        else:
            st.low(16, 3)
            st.settle(2)
        asks[kind] = st.count("battery_resume_ask") - n0
        st.sm.stop_navigation_internal("시나리오 정리", keep_schedule=True)
    return st.res(asks=asks)


@scenario("B22 경로 반복 [오늘은 여기서 종료] → 80% 가 돼도 이어 가지 않음",
          expect=lambda r: _no_err(r) and r["msg"] == "오늘은 여기서 종료합니다" and r["nav"] is False)
def b22(sim):
    st = BatStage(sim)
    _route_loop(st, sim)
    msg = _decide("end")
    _charge(st, 80)
    st.settle(2)
    return st.res(msg=msg, nav=st.sm.is_navigating)


@scenario("B23 근무 [오늘은 여기서 종료] → 근무 battery_low 로 끝, 80% 에 대기 장소로 안 감",
          expect=lambda r: _no_err(r) and not r["shift_alive"] and r["run"] == "battery_low" and r["nav"] is False)
def b23(sim):
    st = BatStage(sim)
    st.arrive_standby()
    sim.robot.place(1.0, 0.0)
    st.low(16, 3)
    st.settle(2)
    _decide("end")
    _charge(st, 80)
    st.settle(2)
    return st.res(nav=st.sm.is_navigating)


def _b24(sim, decision):
    from datetime import datetime, timedelta
    from app.scheduler import runs
    st = BatStage(sim)
    _patrol(st, sid=40, mode="interval")
    st.low(16, 3)
    st.settle(2)
    if decision:
        _decide(decision)
    _charge(st, 85)
    st.rt._runtime[ROBOT]["_is_charging"] = False
    db = st.Session()
    sched = db.query(st.M.ScheduleInfo).filter(st.M.ScheduleInfo.id == 40).first()
    later = datetime.now() + timedelta(hours=1)
    nxt = later.replace(minute=0, second=0, microsecond=0)
    n_before = len(_sched(st, 40)["runs"])
    sim.run(lambda: st.lp._try_execute(db, sched, nxt + timedelta(seconds=10), runs.grace_minutes(sched)),
            until=lambda: True, grace=0.5)
    started = _sched(st, 40)["runs"][n_before:]
    db.close()
    return st.res(started=started)


@scenario("B24a 반복 순찰 [오늘은 여기서 종료] → 80% 이후에도 오늘 다음 회차 시작 안 함",
          expect=lambda r: _no_err(r) and r["started"] == [])
def b24a(sim):
    return _b24(sim, "end")


@scenario("B24b [회귀] 반복 순찰 무응답(재개) → 80% 이후 다음 회차 시작",
          expect=lambda r: _no_err(r) and r["started"] != [])
def b24b(sim):
    return _b24(sim, None)


@scenario("B25 근무: 충전 중 누가 도크에서 뺐어도 묻지 않고 80% 에 대기 장소로 복귀(근무 시간), 다른 이동 중이면 기다림",
          expect=lambda r: _no_err(r) and r["confirm_alerts"] == 0 and r["waited"] is True
          and r["phase"] == "standby" and r["pos"] == [6.0, 0.0])
def b25(sim):
    st = BatStage(sim)
    st.arrive_standby()
    sim.robot.place(1.0, 0.0)
    st.low(16, 3)
    st.settle(2)
    _charge(st, 50)
    st.rt._runtime[ROBOT]["_is_charging"] = False      # 운영자가 도크에서 뺐다
    st.low(50)
    # 운영자가 다른 이동을 시키는 중 — 80% 여도 끊지 않는다
    st.sm.is_navigating = True
    _charge(st, 85)
    st.settle(2)
    waited = st.phase() == "away"
    st.sm.is_navigating = False
    _charge(st, 85)
    st.settle(2)
    st.rt._runtime[ROBOT]["_is_charging"] = False
    st.drive(until=lambda: st.phase() == "standby", max_sec=40)
    return st.res(confirm_alerts=st.count("battery_resume_confirm"), waited=waited)


@scenario("B26 확인 API: 지난 요청 번호 409, 잘못된 결정 400, 맞는 요청은 처리",
          expect=lambda r: _no_err(r) and r["codes"] == [409, 400, 200] and r["pending_after"] is None)
def b26(sim):
    from fastapi import HTTPException
    st = BatStage(sim)
    br = _br()
    if not br:
        return st.res(codes=None, pending_after="모듈 없음")
    _route_loop(st, sim)
    tok = br.current(ROBOT)["token"]
    codes = []
    for t, d in ((tok + 99, "end"), (tok, "stop"), (tok, "end")):
        try:
            br.post_battery_resume(t, br.DecideReq(decision=d))
            codes.append(200)
        except HTTPException as e:
            codes.append(e.status_code)
    return st.res(codes=codes, pending_after=br.get_battery_resume()["pending"])


@scenario("B27 로봇 상태에 확인 대기 요청이 실림(카드 '⚠ 확인 필요' 배지) — 처리하면 사라짐",
          expect=lambda r: _no_err(r) and r["before"] and r["before"]["kind"] == "route" and r["after"] is None)
def b27(sim):
    st = BatStage(sim)
    rt = st.rt

    def status():
        return rt._build_status(rt._runtime[ROBOT] | {"robot_id": ROBOT, "robot_name": "SIM",
                                                     "last_heartbeat": 0, "position": {}}).get("battery_resume")
    _route_loop(st, sim)
    before = status()
    _decide("end")
    return st.res(before=before, after=status())



# ── 서버 재시작 대비(로그) ───────────────────────────────

def _restart(st, sim):
    """서버 재시작 흉내 — 지금까지의 로그를 DB 로그 표에 옮기고, 메모리의 이어 가기를 비운 뒤 시작 확인을 돈다."""
    from datetime import datetime
    br = _br()
    if not br or not hasattr(br, "report_lost_on_startup"):
        return "기능 없음"
    db = st.Session()
    try:
        for _t, kind, msg in sim.events:
            db.add(st.M.LogDataInfo(Category="robot", Action=kind, Message=msg[:500], RobotName="SIM",
                                    CreatedAt=datetime.now()))
        db.commit()
    finally:
        db.close()
    br._pending.clear()
    return br.report_lost_on_startup()


@scenario("B28 경로 반복 충전 중 서버 재시작 → 시작 때 '이어 가기 취소됨' 알림 1건(무엇을 몇 회 남겼는지 포함)",
          expect=lambda r: _no_err(r) and isinstance(r["lost"], str) and "경로 반복 'A → B'" in r["lost"]
          and "남은 2회, 2바퀴째부터" in r["lost"] and r["lost_alerts"] == 1)
def b28(sim):
    st = BatStage(sim)
    _route_loop(st, sim, total=3, remaining=1, count=2)
    _charge(st, 50)
    lost = _restart(st, sim)
    return st.res(lost=lost, lost_alerts=st.count("battery_resume_lost"))


@scenario("B29 [오늘은 여기서 종료]로 처리한 뒤 재시작 → 알림 없음",
          expect=lambda r: _no_err(r) and r["lost"] is None and r["lost_alerts"] == 0)
def b29(sim):
    st = BatStage(sim)
    _route_loop(st, sim)
    _decide("end")
    lost = _restart(st, sim)
    return st.res(lost=lost, lost_alerts=st.count("battery_resume_lost"))


@scenario("B30 80% 에 이어 간 뒤 재시작 → 알림 없음 / 근무는 80% 복귀 뒤 재시작해도 알림 없음",
          expect=lambda r: _no_err(r) and r["lost"] is None and r["lost_alerts"] == 0)
def b30(sim):
    st = BatStage(sim)
    _route_loop(st, sim)
    _charge(st, 80)
    st.settle(2)
    lost = _restart(st, sim)
    return st.res(lost=lost, lost_alerts=st.count("battery_resume_lost"))
