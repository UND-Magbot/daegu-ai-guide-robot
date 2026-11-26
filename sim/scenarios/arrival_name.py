"""목적지 사전 '도착 장소 이름'(arrivalName) — 저장·목록·번들 전달, 현행 vs 변경.

엔진 쪽(후보를 사전으로만, {도착지} = 도착 장소 이름)은 sim/out/arrival_name/run_voice.py 에서 본다.
여기서는 관제가 그 값을 저장하고 Jetson 번들로 넘기는지, 기존 칸(동의어·도착 문구·장소 연결)이
그대로인지만 본다.
"""
from types import SimpleNamespace

from harness import scenario

ROBOT = 10


class Stage:
    def __init__(self, sim):
        from app.database.database import SessionLocal
        from app.database import models as M
        import app.database.routes.voice as v
        import app.database.routes.voice_scenario as vs
        sim.rewire_late_modules()
        self.M, self.v, self.vs, self.Session = M, v, vs, SessionLocal
        db = SessionLocal()
        db.add(M.FloorInfo(id=1, BusinessId=1, FloorName="1F"))
        db.add(M.RobotInfo(id=ROBOT, RobotName="SIM", BusinessId=1, CurrentMapId=1, CurrentFloorId=1))
        db.add(M.LocationInfo(id=1, RobotName="SIM", LacationName="1F_엘리베이터입구", FloorId=1, MapId=1,
                              LocationX=1.0, LocationY=0.0, Yaw=0.0, Category="work"))
        db.commit()
        db.close()
        self.user = SimpleNamespace(id=1, UserId="sim", BusinessId=None, Permission="admin",
                                    UserName="sim", _perms=None)
        self.req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={},
                                   state=SimpleNamespace())

    def call(self, fn, *a, **kw):
        db = self.Session()
        try:
            return fn(*a, self.req, db=db, current_user=self.user, **kw)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{getattr(e, 'status_code', type(e).__name__)} {getattr(e, 'detail', e)}"}
        finally:
            db.close()

    def body(self, **kw):
        try:
            return self.v.SlotReq(**kw)
        except Exception as e:  # noqa: BLE001 — 길이 검증(422)
            return e

    def listed(self, value):
        db = self.Session()
        try:
            rows = self.v.list_voice_slots(ROBOT, db=db, current_user=self.user)
            rows = rows.get("items", rows) if isinstance(rows, dict) else rows
            return next((r for r in rows if r["value"] == value), None)
        finally:
            db.close()

    def bundled(self, value):
        db = self.Session()
        try:
            b = self.vs.get_bundle(ROBOT, db=db, _="sim")
            sl = next(s for s in b.slots if s.value == value)
            return getattr(sl, "arrivalName", "(칸 없음)"), sl.arrivalText, sl.locationId
        finally:
            db.close()

    def slot_id(self, value):
        db = self.Session()
        try:
            return db.query(self.M.VoiceSlotValue).filter(self.M.VoiceSlotValue.Value == value).first().id
        finally:
            db.close()


@scenario("목적지 등록 시 도착 장소 이름 저장 → 목록·번들에 실림",
          expect=lambda r, _w={"saved": "엘리베이터 입구", "bundle": "엘리베이터 입구", "text_kept": True, "link_kept": True}: all(r.get(k) == v for k, v in _w.items()))
def create(sim):
    st = Stage(sim)
    b = st.body(value="3층관공서", arrivalText="엘리베이터를 타고 3층으로 이동해 주세요.",
                arrivalName="엘리베이터 입구", locationIds=[1])
    st.call(st.v.create_voice_slot, ROBOT, b, force=True)
    row = st.listed("3층관공서") or {}
    name, text, loc = st.bundled("3층관공서")
    return {"saved": row.get("arrivalName", "(칸 없음)"), "bundle": name,
            "text_kept": text == "엘리베이터를 타고 3층으로 이동해 주세요.", "link_kept": loc == 1}


@scenario("수정: 이름을 안 보내면 그대로, \"\" 이면 지움",
          expect=lambda r, _w={"after_none": "기초연금 창구", "after_clear": None}: all(r.get(k) == v for k, v in _w.items()))
def update(sim):
    st = Stage(sim)
    st.call(st.v.create_voice_slot, ROBOT, st.body(value="기초연금", arrivalName="기초연금 창구"), force=True)
    sid = st.slot_id("기초연금")
    st.call(st.v.update_voice_slot, sid, st.body(value="기초연금", synonyms="연금"), force=True)
    after_none = st.bundled("기초연금")[0]
    st.call(st.v.update_voice_slot, sid, st.body(value="기초연금", arrivalName=""), force=True)
    return {"after_none": after_none, "after_clear": st.bundled("기초연금")[0]}


@scenario("도착 장소 이름 길이 상한(50자) — 넘으면 거절",
          expect=lambda r, _w={"rejected": True}: all(r.get(k) == v for k, v in _w.items()))
def too_long(sim):
    st = Stage(sim)
    b = st.body(value="안과", arrivalName="가" * 51)
    rejected = isinstance(b, Exception) or "arrivalName" not in getattr(b, "model_fields_set", set())
    return {"rejected": isinstance(b, Exception) and rejected}


@scenario("회귀 — 도착 장소 이름 없이 등록(기존 화면·API)",
          expect=lambda r, _w={"bundle": None, "text": "엘리베이터를 타고 3층으로 이동해 주세요.", "link": 1}: all(r.get(k) == v for k, v in _w.items()))
def legacy(sim):
    st = Stage(sim)
    st.call(st.v.create_voice_slot, ROBOT,
            st.body(value="엘리베이터 입구", arrivalText="엘리베이터를 타고 3층으로 이동해 주세요.",
                    locationIds=[1]), force=True)
    name, text, loc = st.bundled("엘리베이터 입구")
    return {"bundle": None if name in (None, "(칸 없음)") else name, "text": text, "link": loc}
