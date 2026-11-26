"""상시 대기 단발 응대 — 응대 시간 상한·연장·진전 감시 제거(10-22) 현행 vs 변경.

무대는 standby_scenario.py 의 Stage(로봇 10, 로비 대기 (6,0)=id 1, 내과 (6,6)=id 2).
작업에 시나리오를 붙이지 않고(단발 응대) 목적지 사전 '내과' 를 내과 장소에 연결한다.
현행 상한이 빨리 걸리도록 작업의 응대 1건 최대(StandbyMaxSessionMin)를 1분으로 둔다 — 변경안은 이 값을 읽지 않는다.
스케줄러 루프 대신 standby_session.tick 을 5초마다 직접 부르고, 그 모듈의 datetime 을 가상 시계에 맞춘다.
"""
import importlib.util
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "standby_scenario_base_single", os.path.join(os.path.dirname(__file__), "standby_scenario.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
Stage, ROBOT, STANDBY, CLINIC, _no_err = _base.Stage, _base.ROBOT, _base.STANDBY, _base.CLINIC, _base._no_err

TICK_SEC = 5


class SingleStage(Stage):
    def __init__(self, sim, limit_min=1):
        super().__init__(sim, scenario_code=False)
        M = self.M
        db = self.Session()
        db.add(M.VoiceSlotValue(id=1, RobotId=ROBOT, SlotName="목적지", Value="내과"))
        db.add(M.VoiceSlotLocation(SlotValueId=1, LocationId=2, MapId=1))
        db.query(M.ScheduleInfo).filter(M.ScheduleInfo.id == 1).first().StandbyMaxSessionMin = limit_min
        db.commit()
        db.close()

        import app.scheduler.standby_session as ss
        import app.database.routes.standby as sb
        self.ss, self.sb = ss, sb
        sb._scope_ok = lambda *a, **k: True
        sb.write_audit = lambda *a, **k: None
        self.speech = []
        ss._speak = lambda text: self.speech.append(text)

        # 모듈의 datetime.now() 를 가상 시계로 — 해산 확인 만료(dismiss_until)가 가상 시간으로 흐르게
        t0, clock = datetime.now(), sim.clock

        class VDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return t0 + timedelta(seconds=clock.elapsed())
        ss.datetime = VDT
        self.VDT = VDT

        def loop_tick():
            ss.tick(VDT.now(), TICK_SEC)
            sim.at(TICK_SEC, loop_tick)
        sim.at(TICK_SEC, loop_tick)

    def call(self):
        db = self.Session()
        try:
            return self.sb.standby_call(self.sb.StandbyCallReq(scheduleId=1, destinationId=1), MagicMock(),
                                        db=db, current_user=MagicMock(id=1, BusinessId=None))
        except Exception as e:
            return {"error": str(getattr(e, "detail", e))}
        finally:
            db.close()

    def sess_row(self, sid):
        db = self.Session()
        try:
            s = db.query(self.M.ServiceSession).filter(self.M.ServiceSession.id == sid).first()
            return {"Result": s.Result, "Reason": s.Reason, "ended": s.EndedAt is not None}
        finally:
            db.close()

    def live_session(self):
        db = self.Session()
        try:
            live = self.sb.standby_live(db=db, current_user=MagicMock(id=1, BusinessId=None))
            return live.get("session") or {}
        finally:
            db.close()

    def run_until(self, pred, max_sec):
        t0 = self.sim.clock.elapsed()
        while not pred() and self.sim.clock.elapsed() - t0 < max_sec:
            if self.sm.is_navigating:
                self.drive(until=lambda: pred() or not self.sm.is_navigating, max_sec=max_sec)
            else:
                self.sim.wait(TICK_SEC)

    def result(self, sid, **extra):
        return self.out(sess=self.sess_row(sid), speech=self.speech,
                        extended_logs=self.count("standby_session_extended"),
                        timeout_logs=self.count("standby_session_timeout"), **extra)


@scenario("L1 회귀 — 정상 응대: 내과 안내 → 도착 → 무응답 3분 → 대기 장소 복귀 → 완료",
          expect=lambda r: _no_err(r) and r["sess"]["Result"] == "completed" and r["pos"] == list(STANDBY))
def l1(sim):
    st = SingleStage(sim, limit_min=20)
    st.arrive_standby()
    sid = st.call().get("sessionId")
    st.run_until(lambda: st.sess_row(sid)["ended"], max_sec=900)
    return st.result(sid)


@scenario("L2 느린 응대(이동만 2분+, 상한 1분) → 연장 없이 완료 (현행: 연장 후 completed_delayed)",
          expect=lambda r: _no_err(r) and r["sess"]["Result"] == "completed" and r["extended_logs"] == 0)
def l2(sim):
    st = SingleStage(sim, limit_min=1)
    st.arrive_standby()
    sim.robot.p.speed = 0.04          # 안내 이동만 느리게 (6m ≈ 150초)
    sid = st.call().get("sessionId")
    st.run_until(lambda: st.sess_row(sid)["ended"], max_sec=1800)
    return st.result(sid)


@scenario("L3 오류 없이 멈춰 선 응대(10분 제자리) → 끊지 않고 계속, 운영자가 종료 (현행: 진전 없음으로 자동 종료)",
          expect=lambda r: _no_err(r) and r["mid_result"] == "running" and r["timeout_logs"] == 0
          and "죄송합니다. 여기서 안내를 마치겠습니다." not in r["speech"]
          and r["sess"]["Result"] == "aborted_operator")
def l3(sim):
    st = SingleStage(sim, limit_min=1)
    st.arrive_standby()
    sid = st.call().get("sessionId")
    st.drive(until=lambda: sim.robot.y >= 2.0, max_sec=60)
    sim.robot.hold(600)
    st.run_until(lambda: st.sess_row(sid)["ended"], max_sec=540)
    mid = st.sess_row(sid)["Result"]
    st.ss.abort(st.ss.RESULT_ABORTED_OPERATOR, "운영자 종료")
    return st.result(sid, mid_result=mid)


@scenario("L4 실시간 패널 — 경과 시간은 보이고 상한·연장 칸은 없음 (현행: limitMin·extended 있음)",
          expect=lambda r: _no_err(r) and r["elapsed"] > 0 and r["keys_gone"])
def l4(sim):
    st = SingleStage(sim, limit_min=20)
    st.arrive_standby()
    st.call()
    st.drive(until=lambda: sim.robot.y >= 3.0, max_sec=60)
    sim.wait(TICK_SEC * 2)
    sess = st.live_session()
    return {"elapsed": sess.get("elapsedSec", 0),
            "keys_gone": not any(k in sess for k in ("limitMin", "extended", "frozenSec")),
            "keys": sorted(sess)}
