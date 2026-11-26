"""상시 대기 × 안내 시나리오 통합 — 현행 vs 변경 (docs/작업보고/2026-10-19/작업보고_2026-10-19_상시대기×안내시나리오_구현방향.md).

임시 SQLite 에 로봇·대기 장소·근무 작업·시나리오를 넣는다. 근무 루프·세션 열기/닫기·배터리 감시는
Backend 함수를 가상 시계 위에서 직접 부른다. Jetson 단계 엔진은 돌리지 않는다 — Jetson 이 하는 일
(세션 이동 요청·close)은 시나리오가 같은 API 함수를 불러 대신한다.
"""
import os
from datetime import datetime, timedelta

from harness import scenario

# 맵 파일이 없으니 안전 우회 판정은 끈다(직선 이동). 이번 비교는 경로가 아니라 근무·세션 흐름이다.
os.environ.setdefault("AUTO_DETOUR_DISABLED_MAPS", "1")

ROBOT = 10
STANDBY = (6.0, 0.0)     # 로비 대기 장소 (id=1)
CLINIC = (6.0, 6.0)      # 내과 (id=2)


# ── 무대 ──────────────────────────────────────────────

class Stage:
    def __init__(self, sim, scenario_code=True, battery=80):
        from app.database.database import SessionLocal
        from app.database import models as M
        import app.database.routes.voice_scenario as vs
        import app.robot_control.charge as ch
        import app.robot_control.auto_return as ar
        import app.database.routes.standby  # noqa: F401  호출 경로
        import app.database.routes.voice_scenario_admin  # noqa: F401
        sim.rewire_late_modules()     # 늦게 불러온 모듈도 가상 시계·스레드 위에서

        self.sim, self.M, self.vs, self.ar, self.Session = sim, M, vs, ar, SessionLocal
        self.lp, self.rt, self.sm = sim.lp, sim.rt, sim.sm
        self.charge_calls = 0
        self.has_code = hasattr(M.ScheduleInfo, "StandbyScenarioCode")

        db = SessionLocal()
        db.add(M.FloorInfo(id=1, BusinessId=1, FloorName="1F"))
        db.add(M.FloorInfo(id=2, BusinessId=1, FloorName="3F"))
        db.add(M.RobotInfo(id=ROBOT, RobotName="SIM", BusinessId=1, CurrentMapId=1, CurrentFloorId=1,
                           LimitBattery=30))
        for pid, name, (x, y) in ((1, "로비 대기", STANDBY), (2, "내과", CLINIC)):
            db.add(M.LocationInfo(id=pid, RobotName="SIM", LacationName=name, FloorId=1, MapId=1,
                                  LocationX=x, LocationY=y, Yaw=0.0, Category="work"))
        now = datetime.now()
        kw = dict(id=1, RobotName="SIM", WorkName="로비 상시 대기", TaskType="standby",
                  TaskStatus="대기", StartDate=now - timedelta(hours=1), EndDate=now + timedelta(hours=8),
                  ScheduleMode="once", ReturnPolicy="place", ReturnPlaceId=1)
        if scenario_code and self.has_code:
            kw["StandbyScenarioCode"] = "hospital"
        db.add(M.ScheduleInfo(**kw))
        db.add(M.VoiceScenario(id=1, RobotId=ROBOT, Code="hospital", DisplayName="병원 안내",
                               MapId=1, FloorId=1, IsActive=1))
        db.add(M.VoiceScenarioStep(ScenarioId=1, StepCode="greet", StepOrder=1))
        db.commit()
        db.close()

        # 바깥 의존: 온라인·소유권·충전소 이동은 기록만 한다
        lp = self.lp
        lp._is_target_robot_online = lambda s: True
        lp._claim_robot = lambda s: True
        lp._unclaim_robot = lambda sid: None
        lp.get_active_schedule_id = lambda: lp._active_schedule_id   # 하네스 기본값(None) 되돌림
        ch.release_charge_for_work = lambda *a, **k: None

        def fake_charge(cancel_running=True, **_kw):
            self.charge_calls += 1
            sim.robot._log("CHARGE_RETURN")
            return {"ok": True, "msg": "sim"}
        ch._return_to_charge_internal = fake_charge
        vs._prime_in_background = lambda rid: None    # 제자리 보행 미리 전환은 이번 비교 밖
        self.rt._runtime[ROBOT] = {
            "battery": {"SOC": battery}, "robot_type": "", "limit_battery": 30,
            "basic_status": {"Sleep": 0, "MotionState": 1}, "_is_charging": False,
        }

    # 근무
    def start_shift(self):
        db = self.Session()
        try:
            sched = db.query(self.M.ScheduleInfo).filter(self.M.ScheduleInfo.id == 1).first()
            now = datetime.now()
            return self.lp._enter_standby(db, sched, now, (now - timedelta(hours=1), now + timedelta(hours=8)))
        finally:
            db.close()

    def phase(self):
        st = self.lp.get_standby_state()
        return st.get("phase") if st else None

    def drive(self, until=None, max_sec=120):
        """nav_thread 를 돌린다 — 주행이 끝나거나 until 이 참이 될 때까지."""
        sim, sm = self.sim, self.sm
        stop = until or (lambda: not sm.is_navigating)
        t0 = sim.clock.elapsed()
        sim.run(sim.pl.nav_thread, until=lambda: stop() or sim.clock.elapsed() - t0 > max_sec, grace=1.0)

    def arrive_standby(self):
        self.start_shift()
        self.drive(until=lambda: self.phase() == "standby")

    # 운영자 '안내 시작' (음성관리)
    def operator_start(self):
        db = self.Session()
        try:
            robot = db.query(self.M.RobotInfo).filter(self.M.RobotInfo.id == ROBOT).first()
            return self.vs.open_session(db, robot, "hospital").id
        except Exception as e:   # HTTPException 409 등
            return f"거절: {getattr(e, 'detail', e)}"
        finally:
            db.close()

    # Jetson 이 하는 일
    def jetson_move(self, sid, place_id=2, target=CLINIC):
        db = self.Session()
        try:
            self.vs.session_move(sid, place_id=place_id, db=db, _="sim")
        finally:
            db.close()
        self.drive(until=lambda: not self.sm.is_navigating
                   and (round(self.sim.robot.x, 1), round(self.sim.robot.y, 1)) == target)

    def jetson_close(self, sid, result="completed"):
        db = self.Session()
        try:
            self.vs.close_session(sid, self.vs.SessionCloseReq(result=result, closedBy="voice"), db=db, _="sim")
        finally:
            db.close()
        self.after_close()

    def after_close(self):
        self.sim.wait(1.0)        # 종료 후처리 스레드
        if self.sm.is_navigating:
            self.drive()

    def session(self, sid):
        db = self.Session()
        try:
            s = db.query(self.M.ServiceSession).filter(self.M.ServiceSession.id == sid).first()
            return {k: getattr(s, k, None) for k in ("Result", "ScheduleId", "RunId", "AbortRequest")}
        finally:
            db.close()

    def battery(self, pct):
        self.rt._runtime[ROBOT]["battery"] = {"SOC": pct}
        self.ar.check_battery_and_return(ROBOT)

    def stale(self, after_sec):
        return self.vs.close_stale_sessions(datetime.now() + timedelta(seconds=after_sec))

    def pos(self):
        return [round(self.sim.robot.x, 1), round(self.sim.robot.y, 1)]

    def count(self, kind):
        return sum(1 for _t, k, _m in self.sim.events if k == kind)

    def out(self, **extra):
        st = self.lp.get_standby_state()
        return {"phase": st.get("phase") if st else None, "away": (st or {}).get("away_label"),
                "shift_alive": st is not None, "charge_calls": self.charge_calls,
                "standby_ready": self.count("standby_ready"), **extra}


def _no_err(r):
    return r["errors"] == 0 and not r.get("crash")


# ── 회귀 ──────────────────────────────────────────────

@scenario("R1 근무 시작 → 대기 장소 도착",
          expect=lambda r: _no_err(r) and r["phase"] == "standby" and r["standby_ready"] == 1
          and r["pos"] == list(STANDBY))
def r1(sim):
    st = Stage(sim)
    st.arrive_standby()
    return st.out()


@scenario("R2 근무 없이 연 안내 → 완료(복귀 없음, 지금과 같음)",
          expect=lambda r: _no_err(r) and r["pos"] == list(CLINIC) and r["charge_calls"] == 0
          and r["sess"]["Result"] == "completed")
def r2(sim):
    st = Stage(sim)
    sid = st.operator_start()
    st.jetson_move(sid)
    st.jetson_close(sid)
    return st.out(sess=st.session(sid))


# ── 핵심 흐름 ─────────────────────────────────────────

@scenario("S5 대기 중 안내 시작 → 내과 안내 → 완료 → 대기 장소 복귀",
          expect=lambda r: _no_err(r) and r["pos"] == list(STANDBY) and r["phase"] == "standby"
          and r["sess"]["ScheduleId"] == 1 and r["returned"] == 1)
def s5(sim):
    st = Stage(sim)
    st.arrive_standby()
    sid = st.operator_start()
    st.jetson_move(sid)
    mid_phase = st.phase()
    st.jetson_close(sid)
    return st.out(sess=st.session(sid), mid_phase=mid_phase, returned=st.count("standby_returned"))


@scenario("S3 버튼 호출(작업에 시나리오 연결) → 세션이 작업·회차에 붙음",
          expect=lambda r: _no_err(r) and isinstance(r["sid"], int) and r["sess"]["ScheduleId"] == 1
          and r["sess"]["RunId"] is not None and r["phase"] == "serving")
def s3(sim):
    st = Stage(sim)
    st.arrive_standby()
    from app.database.routes import standby as sb
    from unittest.mock import MagicMock
    sb._scope_ok = lambda *a, **k: True
    sb.write_audit = lambda *a, **k: None
    db = st.Session()
    try:
        res = sb.standby_call(sb.StandbyCallReq(scheduleId=1), MagicMock(), db=db,
                              current_user=MagicMock(id=1))
    except Exception as e:
        res = {"error": str(getattr(e, "detail", e))}
    finally:
        db.close()
    sid = res.get("sessionId")
    return st.out(res=res, sid=sid, sess=st.session(sid) if sid else {})


@scenario("S19 대기 장소로 가는 중 안내 시작(D9) → 그 자리 정지·근무 유지",
          expect=lambda r: _no_err(r) and r["shift_alive"] and r["stopped_at"] != list(STANDBY)
          and r["standby_ready"] == 0 and r["phase_after_guide"] == "serving"
          and r["pos"] == list(STANDBY) and r["phase"] == "standby")
def s19(sim):
    st = Stage(sim)
    st.start_shift()
    st.drive(until=lambda: sim.robot.x >= 3.0)          # 대기 장소까지 절반쯤
    sid = st.operator_start()
    sim.wait(2.0)
    stopped_at = st.pos()
    st.jetson_move(sid)                                   # Jetson 첫 이동(내과)
    phase_after_guide = st.phase()
    ready_at_clinic = st.count("standby_ready")
    st.jetson_close(sid)
    return st.out(stopped_at=stopped_at, phase_after_guide=phase_after_guide,
                  ready_at_clinic=ready_at_clinic, sess=st.session(sid))


@scenario("관찰 — 근무 중 기존 정지 함수를 부르면 근무가 취소된다(D9 를 이것으로 만들면 안 되는 이유)",
          expect=None)
def obs_stop(sim):
    st = Stage(sim)
    st.start_shift()
    st.drive(until=lambda: sim.robot.x >= 3.0)
    sim.sm.stop_navigation_internal("시험 정지")
    db = st.Session()
    try:
        status = db.query(st.M.ScheduleInfo).filter(st.M.ScheduleInfo.id == 1).first().TaskStatus
    finally:
        db.close()
    return st.out(task_status=status, cancel_log=st.count("schedule_cancel"))


# ── 선행 버그 ─────────────────────────────────────────

def _due_patrol(st):
    """지금 시작해야 하는 예약 작업(경로 없음 — 양보 여부만 본다)."""
    db = st.Session()
    now = datetime.now()
    db.add(st.M.ScheduleInfo(id=2, RobotName="SIM", WorkName="10시 순찰", TaskType="patrol",
                             TaskStatus="대기", StartDate=now - timedelta(seconds=20),
                             EndDate=now + timedelta(hours=1), ScheduleMode="once", WayName="없는경로"))
    db.commit()
    db.close()


def _one_loop_tick(st):
    """scheduler_thread 한 바퀴(앞 5초 대기 + 판정 + 30초 sleep 직전)."""
    lp = st.lp
    t0 = st.sim.clock.elapsed()
    st.sim.run(lp.scheduler_thread, until=lambda: st.sim.clock.elapsed() - t0 >= 6.0, grace=0.5)


@scenario("S9/B1 안내 중 예약 작업 시각 → 안내를 끊지 않고 보류, 끝난 뒤 양보",
          expect=lambda r: _no_err(r) and r["shift_alive_during"] and r["sess_during"] == "running"
          and r["yield_end_during"] == 0 and r["yield_end"] == 1)
def s9(sim):
    st = Stage(sim)
    st.arrive_standby()
    sid = st.operator_start()
    _due_patrol(st)
    _one_loop_tick(st)
    alive = st.lp.get_standby_state() is not None
    sess_during = st.session(sid)["Result"]
    yield_end_during = st.count("standby_end")
    if sess_during == "running":
        st.jetson_close(sid)          # 대기 장소 앞이라 복귀 이동 없음
        _one_loop_tick(st)            # 다음 틱 — 예약 작업으로 넘긴다
    return st.out(shift_alive_during=alive, sess_during=sess_during, yield_end_during=yield_end_during,
                  yield_end=st.count("standby_end"), deferred=st.count("standby_yield_deferred"))


@scenario("S9b/B1 단발 응대 중 예약 작업 시각 → 단발 응대를 끊지 않음",
          expect=lambda r: _no_err(r) and r["aborted"] == 0)
def s9b(sim):
    st = Stage(sim, scenario_code=False)
    st.arrive_standby()
    aborted = []
    sim.ss.is_active = lambda: True
    sim.ss.abort = lambda result, reason: aborted.append(reason)
    _due_patrol(st)
    _one_loop_tick(st)
    return st.out(aborted=len(aborted))


@scenario("S11/B3 운영자 입력 대기 중 배터리 25% 1회(오독 확인 전) → 떠나지 않음, 끝난 뒤 충전",
          expect=lambda r: _no_err(r) and r["charge_during"] == 0 and r["charge_calls"] == 1
          and r["critical"] == 0)
def s11(sim):
    st = Stage(sim)
    st.arrive_standby()
    sid = st.operator_start()
    st.jetson_move(sid)          # 내과 앞 대기석 — 운영자 입력을 기다리며 서 있다
    st.battery(25)
    sim.wait(2.0)
    charge_during = st.charge_calls
    st.jetson_close(sid)
    return st.out(charge_during=charge_during, critical=st.count("guide_battery_critical"))


@scenario("S20/B5 반복 실행 회차 사이 10초 틈 → 근무가 로봇을 잡지 않음",
          expect=lambda r: _no_err(r) and r["entered"] is False)
def s20(sim):
    st = Stage(sim)
    sid = st.operator_start()          # 근무 없이 화면에서 반복 실행 1회차
    db = st.Session()
    try:                               # 방문객 대답이 있었던 정상 회차(반복 판정 조건)
        db.query(st.M.ServiceSession).filter(st.M.ServiceSession.id == sid).first().Heard = "내과"
        db.commit()
    finally:
        db.close()
    st.jetson_close(sid)
    import app.database.routes.voice_scenario_admin as adm
    from unittest.mock import MagicMock
    db = st.Session()
    try:
        robot = db.query(st.M.RobotInfo).filter(st.M.RobotInfo.id == ROBOT).first()
        adm._get_robot_or_404 = lambda d, rid, u: robot
        res = adm.check_voice_loop(ROBOT, adm.LoopCheckReq(sessionId=sid, round=1), db=db,
                                   current_user=MagicMock(id=1))
    finally:
        db.close()
    sim.wait(4.0)                      # 다음 회차를 열기 전 틈
    entered = st.start_shift()
    return st.out(loop_ok=res.get("ok"), entered=entered)


# ── 배터리 최소 기준 (2026-10-22 기준 하나) ───────────
# 예전 S17·S18(D10: 다음 이동 전 중단 요청 → Jetson 이 닫음 / 3분 안전장치)은 정책이 바뀌어 뺐다.
# 같은 상황의 새 기대 동작은 battery_policy.py B4(이동 중)·B5(서 있을 때)가 본다.

@scenario("S17 안내 이동 중 배터리 12% 3회 → 멘트 뒤 관제가 닫음 → 알림 1건 → 충전소, 근무 유지(80% 에 복귀)",
          expect=lambda r: _no_err(r) and r["sess"]["Result"] == "aborted_battery" and r["pos"] != list(CLINIC)
          and r["aborted_alert"] == 1 and r["charge_calls"] == 1 and r["shift_alive"])
def s17(sim):
    st = Stage(sim)
    st.arrive_standby()
    sid = st.operator_start()
    db = st.Session()
    try:
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
    finally:
        db.close()
    for k in range(3):                                    # 이동 중 heartbeat 마다 배터리 확인
        sim.at(1.0 + k, lambda: st.battery(12))
    st.drive(until=lambda: not st.sm.is_navigating, max_sec=30)
    sim.wait(15.0)                 # 방문객 멘트가 끝난 뒤 닫고 떠난다
    st.after_close()
    return st.out(aborted_alert=st.count("guide_battery_aborted"), sess=st.session(sid))


# ── 종료 후 갈래 ──────────────────────────────────────

def _set_map(st, mid, floor_id):
    db = st.Session()
    try:
        r = db.query(st.M.RobotInfo).filter(st.M.RobotInfo.id == ROBOT).first()
        r.CurrentMapId, r.CurrentFloorId = mid, floor_id
        db.commit()
    finally:
        db.close()


def _other_floor_case(sim, with_spot):
    st = Stage(sim)
    if with_spot:
        db = st.Session()
        db.add(st.M.LocationInfo(id=3, RobotName="SIM", LacationName="3F_로봇_대기장소", FloorId=2, MapId=2,
                                 LocationX=FLOOR3_WAIT[0], LocationY=FLOOR3_WAIT[1], Yaw=0.0,
                                 Category="work"))
        db.commit()
        db.close()
    st.arrive_standby()
    sid = st.operator_start()
    st.jetson_move(sid)
    _set_map(st, 2, 2)                           # 직원이 엘리베이터로 3F 에 데려감
    st.jetson_close(sid)
    phase_away, label = st.phase(), (st.lp.get_standby_state() or {}).get("away_label")
    pos_away = st.pos()
    st.lp._standby_watchdog(datetime.now())      # 30초 감시 — 이동을 또 보내지 않아야 한다
    if st.sm.is_navigating:
        st.drive()                               # 감시가 이동을 다시 보냈다면 여기서 드러난다
    pos_after_tick = st.pos()
    blocked = st.operator_start()                # 다른 층에서 호출
    _set_map(st, 1, 1)                           # 1F 로 데려옴
    st.lp._standby_watchdog(datetime.now())
    if st.sm.is_navigating:
        st.drive()
    return st.out(phase_away=phase_away, label=label, pos_away=pos_away, pos_after_tick=pos_after_tick,
                  blocked=blocked,
                  away_alert=st.count("standby_away"))


FLOOR3_WAIT = (2.0, 6.0)


@scenario("S8 다른 층에서 안내 종료 → 그 층 로봇 대기 장소로 비켜 섬 + 알림 → 1F 로 오면 자동 복귀",
          expect=lambda r: _no_err(r) and r["phase_away"] == "away" and r["pos_away"] == list(FLOOR3_WAIT)
          and "3F 로봇 대기 장소" in (r["label"] or "") and str(r["blocked"]).startswith("거절")
          and r["away_alert"] >= 1 and r["pos"] == list(STANDBY) and r["phase"] == "standby")
def s8(sim):
    return _other_floor_case(sim, with_spot=True)


@scenario("S8b 다른 층에 로봇 대기 장소가 없음 → 그 자리에 섬 + 알림 → 1F 로 오면 자동 복귀",
          expect=lambda r: _no_err(r) and r["phase_away"] == "away" and r["pos_away"] == list(CLINIC)
          and str(r["blocked"]).startswith("거절") and r["away_alert"] == 1
          and r["pos"] == list(STANDBY) and r["phase"] == "standby")
def s8b(sim):
    return _other_floor_case(sim, with_spot=False)


@scenario("S12 Jetson 끊김 3분 자동 종료 → 관제가 대기 장소로 복귀",
          expect=lambda r: _no_err(r) and r["closed"] == "aborted_offline" and r["pos"] == list(STANDBY))
def s12(sim):
    st = Stage(sim)
    st.arrive_standby()
    sid = st.operator_start()
    st.jetson_move(sid)
    closed = st.stale(185)
    st.after_close()
    return st.out(closed=closed[0][1] if closed else None)


@scenario("S10 안내 중 운영 종료 시각 → 끝나는 즉시 근무 마감·충전소",
          expect=lambda r: _no_err(r) and r["shift_alive"] is False and r["charge_calls"] == 1)
def s10(sim):
    st = Stage(sim)
    st.arrive_standby()
    sid = st.operator_start()
    st.jetson_move(sid)
    st.lp._standby["ends_at"] = datetime.now() - timedelta(minutes=1)
    st.lp._standby_watchdog(datetime.now())      # 응대 중 — 기다린다
    alive_during = st.lp.get_standby_state() is not None
    st.jetson_close(sid)
    return st.out(alive_during=alive_during)
