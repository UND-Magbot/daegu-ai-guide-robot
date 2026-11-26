"""안내 시나리오 편집기 S1 서버 API — 새로 만들기·검사·단계 전체 저장·버전·삭제·사용 켜기 (현행 vs 변경).

무대: 관제 DB(dev) 에서 읽기 전용으로 뽑은 sim/out/fixture_dev_voice.json(사업장·층·맵·장소·로봇·시나리오·단계).
로봇 10 = 사업장 1, 시나리오 hospital(맵 47) · public_office(맵 43) · parking(맵 46).
현행에는 편집기 API 가 없으므로 해당 시나리오는 'API 없음' 으로 실패한다(새 기능).
"""
import json
import os
from unittest.mock import MagicMock

from harness import scenario

ADMIN = MagicMock(id=1, Permission=1, BusinessId=None)
FIXTURE = os.path.join(os.path.dirname(__file__), "..", "out", "fixture_dev_voice.json")
ROBOT = 10
DROP = {"CreatedAt", "UpdatedAt", "AppliedAt", "Adddate"}


class Stage:
    def __init__(self, sim):
        from app.database.database import SessionLocal
        from app.database import models as M
        import app.database.routes.voice_scenario as vs
        import app.database.routes.voice_scenario_admin as va
        sim.rewire_late_modules()
        self.M, self.Session, self.vs, self.va = M, SessionLocal, vs, va
        try:
            import app.database.routes.voice_scenario_editor as ve
            import app.database.voice_scenario_editor as ed
            self.ve, self.ed = ve, ed
        except ImportError:
            self.ve = self.ed = None
        for mod in (va, self.ve):
            if mod:
                mod.write_audit = lambda *a, **k: None
        vs._ensure_standing = lambda *a, **k: None
        import app.user_cache as uc
        uc.get_robot_id = lambda: -1           # 이 관제가 물고 있는 로봇이 아니라고 — 스케줄 충돌 검사 생략

        data = json.load(open(FIXTURE, encoding="utf-8"))
        models = {"business_info": M.BusinessInfo, "floor_info": M.FloorInfo, "robot_map_info": M.RobotMapInfo,
                  "location_info": M.LocationInfo, "robot_info": M.RobotInfo, "voice_scenario": M.VoiceScenario,
                  "voice_scenario_step": M.VoiceScenarioStep}
        db = SessionLocal()
        for table, cls in models.items():
            cols = {c.name for c in cls.__table__.columns}
            for row in data[table]:
                db.add(cls(**{k: v for k, v in row.items() if k in cols and k not in DROP}))
            db.flush()
        db.commit()
        db.close()

    # ── 호출 ──
    def call(self, fn):
        if not self.ve:
            return {"status": "API 없음"}
        db = self.Session()
        try:
            return {"status": 200, "body": fn(db)}
        except Exception as e:
            if type(e).__name__ == "ValidationError":
                return {"status": 422, "detail": "요청 형식 오류"}
            return {"status": getattr(e, "status_code", "예외"), "detail": getattr(e, "detail", repr(e))}
        finally:
            db.rollback()
            db.close()

    def sc(self, code, robot=ROBOT):
        db = self.Session()
        try:
            return db.query(self.M.VoiceScenario).filter(self.M.VoiceScenario.RobotId == robot,
                                                         self.M.VoiceScenario.Code == code).first()
        finally:
            db.close()

    def steps(self, sid):
        db = self.Session()
        try:
            rows = (db.query(self.M.VoiceScenarioStep).filter(self.M.VoiceScenarioStep.ScenarioId == sid)
                    .order_by(self.M.VoiceScenarioStep.StepOrder, self.M.VoiceScenarioStep.id).all())
            keys = ("StepCode", "SayText", "WaitFor", "ExpectedIntent", "RetryCount", "ResponseTimeoutSec",
                    "ActionType", "SlotName", "TargetLocationId", "NextStepCode", "OnNoStepCode",
                    "OnTimeoutStepCode", "AutoAdvanceSec", "IsActive")
            return [{k: getattr(r, k) for k in keys} for r in rows]
        finally:
            db.close()

    def create(self, name="약국 안내", map_id=47):
        return self.call(lambda db: self.ve.create_voice_scenario(
            ROBOT, self.ve.ScenarioCreateReq(displayName=name, mapId=map_id), MagicMock(), db=db, current_user=ADMIN))

    def save(self, sid, steps, base):
        def run(db):
            req = self.ve.StepsSaveReq(steps=[self.ve.StepIn(**s) for s in steps], baseVersion=base)
            return self.ve.save_voice_scenario_steps(sid, req, MagicMock(), db=db, current_user=ADMIN)
        return self.call(run)

    def validate(self, sid, steps):
        return self.call(lambda db: self.ve.validate_voice_scenario(
            sid, self.ve.StepsReq(steps=[self.ve.StepIn(**s) for s in steps]), db=db, current_user=ADMIN))

    def current(self, sid):
        db = self.Session()
        try:
            sc = db.query(self.M.VoiceScenario).get(sid)
            return self.ed.current_steps(db, sc), sc.Version
        finally:
            db.close()

    def activate(self, sid, on=True):
        def run(db):
            return self.va.update_voice_scenario(sid, self.va.ScenarioPatchReq(isActive=on), MagicMock(),
                                                 db=db, current_user=ADMIN)
        db = self.Session()
        try:
            return {"status": 200, "body": run(db)}
        except Exception as e:
            return {"status": getattr(e, "status_code", "예외"), "detail": getattr(e, "detail", repr(e))}
        finally:
            db.rollback()
            db.close()

    def set_active(self, code, on):
        db = self.Session()
        db.query(self.M.VoiceScenario).filter(self.M.VoiceScenario.RobotId == ROBOT,
                                              self.M.VoiceScenario.Code == code).update({"IsActive": 1 if on else 0})
        db.commit()
        db.close()


def _new_flow(place):
    """인사 → 준비 확인(예/아니요) → 접수 이동 → 종료."""
    return [
        {"stepCode": "greet", "stepLabel": "인사", "sayText": "안녕하세요.", "waitFor": "none", "nextStepCode": "s_001"},
        {"stepCode": "s_001", "stepLabel": "준비 확인", "sayText": "따라오실 준비 되셨나요?", "waitFor": "user",
         "expectedIntent": "YES_NO", "nextStepCode": "s_002", "onNoStepCode": "finish"},
        {"stepCode": "s_002", "stepLabel": "접수 이동", "sayText": "접수로 가겠습니다.", "waitFor": "arrival",
         "targetLocationId": place, "nextStepCode": "finish"},
        {"stepCode": "finish", "stepLabel": "종료", "sayText": "안녕히 가세요.", "waitFor": "none"},
    ]


def _place_on(st, map_id):
    db = st.Session()
    try:
        return db.query(st.M.LocationInfo.id).filter(st.M.LocationInfo.MapId == map_id).first()[0]
    finally:
        db.close()


def _st(r):
    return r["res"]["status"]


# ── 기존 시나리오 그대로 저장 (설계 S1 완료 조건) ─────────────
@scenario("F1 기존 시나리오 3종(로봇 10)을 그대로 저장 → 검사 오류 0, 바뀐 칸 0",
          expect=lambda r: r["res"] == "ok")
def f1(sim):
    st = Stage(sim)
    if not st.ve:
        return {"res": "API 없음"}
    out = {}
    for code in ("hospital", "public_office", "parking"):
        sc = st.sc(code)
        before = st.steps(sc.id)
        cur, ver = st.current(sc.id)
        r = st.save(sc.id, cur, ver)
        after = st.steps(sc.id)
        out[code] = {"status": r["status"], "detail": r.get("detail"),
                     "changed": sum(1 for a, b in zip(before, after) if a != b) + abs(len(before) - len(after)),
                     "warnings": [w["message"] for w in (r.get("body") or {}).get("warnings", [])],
                     "version": (r.get("body") or {}).get("version")}
    ok = all(v["status"] == 200 and v["changed"] == 0 and v["version"] == 2 for v in out.values())
    return {"res": "ok" if ok else "fail", "detail": out}


@scenario("F2 로봇 13 병원(운영 맵·장소 연결 없음) 검사 → 이동 단계에만 오류, 저장 안 함",
          expect=lambda r: _st(r) == 200 and r["res"]["body"]["errors"]
          and all(e["field"] == "targetLocationId" for e in r["res"]["body"]["errors"]))
def f2(sim):
    st = Stage(sim)
    if not st.ve:
        return {"res": {"status": "API 없음"}}
    sc = st.sc("hospital", robot=13)
    cur, _ = st.current(sc.id)
    return {"res": st.validate(sc.id, cur)}


# ── 새로 만들기 ─────────────────────────────────────────
@scenario("C1 새로 만들기 → 코드 자동·꺼짐·인사→종료 2단계·버전 1",
          expect=lambda r: _st(r) == 200 and r["sc"]["Code"].startswith("scenario_") and r["sc"]["IsActive"] == 0
          and [s["StepCode"] for s in r["steps"]] == ["greet", "finish"] and r["sc"]["Version"] == 1)
def c1(sim):
    st = Stage(sim)
    r = st.create()
    if _st({"res": r}) != 200:
        return {"res": r}
    sc = st.sc(r["body"]["code"])
    return {"res": r, "sc": {"Code": sc.Code, "IsActive": sc.IsActive, "Version": sc.Version, "MapId": sc.MapId},
            "steps": st.steps(sc.id)}


@scenario("C2 두 번 만들기 → 코드 겹치지 않음",
          expect=lambda r: r["a"].get("status") == 200 and r["b"].get("status") == 200
          and r["a"]["body"]["code"] != r["b"]["body"]["code"])
def c2(sim):
    st = Stage(sim)
    return {"a": st.create("A"), "b": st.create("B"), "res": {"status": 200}}


@scenario("C3 다른 사업장 맵으로 만들기 → 422",
          expect=lambda r: _st(r) == 422)
def c3(sim):
    st = Stage(sim)
    db = st.Session()
    db.add(st.M.RobotMapInfo(id=9999, BusinessId=2, FloorId=1, MapName="다른 사업장 맵"))
    db.commit()
    db.close()
    return {"res": st.create(map_id=9999)}


@scenario("C4 이름 공백만 → 422", expect=lambda r: _st(r) == 422)
def c4(sim):
    return {"res": Stage(sim).create(name="   ")}


# ── 저장·검사 ───────────────────────────────────────────
def _made(st):
    r = st.create()
    return st.sc(r["body"]["code"]) if r.get("status") == 200 else None


@scenario("S1 새 시나리오에 단계 추가 저장 → 200, 버전 2, 순서·갈래 그대로",
          expect=lambda r: _st(r) == 200 and r["res"]["body"]["version"] == 2
          and [s["StepCode"] for s in r["steps"]] == ["greet", "s_001", "s_002", "finish"]
          and r["steps"][1]["OnNoStepCode"] == "finish" and r["steps"][2]["ActionType"] == "navigate")
def s1(sim):
    st = Stage(sim)
    sc = _made(st)
    if not sc:
        return {"res": {"status": "API 없음"}}
    r = st.save(sc.id, _new_flow(_place_on(st, 47)), 1)
    return {"res": r, "steps": st.steps(sc.id)}


@scenario("S2 화면이 들고 있던 버전이 옛것 → 409(다른 사람이 먼저 저장)",
          expect=lambda r: _st(r) == 409)
def s2(sim):
    st = Stage(sim)
    sc = _made(st)
    if not sc:
        return {"res": {"status": "API 없음"}}
    st.save(sc.id, _new_flow(_place_on(st, 47)), 1)
    return {"res": st.save(sc.id, _new_flow(_place_on(st, 47)), 1)}


def _bad(mutate):
    def run(sim):
        st = Stage(sim)
        sc = _made(st)
        if not sc:
            return {"res": {"status": "API 없음"}}
        flow = _new_flow(_place_on(st, 47))
        mutate(flow, st)
        before = st.steps(sc.id)
        r = st.save(sc.id, flow, 1)
        errs = r.get("detail", {}).get("errors", []) if isinstance(r.get("detail"), dict) else []
        return {"res": r, "errors": [e["message"] for e in errs], "unchanged": st.steps(sc.id) == before}
    return run


def _bad_expect(word):
    return lambda r: _st(r) == 422 and r["unchanged"] and any(word in m for m in r["errors"])


scenario("S3 대답 단계에 인식 종류 없음 → 422·저장 안 됨", expect=_bad_expect("인식 종류"))(
    _bad(lambda f, st: f[1].update(expectedIntent=None)))
scenario("S4 없는 단계로 연결 → 422", expect=_bad_expect("없는 단계"))(
    _bad(lambda f, st: f[0].update(nextStepCode="nope")))
scenario("S5 다른 맵의 장소로 이동 → 422", expect=_bad_expect("운영 맵의 장소가 아닙니다"))(
    _bad(lambda f, st: f[2].update(targetLocationId=_place_on(st, 43))))
scenario("S6 예약 코드 call_move → 422", expect=_bad_expect("쓸 수 없는"))(
    _bad(lambda f, st: f[1].update(stepCode="call_move")))
scenario("S7 단계 코드 중복 → 422", expect=_bad_expect("겹칩니다"))(
    _bad(lambda f, st: f[2].update(stepCode="s_001")))
scenario("S8 목적지 묻기 전에 고객 목적지로 이동 → 422", expect=_bad_expect("목적지를 묻는 단계"))(
    _bad(lambda f, st: f[2].update(targetLocationId=None, slotName="목적지")))
scenario("S9 꺼진 단계로 연결 → 422", expect=_bad_expect("꺼진 단계"))(
    _bad(lambda f, st: f[2].update(isActive=False)))
scenario("S10 운영자 단계에 입력 종류 없음 → 422", expect=_bad_expect("입력 종류"))(
    _bad(lambda f, st: f[1].update(waitFor="operator", expectedIntent=None)))


@scenario("S11 목적 질문 뒤 고객 목적지 이동 → 200",
          expect=lambda r: _st(r) == 200)
def s11(sim):
    st = Stage(sim)
    sc = _made(st)
    if not sc:
        return {"res": {"status": "API 없음"}}
    f = _new_flow(_place_on(st, 47))
    f[1].update(expectedIntent="PURPOSE", onNoStepCode=None)
    f[2].update(targetLocationId=None, slotName="목적지", sayText="{목적지}로 안내하겠습니다.")
    return {"res": st.save(sc.id, f, 1)}


@scenario("S12 경고만(아니요 갈래 없음·finish 없음·빈 멘트) → 200 + 경고 3",
          expect=lambda r: _st(r) == 200 and len(r["res"]["body"]["warnings"]) >= 3)
def s12(sim):
    st = Stage(sim)
    sc = _made(st)
    if not sc:
        return {"res": {"status": "API 없음"}}
    f = _new_flow(_place_on(st, 47))
    f[1]["onNoStepCode"] = None
    f[3].update(stepCode="bye")
    f[1]["nextStepCode"], f[2]["nextStepCode"], f[0]["sayText"] = "s_002", "bye", ""
    return {"res": st.save(sc.id, f, 1)}


@scenario("S13 기다림을 '말만'으로 바꾸면 장소·인식 종류·갈래가 비워져 저장",
          expect=lambda r: _st(r) == 200 and r["step"]["TargetLocationId"] is None
          and r["step"]["ExpectedIntent"] is None and r["step"]["OnNoStepCode"] is None and r["step"]["ActionType"] == "none")
def s13(sim):
    st = Stage(sim)
    sc = _made(st)
    if not sc:
        return {"res": {"status": "API 없음"}}
    f = _new_flow(_place_on(st, 47))
    f[1].update(waitFor="none", targetLocationId=_place_on(st, 47))   # 대답 → 말만, 장소가 남아 온 경우
    r = st.save(sc.id, f, 1)
    return {"res": r, "step": next(s for s in st.steps(sc.id) if s["StepCode"] == "s_001")}


@scenario("S14 그 시나리오로 안내 진행 중 저장 → 409",
          expect=lambda r: _st(r) == 409)
def s14(sim):
    st = Stage(sim)
    if not st.ve:
        return {"res": {"status": "API 없음"}}
    sc = st.sc("hospital")
    db = st.Session()
    from datetime import datetime
    db.add(st.M.ServiceSession(RobotName="CA9B_NO.296_5G", StartedAt=datetime.now(), Result="running",
                               ScenarioId=sc.id, CurrentStepCode="greet"))
    db.commit()
    db.close()
    cur, ver = st.current(sc.id)
    return {"res": st.save(sc.id, cur, ver)}


@scenario("S15 검사만(validate) → 저장 안 하고 오류·경고 돌려줌",
          expect=lambda r: _st(r) == 200 and r["res"]["body"]["errors"] and r["unchanged"])
def s15(sim):
    st = Stage(sim)
    sc = _made(st)
    if not sc:
        return {"res": {"status": "API 없음"}}
    f = _new_flow(_place_on(st, 47))
    f[1]["expectedIntent"] = None
    before = st.steps(sc.id)
    return {"res": st.validate(sc.id, f), "unchanged": st.steps(sc.id) == before}


# ── 버전 ───────────────────────────────────────────────
@scenario("V1 기존 시나리오 첫 저장 → 버전 1(고치기 전) + 2(저장) 남고, 1로 되돌리면 3 = 1과 같은 단계",
          expect=lambda r: r["res"] == "ok")
def v1(sim):
    st = Stage(sim)
    if not st.ve:
        return {"res": "API 없음"}
    sc = st.sc("parking")
    orig = st.steps(sc.id)
    cur, _ = st.current(sc.id)
    cur[0]["sayText"] = "바꾼 인사"
    st.save(sc.id, cur, 0)
    db = st.Session()
    vs_ = st.ve.list_voice_scenario_versions(sc.id, db=db, current_user=ADMIN)
    db.close()
    r = st.call(lambda db: st.ve.restore_voice_scenario_version(
        sc.id, 1, st.ve.RestoreReq(baseVersion=2), MagicMock(), db=db, current_user=ADMIN))
    ok = [i["version"] for i in vs_["items"]] == [2, 1] and r["status"] == 200 and st.steps(sc.id) == orig
    return {"res": "ok" if ok else "fail", "versions": vs_["items"], "restore": r}


@scenario("V2 기존 화면에서 단계 하나 고치면 버전이 올라가 편집기의 옛 버전 저장은 409",
          expect=lambda r: _st(r) == 409 and r["ver"] == 2)
def v2(sim):
    st = Stage(sim)
    if not st.ve:
        return {"res": {"status": "API 없음"}, "ver": None}
    sc = st.sc("parking")
    cur, ver = st.current(sc.id)
    db = st.Session()
    step = db.query(st.M.VoiceScenarioStep).filter(st.M.VoiceScenarioStep.ScenarioId == sc.id).first()
    st.va.update_voice_scenario_step(step.id, st.va.StepPatchReq(sayText="옛 화면에서 고침"), MagicMock(),
                                     db=db, current_user=ADMIN)
    db.close()
    return {"res": st.save(sc.id, cur, ver), "ver": st.sc("parking").Version}


# ── 삭제 ───────────────────────────────────────────────
@scenario("D1 상시 대기 작업이 쓰는 시나리오 삭제 → 409, 안 쓰는 새 시나리오 삭제 → 단계·버전까지 지워짐",
          expect=lambda r: r["used"]["status"] == 409 and r["free"]["status"] == 200 and r["left"] == (0, 0))
def d1(sim):
    st = Stage(sim)
    if not st.ve:
        return {"used": {"status": "API 없음"}, "free": {}, "left": None}
    from datetime import datetime, timedelta
    db = st.Session()
    db.add(st.M.ScheduleInfo(RobotName="CA9B_NO.296_5G", WorkName="로비 상시 대기", TaskType="standby",
                             TaskStatus="대기", StartDate=datetime.now(), EndDate=datetime.now() + timedelta(hours=8),
                             ScheduleMode="standby", StandbyScenarioCode="hospital"))
    db.commit()
    db.close()
    used = st.call(lambda db: st.ve.delete_voice_scenario(st.sc("hospital").id, MagicMock(), db=db, current_user=ADMIN))
    sc = _made(st)
    st.save(sc.id, _new_flow(_place_on(st, 47)), 1)
    free = st.call(lambda db: st.ve.delete_voice_scenario(sc.id, MagicMock(), db=db, current_user=ADMIN))
    db = st.Session()
    left = (db.query(st.M.VoiceScenarioStep).filter(st.M.VoiceScenarioStep.ScenarioId == sc.id).count(),
            db.query(st.M.VoiceScenarioVersion).filter(st.M.VoiceScenarioVersion.ScenarioId == sc.id).count())
    db.close()
    return {"used": used, "free": free, "left": left}


# ── 사용 켜기 · 꺼진 시나리오 실행 ─────────────────────────
@scenario("A1 오류 있는 시나리오 '사용' 켜기 → 422 (현행: 그냥 켜짐)",
          expect=lambda r: r["res"]["status"] == 422)
def a1(sim):
    st = Stage(sim)
    sc = st.sc("hospital", robot=13)          # 운영 맵 없음
    db = st.Session()
    db.query(st.M.VoiceScenario).filter(st.M.VoiceScenario.id == sc.id).update({"IsActive": 0})
    db.commit()
    db.close()
    return {"res": st.activate(sc.id)}


@scenario("A2 준비된 새 시나리오 '사용' 켜기 → 200",
          expect=lambda r: r["res"]["status"] == 200)
def a2(sim):
    st = Stage(sim)
    sc = _made(st)
    if not sc:
        return {"res": {"status": "API 없음"}}
    st.save(sc.id, _new_flow(_place_on(st, 47)), 1)
    return {"res": st.activate(sc.id)}


@scenario("F3 목적지 단계의 '아니요(없어요)' 갈래는 저장해도 남음 (엔진이 씀)",
          expect=lambda r: r["kept"] == ["exit_ask", "extra_dest"])
def f3(sim):
    st = Stage(sim)
    if not st.ve:
        return {"kept": "API 없음"}
    sc = st.sc("hospital")
    cur, ver = st.current(sc.id)
    st.save(sc.id, cur, ver)
    return {"kept": sorted(s["StepCode"] for s in st.steps(sc.id)
                           if s["WaitFor"] == "user" and s["ExpectedIntent"] != "YES_NO" and s["OnNoStepCode"])}


@scenario("A3 꺼진 시나리오를 운영자가 안내 시작 → 세션 열림 (현행: 404)",
          expect=lambda r: r["res"]["status"] == 200)
def a3(sim):
    st = Stage(sim)
    st.set_active("hospital", False)
    db = st.Session()
    try:
        out = st.va.open_voice_session(ROBOT, st.va.SessionOpenReq(scenarioCode="hospital"), MagicMock(),
                                       db=db, current_user=ADMIN)
        return {"res": {"status": 200, "step": out["session"].get("currentStepCode")}}
    except Exception as e:
        return {"res": {"status": getattr(e, "status_code", "예외"), "detail": str(getattr(e, "detail", e))}}
    finally:
        db.close()


@scenario("A4 꺼진 시나리오를 로봇(Jetson) 쪽에서 열기 → 그대로 404 (회귀)",
          expect=lambda r: r["res"]["status"] == 404)
def a4(sim):
    st = Stage(sim)
    st.set_active("hospital", False)
    db = st.Session()
    try:
        robot = db.query(st.M.RobotInfo).get(ROBOT)
        st.vs.open_session(db, robot, "hospital")
        return {"res": {"status": 200}}
    except Exception as e:
        return {"res": {"status": getattr(e, "status_code", "예외")}}
    finally:
        db.rollback()
        db.close()


@scenario("A5 켜진 시나리오 운영자 안내 시작 → 그대로 열림 (회귀)",
          expect=lambda r: r["res"]["status"] == 200)
def a5(sim):
    st = Stage(sim)
    db = st.Session()
    try:
        st.va.open_voice_session(ROBOT, st.va.SessionOpenReq(scenarioCode="hospital"), MagicMock(),
                                 db=db, current_user=ADMIN)
        return {"res": {"status": 200}}
    except Exception as e:
        return {"res": {"status": getattr(e, "status_code", "예외"), "detail": str(getattr(e, "detail", e))}}
    finally:
        db.close()


@scenario("B1 엔진 번들 — 꺼진 시나리오도 실림(엔진이 코드로 찾음), 켜진 것은 그대로",
          expect=lambda r: r["has_off"] and r["has_on"])
def b1(sim):
    st = Stage(sim)
    st.set_active("hospital", False)
    db = st.Session()
    try:
        b = st.vs.get_bundle(ROBOT, db=db, _="sim")
        codes = [s.code for s in b.scenarios]
        return {"codes": codes, "has_off": "hospital" in codes, "has_on": "parking" in codes}
    finally:
        db.close()


@scenario("L1 시나리오 목록 — 버전·준비 점검·단계 표시 이름이 함께 옴",
          expect=lambda r: r["keys"] >= {"version", "ready", "checkErrors"} and "stepLabel" in r["stepKeys"])
def l1(sim):
    st = Stage(sim)
    db = st.Session()
    try:
        body = st.va.list_voice_scenarios(ROBOT, db=db, current_user=ADMIN)
        row = body["scenarios"][0]
        return {"keys": set(row), "stepKeys": set(row["steps"][0]),
                "ready": {s["code"]: (s.get("ready"), len(s.get("checkErrors", []))) for s in body["scenarios"]}}
    finally:
        db.close()
