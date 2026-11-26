"""사업장 검사 — 로봇 사업장이 없거나 다른 사업장 값이 들어오는 경우 (현행 vs 변경).

화면이 부르는 API 함수(시나리오 목록·목적지 사전·시나리오 설정·로봇 등록·로봇 수정)를
임시 DB 위에서 직접 부른다. 로봇 이동은 없다.

무대: 사업장 1(UND)·2(다른 곳). 층·맵은 사업장마다 하나씩(1F 맵 1 / 9F 맵 9).
로봇 10 = 사업장 1, 로봇 20 = 사업장 없음(예전 데이터·API 직접 호출로만 생김).
"""
from unittest.mock import MagicMock

from harness import scenario

ADMIN = MagicMock(id=1, Permission=1, BusinessId=None)
MEMBER = MagicMock(id=2, Permission=3, BusinessId=1)     # 사업장 1 소속 일반 사용자


class Stage:
    def __init__(self, sim):
        from app.database.database import SessionLocal
        from app.database import models as M
        import app.database.routes.robot as rb
        import app.database.routes.voice as vo
        import app.database.routes.voice_scenario_admin as va
        sim.rewire_late_modules()
        self.M, self.Session, self.rb, self.vo, self.va = M, SessionLocal, rb, vo, va
        for mod in (rb, va):
            mod.write_audit = lambda *a, **k: None
        import app.robot_io.runtime as runtime
        runtime.add_or_update_robot = lambda *a, **k: None

        db = SessionLocal()
        db.add(M.BusinessInfo(id=1, BusinessName="UND"))
        db.add(M.BusinessInfo(id=2, BusinessName="다른 사업장"))
        db.add(M.FloorInfo(id=1, BusinessId=1, FloorName="1F"))
        db.add(M.FloorInfo(id=9, BusinessId=2, FloorName="9F"))
        db.add(M.RobotMapInfo(id=1, BusinessId=1, FloorId=1, MapName="UND_1F"))
        db.add(M.RobotMapInfo(id=9, BusinessId=2, FloorId=9, MapName="OTHER_9F"))
        db.add(M.RobotInfo(id=10, RobotName="R10", SerialNumber="SN10", BusinessId=1, CurrentMapId=1, CurrentFloorId=1))
        db.add(M.RobotInfo(id=20, RobotName="R20", SerialNumber="SN20", BusinessId=None))
        db.add(M.VoiceScenario(id=1, RobotId=10, Code="hospital", DisplayName="병원 안내", FloorId=1, MapId=1, IsActive=1))
        db.add(M.VoiceScenario(id=2, RobotId=20, Code="hospital", DisplayName="병원 안내", IsActive=1))
        db.commit()
        db.close()

    def call(self, fn):
        db = self.Session()
        try:
            return {"status": 200, "body": fn(db)}
        except Exception as e:
            # 요청 모델 검사 실패(필수 칸 없음)는 FastAPI 가 422 로 돌려준다
            if type(e).__name__ == "ValidationError":
                return {"status": 422, "detail": "요청 형식 오류(필수 칸)"}
            return {"status": getattr(e, "status_code", "예외"), "detail": str(getattr(e, "detail", e))}
        finally:
            db.rollback()
            db.close()

    def scenarios(self, robot_id):
        return self.call(lambda db: self.va.list_voice_scenarios(robot_id, db=db, current_user=ADMIN))

    def slots(self, robot_id):
        return self.call(lambda db: self.vo.list_voice_slots(robot_id, db=db, current_user=ADMIN))

    def patch(self, sid, **body):
        return self.call(lambda db: self.va.update_voice_scenario(
            sid, self.va.ScenarioPatchReq(**body), MagicMock(), db=db, current_user=ADMIN))

    def insert(self, user=ADMIN, **over):
        body = dict(robot_id="SN_NEW", robot_name="새 로봇", robot_model="M20")
        body.update(over)

        def run(db):
            res = self.rb.insert_Robot(self.rb.RobotInsertReq(**body), MagicMock(), db=db, current_user=user)
            row = db.query(self.M.RobotInfo).filter(self.M.RobotInfo.SerialNumber == body["robot_id"]).first()
            return {"saved_business": row.BusinessId if row else "저장 안 됨", "res": str(res)[:60]}
        return self.call(run)

    def update(self, robot_id, user=ADMIN, **body):
        def run(db):
            self.rb.update_robot(robot_id, self.rb.RobotUpdateReq(**body), MagicMock(), db=db, current_user=user)
            row = db.query(self.M.RobotInfo).filter(self.M.RobotInfo.id == robot_id).first()
            return {"business": row.BusinessId, "name": row.RobotName}
        return self.call(run)


def _maps(r):
    return sorted(m["id"] for m in r["res"]["body"]["maps"]) if r["res"]["status"] == 200 else None


# ── 시나리오 편집 화면이 쓰는 맵 목록 ─────────────────────────
@scenario("S1 사업장 있는 로봇 → 시나리오 목록에 자기 사업장 맵만 (회귀)",
          expect=lambda r: r["res"]["status"] == 200 and _maps(r) == [1])
def s1(sim):
    return {"res": Stage(sim).scenarios(10)}


@scenario("S2 사업장 없는 로봇 → 시나리오 목록 409 (현행: 모든 사업장 맵을 돌려줌)",
          expect=lambda r: r["res"]["status"] == 409)
def s2(sim):
    r = Stage(sim).scenarios(20)
    return {"res": r, "maps": _maps({"res": r})}


@scenario("S3 사업장 없는 로봇 → 목적지 사전 409 (현행: 모든 사업장 장소 후보)",
          expect=lambda r: r["res"]["status"] == 409)
def s3(sim):
    return {"res": Stage(sim).slots(20)}


@scenario("S4 사업장 있는 로봇 → 목적지 사전 정상 (회귀)",
          expect=lambda r: r["res"]["status"] == 200)
def s4(sim):
    return {"res": Stage(sim).slots(10)}


# ── 시나리오 설정에서 층·맵 바꾸기 ─────────────────────────────
@scenario("P1 같은 사업장 맵으로 설정 → 200 (회귀)",
          expect=lambda r: r["res"]["status"] == 200)
def p1(sim):
    return {"res": Stage(sim).patch(1, mapId=1)}


@scenario("P2 다른 사업장 맵(9)으로 설정 → 422 (현행: 받아 줌)",
          expect=lambda r: r["res"]["status"] == 422)
def p2(sim):
    return {"res": Stage(sim).patch(1, clearFloor=True, mapId=9)}


@scenario("P3 다른 사업장 층(9F)으로 설정 → 422 (현행: 받아 줌)",
          expect=lambda r: r["res"]["status"] == 422)
def p3(sim):
    return {"res": Stage(sim).patch(1, floorId=9)}


@scenario("P4 사업장 없는 로봇의 시나리오 이름만 바꿈 → 409 (현행: 200)",
          expect=lambda r: r["res"]["status"] == 409)
def p4(sim):
    return {"res": Stage(sim).patch(2, displayName="새 이름")}


# ── 로봇 등록·수정 ────────────────────────────────────────────
@scenario("R1 관리자가 사업장 1로 등록 → 200, 사업장 1 저장 (회귀)",
          expect=lambda r: r["res"]["status"] == 200 and r["res"]["body"]["saved_business"] == 1)
def r1(sim):
    return {"res": Stage(sim).insert(business_id=1)}


@scenario("R2 사업장 없이 등록(API 직접) → 422 (현행: 사업장 없는 로봇이 생김)",
          expect=lambda r: r["res"]["status"] == 422)
def r2(sim):
    return {"res": Stage(sim).insert()}


@scenario("R3 없는 사업장(999)으로 등록 → 422 (현행: 저장됨)",
          expect=lambda r: r["res"]["status"] == 422)
def r3(sim):
    return {"res": Stage(sim).insert(business_id=999)}


@scenario("R4 사업장 1 일반 사용자가 사업장 2로 등록 → 403 (현행: 저장 후 본인은 못 봄)",
          expect=lambda r: r["res"]["status"] == 403)
def r4(sim):
    return {"res": Stage(sim).insert(user=MEMBER, business_id=2)}


@scenario("R5 사업장 1 일반 사용자가 자기 사업장으로 등록 → 200 (회귀)",
          expect=lambda r: r["res"]["status"] == 200 and r["res"]["body"]["saved_business"] == 1)
def r5(sim):
    return {"res": Stage(sim).insert(user=MEMBER, business_id=1)}


@scenario("R6 시리얼 중복 → 409 가 사업장 검사보다 먼저 (회귀)",
          expect=lambda r: r["res"]["status"] == 409)
def r6(sim):
    return {"res": Stage(sim).insert(robot_id="SN10", business_id=999)}


@scenario("U1 로봇 수정에서 없는 사업장(999) → 422 (현행: 999 저장)",
          expect=lambda r: r["res"]["status"] == 422)
def u1(sim):
    return {"res": Stage(sim).update(10, business_id=999)}


@scenario("U2 일반 사용자가 로봇을 사업장 2로 옮김 → 403 (현행: 옮겨짐)",
          expect=lambda r: r["res"]["status"] == 403)
def u2(sim):
    return {"res": Stage(sim).update(10, user=MEMBER, business_id=2)}


@scenario("U3 사업장 그대로 두고 이름만 수정 → 200 (회귀)",
          expect=lambda r: r["res"]["status"] == 200 and r["res"]["body"] == {"business": 1, "name": "R10-수정"})
def u3(sim):
    return {"res": Stage(sim).update(10, robotName="R10-수정", business_id=1)}


@scenario("U4 사업장 없는 로봇에 사업장 1 지정 → 200 (지정해서 고칠 수 있어야 함)",
          expect=lambda r: r["res"]["status"] == 200 and r["res"]["body"]["business"] == 1)
def u4(sim):
    return {"res": Stage(sim).update(20, business_id=1)}
