"""상시 대기 응대 방식 '단순 동작'(앉아/일어서만) — 작업 등록 API·근무 문맥·호출 거절·근무 칩 (현행 vs 변경).

무대는 standby_scenario_api.py 의 ApiStage 를 그대로 쓴다(로봇 10, 로비 대기 장소 id=1).
Jetson 쪽(듣기 → receiver 직접 전송)은 sim/out/standby_motion/jetson_standby_motion.py 가 따로 본다.
"""
import importlib.util
import os
from unittest.mock import MagicMock

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "standby_scenario_api_base", os.path.join(os.path.dirname(__file__), "standby_scenario_api.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
ApiStage = _base.ApiStage


class MotionStage(ApiStage):
    def insert_mode(self, **over):
        """작업 등록 — 저장된 응대 방식 칸과 목록 응답을 돌려준다."""
        from datetime import date
        body = dict(RobotName="SIM", TaskName="등록 시험", TaskType="standby", WorkStatus="대기",
                    ScheduleMode="standby", ActiveStartTime="00:00", ActiveEndTime="23:59",
                    SeriesStartDate=date.today().isoformat(), StandbyTrigger="mic", ReturnPlaceId=1)
        body.update(over)
        db = self.Session()
        try:
            res = self.sc.insert_schedule(self.sc.ScheduleInsertReq(**body), MagicMock(), db=db,
                                          current_user=_base.USER)
            row = db.query(self.M.ScheduleInfo).filter(self.M.ScheduleInfo.id == res["id"]).first()
            listed = next(r for r in self.sc.get_schedules(db=db, current_user=_base.USER) if r["id"] == res["id"])
            return {"status": 200,
                    "mode": getattr(row, "StandbyResponseMode", "칸 없음"),
                    "code": row.StandbyScenarioCode,
                    "listed": listed.get("StandbyResponseMode", "응답 없음")}
        except Exception as e:
            return {"status": getattr(e, "status_code", "예외"), "detail": str(getattr(e, "detail", e))[:90]}
        finally:
            db.close()

    def context(self):
        db = self.Session()
        try:
            return self.ab.agent_standby_context(db=db, _="sim")
        finally:
            db.close()

    def sessions(self):
        db = self.Session()
        try:
            return db.query(self.M.ServiceSession).count()
        finally:
            db.close()


def _ok(r):
    return r["errors"] == 0 and not r.get("crash")


def _rejected(res):
    return res.get("status") != 200


# ── 작업 등록 API ─────────────────────────────────────

@scenario("M1 '단순 동작'(마이크)로 등록 → 저장·목록에 나옴, 시나리오 코드는 비어 있음 (현행: 칸이 없어 무시)",
          expect=lambda r: _ok(r) and r["res"]["status"] == 200 and r["res"]["mode"] == "motion"
          and r["res"]["listed"] == "motion" and r["res"]["code"] is None)
def m1(sim):
    return {"res": MotionStage(sim, scenario_code=False).insert_mode(StandbyResponseMode="motion")}


@scenario("M2 '단순 동작' + 버튼 호출 → 거절 (현행: 무시하고 단발로 저장)",
          expect=lambda r: _ok(r) and _rejected(r["res"]))
def m2(sim):
    return {"res": MotionStage(sim, scenario_code=False).insert_mode(
        StandbyResponseMode="motion", StandbyTrigger="button")}


@scenario("M3 '단순 동작' + 안내 시나리오 동시 지정 → 거절",
          expect=lambda r: _ok(r) and _rejected(r["res"]))
def m3(sim):
    return {"res": MotionStage(sim).insert_mode(StandbyResponseMode="motion", StandbyScenarioCode="hospital")}


@scenario("M4 알 수 없는 값('xyz') → 거절 (현행: 무시)",
          expect=lambda r: _ok(r) and _rejected(r["res"]))
def m4(sim):
    return {"res": MotionStage(sim, scenario_code=False).insert_mode(StandbyResponseMode="xyz")}


@scenario("M5 회귀 — 응대 방식 비움(단발) → 단순 동작 칸은 NULL",
          expect=lambda r: _ok(r) and r["res"]["status"] == 200 and r["res"]["mode"] in (None, "칸 없음")
          and r["res"]["code"] is None)
def m5(sim):
    return {"res": MotionStage(sim, scenario_code=False).insert_mode()}


@scenario("M6 회귀 — 안내 시나리오(병원)로 등록 → 코드 저장, 단순 동작 칸은 NULL",
          expect=lambda r: _ok(r) and r["res"]["status"] == 200 and r["res"]["code"] == "hospital"
          and r["res"]["mode"] in (None, "칸 없음"))
def m6(sim):
    return {"res": MotionStage(sim).insert_mode(StandbyScenarioCode="hospital")}


@scenario("M7 시리즈 분할 복사 때 '단순 동작' 유지",
          expect=lambda r: _ok(r) and r["copied"] == "motion")
def m7(sim):
    st = MotionStage(sim, scenario_code=False)
    st.set_sched(StandbyResponseMode="motion")
    db = st.Session()
    try:
        src = db.query(st.M.ScheduleInfo).filter(st.M.ScheduleInfo.id == 1).first()
        return {"copied": getattr(st.sc._clone_schedule_for_split(src), "StandbyResponseMode", "칸 없음")}
    finally:
        db.close()


# ── 근무 중 문맥·호출 ─────────────────────────────────

def _arrive_motion(sim):
    st = MotionStage(sim, scenario_code=False)
    st.set_sched(StandbyTrigger="mic", StandbyResponseMode="motion")
    st.arrive_standby()
    return st


@scenario("C1 단순 동작 근무가 대기 장소에 도착 → 문맥 responseMode=motion, 듣기 켜짐 (현행: single)",
          expect=lambda r: _ok(r) and r["ctx"]["responseMode"] == "motion" and r["ctx"]["listening"] is True)
def c1(sim):
    st = _arrive_motion(sim)
    ctx = st.context()
    return st.out(ctx={k: ctx.get(k) for k in ("responseMode", "listening", "reason", "phase")})


@scenario("C2 단순 동작 근무에 마이크 호출(목적지 말)이 올라옴 → 409, 세션 안 열림, 대기 유지 (현행: 단발 응대 세션 열림)",
          expect=lambda r: _ok(r) and r["res"].get("status") == 409 and r["sessions"] == 0
          and r["phase"] == "standby")
def c2(sim):
    st = _arrive_motion(sim)
    res = st.mic_call("원무과 가고 싶어요")
    return st.out(res={k: res.get(k) for k in ("status", "detail", "ok", "result")},
                  sessions=st.sessions(), phase=st.phase())


@scenario("C3 단순 동작 근무에 화면 [안내 시작]/관제 개입 호출 → 409, 세션 안 열림",
          expect=lambda r: _ok(r) and r["res"].get("status") == 409 and r["sessions"] == 0)
def c3(sim):
    st = _arrive_motion(sim)
    res = st.button_call()
    return st.out(res={k: res.get(k) for k in ("status", "detail")}, sessions=st.sessions())


@scenario("C4 단순 동작 근무 칩 → mode=motion, 안내 단계 없음, '대기 중(응대 가능)' (현행: single·6단계)",
          expect=lambda r: _ok(r) and r["chip"]["mode"] == "motion" and r["chip"]["steps"] == []
          and r["chip"]["phaseLabel"] == "대기 중(응대 가능)")
def c4(sim):
    st = _arrive_motion(sim)
    live = st.live()
    return st.out(chip={k: live.get(k) for k in ("mode", "steps", "phaseLabel", "step")})


# ── 회귀: 기존 응대 방식은 그대로 ─────────────────────

@scenario("R1 회귀 — 단발 근무 문맥은 single",
          expect=lambda r: _ok(r) and r["ctx"]["responseMode"] == "single")
def r1(sim):
    st = MotionStage(sim, scenario_code=False)
    st.set_sched(StandbyTrigger="mic")
    st.arrive_standby()
    return st.out(ctx={"responseMode": st.context().get("responseMode")})


@scenario("R2 회귀 — 시나리오 근무 문맥은 scenario, 마이크 호출 → 안내 세션이 열림",
          expect=lambda r: _ok(r) and r["ctx"]["responseMode"] == "scenario" and len(r["guide"]) == 1)
def r2(sim):
    st = MotionStage(sim)
    st.set_sched(StandbyTrigger="mic")
    st.arrive_standby()
    ctx = st.context()
    st.mic_call()
    return st.out(ctx={"responseMode": ctx.get("responseMode")}, guide=st.guide_sessions())


@scenario("R3 회귀 — 단발 근무 칩은 single·6단계",
          expect=lambda r: _ok(r) and r["chip"]["mode"] in ("single", None) and len(r["chip"]["steps"]) == 6)
def r3(sim):
    st = MotionStage(sim, scenario_code=False)
    st.arrive_standby()
    live = st.live()
    return st.out(chip={k: live.get(k) for k in ("mode", "steps")})
