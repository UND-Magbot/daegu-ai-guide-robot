"""목적지 사전 '도착지 음성명'(voice_slot_value.SpokenName, 메모 2026-11-03).

  sim\\run.bat sim\\scenarios\\slot_spoken_name.py

고객 목적지로 가는 단계(SlotName='목적지' — clinic_move·extra_move)는 장소가 세션마다 달라 단계에 부르는 이름을
둘 수 없었다. 그래서 '곧 ○○에 도착합니다'·'○○ 방향으로 이동하고 있습니다'에 장소 DB 이름(간호실데스크,
1F_엘리베이터입구)이 그대로 나갔다. 목적지 사전 항목에 음성명을 두면 그 이름으로 말한다.
무대는 arrive_name.py(병원, 경유지 1곳 가정). 실제 DB 와 같은 장소 이름을 쓴다(간호실데스크·1F_엘리베이터입구).
현행(칸 없음)은 이름 시나리오가 실패하는 것이 정상이다.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("arrive_name_base",
                                               os.path.join(os.path.dirname(__file__), "arrive_name.py"))
an = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(an)
del REGISTRY[_n:]


def _setup(sim, spoken: dict, progress_on=False, step_spoken=None):
    """spoken: 목적지 값 → 음성명. progress_on: 진행 멘트 켬(칸 비움 = 기본 문장)."""
    st = an._stage(sim)
    db = st.Session()
    try:
        has_col = hasattr(st.M.VoiceSlotValue, "SpokenName")
        for row in db.query(st.M.VoiceSlotValue):
            if row.Value in spoken and has_col:
                row.SpokenName = spoken[row.Value]
        for r in db.query(st.M.VoiceScenarioStep).filter(st.M.VoiceScenarioStep.ScenarioId == 1):
            if progress_on and r.ActionType == "navigate":
                r.ProgressText = None
            if step_spoken is not None and r.StepCode == "clinic_move":
                r.SpokenPlaceName = step_spoken
        db.commit()
    finally:
        db.close()
    st.has_slot_col = has_col
    return st


def _move_dest(st, sim, step, dest, place_id):
    """안내를 열고 고객 목적지를 정한 뒤(Jetson 이 하는 PATCH 와 같은 값) 그 단계 이동을 지시한다."""
    res = st.open_guide()
    sid = res["session"]["id"]
    db = st.Session()
    try:
        s = db.query(st.M.ServiceSession).filter(st.M.ServiceSession.id == sid).first()
        s.CurrentStepCode = step
        s.DestinationName = dest
        s.DestinationId = place_id
        db.commit()
        out = st.vs.session_move(sid, place_id=place_id, db=db, _="sim")
    finally:
        db.close()
    heard, seq = [], sim.sm.get_nav_cue()["seq"]

    def listen():
        nonlocal seq
        c = sim.sm.get_nav_cue()
        if c["seq"] != seq:
            seq = c["seq"]
            heard.append(c["text"])
        if sim.sm.is_navigating:
            sim.at(0.2, listen)
    sim.at(0.2, listen)
    st.drive(until=lambda: not sim.sm.is_navigating)
    return {"move": (out or {}).get("status"), "heard": heard}


@scenario("[이름] 정형외과(→간호실데스크) 음성명 '진료실' → '곧 진료실에 도착합니다'",
          expect=lambda r: r.get("heard") and r["heard"][-1] == "곧 진료실에 도착합니다." and r["errors"] == 0)
def clinic_named(sim):
    st = _setup(sim, {"정형외과": "진료실"})
    return _move_dest(st, sim, "clinic_move", "정형외과", 5)


@scenario("[이름] 3층관공서(→1F_엘리베이터입구) 음성명 '엘리베이터 입구' → '곧 엘리베이터 입구에 도착합니다'",
          expect=lambda r: r.get("heard") and r["heard"][-1] == "곧 엘리베이터 입구에 도착합니다." and r["errors"] == 0)
def elevator_named(sim):
    st = _setup(sim, {"3층관공서": "엘리베이터 입구"})
    sim.robot.place(0.0, 0.0)
    return _move_dest(st, sim, "extra_move", "3층관공서", 6)


@scenario("[진행 멘트] 칸 비움 + 음성명 '진료실' → '진료실 방향으로 이동하고 있습니다'(간호실데스크 아님)",
          expect=lambda r: "진료실 방향으로 이동하고 있습니다." in (r.get("heard") or [])
          and not any("간호실" in h for h in r.get("heard") or []) and r["errors"] == 0)
def clinic_progress(sim):
    st = _setup(sim, {"정형외과": "진료실"}, progress_on=True)
    return _move_dest(st, sim, "clinic_move", "정형외과", 5)


@scenario("[회귀] 음성명 비움 → 지금과 같음('곧 도착합니다')",
          expect=lambda r: r.get("heard") and r["heard"][-1] == "곧 도착합니다." and r["errors"] == 0)
def clinic_unnamed(sim):
    st = _setup(sim, {})
    return _move_dest(st, sim, "clinic_move", "정형외과", 5)


@scenario("[회귀] 진행 멘트 칸 비움 + 음성명 비움 → 장소 이름 기본 문장(현행과 같음)",
          expect=lambda r: "간호실데스크 방향으로 이동하고 있습니다." in (r.get("heard") or []) and r["errors"] == 0)
def clinic_progress_unnamed(sim):
    st = _setup(sim, {}, progress_on=True)
    return _move_dest(st, sim, "clinic_move", "정형외과", 5)


@scenario("[우선] 단계의 부르는 이름이 있으면 그것이 먼저(단계 '정형외과 앞' > 목적지 '진료실')",
          expect=lambda r: r.get("heard") and r["heard"][-1] == "곧 정형외과 앞에 도착합니다." and r["errors"] == 0)
def step_name_wins(sim):
    st = _setup(sim, {"정형외과": "진료실"}, step_spoken="정형외과 앞")
    return _move_dest(st, sim, "clinic_move", "정형외과", 5)


@scenario("[경계] 고정 장소 단계(seat_move)는 목적지 음성명이 새지 않음 → '곧 도착합니다'",
          expect=lambda r: r.get("heard") and r["heard"][-1] == "곧 도착합니다." and r["errors"] == 0)
def fixed_step_no_leak(sim):
    st = _setup(sim, {"정형외과": "진료실"})
    return _move_dest(st, sim, "seat_move", "정형외과", 3)


@scenario("[번들] 목적지 사전 음성명이 Jetson 번들 slots[].spokenName 으로 나감",
          expect=lambda r: r.get("spoken") == "엘리베이터 입구" and r.get("other") is None)
def bundle_has_spoken(sim):
    st = _setup(sim, {"3층관공서": "엘리베이터 입구"})
    db = st.Session()
    try:
        b = st.vs.get_bundle(an.gp.ROBOT, db=db, _="sim")
    finally:
        db.close()
    by = {s.value: getattr(s, "spokenName", "칸 없음") for s in b.slots}
    return {"spoken": by.get("3층관공서"), "other": by.get("정형외과")}


@scenario("[화면 API] 목적지 저장·수정 — 음성명 저장, None 은 그대로, '' 은 지움, 31자 거부",
          expect=lambda r: r.get("created") == "진료실" and r.get("kept") == "진료실" and r.get("cleared") is None
          and str(r.get("too_long", "")).startswith("ValidationError"))
def slot_api(sim):
    st = _setup(sim, {})
    import app.database.routes.voice as v
    from pydantic import ValidationError
    v.write_audit = lambda *a, **k: None
    v.get_client_ip = lambda *a, **k: "sim"
    v._get_robot_or_404 = lambda db, rid, user: db.query(st.M.RobotInfo).filter(st.M.RobotInfo.id == rid).first()
    out = {}
    db = st.Session()
    try:
        res = v.create_voice_slot(an.gp.ROBOT, v.SlotReq(value="피부과", spokenName=" 진료실 "), request=None,
                                  force=True, db=db, current_user=an.gp.USER)
        sid = res.get("id") or db.query(st.M.VoiceSlotValue).filter(st.M.VoiceSlotValue.Value == "피부과").first().id
        out["created"] = db.query(st.M.VoiceSlotValue).get(sid).SpokenName
        v.update_voice_slot(sid, v.SlotReq(value="피부과", synonyms="피부"), request=None, force=True,
                            db=db, current_user=an.gp.USER)
        db.expire_all()
        out["kept"] = db.query(st.M.VoiceSlotValue).get(sid).SpokenName
        v.update_voice_slot(sid, v.SlotReq(value="피부과", spokenName=""), request=None, force=True,
                            db=db, current_user=an.gp.USER)
        db.expire_all()
        out["cleared"] = db.query(st.M.VoiceSlotValue).get(sid).SpokenName
        row = v._slot_row(db.query(st.M.VoiceSlotValue).get(sid), [], None)
        out["row_has_key"] = "spokenName" in row
    except Exception as e:      # noqa: BLE001 — 현행·변경 비교용
        out["err"] = repr(e)[:200]
    finally:
        db.close()
    try:
        v.SlotReq(value="x", spokenName="가" * 31)
        out["too_long"] = "통과"
    except ValidationError:
        out["too_long"] = "ValidationError"
    return out
