"""영상 관리 삭제 (현행 vs 변경, 11-03).

실측(11-03): 화면에서 선택 삭제 → POST /api/recordings/delete-groups 200 이지만 DB DeletedAt 그대로(id 263~265),
새로 고치면 다시 보인다. 목록 API 는 그룹 키를 "GroupId_ModuleId"(카메라별, bd135a3 04-09)로 주는데
삭제는 GroupId 그대로 찾아 0건이 된다.
변경: 삭제가 "GroupId_ModuleId" 키를 풀어 그 카메라 것만 지운다. 예전 키(GroupId 만)도 받는다.

무대: 임시 DB 에 녹화 레코드를 직접 넣고, 목록 API 가 준 group_id 로 라우트와 같은 서비스 함수를 부른다.
"""
from datetime import datetime

from harness import scenario

ROBOT = 13


class Stage:
    def __init__(self, sim):
        from app.database.database import SessionLocal
        from app.database import models as M
        import app.recording.service as svc
        sim.rewire_late_modules()
        self.M, self.Session, self.svc = M, SessionLocal, svc
        db = SessionLocal()
        db.add(M.RobotInfo(id=ROBOT, RobotName="R13", SerialNumber="SN13"))
        db.commit()
        db.close()

    def add(self, rid, module, group, rtype="manual"):
        db = self.Session()
        db.add(self.M.RecordingInfo(id=rid, RobotId=ROBOT, ModuleId=module, GroupId=group, RecordType=rtype,
                                    VideoPath="recordings/13/2026-11-03", Status="completed",
                                    RecordStart=datetime(2026, 11, 3, 14, 0, rid % 60),
                                    RecordEnd=datetime(2026, 11, 3, 14, 1, rid % 60)))
        db.commit()
        db.close()

    def keys(self):
        db = self.Session()
        try:
            return sorted(it["group_id"] for it in self.svc.get_recordings_grouped(db, size=100)["items"])
        finally:
            db.close()

    def delete(self, keys):
        db = self.Session()
        try:
            return self.svc.soft_delete_by_groups(db, keys)
        finally:
            db.close()

    def alive(self):
        db = self.Session()
        try:
            return sorted(r.id for r in db.query(self.M.RecordingInfo)
                          .filter(self.M.RecordingInfo.DeletedAt.is_(None)).all())
        finally:
            db.close()


G1, G2, G3 = ("a723b4a7-9665-4876-88cc-48efbe134fae", "2db2a83e-3d06-4a2a-8b91-16197edff696",
              "5b205400-ab13-448b-a549-d66900601731")


@scenario("D1 [재현] 목록이 준 키로 수동 녹화 1건 삭제 → 지워지고 목록에서 빠짐 (현행: 0건, 다시 보임)",
          expect=lambda r: r["deleted"] == 1 and r["alive"] == [264, 265] and len(r["after"]) == 2)
def d1(sim):
    st = Stage(sim)
    st.add(263, 5, G1)
    st.add(264, 5, G2)
    st.add(265, 5, G3)
    key = [k for k in st.keys() if k.startswith(G1)][0]
    n = st.delete([key])
    return {"key": key, "deleted": n, "alive": st.alive(), "after": st.keys()}


@scenario("D2 자동 녹화(카메라 3대가 같은 GroupId) 중 한 카메라만 삭제 → 그 카메라만 지움",
          expect=lambda r: r["deleted"] == 1 and r["alive"] == [11, 12] and len(r["after"]) == 2)
def d2(sim):
    st = Stage(sim)
    for rid, m in ((10, 1), (11, 5), (12, 10)):
        st.add(rid, m, "auto-g", "auto")
    key = [k for k in st.keys() if k.endswith("_1")][0]
    n = st.delete([key])
    return {"key": key, "deleted": n, "alive": st.alive(), "after": st.keys()}


@scenario("D3 여러 개 선택 삭제 → 모두 지움",
          expect=lambda r: r["deleted"] == 3 and r["alive"] == [])
def d3(sim):
    st = Stage(sim)
    st.add(263, 5, G1)
    st.add(264, 5, G2)
    st.add(265, 5, G3)
    n = st.delete(st.keys())
    return {"deleted": n, "alive": st.alive()}


@scenario("D4 예전 키(GroupId 만) → 그 그룹 전부 지움 (회귀)",
          expect=lambda r: r["deleted"] == 3 and r["alive"] == [20])
def d4(sim):
    st = Stage(sim)
    for rid, m in ((10, 1), (11, 5), (12, 10)):
        st.add(rid, m, "auto-g", "auto")
    st.add(20, 5, G1)
    n = st.delete(["auto-g"])
    return {"deleted": n, "alive": st.alive()}


@scenario("D5 없는 키 → 0건, 다른 녹화 그대로 (회귀)",
          expect=lambda r: r["deleted"] == 0 and r["alive"] == [263])
def d5(sim):
    st = Stage(sim)
    st.add(263, 5, G1)
    n = st.delete(["no-such_5", "nothing"])
    return {"deleted": n, "alive": st.alive()}


@scenario("D6 이미 지운 것 다시 삭제 → 0건 (회귀)",
          expect=lambda r: r["first"] == 1 and r["second"] == 0)
def d6(sim):
    st = Stage(sim)
    st.add(263, 5, G1)
    key = st.keys()[0]
    return {"first": st.delete([key]), "second": st.delete([key])}
