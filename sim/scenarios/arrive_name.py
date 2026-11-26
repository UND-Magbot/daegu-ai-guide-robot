"""회전·도착 접근 안내 문구(2026-10-27) — 본사 시연 대본 개정안(E3)과 단계별 '부르는 이름'.

  sim\\run.bat sim\\scenarios\\arrive_name.py --cand sim\\out\\cand_arrive_name
무대는 guide_prerun.py 의 병원 시나리오. 경로가 꺾이도록 장소 이동의 벽 우회 계산(plan_via_waypoints)을
경유지 1곳으로 바꿔 둔다(가정 — 실제 경유지는 맵·노드망이 정한다).
현행은 "잠시 후 ○쪽으로 돕니다"·"곧 도착합니다" 이고 부르는 이름 칸이 없다 — 문구·이름 시나리오는
현행에서 실패하는 것이 정상이다.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("guide_prerun_base",
                                               os.path.join(os.path.dirname(__file__), "guide_prerun.py"))
gp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gp)
del REGISTRY[_n:]

RIGHT = "잠시 후 오른쪽으로 방향을 바꾸겠습니다."
LEFT = "잠시 후 왼쪽으로 방향을 바꾸겠습니다."


def _stage(sim, via=(0.0, 3.0), spoken=None, say=None):
    """병원 무대 + 경유지 1곳. spoken: 원무과 접수 이동 단계(ticket_move)의 부르는 이름."""
    st = gp.Stage(sim)
    import app.navigation.via_route as vr

    def one_via(db, place, start):
        if via is None or start is None:
            return [], "직선"
        return [{"x": via[0], "y": via[1], "yaw": 0.0, "name": "경유지_가정",
                 "map_id": place.MapId, "floor_id": place.FloorId, "pass_through": True}], "가정"
    vr.plan_via_waypoints = one_via
    db = st.Session()
    try:
        row = (db.query(st.M.VoiceScenarioStep)
               .filter(st.M.VoiceScenarioStep.ScenarioId == 1, st.M.VoiceScenarioStep.StepCode == "ticket_move").first())
        has_col = hasattr(st.M.VoiceScenarioStep, "SpokenPlaceName")
        # 실제 DB 의 이동 단계는 도착 대기(arrival) — 무대에 빠져 있으면 편집기가 말만 단계로 정리한다
        for r in db.query(st.M.VoiceScenarioStep).filter(st.M.VoiceScenarioStep.ActionType == "navigate"):
            r.WaitFor = "arrival"
            # 이 파일은 도착 안내 이름만 본다 — 진행 멘트를 끈다. 11-02 부터 짧은 구간은 '곧 도착' 대신 진행 멘트를
            # 남기므로(progress_soon_drop) 켜 두면 이 경로(약 7m)에서 '곧 ○○에 도착합니다'가 나오지 않는다
            if hasattr(r, "ProgressText"):
                r.ProgressText = "-"
        if spoken is not None and has_col:
            row.SpokenPlaceName = spoken
        if say is not None:
            row.SayText = say
        db.commit()
    finally:
        db.close()
    st.has_col = has_col
    return st


def _move(st, sim, step="ticket_move", place_id=2):
    """안내를 열고 그 단계에서 장소 이동을 지시한다(Jetson 이 하는 호출과 같다). 들린 안내를 모은다."""
    res = st.open_guide()
    sid = res["session"]["id"]
    db = st.Session()
    try:
        s = db.query(st.M.ServiceSession).filter(st.M.ServiceSession.id == sid).first()
        s.CurrentStepCode = step
        db.commit()
        out = st.vs.session_move(sid, place_id=place_id, db=db, _="sim")
    finally:
        db.close()
    planned = dict(sim.sm._route_cues)
    heard = []
    seq = sim.sm.get_nav_cue()["seq"]

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
    return {"move": (out or {}).get("status"), "planned": [planned[k] for k in sorted(planned)], "heard": heard}


@scenario("[문구] 부르는 이름 있는 단계 → '오른쪽으로 방향을 바꾸겠습니다' 뒤 '곧 원무과에 도착합니다'",
          expect=lambda r: r.get("heard") == [RIGHT, "곧 원무과에 도착합니다."] and r.get("pos") == [4, 0]
          and r["errors"] == 0)
def named_arrival(sim):
    st = _stage(sim, spoken="원무과")
    return _move(st, sim)


@scenario("[문구] 부르는 이름 비어 있음 → '곧 도착합니다'(기존과 같음), 회전 문구만 새것",
          expect=lambda r: r.get("heard") == [RIGHT, "곧 도착합니다."] and r["errors"] == 0)
def unnamed_arrival(sim):
    st = _stage(sim)
    return _move(st, sim)


@scenario("[합침] 꺾는 곳이 목적지 가까이 → 한 문장 '…방향을 바꾸겠습니다. 곧 원무과에 도착합니다.'",
          expect=lambda r: r.get("heard") == [f"{LEFT} 곧 원무과에 도착합니다."] and r["errors"] == 0)
def merged(sim):
    # (0,-1) 동쪽으로 3.5m → 경유지 (3.5,-1) 에서 왼쪽 63° → 목적지 (4,0) 까지 1.1m(안내 2m 앞 + 1.1 ≤ 3.5)
    # 11-09 방향 안내 기준 50° — 예전 무대(왼쪽 45°)는 기준 아래라 꺾는 안내가 없어진다
    st = _stage(sim, via=(3.5, -1.0), spoken="원무과")
    sim.robot.place(0.0, -1.0)
    return _move(st, sim)


@scenario("[경계] 멘트 없는 이동 단계는 이름을 넣지 않음(따라오는 사람 없음) → '곧 도착합니다'",
          expect=lambda r: r.get("heard") == [RIGHT, "곧 도착합니다."] and r["errors"] == 0)
def silent_step(sim):
    st = _stage(sim, spoken="원무과", say="")
    return _move(st, sim)


@scenario("[경계] 다른 단계 이동에는 이전 단계 이름이 새지 않음 (원무과 → 진료대기장소)",
          expect=lambda r: r.get("first") == [RIGHT, "곧 원무과에 도착합니다."]
          and r.get("second") and r["second"][-1] == "곧 도착합니다." and r["errors"] == 0, limit_sec=600)
def no_leak(sim):
    st = _stage(sim, spoken="원무과")
    first = _move(st, sim)["heard"]
    st.vs._active_session(st.Session(), "SIM")
    db = st.Session()
    try:
        s = db.query(st.M.ServiceSession).order_by(st.M.ServiceSession.id.desc()).first()
        s.CurrentStepCode = "seat_move"
        db.commit()
        st.vs.session_move(s.id, place_id=3, db=db, _="sim")
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
    return {"first": first, "second": heard}


@scenario("[재계산] 주행 중 안내를 다시 계산해도(노드 끼움·측위 복구) 도착 이름 유지",
          expect=lambda r: r.get("after") and r["after"][-1] == "곧 원무과에 도착합니다.")
def recompute_keeps(sim):
    st = _stage(sim, spoken="원무과")
    st.open_guide()
    db = st.Session()
    try:
        s = db.query(st.M.ServiceSession).order_by(st.M.ServiceSession.id.desc()).first()
        s.CurrentStepCode = "ticket_move"
        db.commit()
        st.vs.session_move(s.id, place_id=2, db=db, _="sim")
    finally:
        db.close()
    sm = sim.sm
    got = {"initial": [sm._route_cues[k] for k in sorted(sm._route_cues)], "n_wps": len(sm.waypoints_list)}

    # 경유지를 지나기 전, 조금 옮긴 위치에서 남은 구간 안내를 다시 계산한다(측위 복구·노드 끼움과 같은 호출).
    # 남은 점이 목적지 하나뿐이면 원래도 안내를 다시 만들지 않는다(현행과 같음) — 그래서 출발 직후에 부른다.
    sm._set_route_cues((0.0, 0.5), sm.waypoints_list, 0)
    got["after"] = [sm._route_cues[k] for k in sorted(sm._route_cues)]
    st.drive(until=lambda: not sm.is_navigating)
    return got


@scenario("[문구] 뒤로 돌기(135° 넘게) → '잠시 후 뒤로 방향을 바꾸겠습니다'",
          expect=lambda r: r.get("phrase") == "잠시 후 뒤로 방향을 바꾸겠습니다.")
def back_turn(sim):
    from app.navigation.turn_cues import turn_phrase
    return {"phrase": turn_phrase(170.0)}


def _editor_steps(st):
    import app.database.voice_scenario_editor as ed
    db = st.Session()
    try:
        sc = db.query(st.M.VoiceScenario).filter(st.M.VoiceScenario.id == 1).first()
        return ed, db, sc, ed.current_steps(db, sc)
    except Exception:
        db.close()
        raise


@scenario("[편집기] 이동 단계에 부르는 이름 저장 → DB·편집기·엔진 묶음(bundle)에 그대로, 말만 단계에 넣은 값은 지워짐",
          expect=lambda r: r.get("db") == "원무과" and r.get("editor") == "원무과" and r.get("bundle") == "원무과"
          and r.get("greet_db") is None)
def editor_save(sim):
    st = _stage(sim)
    import app.database.routes.voice_scenario_editor as ve
    ed, db, sc, steps = _editor_steps(st)
    try:
        for s in steps:
            if s["stepCode"] == "ticket_move":
                s["spokenPlaceName"] = "  원무과 "
            if s["stepCode"] == "greet":
                s["spokenPlaceName"] = "인사에 넣은 값"
        try:
            ve._save_steps(db, sc, steps, sc.Version or 0, "sim", gp.USER)
            db.commit()
        except Exception as e:      # noqa: BLE001 — 현행·변경 비교용
            return {"err": repr(e)[:200]}
        rows = {r.StepCode: r for r in db.query(st.M.VoiceScenarioStep).filter(st.M.VoiceScenarioStep.ScenarioId == 1)}
        got = getattr(rows["ticket_move"], "SpokenPlaceName", "칸 없음")
        greet = getattr(rows["greet"], "SpokenPlaceName", None)
        editor = next((s.get("spokenPlaceName", "칸 없음") for s in ed.current_steps(db, sc)
                       if s["stepCode"] == "ticket_move"), None)
    finally:
        db.close()
    db = st.Session()
    try:
        b = st.vs.get_bundle(gp.ROBOT, db=db, _="sim")
    finally:
        db.close()
    sc_b = next(x for x in b.scenarios if x.code == "hospital")
    bundle = next((getattr(x, "spokenPlaceName", "칸 없음") for x in sc_b.steps if x.stepCode == "ticket_move"), None)
    return {"db": got, "editor": editor, "bundle": bundle, "greet_db": greet}


@scenario("[편집기·경계] 부르는 이름 31자 → 저장 거부(422), 단계는 그대로",
          expect=lambda r: str(r.get("res")).startswith("422") and "부르는 이름" in str(r.get("res")))
def editor_too_long(sim):
    st = _stage(sim)
    import app.database.routes.voice_scenario_editor as ve
    from fastapi import HTTPException
    ed, db, sc, steps = _editor_steps(st)
    try:
        for s in steps:
            if s["stepCode"] == "ticket_move":
                s["spokenPlaceName"] = "가" * 31
        try:
            ve._save_steps(db, sc, steps, sc.Version or 0, "sim", gp.USER)
            db.commit()
            return {"res": "저장됨"}
        except HTTPException as e:
            return {"res": f"{e.status_code} {e.detail}"}
    finally:
        db.close()


@scenario("[회귀] 안내 아닌 장소 이동(관제 화면) → 경유지 안내는 새 문구, 도착은 '곧 도착합니다'",
          expect=lambda r: r.get("heard") == [RIGHT, "곧 도착합니다."] and r["errors"] == 0)
def plain_place_move(sim):
    st = _stage(sim, spoken="원무과")
    db = st.Session()
    try:
        sim.sm.start_place_move(2, db)
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
    return {"heard": heard}
