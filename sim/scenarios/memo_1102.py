"""docs/memo.md 개선점 3건 중 관제 백엔드 부분 (현행 vs 변경, 11-02).

① 편의점 입구에서 "진료대기장소로 안내해줘" 를 무시하고 간호실로 출발(세션 527)
   원인: store_wait(운영자 단계)는 듣지 않고, 단계 값이 없어 전역 규칙(5초)으로 turn_ready 로 자동 진행.
   변경: 운영자 단계 '음성 출구'(VoiceExitKeywords·VoiceExitStepCode)
         - 음성 출구가 있는 단계는 전역 5초 자동 진행을 하지 않는다(단계 값이 있으면 그 값)
         - 엔진의 조건부 단계 이동(fromStepCode) — 운영자가 먼저 넘겼으면 덮어쓰지 않는다
         - 편집기 검사: 음성 출구도 연결로 본다(store_return_move '실행 안 됨' 경고 해소), 짝 없는 칸은 오류
② 'M20-SIM이(가) 연결되어 있지 않아…' — 로봇 목록에 없는 로봇의 작업을 '연결 안 됨'으로 알림
   변경: 등록되지 않은 로봇은 schedule_robot_missing 으로 따로 알리고, 알림 로봇 이름은 작업의 로봇 이름
엔진(Jetson)은 돌리지 않는다 — 엔진이 할 patch 를 시나리오가 대신 부른다(e9_auto_advance.py 와 같은 방식).
"""
import importlib.util
import os
from datetime import date, datetime, timedelta

from harness import REGISTRY, scenario

ROBOT = 10
WAIT, NEXT, BACK = "store_wait", "turn_ready", "store_return_move"
KEYWORDS = "대기 자리, 대기자리, 대기장소, 진료대기, 돌아갈"


def _load(name, alias):
    n = len(REGISTRY)
    spec = importlib.util.spec_from_file_location(alias, os.path.join(os.path.dirname(__file__), name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    del REGISTRY[n:]
    return mod


_e9 = _load("e9_auto_advance.py", "e9_base_memo1102")
_map = _load("standby_map.py", "standby_map_base_memo1102")
_api = _load("standby_scenario_api.py", "standby_api_base_memo1102")


# ── ① 음성 출구 ───────────────────────────────────────────────
def setup_store(auto=None, voice_exit=True) -> int:
    """병원 시나리오의 편의점 앞 대기(store_wait) + 다음(turn_ready) + 음성 출구(store_return_move)."""
    from app.database.database import SessionLocal
    from app.database import models as M
    db = SessionLocal()
    db.add(M.RobotInfo(id=ROBOT, RobotName="SIM", BusinessId=1, CurrentMapId=1, CurrentFloorId=1))
    db.add(M.VoiceScenario(id=1, RobotId=ROBOT, Code="hospital", DisplayName="병원 안내",
                           MapId=1, FloorId=1, IsActive=1))
    kw = dict(ScenarioId=1, StepCode=WAIT, StepOrder=140, WaitFor="operator", ExpectedIntent="RESUME",
              SayText="편의점 입구에 도착했습니다. 대기 자리로 돌아가실 때는 '대기 자리로 안내해 줘'라고 말씀해 주세요.",
              NextStepCode=NEXT, AutoAdvanceSec=auto)
    if voice_exit and hasattr(M.VoiceScenarioStep, "VoiceExitKeywords"):
        kw.update(VoiceExitKeywords=KEYWORDS, VoiceExitStepCode=BACK)
    db.add(M.VoiceScenarioStep(**kw))
    db.add(M.VoiceScenarioStep(ScenarioId=1, StepCode=BACK, StepOrder=150, SayText="네, 대기 자리로 모시겠습니다.",
                               WaitFor="arrival", ActionType="navigate", NextStepCode="seat_wait"))
    db.add(M.VoiceScenarioStep(ScenarioId=1, StepCode=NEXT, StepOrder=170, SayText="진료 순서가 되었습니다.",
                               WaitFor="user", ExpectedIntent="YES_NO"))
    s = M.ServiceSession(ScenarioId=1, RobotName="SIM", StartedAt=datetime.now(), Result="running",
                         CurrentStepCode=WAIT)
    db.add(s)
    db.commit()
    sid = s.id
    db.close()
    return sid


def step_of(sid):
    from app.database.database import SessionLocal
    from app.database import models as M
    db = SessionLocal()
    try:
        s = db.query(M.ServiceSession).filter(M.ServiceSession.id == sid).first()
        return s.CurrentStepCode, s.Result
    finally:
        db.close()


def engine_patch(sim, sid, **fields):
    """엔진의 세션 갱신. 현행 SessionPatchReq 에 없는 칸(fromStepCode)은 pydantic 이 버린다 — 현행 동작 그대로."""
    from app.database.database import SessionLocal
    from app.database.routes.voice_scenario import SessionPatchReq, patch_session
    db = SessionLocal()
    try:
        out = sim.run(lambda: patch_session(sid, SessionPatchReq(**fields), db=db, _="agent"))
        return getattr(out, "currentStepCode", None)
    finally:
        db.close()


def first_advance(sim, sid, horizon, events=None):
    """1초 간격으로 단계를 본다. 반환: (처음 바뀐 시각, 그때 단계, 마지막 단계)"""
    at, to = None, None
    for t in range(1, horizon + 1):
        sim.wait(1.0)
        if events and t in events:
            events[t]()
        cur, _res = step_of(sid)
        if at is None and cur != WAIT:
            at, to = t, cur
    return at, to, step_of(sid)[0]


def _ok(r):
    return r["errors"] == 0 and not r.get("crash")


@scenario("V1 재현 · 편의점 앞(음성 출구, 단계 값 없음) → 현행 5초 뒤 진료실로 / 변경: 60초 기다려도 그대로",
          expect=lambda r: _ok(r) and r["adv_at"] is None and r["step"] == WAIT)
def v1(sim):
    _e9.wire_clock(sim)
    sid = setup_store()
    engine_patch(sim, sid, result="paused")
    at, to, last = first_advance(sim, sid, 60)
    return {"adv_at": at, "to": to, "step": last}


@scenario("V2 방문자 '진료대기장소로 안내해줘'(엔진 음성 출구, 12초) → store_return_move 로",
          expect=lambda r: _ok(r) and r["adv_at"] == 12 and r["to"] == BACK and r["reply"] == BACK)
def v2(sim):
    _e9.wire_clock(sim)
    sid = setup_store()
    engine_patch(sim, sid, result="paused")
    got = {}
    ev = {12: lambda: got.setdefault("reply", engine_patch(sim, sid, currentStepCode=BACK,
                                                            fromStepCode=WAIT, result="running"))}
    at, to, last = first_advance(sim, sid, 30, ev)
    return {"adv_at": at, "to": to, "step": last, "reply": got.get("reply")}


@scenario("V3 경합 · 운영자 [재개] 10초 → 엔진 음성 출구 11초(늦게 옴) → 변경: 운영자 결정 유지(turn_ready)",
          expect=lambda r: _ok(r) and r["step"] == NEXT and r["reply"] == NEXT)
def v3(sim):
    _e9.wire_clock(sim)
    sid = setup_store()
    engine_patch(sim, sid, result="paused")
    got = {}

    def operator():
        from app.database.database import SessionLocal
        from app.database import models as M
        db = SessionLocal()
        s = db.query(M.ServiceSession).filter(M.ServiceSession.id == sid).first()
        s.CurrentStepCode, s.Result = NEXT, "running"
        db.commit()
        db.close()
    ev = {10: operator,
          11: lambda: got.setdefault("reply", engine_patch(sim, sid, currentStepCode=BACK,
                                                            fromStepCode=WAIT, result="running"))}
    at, to, last = first_advance(sim, sid, 20, ev)
    return {"adv_at": at, "to": to, "step": last, "reply": got.get("reply")}


@scenario("V4 회귀 · 음성 출구 없는 재개 단계(값 없음) → 5초 뒤 자동 진행",
          expect=lambda r: _ok(r) and r["adv_at"] is not None and abs(r["adv_at"] - 5) <= 1.5 and r["to"] == NEXT)
def v4(sim):
    _e9.wire_clock(sim)
    sid = setup_store(voice_exit=False)
    engine_patch(sim, sid, result="paused")
    at, to, last = first_advance(sim, sid, 20)
    return {"adv_at": at, "to": to, "step": last}


@scenario("V5 음성 출구 + 단계 값 30초 → 30초 뒤 자동 진행(단계 값이 우선)",
          expect=lambda r: _ok(r) and r["adv_at"] is not None and abs(r["adv_at"] - 30) <= 1.5)
def v5(sim):
    _e9.wire_clock(sim)
    sid = setup_store(auto=30)
    engine_patch(sim, sid, result="paused")
    at, to, last = first_advance(sim, sid, 45)
    return {"adv_at": at, "to": to, "step": last}


@scenario("V6 회귀 · fromStepCode 없는 일반 단계 갱신은 그대로 반영",
          expect=lambda r: _ok(r) and r["step"] == NEXT)
def v6(sim):
    sid = setup_store(voice_exit=False)
    engine_patch(sim, sid, currentStepCode=NEXT, result="running")
    return {"step": step_of(sid)[0]}


# ── ① 편집기 검사 ─────────────────────────────────────────────
def _hospital_steps(**exit_fields):
    base = dict(stepLabel=None, expectedIntent=None, retryCount=2, responseTimeoutSec=10, slotName=None,
                targetLocationId=None, onNoStepCode=None, onTimeoutStepCode=None, autoAdvanceSec=None,
                spokenPlaceName=None, progressText=None, isActive=True)
    rows = [
        dict(stepCode="greet", sayText="안녕하세요", waitFor="none", nextStepCode="store_move"),
        dict(stepCode="store_move", sayText="편의점으로", waitFor="arrival", targetLocationId=1, nextStepCode=WAIT),
        dict(stepCode=WAIT, sayText="대기 자리로 안내해 줘라고 말씀해 주세요", waitFor="operator",
             expectedIntent="RESUME", nextStepCode=NEXT, **exit_fields),
        dict(stepCode=BACK, sayText="대기 자리로 모시겠습니다", waitFor="arrival", targetLocationId=2,
             nextStepCode="seat_wait"),
        dict(stepCode="seat_wait", sayText="기다려 주세요", waitFor="operator", expectedIntent="RESUME",
             nextStepCode=NEXT),
        dict(stepCode=NEXT, sayText="준비됐어요라고 말씀해 주세요", waitFor="user", expectedIntent="YES_NO",
             onNoStepCode=NEXT, nextStepCode="finish"),
        dict(stepCode="finish", sayText="감사합니다", waitFor="none", nextStepCode=None),
    ]
    return [{**base, **r} for r in rows]


def _check(steps):
    from app.database import voice_scenario_editor as ed
    res = ed.check(ed.normalize(steps), map_id=1, map_place_ids=[1, 2])
    unreached = sorted(w["stepCode"] for w in res["warnings"] if w.get("short") == ed.UNREACHED_SHORT)
    return {"err_fields": sorted({e["field"] for e in res["errors"] if e.get("field")}),
            "err_n": len(res["errors"]), "unreached": unreached}


@scenario("E1 편집기 · store_wait 음성 출구 → store_return_move·seat_wait '실행 안 됨' 경고 없음, 오류 없음",
          expect=lambda r: _ok(r) and r["unreached"] == [] and r["err_n"] == 0)
def e1(sim):
    return _check(_hospital_steps(voiceExitKeywords=KEYWORDS, voiceExitStepCode=BACK))


@scenario("E2 편집기 회귀 · 음성 출구 없음 → 지금처럼 store_return_move·seat_wait 경고",
          expect=lambda r: _ok(r) and r["unreached"] == ["seat_wait", BACK] and r["err_n"] == 0)
def e2(sim):
    return _check(_hospital_steps())


@scenario("E3 편집기 · 말만 있고 넘어갈 단계 없음 / 한 글자 말 / 없는 단계 → 저장 막는 오류",
          expect=lambda r: _ok(r) and r["a"] == ["voiceExitStepCode"] and r["b"] == ["voiceExitKeywords"]
          and r["c"] == ["voiceExitStepCode"])
def e3(sim):
    return {"a": _check(_hospital_steps(voiceExitKeywords=KEYWORDS))["err_fields"],
            "b": _check(_hospital_steps(voiceExitKeywords="대기 자리, 네", voiceExitStepCode=BACK))["err_fields"],
            "c": _check(_hospital_steps(voiceExitKeywords=KEYWORDS, voiceExitStepCode="nope"))["err_fields"]}


@scenario("E4 편집기 · 대답 단계로 바꾸면 음성 출구 칸을 비운다 / 말 목록 정리(공백·중복)",
          expect=lambda r: _ok(r) and r["cleared"] == (None, None) and r["words"] == "대기 자리, 돌아갈")
def e4(sim):
    from app.database import voice_scenario_editor as ed
    steps = _hospital_steps(voiceExitKeywords=" 대기  자리 ,돌아갈,대기 자리,, ", voiceExitStepCode=BACK)
    norm = {s["stepCode"]: s for s in ed.normalize(steps)}
    as_user = [dict(s, waitFor="user", expectedIntent="YES_NO") if s["stepCode"] == WAIT else s for s in steps]
    u = {s["stepCode"]: s for s in ed.normalize(as_user)}[WAIT]
    return {"words": norm[WAIT].get("voiceExitKeywords"),
            "cleared": (u.get("voiceExitKeywords"), u.get("voiceExitStepCode"))}


# ── ② 등록되지 않은 로봇의 작업 ────────────────────────────────
class LogCap:
    """log_event 를 감싸 action·로봇 이름까지 받아 둔다(하네스 events 는 본문만 남긴다)."""

    def __init__(self, lp):
        self.rows = []
        inner = lp.log_event

        def rec(*a, **k):
            if len(a) > 1:
                self.rows.append((a[1], a[2] if len(a) > 2 else "", k.get("robot_name"), k.get("robot_id")))
            return inner(*a, **k)
        lp.log_event = rec

    def of(self, action):
        return [r for r in self.rows if r[0] == action]


class ShiftStage(_map.MapStage):
    def __init__(self, sim, robot_name="SIM", online=False):
        is_online = sim.lp._is_target_robot_online          # Stage 가 True 로 덮기 전 원본
        super().__init__(sim, place_map=1, robot_map=1)
        self.lp._is_target_robot_online = is_online
        from app.robot_io import polling
        polling._was_online[ROBOT] = online
        db = self.Session()
        db.query(self.M.ScheduleInfo).filter(self.M.ScheduleInfo.id == 1).update(
            {self.M.ScheduleInfo.RobotName: robot_name})
        db.commit()
        db.close()
        self.cap = LogCap(self.lp)


@scenario("M1 재현 · 상시 대기 작업 로봇 'M20-SIM'(목록에 없음) → 변경: '등록되지 않은 로봇' 알림 1건(틱 3번), "
          "알림 로봇=M20-SIM, '연결 안 됨' 없음",
          expect=lambda r: _ok(r) and not r["started"] and r["hold"] == [] and len(r["missing"]) == 1
          and r["missing"][0][2] == "M20-SIM" and "로봇 목록에 없어" in r["missing"][0][1])
def m1(sim):
    st = ShiftStage(sim, robot_name="M20-SIM")
    started = any([st.start_shift(), st.start_shift(), st.start_shift()])
    return {"started": started, "hold": st.cap.of("standby_offline_hold"),
            "missing": st.cap.of("schedule_robot_missing")}


@scenario("M2 회귀 · 등록된 로봇이 끊김 → '연결 안 됨' 알림 1건 그대로, 알림 로봇=작업 로봇(SIM)",
          expect=lambda r: _ok(r) and not r["started"] and len(r["hold"]) == 1 and r["hold"][0][2] == "SIM"
          and r["missing"] == [])
def m2(sim):
    st = ShiftStage(sim)
    started = any([st.start_shift(), st.start_shift()])
    return {"started": started, "hold": st.cap.of("standby_offline_hold"),
            "missing": st.cap.of("schedule_robot_missing")}


@scenario("M3 회귀 · 등록된 로봇 연결됨 → 알림 없이 근무 시작",
          expect=lambda r: _ok(r) and r["started"] and r["hold"] == [] and r["missing"] == [])
def m3(sim):
    st = ShiftStage(sim, online=True)
    return {"started": st.start_shift(), "hold": st.cap.of("standby_offline_hold"),
            "missing": st.cap.of("schedule_robot_missing")}


def _planned():
    return datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=23, minutes=30)


class WeeklyStage(_api.ApiStage):
    def __init__(self, sim):
        is_online = sim.lp._is_target_robot_online
        super().__init__(sim)
        self.lp._is_target_robot_online = is_online
        from app.robot_io import polling
        polling._was_online[ROBOT] = True
        self.cap = LogCap(self.lp)

    def add_weekly_for(self, robot_name):
        from unittest.mock import MagicMock
        body = dict(RobotName="SIM", TaskName="순회", TaskType="patrol", WorkStatus="대기", WayName="경로1",
                    ScheduleMode="weekly", ExecutionTime="23:30", RepeatDays="월,화,수,목,금,토,일",
                    SeriesStartDate=date.today().isoformat())
        db = self.Session()
        try:
            sid = self.sc.insert_schedule(self.sc.ScheduleInsertReq(**body), MagicMock(), db=db,
                                          current_user=_api.USER)["id"]
            db.query(self.M.ScheduleInfo).filter(self.M.ScheduleInfo.id == sid).update(
                {self.M.ScheduleInfo.RobotName: robot_name})
            db.commit()
            return sid
        finally:
            db.close()

    def tick(self, sid, now):
        db = self.Session()
        try:
            sched = db.query(self.M.ScheduleInfo).filter(self.M.ScheduleInfo.id == sid).first()
            grace = self.lp.runs.grace_minutes(sched)
            self.lp._record_expired(db, sched, now, grace)
            return self.lp._try_execute(db, sched, now, grace) if self.lp._active_schedule_id is None else False
        finally:
            db.close()


@scenario("M4 재현 · 반복 작업 로봇 'M20-SIM' 실행 시각 → 변경: '등록되지 않은 로봇' 알림(연결 대기 알림 없음), 실행 안 함",
          expect=lambda r: _ok(r) and not r["started"] and r["wait"] == 0 and r["missing_first"] == 1
          and r["missing_robot"] == "M20-SIM")
def m4(sim):
    st = WeeklyStage(sim)
    st.lp._watch_since = _planned() - timedelta(hours=1)
    sid = st.add_weekly_for("M20-SIM")
    p = _planned()
    started = any(st.tick(sid, p + timedelta(seconds=s)) for s in (10, 40, 70))
    missing = st.cap.of("schedule_robot_missing")
    return {"started": started, "wait": len(st.cap.of("schedule_wait_offline")),
            "missing_first": len(missing), "missing_robot": missing[0][2] if missing else None}
