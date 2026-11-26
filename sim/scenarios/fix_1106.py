"""11-06 분석보고 5장 수정 2건 (현행 vs 변경).

1) 상시 대기 단발 응대 복귀 멘트: LocationName(없는 칸)을 읽어 늘 "대기구역으로" → 실제 장소 이름 + 로/으로
2) 작업 등록·수정·삭제: 사업장 범위 검사 없음 → 비관리자는 자기 사업장 로봇만(403)

로봇 이동은 없다. 함수를 임시 DB 위에서 직접 부른다.
무대: 사업장 1·2, 로봇 R10(사업장 1)·R20(사업장 2), 작업 101(R10)·102(R20).
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from harness import scenario

ADMIN = MagicMock(id=1, Permission=1, BusinessId=None)
MEMBER = MagicMock(id=2, Permission=3, BusinessId=1)     # 사업장 1 소속 일반 사용자


class Stage:
    def __init__(self, sim):
        from app.database.database import SessionLocal
        from app.database import models as M
        import app.database.routes.schedule as sc
        sim.rewire_late_modules()
        self.M, self.Session, self.sc = M, SessionLocal, sc
        sc.write_audit = lambda *a, **k: None
        db = SessionLocal()
        db.add(M.BusinessInfo(id=1, BusinessName="UND"))
        db.add(M.BusinessInfo(id=2, BusinessName="다른 사업장"))
        db.add(M.RobotInfo(id=10, RobotName="R10", SerialNumber="SN10", BusinessId=1))
        db.add(M.RobotInfo(id=20, RobotName="R20", SerialNumber="SN20", BusinessId=2))
        later = datetime.now() + timedelta(days=2)
        for sid, rn in ((101, "R10"), (102, "R20")):
            db.add(M.ScheduleInfo(id=sid, UserId=1, RobotName=rn, WorkName=f"작업{sid}", TaskType="순찰",
                                  WayName="경로A", TaskStatus="대기", StartDate=later, ScheduleMode="once"))
        db.commit()
        db.close()

    def call(self, fn):
        db = self.Session()
        try:
            return {"status": 200, "body": fn(db)}
        except Exception as e:
            return {"status": getattr(e, "status_code", "예외"), "detail": str(getattr(e, "detail", e))[:80]}
        finally:
            db.rollback()
            db.close()

    def insert(self, robot, user):
        req = self.sc.ScheduleInsertReq(RobotName=robot, TaskName="새 작업", TaskType="순찰", WayName="경로A",
                                        WorkStatus="대기", ScheduleMode="once",
                                        StartTime=datetime.now() + timedelta(days=3))
        return self.call(lambda db: self.sc.insert_schedule(req, MagicMock(), db=db, current_user=user))

    def update(self, sid, user):
        req = self.sc.ScheduleUpdateReq(WorkName="이름 바꿈")
        return self.call(lambda db: self.sc.update_schedule(sid, req, MagicMock(), db=db, current_user=user))

    def delete(self, sid, user):
        return self.call(lambda db: self.sc.delete_schedule(sid, MagicMock(), None, db=db, current_user=user))


def _not403(r):
    return r["res"]["status"] != 403


# ── 2) 사업장 범위 ────────────────────────────────────────────
@scenario("B1 사업장1 사용자가 사업장2 로봇 작업 등록 → 403 (현행: 등록됨)",
          expect=lambda r: r["res"]["status"] == 403)
def b1(sim):
    return {"res": Stage(sim).insert("R20", MEMBER)}


@scenario("B2 사업장1 사용자가 자기 로봇 작업 등록 → 막히지 않음 (회귀)",
          expect=lambda r: r["res"]["status"] == 200)
def b2(sim):
    return {"res": Stage(sim).insert("R10", MEMBER)}


@scenario("B3 관리자가 사업장2 로봇 작업 등록 → 막히지 않음 (회귀)",
          expect=lambda r: r["res"]["status"] == 200)
def b3(sim):
    return {"res": Stage(sim).insert("R20", ADMIN)}


@scenario("B4 사업장1 사용자가 사업장2 작업 수정 → 403 (현행: 수정됨)",
          expect=lambda r: r["res"]["status"] == 403)
def b4(sim):
    return {"res": Stage(sim).update(102, MEMBER)}


@scenario("B5 사업장1 사용자가 자기 작업 수정 → 200 (회귀)",
          expect=lambda r: r["res"]["status"] == 200)
def b5(sim):
    return {"res": Stage(sim).update(101, MEMBER)}


@scenario("B6 사업장1 사용자가 사업장2 작업 삭제 → 403 (현행: 삭제됨)",
          expect=lambda r: r["res"]["status"] == 403)
def b6(sim):
    return {"res": Stage(sim).delete(102, MEMBER)}


@scenario("B7 사업장1 사용자가 자기 작업 삭제 → 200 (회귀)",
          expect=lambda r: r["res"]["status"] == 200)
def b7(sim):
    return {"res": Stage(sim).delete(101, MEMBER)}


@scenario("B8 없는 작업 수정 → 404 가 권한 검사보다 먼저 (회귀)",
          expect=lambda r: r["res"]["status"] == 404)
def b8(sim):
    return {"res": Stage(sim).update(999, MEMBER)}


# ── 1) 복귀 멘트 ─────────────────────────────────────────────
def _return_speech(sim, place_name, has_place=True):
    from app.database import models as M
    import app.scheduler.standby_session as ss
    said, finished = [], []
    ss._speak = said.append
    ss._nav_to = lambda place: True
    ss._finish = lambda result, reason, returned=False: finished.append(result)
    ss.log_event = lambda *a, **k: None
    place = M.LocationInfo(id=5, LacationName=place_name) if has_place else None
    ss._session = {"phase": "awaiting_dismiss", "return_place": place, "destination_name": "원무과"}
    ok = ss._begin_return("시험")
    return {"said": said, "ok": ok, "finished": finished}


@scenario("N1 대기 장소 '로비입구' → '로비입구로 이동하겠습니다.' (현행: 대기구역으로)",
          expect=lambda r: r["said"] == ["로비입구로 이동하겠습니다."] and r["ok"] is True)
def n1(sim):
    return _return_speech(sim, "로비입구")


@scenario("N2 받침 있는 이름 '편의점' → '편의점으로'",
          expect=lambda r: r["said"] == ["편의점으로 이동하겠습니다."])
def n2(sim):
    return _return_speech(sim, "편의점")


@scenario("N3 ㄹ 받침 '로봇대기실' → '로봇대기실로'",
          expect=lambda r: r["said"] == ["로봇대기실로 이동하겠습니다."])
def n3(sim):
    return _return_speech(sim, "로봇대기실")


@scenario("N4 이름이 빈 장소 → '대기구역으로' (회귀)",
          expect=lambda r: r["said"] == ["대기구역으로 이동하겠습니다."] and r["ok"] is True)
def n4(sim):
    return _return_speech(sim, "  ")


@scenario("N5 대기 장소 없음 → '대기구역으로' + nav_failed (회귀)",
          expect=lambda r: r["said"] == ["대기구역으로 이동하겠습니다."] and r["ok"] is False
          and r["finished"] == ["nav_failed"])
def n5(sim):
    return _return_speech(sim, None, has_place=False)


@scenario("N6 영문 이름 'EV' → 'EV로'",
          expect=lambda r: r["said"] == ["EV로 이동하겠습니다."])
def n6(sim):
    return _return_speech(sim, "EV")
