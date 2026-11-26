"""이동 멈춤 — 지점에 못 가면 건너뛰지 않고 운영자 선택을 기다린다 (P2, 10-22) 현행 vs 변경.

현행: 60초 제자리 × 다시 보내기 3번 → 도착으로 치고 다음 지점(도착 안내를 막힌 자리에서 함, 회차 성공).
변경: 그 자리에서 멈춤 + 알림 → [다시 시도]/[건너뛰고 계속]/[중단], 응답 없음 5분이면 종류별 자동 처리.
보고서: docs/계획보고/2026-10-22/계획보고_2026-10-22_주행멈춤판단·막힘공유_개선.md 4-6
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "standby_single_base_hold", os.path.join(os.path.dirname(__file__), "standby_single_no_limit.py"))
_single = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_single)
del REGISTRY[_n:]
SingleStage, Stage, _no_err = _single.SingleStage, _single.Stage, _single._no_err
_patrol_spec = importlib.util.spec_from_file_location(
    "battery_policy_base_hold", os.path.join(os.path.dirname(__file__), "battery_policy.py"))
_bp = importlib.util.module_from_spec(_patrol_spec)
_patrol_spec.loader.exec_module(_bp)
del REGISTRY[_n:]
_patrol = _bp._patrol

STOP_SPEECH = "복도 끝에 도착했습니다"
ROUTE = [(5, 0, "로비"), (10, 0, "복도 끝"), (15, 0, "비상구 앞")]


def _wire(sim, ss):
    """새 모듈(nav_hold)도 가상 시계에 — 하네스는 늦게 import 된 모듈을 다시 묶어야 한다.
    하네스 기본값은 단발 응대 is_active 를 늘 False 로 두므로 실제 상태로 되돌린다."""
    try:
        import app.navigation.nav_hold  # noqa: F401
    except ImportError:
        pass
    sim.rewire_late_modules()
    ss.is_active = lambda: ss._session is not None


def _count(sim, kind):
    return sum(1 for _t, k, _m in sim.events if k == kind)


class HoldStage(Stage):
    def __init__(self, sim):
        super().__init__(sim)
        self.speech = []
        import app.navigation.arrival_gate as ag
        ag.speak_only = lambda rid, text: self.speech.append(text)
        import app.scheduler.standby_session as ss
        _wire(sim, ss)
        try:
            from app.navigation import nav_hold
            self.nh = nav_hold
        except ImportError:
            self.nh = None          # 현행에는 없다

    def hold(self):
        return self.nh.get_hold() if self.nh else None

    def route(self, until, max_sec, charge=False):
        sim, sm = self.sim, self.sm
        wps = [{"x": x, "y": y, "yaw": 0.0, "name": n, "map_id": None} for x, y, n in ROUTE]
        wps[1]["stop_action"] = {"announceText": STOP_SPEECH, "confirm": "none"}
        sm.waypoints_list = wps
        sm.current_wp_index, sm.is_navigating = 0, True
        sm.charge_on_arrival = charge
        sm._signal_nav_reset(full=True)
        self.keep(until, max_sec, first=True)

    def keep(self, until, max_sec, first=False):
        sim, sm = self.sim, self.sm
        t0 = sim.clock.elapsed()
        fn = (lambda: (sm.navigation_send_next(), sim.pl.nav_thread())) if first else sim.pl.nav_thread
        sim.run(fn, until=lambda: until() or not sm.is_navigating or sim.clock.elapsed() - t0 > max_sec,
                grace=1.0)

    def stuck_at_corridor(self, sec):
        """로비를 지나 복도 끝으로 가는 중(x≥7) 오류 없이 sec 초 제자리."""
        self.keep(lambda: self.sim.robot.x >= 7.0, 60, first=False)
        self.sim.robot.hold(sec)

    def release(self):
        self.sim.robot.hold_until = 0

    def run_row(self):
        db = self.Session()
        try:
            r = db.query(self.M.ScheduleRun).order_by(self.M.ScheduleRun.id.desc()).first()
            return {"result": r.Result if r else None, "reason": r.Reason if r else None}
        finally:
            db.close()

    def res(self, **extra):
        return self.out(speech=self.speech, said_stop=any(STOP_SPEECH in s for s in self.speech),
                        hold=self.hold(), run=self.run_row(),
                        n={k: _count(self.sim, k) for k in ("nav_hang_retry", "nav_hang_skip", "nav_unreached_hold",
                                                            "nav_hold_resumed", "nav_hold_timeout", "nav_hold_action")},
                        navigating=bool(self.sm.is_navigating), **extra)


def _start_patrol_stuck(sim, stuck_sec):
    st = HoldStage(sim)
    _patrol(st)
    st.route(until=lambda: sim.robot.x >= 7.0, max_sec=60)
    sim.robot.hold(stuck_sec)
    st.keep(lambda: st.hold() is not None or not st.sm.is_navigating, 400)
    return st


@scenario("H1 순회 막힘 → 건너뛰지 않고 멈춤(작업) · 알림 1건 · 막힌 자리에서 도착 안내 없음 (현행: 건너뛰고 안내)",
          expect=lambda r: _no_err(r) and r["hold"] and r["hold"]["kind"] == "work" and r["hold"]["wpName"] == "복도 끝"
          and r["navigating"] and not r["said_stop"] and r["n"]["nav_unreached_hold"] == 1 and r["n"]["nav_hang_skip"] == 0
          and r["run"]["result"] == "running")
def h1(sim):
    st = _start_patrol_stuck(sim, 900)
    st.keep(lambda: False, 30)
    return st.res()


@scenario("H2 [다시 시도] → 같은 지점 다시 보냄 → 도착 안내는 복도 끝에서 → 회차 성공",
          expect=lambda r: _no_err(r) and r["hold"] is None and r["said_stop"] and r["stop_pos"] == [10, 0]
          and r["run"]["result"] in ("success", "completed_delayed") and r["pos"] == [15, 0])
def h2(sim):
    st = _start_patrol_stuck(sim, 900)
    stop_pos = {}
    import app.navigation.arrival_gate as ag
    ag.speak_only = lambda rid, text: (st.speech.append(text), stop_pos.setdefault("p", st.pos()))
    if st.nh:
        st.nh.request("retry")
    st.release()
    st.keep(lambda: False, 200)
    return st.res(stop_pos=stop_pos.get("p"))


@scenario("H3 [건너뛰고 계속] → 복도 끝 도착 안내 없이 비상구 앞으로 → 회차 성공 + 사유 '건너뛴 지점 1: 복도 끝'",
          expect=lambda r: _no_err(r) and not r["said_stop"] and r["pos"] == [15, 0]
          and r["run"]["result"] in ("success", "completed_delayed") and "건너뛴 지점 1: 복도 끝" in (r["run"]["reason"] or ""))
def h3(sim):
    st = _start_patrol_stuck(sim, 900)
    if st.nh:
        st.nh.request("skip")
    st.release()
    st.keep(lambda: False, 200)
    return st.res()


@scenario("H4 [작업 중단] → 주행 정지 · 회차 취소(사유: 운영자 중단) · 충전소 복귀",
          expect=lambda r: _no_err(r) and not r["navigating"] and r["run"]["result"] == "cancelled"
          and "운영자 중단" in (r["run"]["reason"] or "") and r["charge_calls"] == 1)
def h4(sim):
    st = _start_patrol_stuck(sim, 900)
    if st.nh:
        st.nh.request("abort")
    st.keep(lambda: False, 10)
    sim.wait(2.0)
    return st.res()


@scenario("H5 응답 없음 5분 → 회차 중단(사유: 응답 없음) · 충전소 복귀 · 다음 회차 그대로",
          limit_sec=1500,
          expect=lambda r: _no_err(r) and not r["navigating"] and r["run"]["result"] == "aborted"
          and "응답 없음" in (r["run"]["reason"] or "") and r["charge_calls"] == 1 and r["n"]["nav_hold_timeout"] == 1)
def h5(sim):
    st = _start_patrol_stuck(sim, 1200)
    st.keep(lambda: False, 330)
    sim.wait(2.0)
    return st.res()


@scenario("H6 멈춤 중 막힘이 저절로 풀림 → 멈춤 닫힘·'이동 재개' 기록 → 이어서 완주, 회차 성공",
          expect=lambda r: _no_err(r) and r["hold"] is None and r["n"]["nav_hold_resumed"] == 1
          and r["run"]["result"] in ("success", "completed_delayed") and r["pos"] == [15, 0])
def h6(sim):
    st = _start_patrol_stuck(sim, 900)
    st.release()
    st.keep(lambda: False, 200)
    return st.res()


@scenario("H7 직접 이동(작업 없음) 막힘 → 응답 없음 5분 → 이동만 취소, 충전소로 보내지 않음",
          limit_sec=1500,
          expect=lambda r: _no_err(r) and r["kind"] == "manual" and not r["navigating"] and r["charge_calls"] == 0)
def h7(sim):
    st = HoldStage(sim)
    st.route(until=lambda: sim.robot.x >= 7.0, max_sec=60)
    sim.robot.hold(1200)
    st.keep(lambda: st.hold() is not None or not st.sm.is_navigating, 400)
    kind = (st.hold() or {}).get("kind")
    st.keep(lambda: False, 330)
    return st.res(kind=kind)


@scenario("H8 대기 장소로 가다 막힘 → 근무 칩 '복귀하지 못함' → 5분 뒤 자리 비움, 근무 유지",
          limit_sec=1500,
          expect=lambda r: _no_err(r) and r["kind"] == "standby" and r["label"].startswith("복귀하지 못함")
          and r["shift_alive"] and r["phase"] == "away" and not r["navigating"])
def h8(sim):
    st = HoldStage(sim)
    st.start_shift()
    st.drive(until=lambda: sim.robot.x >= 3.0, max_sec=60)
    sim.robot.hold(1200)
    st.drive(until=lambda: st.hold() is not None, max_sec=400)
    kind = (st.hold() or {}).get("kind")
    from unittest.mock import MagicMock
    import app.database.routes.standby as sb
    sb._scope_ok = lambda *a, **k: True
    db = st.Session()
    try:
        label = sb.standby_live(db=db, current_user=MagicMock(id=1, BusinessId=None)).get("phaseLabel")
    finally:
        db.close()
    st.drive(until=lambda: False, max_sec=330)
    return st.res(kind=kind, label=label or "")


@scenario("H9 충전소로 가다 막힘 → 멈춤(충전) 유지, 10분 지나도 자동 처리 없음, 고를 수 있는 건 다시 시도뿐",
          limit_sec=1500,
          expect=lambda r: _no_err(r) and r["hold"] and r["hold"]["kind"] == "charge" and r["navigating"]
          and r["n"]["nav_hold_timeout"] == 0 and r["skip_rejected"])
def h9(sim):
    st = HoldStage(sim)
    st.route(until=lambda: sim.robot.x >= 7.0, max_sec=60, charge=True)
    sim.robot.hold(1500)
    st.keep(lambda: st.hold() is not None or not st.sm.is_navigating, 400)
    rej = (not st.nh.request("skip")["ok"]) if st.nh else False
    st.keep(lambda: False, 600)
    return st.res(skip_rejected=rej)


@scenario("H10 안내(단발 응대) 막힘 → 근무 칩 '이동 막힘' → [안내 종료] → 응대 종료·근무 유지·대기 장소로 복귀",
          expect=lambda r: _no_err(r) and r["label"].startswith("이동 막힘") and r["sess"]["Result"] == "aborted_operator"
          and r["shift_alive"] and r["returning"])
def h10(sim):
    st = SingleStage(sim, limit_min=20)
    _wire(sim, st.ss)
    st.arrive_standby()
    sid = st.call().get("sessionId")
    st.drive(until=lambda: sim.robot.y >= 2.0, max_sec=60)
    sim.robot.hold(900)
    from app.navigation import nav_hold
    st.run_until(lambda: nav_hold.get_hold() is not None, max_sec=200)
    sess_live = st.sb.standby_live(db=st.Session(), current_user=__import__("unittest.mock").mock.MagicMock(id=1, BusinessId=None))
    nav_hold.request("abort")
    sim.robot.hold_until = 0
    st.drive(until=lambda: False, max_sec=8)
    phase = st.lp.get_standby_state() or {}
    return st.result(sid, label=sess_live.get("phaseLabel") or "", returning=phase.get("phase") == "returning")


# ── 음성관리 [안내 종료] · 근무 소속 표시 ─────────────────

def _scenario_guide_moving(sim):
    """상시 대기 근무에서 버튼 호출로 연 시나리오 안내가 내과로 이동 중."""
    from unittest.mock import MagicMock
    st = HoldStage(sim)
    st.arrive_standby()
    import app.database.routes.standby as sb
    import app.database.routes.voice_scenario_admin as va
    sb._scope_ok = lambda *a, **k: True
    sb.write_audit = va.write_audit = lambda *a, **k: None
    db = st.Session()
    try:
        sid = sb.standby_call(sb.StandbyCallReq(scheduleId=1), MagicMock(), db=db,
                              current_user=MagicMock(id=1, BusinessId=None)).get("sessionId")
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
    finally:
        db.close()
    st.drive(until=lambda: sim.robot.y >= 2.0, max_sec=60)
    return st, va, sid


@scenario("H11 근무 중 시나리오 안내가 이동 중일 때 음성관리 [안내 종료] → 근무 유지·대기 장소로 복귀 (현행: 근무 취소)",
          expect=lambda r: _no_err(r) and r["shift_alive"] and r["sess"]["Result"] == "aborted_operator"
          and r["phase_after"] in ("returning", "standby"))
def h11(sim):
    from unittest.mock import MagicMock
    st, va, sid = _scenario_guide_moving(sim)
    db = st.Session()
    try:
        va.close_voice_session(sid, va.SessionCloseReq(reason="운영자 종료"), MagicMock(), db=db,
                               current_user=MagicMock(id=1, BusinessId=None))
    finally:
        db.close()
    st.after_close()
    sim.wait(2.0)
    return st.out(sess=st.session(sid), phase_after=st.phase())


@scenario("H12 음성관리 현재 세션 — 근무 소속(scheduleId·작업명)과 로봇 근무 정보가 함께 온다",
          expect=lambda r: _no_err(r) and r["schedule_id"] == 1 and r["schedule_name"] == "로비 상시 대기"
          and (r["standby_work"] or {}).get("scheduleId") == 1)
def h12(sim):
    from unittest.mock import MagicMock
    st, va, sid = _scenario_guide_moving(sim)
    db = st.Session()
    try:
        cur = va.current_voice_session(10, db=db, current_user=MagicMock(id=1, BusinessId=None))
    finally:
        db.close()
    sess = cur.get("session") or {}
    return {"schedule_id": sess.get("scheduleId"), "schedule_name": sess.get("scheduleName"),
            "standby_work": cur.get("standbyWork")}


# ── 오프라인 ─────────────────────────────────────────

@scenario("H13 멈춤 중 통신 30초 두절 사이 [작업 중단] → 거절(통신 두절), 복구 뒤에도 멈춤 유지 (현행: 받아 두었다가 복구 뒤 실행)",
          expect=lambda r: _no_err(r) and r["rejected"] and r["hold"] is not None and r["run"]["result"] == "running")
def h13(sim):
    st = _start_patrol_stuck(sim, 900)
    sim.robot.drop_comm(30)
    st.keep(lambda: False, 5)
    res = st.nh.request("abort") if st.nh else {"ok": True}
    st.keep(lambda: False, 40)
    return st.res(rejected=not res.get("ok"))


def _watchdog_ticks(sim, st, every=5.0):
    from datetime import datetime

    def tick():
        try:
            st.lp._standby_watchdog(datetime.now())
        except Exception as e:  # noqa: BLE001
            print("[SIM] watchdog", e)
        sim.at(every, tick)
    sim.at(every, tick)


@scenario("H14 근무 중(대기 장소로 이동 중) 통신 90초 두절 → 안전 정지, 근무 유지(자리 비움·통신 두절) → 복구 뒤 대기 장소로 (현행: 근무 취소)",
          limit_sec=1500,
          expect=lambda r: _no_err(r) and r["away_mid"] == "comm_lost" and r["shift_alive"]
          and r["phase"] == "standby" and r["pos"] == [6, 0]
          and r["a"]["standby_comm_lost"] == 1 and r["a"]["standby_comm_restored"] == 1 and r["a"]["nav_comm_lost"] == 0)
def h14(sim):
    st = HoldStage(sim)
    _watchdog_ticks(sim, st)
    st.start_shift()
    st.drive(until=lambda: sim.robot.x >= 3.0, max_sec=60)
    sim.robot.drop_comm(90)
    st.drive(until=lambda: not st.sm.is_navigating, max_sec=120)
    away_mid = (st.lp.get_standby_state() or {}).get("away")
    sim.wait(40)
    st.drive(until=lambda: st.phase() == "standby", max_sec=120)
    return st.res(away_mid=away_mid, a=_alerts(sim))


def _alerts(sim):
    return {k: _count(sim, k) for k in ("standby_comm_lost", "standby_comm_lost_long", "standby_comm_restored",
                                        "nav_comm_lost")}


@scenario("H16 근무 중 통신 두절이 12분 이어짐 → 두절 알림 1건 + 10분째 현장 확인 알림 1건(한 번만)",
          limit_sec=2400,
          expect=lambda r: _no_err(r) and r["a"]["standby_comm_lost"] == 1 and r["a"]["standby_comm_lost_long"] == 1
          and r["shift_alive"])
def h16(sim):
    st = HoldStage(sim)
    _watchdog_ticks(sim, st)
    st.start_shift()
    st.drive(until=lambda: sim.robot.x >= 3.0, max_sec=60)
    sim.robot.drop_comm(780)
    st.drive(until=lambda: not st.sm.is_navigating, max_sec=120)
    sim.wait(720)
    return st.res(a=_alerts(sim))


@scenario("H15 회귀 — 순회 작업 중 통신 90초 두절 → 지금처럼 안전 정지·회차 취소",
          limit_sec=1500,
          expect=lambda r: _no_err(r) and not r["navigating"] and r["run"]["result"] == "cancelled"
          and r["a"]["nav_comm_lost"] == 1)
def h15(sim):
    st = HoldStage(sim)
    _patrol(st)
    st.route(until=lambda: sim.robot.x >= 3.0, max_sec=60)
    sim.robot.drop_comm(90)
    st.keep(lambda: False, 120)
    return st.res(a=_alerts(sim))
