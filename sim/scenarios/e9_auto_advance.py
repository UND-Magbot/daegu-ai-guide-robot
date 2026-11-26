"""E9 운영자 단계별 자동 진행 시간(VoiceScenarioStep.AutoAdvanceSec) — 현행 vs 변경.

docs/본사_Demo_시연_관제단계표_261019.md 의 E9. 관제는 운영자 입력 단계(WaitFor=operator)에서
엔진이 result=paused 를 보내면 자동 진행을 예약한다(routes/voice_scenario._auto_operator_input).

Jetson 단계 엔진은 돌리지 않는다 — 엔진이 할 일(대기 시작 patch)을 시나리오가 대신 부른다.
운영자 입력은 voice_scenario_admin.operator_input 의 DB 변경(다음 단계·running)과 같게 직접 넣는다
(그 함수는 로그인 사용자·권한 의존이 있어 하네스에서 부르지 않는다 — 가정).
"""
from datetime import datetime

from harness import scenario

ROBOT = 10
NEXT = "after"          # 자동·운영자 진행이 넘어갈 다음 단계(대답 단계)


def setup(intent: str, auto=None, step_code: str = "op_wait") -> int:
    """운영자 단계 하나 + 다음 대답 단계, 그 운영자 단계에 서 있는 세션을 만든다. 반환: 세션 id"""
    from app.database.database import SessionLocal
    from app.database import models as M
    db = SessionLocal()
    db.add(M.RobotInfo(id=ROBOT, RobotName="SIM", BusinessId=1, CurrentMapId=1, CurrentFloorId=1))
    db.add(M.VoiceScenario(id=1, RobotId=ROBOT, Code="hospital", DisplayName="병원 안내",
                           MapId=1, FloorId=1, IsActive=1))
    kw = dict(ScenarioId=1, StepCode=step_code, StepOrder=10, SayText="기다려 주세요.",
              WaitFor="operator", ExpectedIntent=intent, NextStepCode=NEXT)
    if auto is not None and hasattr(M.VoiceScenarioStep, "AutoAdvanceSec"):
        kw["AutoAdvanceSec"] = auto
    db.add(M.VoiceScenarioStep(**kw))
    db.add(M.VoiceScenarioStep(ScenarioId=1, StepCode=NEXT, StepOrder=20, SayText="다음",
                               WaitFor="user", ExpectedIntent="YES_NO"))
    s = M.ServiceSession(ScenarioId=1, RobotName="SIM", StartedAt=datetime.now(),
                         Result="running", CurrentStepCode=step_code)
    db.add(s)
    db.commit()
    sid = s.id
    db.close()
    return sid


def engine_paused(sim, sid: int) -> None:
    """엔진이 운영자 단계에 닿아 입력 대기를 알린다(Jetson _wait_operator 의 _patch(result=paused))."""
    from app.database.database import SessionLocal
    from app.database.routes.voice_scenario import SessionPatchReq, patch_session
    db = SessionLocal()
    try:
        sim.run(lambda: patch_session(sid, SessionPatchReq(result="paused"), db=db, _="agent"))
    finally:
        db.close()


def operator(sid: int) -> None:
    """운영자 입력 — operator_input 의 RESUME 처리와 같은 DB 변경."""
    from app.database.database import SessionLocal
    from app.database import models as M
    db = SessionLocal()
    s = db.query(M.ServiceSession).filter(M.ServiceSession.id == sid).first()
    if s.CurrentStepCode != NEXT:
        s.CurrentStepCode, s.Result = NEXT, "running"
        s.Heard = "operator"            # 누가 넘겼는지 표시(관찰용)
        db.commit()
    db.close()


def close_session(sid: int) -> None:
    from app.database.database import SessionLocal
    from app.database import models as M
    db = SessionLocal()
    s = db.query(M.ServiceSession).filter(M.ServiceSession.id == sid).first()
    s.Result, s.EndedAt = "aborted_operator", datetime.now()
    db.commit()
    db.close()


def watch(sim, sid: int, horizon: float, events: dict | None = None) -> dict:
    """1초 간격으로 단계를 본다. events={초: 함수}. 반환: 넘어간 시각·방법·번호."""
    from app.database.database import SessionLocal
    from app.database import models as M
    adv_at, by = None, None
    for t in range(1, int(horizon) + 1):
        sim.wait(1.0)
        if events and t in events:
            events[t](sid)
        db = SessionLocal()
        s = db.query(M.ServiceSession).filter(M.ServiceSession.id == sid).first()
        if adv_at is None and s.CurrentStepCode == NEXT:
            adv_at, by = t, ("운영자" if s.Heard == "operator" else "자동")
        last = (s.CurrentStepCode, s.Result, s.TicketNo, s.CounterNo)
        db.close()
    return {"adv_at": adv_at, "by": by, "step": last[0], "res": last[1],
            "ticket": last[2], "counter": last[3]}


def wire_clock(sim) -> None:
    """routes.voice_scenario 는 하네스 시작 뒤에 import 돼 threading·time 이 실제 모듈로 남는다.
    자동 진행 Timer 가 가상 시계로 돌게 여기서 바꿔 끼운다(하네스 _wire 와 같은 방식)."""
    import threading as real_threading
    import time as real_time
    from app.database.routes import voice_scenario as vs
    if vs.threading is real_threading:
        vs.threading = sim.clock.threading_module(real_threading)
    if getattr(vs, "time", None) is real_time:
        vs.time = sim.clock.time_module()


def run_case(sim, intent, auto=None, horizon=60, events=None):
    wire_clock(sim)
    sid = setup(intent, auto)
    engine_paused(sim, sid)
    # 번호표는 patch 안에서 바로 넘어간다 — 0초 진행
    from app.database.database import SessionLocal
    from app.database import models as M
    db = SessionLocal()
    s = db.query(M.ServiceSession).filter(M.ServiceSession.id == sid).first()
    immediate = s.CurrentStepCode == NEXT
    db.close()
    out = watch(sim, sid, horizon, events)
    if immediate:
        out["adv_at"], out["by"] = 0, "자동"
    return out


def near(v, want, tol=1.5):
    return v is not None and abs(v - want) <= tol


# ── 회귀: 단계 값이 없으면 지금 규칙 그대로 ────────────────────────
@scenario("회귀 · 재개 단계 값 없음 → 5초 뒤 자동",
          expect=lambda r: near(r["adv_at"], 5) and r["by"] == "자동")
def reg_resume(sim):
    return run_case(sim, "RESUME")


@scenario("회귀 · 번호표 값 없음 → 바로 10번",
          expect=lambda r: r["adv_at"] == 0 and r["ticket"] == "10")
def reg_ticket(sim):
    return run_case(sim, "TICKET", horizon=10)


@scenario("회귀 · 창구 값 없음 → 5초 뒤 1번",
          expect=lambda r: near(r["adv_at"], 5) and r["counter"] == "1")
def reg_counter(sim):
    return run_case(sim, "COUNTER")


@scenario("회귀 · 예약 뒤 세션 종료 → 진행 안 함",
          expect=lambda r: r["adv_at"] is None and r["step"] == "op_wait")
def reg_closed(sim):
    return run_case(sim, "RESUME", horizon=20, events={2: close_session})


# ── 바뀌어야 하는 것 ───────────────────────────────────────────
@scenario("1F 편의점 대기(-1) → 2분 기다려도 그대로",
          expect=lambda r: r["adv_at"] is None and r["step"] == "op_wait" and r["res"] == "paused")
def store_wait_off(sim):
    return run_case(sim, "RESUME", auto=-1, horizon=120)


@scenario("1F 편의점 대기(-1) + 운영자 40초 입력 → 40초에 운영자로",
          expect=lambda r: r["adv_at"] == 40 and r["by"] == "운영자")
def store_wait_operator(sim):
    return run_case(sim, "RESUME", auto=-1, horizon=60, events={40: operator})


@scenario("1F 진료실 입구(-1) → 5분 기다려도 그대로",
          expect=lambda r: r["adv_at"] is None and r["step"] == "op_wait")
def clinic_wait_off(sim):
    return run_case(sim, "RESUME", auto=-1, horizon=300)


@scenario("1F 대기 자리(20) → 20초 뒤 자동",
          expect=lambda r: near(r["adv_at"], 20) and r["by"] == "자동")
def seat_wait_20(sim):
    return run_case(sim, "RESUME", auto=20, horizon=40)


@scenario("1F 대기 자리(20) + 운영자 10초 입력 → 10초 운영자, 20초 예약은 무시",
          expect=lambda r: r["adv_at"] == 10 and r["by"] == "운영자" and r["step"] == NEXT)
def seat_wait_operator_first(sim):
    return run_case(sim, "RESUME", auto=20, horizon=40, events={10: operator})


@scenario("3F 대기(창구, 15) → 15초 뒤 창구 1번",
          expect=lambda r: near(r["adv_at"], 15) and r["counter"] == "1")
def office_seat_15(sim):
    return run_case(sim, "COUNTER", auto=15, horizon=30)


@scenario("번호표 단계(-1) → 자동 발급 안 함(운영자 직접 입력)",
          expect=lambda r: r["adv_at"] is None and r["ticket"] in (None, ""))
def ticket_off(sim):
    return run_case(sim, "TICKET", auto=-1, horizon=10)


@scenario("전역 자동 끔(-1) + 단계 15 → 그 단계만 15초 뒤",
          env={"VOICE_AUTO_OPERATOR_SEC": "-1"},
          expect=lambda r: near(r["adv_at"], 15))
def global_off_step_on(sim):
    return run_case(sim, "RESUME", auto=15, horizon=30)


@scenario("전역 자동 끔(-1) + 단계 값 없음 → 그대로(회귀)",
          env={"VOICE_AUTO_OPERATOR_SEC": "-1"},
          expect=lambda r: r["adv_at"] is None)
def global_off_step_none(sim):
    return run_case(sim, "RESUME", horizon=30)
