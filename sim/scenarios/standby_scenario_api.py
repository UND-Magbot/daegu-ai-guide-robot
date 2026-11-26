"""상시 대기 × 안내 시나리오 — 작업 등록 API·마이크 호출·근무 칩 (현행 vs 변경).

무대는 standby_scenario.py 의 Stage 를 그대로 쓴다(로봇 10, 로비 대기 장소 id=1, 병원 시나리오 hospital).
여기서는 화면이 부르는 API 함수(작업 등록·등록 가능 판정·실시간 패널·시나리오 목록)와
로봇 토큰 경로(마이크 호출)를 가상 시계 위에서 직접 부른다.
"""
import importlib.util
import os
from datetime import date
from unittest.mock import MagicMock

from harness import REGISTRY, scenario

# Stage 만 빌려 온다 — 그 파일의 시나리오가 이 묶음에 섞이지 않게 등록분은 되돌린다
_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "standby_scenario_base", os.path.join(os.path.dirname(__file__), "standby_scenario.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
Stage, ROBOT = _base.Stage, _base.ROBOT

USER = MagicMock(id=1, BusinessId=None)


class ApiStage(Stage):
    """Stage + 음성 모듈·다른 맵 장소·꺼진 시나리오·다른 로봇 시나리오."""

    def __init__(self, sim, **kw):
        super().__init__(sim, **kw)
        M = self.M
        db = self.Session()
        mod = M.RobotModule(id=1, RobotId=ROBOT, ModuleType="voice", Label="음성", IsActive=1)
        db.add(mod)
        db.flush()
        db.add(M.ModuleVoiceInfo(ModuleId=1, Port=8090, HasMic=1, HasSpeaker=1))
        db.add(M.LocationInfo(id=3, RobotName="SIM", LacationName="3층 대기", FloorId=2, MapId=2,
                              LocationX=0.0, LocationY=0.0, Yaw=0.0, Category="work"))
        db.add(M.VoiceScenario(id=2, RobotId=ROBOT, Code="parking", DisplayName="주차장 안내",
                               MapId=1, FloorId=1, IsActive=0))
        db.add(M.RobotInfo(id=13, RobotName="OTHER", BusinessId=1, CurrentMapId=1, CurrentFloorId=1))
        db.add(M.VoiceScenario(id=3, RobotId=13, Code="office", DisplayName="관공서 안내",
                               MapId=1, FloorId=1, IsActive=1))
        db.commit()
        db.close()

        import app.database.routes.schedule as sc
        import app.database.routes.standby as sb
        import app.database.routes.agent_bridge as ab
        import app.database.routes.voice_scenario_admin as va
        self.sc, self.sb, self.ab, self.va = sc, sb, ab, va
        sc.write_audit = lambda *a, **k: None
        sc.slot_ids_linked = lambda db, rid: {1}       # 목적지 사전 연결 1건
        sb._scope_ok = lambda *a, **k: True
        sb.write_audit = lambda *a, **k: None
        # 로봇 연결됨 — 지금 근무 시간인 상시 대기는 끊긴 로봇이면 등록을 거절한다(10-22)
        from app.robot_io import polling
        polling._was_online[ROBOT] = True

    # 화면 '작업 등록'
    def insert(self, **over):
        body = dict(RobotName="SIM", TaskName="등록 시험", TaskType="standby", WorkStatus="대기",
                    ScheduleMode="standby", ActiveStartTime="00:00", ActiveEndTime="23:59",
                    SeriesStartDate=date.today().isoformat(), StandbyTrigger="button", ReturnPlaceId=1)
        body.update(over)
        db = self.Session()
        try:
            res = self.sc.insert_schedule(self.sc.ScheduleInsertReq(**body), MagicMock(), db=db,
                                          current_user=USER)
            row = db.query(self.M.ScheduleInfo).filter(self.M.ScheduleInfo.id == res["id"]).first()
            listed = next(r for r in self.sc.get_schedules(db=db, current_user=USER) if r["id"] == res["id"])
            return {"status": 200, "saved": getattr(row, "StandbyScenarioCode", "칸 없음"),
                    "listed": listed.get("StandbyScenarioCode", "응답 없음"),
                    "warnings": res.get("warnings", [])}
        except Exception as e:
            return {"status": getattr(e, "status_code", "예외"), "detail": str(getattr(e, "detail", e))}
        finally:
            db.close()

    def capability(self):
        db = self.Session()
        try:
            return self.sc.standby_capability(db, "SIM")
        finally:
            db.close()

    def set_sched(self, **fields):
        db = self.Session()
        try:
            row = db.query(self.M.ScheduleInfo).filter(self.M.ScheduleInfo.id == 1).first()
            for k, v in fields.items():
                setattr(row, k, v)
            db.commit()
        finally:
            db.close()

    def set_robot_map(self, map_id):
        db = self.Session()
        try:
            db.query(self.M.RobotInfo).filter(self.M.RobotInfo.id == ROBOT).first().CurrentMapId = map_id
            db.commit()
        finally:
            db.close()

    def button_call(self):
        db = self.Session()
        try:
            return self.sb.standby_call(self.sb.StandbyCallReq(scheduleId=1), MagicMock(), db=db,
                                        current_user=USER)
        except Exception as e:
            return {"status": getattr(e, "status_code", "예외"), "detail": str(getattr(e, "detail", e))}
        finally:
            db.close()

    def mic_call(self, heard="내과 가고 싶어요"):
        db = self.Session()
        try:
            return self.ab.agent_standby_call(self.ab.AgentCallReq(heard=heard), db=db, _="sim")
        except Exception as e:
            return {"status": getattr(e, "status_code", "예외"), "detail": str(getattr(e, "detail", e))}
        finally:
            db.close()

    def live(self):
        db = self.Session()
        try:
            return self.sb.standby_live(db=db, current_user=USER)
        finally:
            db.close()

    def guide_sessions(self):
        db = self.Session()
        try:
            return [s.id for s in db.query(self.M.ServiceSession)
                    .filter(self.M.ServiceSession.ScenarioId.isnot(None)).all()]
        finally:
            db.close()


def _ok(r):
    return r["errors"] == 0 and not r.get("crash")


# ── 작업 등록 API ─────────────────────────────────────

@scenario("A1 응대 방식 '병원 안내'로 등록 → 저장·목록에 나옴",
          expect=lambda r: _ok(r) and r["res"]["status"] == 200 and r["res"]["saved"] == "hospital"
          and r["res"]["listed"] == "hospital")
def a1(sim):
    st = ApiStage(sim)
    return {"res": st.insert(StandbyScenarioCode="hospital")}


@scenario("A2 없는 시나리오 코드 → 400 (현행: 조용히 단발 응대로 저장)",
          expect=lambda r: _ok(r) and r["res"]["status"] == 400)
def a2(sim):
    st = ApiStage(sim)
    return {"res": st.insert(StandbyScenarioCode="xyz")}


@scenario("A3 꺼진 시나리오(주차장) → 400",
          expect=lambda r: _ok(r) and r["res"]["status"] == 400)
def a3(sim):
    st = ApiStage(sim)
    return {"res": st.insert(StandbyScenarioCode="parking")}


@scenario("A4 다른 로봇의 시나리오 코드 → 400",
          expect=lambda r: _ok(r) and r["res"]["status"] == 400)
def a4(sim):
    st = ApiStage(sim)
    return {"res": st.insert(StandbyScenarioCode="office")}


@scenario("A5 회귀 — 응대 방식 비움 → 단발 응대(NULL)로 저장",
          expect=lambda r: _ok(r) and r["res"]["status"] == 200 and r["res"]["saved"] is None)
def a5(sim):
    st = ApiStage(sim)
    return {"res": st.insert()}


@scenario("A6 대기 장소가 시나리오와 다른 맵 → 저장하되 경고",
          expect=lambda r: _ok(r) and r["res"]["status"] == 200 and r["res"]["saved"] == "hospital"
          and any("맵이 다릅니다" in w for w in r["res"]["warnings"]))
def a6(sim):
    st = ApiStage(sim)
    return {"res": st.insert(StandbyScenarioCode="hospital", ReturnPlaceId=3)}


@scenario("A7 등록 가능 판정에 고를 수 있는 시나리오(켜진 것만)",
          expect=lambda r: _ok(r) and r["codes"] == ["hospital"])
def a7(sim):
    st = ApiStage(sim)
    cap = st.capability()
    return {"codes": [s["code"] for s in cap.get("scenarios", [])], "triggers": cap.get("allowedTriggers")}


@scenario("A8 시리즈 분할 복사 때 응대 방식 유지",
          expect=lambda r: _ok(r) and r["copied"] == "hospital")
def a8(sim):
    st = ApiStage(sim)
    db = st.Session()
    try:
        src = db.query(st.M.ScheduleInfo).filter(st.M.ScheduleInfo.id == 1).first()
        return {"copied": st.sc._clone_schedule_for_split(src).StandbyScenarioCode}
    finally:
        db.close()


# ── 호출 ──────────────────────────────────────────────

@scenario("B1 마이크 호출 + 시나리오 작업 → 안내 시나리오 세션이 열림 (현행: 단발 응대)",
          expect=lambda r: _ok(r) and len(r["guide"]) == 1 and r["phase"] == "serving")
def b1(sim):
    st = ApiStage(sim)
    st.set_sched(StandbyTrigger="mic")
    st.arrive_standby()
    res = st.mic_call()
    return st.out(res=res, guide=st.guide_sessions())


@scenario("B2 회귀 — 마이크 호출 + 단발 작업 → 시나리오 세션 없음",
          expect=lambda r: _ok(r) and r["guide"] == [])
def b2(sim):
    st = ApiStage(sim, scenario_code=False)
    st.set_sched(StandbyTrigger="mic")
    st.arrive_standby()
    res = st.mic_call("내과")
    return st.out(res={k: res.get(k) for k in ("ok", "result", "status", "detail")}, guide=st.guide_sessions())


@scenario("B3 로봇이 시나리오와 다른 맵에 있을 때 버튼 호출 → 409, 세션 안 열림 (현행: 열림)",
          expect=lambda r: _ok(r) and r["res"].get("status") == 409 and r["guide"] == [])
def b3(sim):
    st = ApiStage(sim)
    st.arrive_standby()
    st.set_robot_map(2)
    res = st.button_call()
    return st.out(res=res, guide=st.guide_sessions())


@scenario("B4 회귀 — 같은 맵에서 버튼 호출 → 시나리오 세션",
          expect=lambda r: _ok(r) and isinstance(r["res"].get("sessionId"), int))
def b4(sim):
    st = ApiStage(sim)
    st.arrive_standby()
    return st.out(res=st.button_call())


# ── 근무 칩(실시간 패널) ──────────────────────────────

def _chip(live):
    return {k: live.get(k) for k in ("phaseLabel", "mode", "scenarioName", "step", "steps")} | {
        "guide_id": (live.get("guide") or {}).get("id")}


@scenario("C1 시나리오 안내 중 → '안내 중'·mode=scenario·세션 id (현행: '대기 장소로 이동 중')",
          expect=lambda r: _ok(r) and r["chip"]["phaseLabel"] == "안내 중" and r["chip"]["mode"] == "scenario"
          and r["chip"]["guide_id"] == r["sid"] and r["chip"]["step"] == 1)
def c1(sim):
    st = ApiStage(sim)
    st.arrive_standby()
    sid = st.button_call().get("sessionId")
    return {"sid": sid, "chip": _chip(st.live())}


@scenario("C2 안내 중 배터리 기준 미만(3회 확인 전) → '안내 중' 유지, '위급' 예고 없음(10-22 정책: 확인되면 바로 멈추고 충전소 — C3)",
          expect=lambda r: _ok(r) and r["chip"]["phaseLabel"] == "안내 중")
def c2(sim):
    st = ApiStage(sim)
    st.arrive_standby()
    st.button_call()
    st.battery(12)
    return {"chip": _chip(st.live())}


@scenario("C3 배터리 중단 종료 뒤 → 충전 표시(away 문구)",
          expect=lambda r: _ok(r) and "충전" in r["chip"]["phaseLabel"] and r["chip"]["guide_id"] is None)
def c3(sim):
    st = ApiStage(sim)
    st.arrive_standby()
    sid = st.button_call().get("sessionId")
    st.battery(12)
    st.jetson_close(sid, "aborted_battery")
    return {"chip": _chip(st.live()), "alerts": st.count("guide_battery_aborted")}


@scenario("C4 시나리오 안내 끝 → 복귀 중 표시 → 대기 중",
          expect=lambda r: _ok(r) and r["returning"] == "대기 장소로 복귀 중"
          and r["after"] == "대기 중(응대 가능)")
def c4(sim):
    st = ApiStage(sim)
    st.arrive_standby()
    sid = st.button_call().get("sessionId")
    st.jetson_move(sid)
    db = st.Session()
    try:
        st.vs.close_session(sid, st.vs.SessionCloseReq(result="completed", closedBy="voice"), db=db, _="sim")
    finally:
        db.close()
    sim.wait(1.0)
    returning = st.live().get("phaseLabel")
    st.drive()
    return {"returning": returning, "after": st.live().get("phaseLabel")}


@scenario("C5 회귀 — 단발 작업 대기 중 → mode=single·6단계",
          expect=lambda r: _ok(r) and r["chip"]["phaseLabel"] == "대기 중(응대 가능)"
          and r["chip"]["mode"] in ("single", None) and len(r["chip"]["steps"]) == 6)
def c5(sim):
    st = ApiStage(sim, scenario_code=False)
    st.arrive_standby()
    return {"chip": _chip(st.live())}


# ── 음성관리 '사용 중 작업' ──────────────────────────

@scenario("D1 시나리오 목록에 이 시나리오를 쓰는 상시 대기 작업",
          expect=lambda r: _ok(r) and r["used"].get("hospital") == [1] and r["used"].get("parking") == [])
def d1(sim):
    st = ApiStage(sim)
    st.set_sched(ScheduleMode="standby")     # 무대 작업은 근무 진입 시험용이라 once 로 만들어져 있다
    db = st.Session()
    try:
        res = st.va.list_voice_scenarios(ROBOT, db=db, current_user=USER)
    finally:
        db.close()
    return {"used": {s["code"]: [t["id"] for t in s.get("standbyTasks", [])] if "standbyTasks" in s else "응답 없음"
                     for s in res["scenarios"]}}
