"""이동 중 진행 멘트(2026-10-29) — 본사 시연 대본 개정안 E4 "편의점 방향으로 이동하고 있습니다."

  sim\\run.bat sim\\scenarios\\progress_cue.py --cand sim\\out\\cand_progress_cue
무대는 arrive_name.py 와 같은 병원 시나리오(guide_prerun.py). 경로는 장소 이동의 벽 우회 계산을
가정 경유지로 바꿔 만든다(실제 경유지는 맵·노드망이 정한다). 가짜 로봇은 0.5 m/s 로 걷는다.
현행에는 진행 멘트 칸이 없다 — 진행 멘트가 들려야 하는 시나리오는 현행에서 실패하는 것이 정상이다.
규칙(10-29 실기 뒤 개정): 경로 5m 이상, 걷기 시작(0.5m) 2초 뒤부터 한 번, 방향 안내에 붙이지 않고 따로.
앞 안내 뒤 5초가 지났고 다음 안내까지 5초 이상 남은 구간에서만 낸다(시간은 보낸 속도 명목값 — 보통 1.2 m/s).
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("guide_prerun_base",
                                               os.path.join(os.path.dirname(__file__), "guide_prerun.py"))
gp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gp)
del REGISTRY[_n:]

RIGHT = "잠시 후 오른쪽으로 방향을 바꾸겠습니다."
SOON = "곧 도착합니다."
P = "원무과 방향으로 이동하고 있습니다."


def _stage(sim, vias=(), progress=None, robot_at=(-12.0, 0.0)):
    """병원 무대 + 가정 경유지. progress: 원무과 접수 이동 단계(ticket_move)의 진행 멘트."""
    st = gp.Stage(sim)
    import app.navigation.via_route as vr

    def plan(db, place, start):
        if start is None:
            return [], "직선"
        return [{"x": x, "y": y, "yaw": 0.0, "name": f"경유지_{i + 1}", "map_id": place.MapId,
                 "floor_id": place.FloorId, "pass_through": True} for i, (x, y) in enumerate(vias)], "가정"
    vr.plan_via_waypoints = plan
    db = st.Session()
    try:
        for r in db.query(st.M.VoiceScenarioStep).filter(st.M.VoiceScenarioStep.ActionType == "navigate"):
            r.WaitFor = "arrival"
        st.has_col = hasattr(st.M.VoiceScenarioStep, "ProgressText")
        if progress is not None and st.has_col:
            row = (db.query(st.M.VoiceScenarioStep)
                   .filter(st.M.VoiceScenarioStep.ScenarioId == 1, st.M.VoiceScenarioStep.StepCode == "ticket_move").first())
            row.ProgressText = progress
        db.commit()
    finally:
        db.close()
    sim.robot.place(*robot_at)
    return st


def _listen(sim, start):
    """들린 안내를 (문구, 출발 뒤 초) 로 모은다. 출발 = 출발점에서 0.5m 움직인 때."""
    out = {"heard": [], "depart": None}
    seq = sim.sm.get_nav_cue()["seq"]

    def tick():
        nonlocal seq
        now = sim.clock.elapsed()
        if out["depart"] is None and math.hypot(sim.robot.x - start[0], sim.robot.y - start[1]) >= 0.5:
            out["depart"] = now
        c = sim.sm.get_nav_cue()
        if c["seq"] != seq:
            seq = c["seq"]
            out["heard"].append((c["text"], now))
        if sim.sm.is_navigating:
            sim.at(0.2, tick)
    sim.at(0.2, tick)
    return out


def _result(out, sim):
    d = out["depart"] or 0.0
    texts = [t for t, _ in out["heard"]]
    times = [round(at - d, 1) for _, at in out["heard"]]
    gaps = [round(b - a, 1) for a, b in zip(times, times[1:])]
    return {"heard": texts, "t": times, "min_gap": min(gaps) if gaps else None,
            "pos": [round(sim.robot.x), round(sim.robot.y)]}


def _guide_move(st, sim, step="ticket_move", place_id=2):
    """안내를 열고 그 단계에서 장소 이동을 지시한다(Jetson 이 하는 호출과 같다)."""
    res = st.open_guide()
    sid = res["session"]["id"]
    start = (sim.robot.x, sim.robot.y)
    db = st.Session()
    try:
        s = db.query(st.M.ServiceSession).filter(st.M.ServiceSession.id == sid).first()
        s.CurrentStepCode = step
        db.commit()
        st.vs.session_move(sid, place_id=place_id, db=db, _="sim")
    finally:
        db.close()
    out = _listen(sim, start)
    st.drive(until=lambda: not sim.sm.is_navigating)
    return _result(out, sim)


def _ok_gap(r):
    return r.get("min_gap") is None or r["min_gap"] >= 5.0


@scenario("[기능] 직선 16m · 꺾임 없음 → 걷기 시작 약 2초 뒤 따로 한 번",
          expect=lambda r: r.get("heard") == [P] and 1.5 <= r["t"][0] <= 4.0 and r.get("pos") == [4, 0]
          and r["errors"] == 0)
def straight_long(sim):
    st = _stage(sim, progress=P)
    return _guide_move(st, sim)


# 10-30 오후부터 남은 시간을 실제 걸음(가짜 로봇 0.5 m/s)으로 잰다 — 예전엔 명목 1.2 m/s 로 잡아 '여유 없음'이었다.
@scenario("[0.5 m/s 여유 있음] 15.4m · 꺾임 1곳(첫 구간 10.4m) — 꺾기 전 긴 구간에 진행 멘트, 앞뒤 안내와 5초 넘게 떨어짐",
          expect=lambda r: r.get("heard") == [P, RIGHT, SOON] and _ok_gap(r) and r["errors"] == 0)
def long_with_turn(sim):
    st = _stage(sim, vias=[(0.0, 3.0)], progress=P, robot_at=(-10.0, 0.0))
    return _guide_move(st, sim)


@scenario("[겹침] 출발 직후 꺾임(걷기 전 방향 안내) · 저속 명령 0.35 m/s → 긴 직선에서 5초 이상 뒤에 따로, 겹치지 않음",
          expect=lambda r: r.get("heard") == [RIGHT, P, SOON] and _ok_gap(r) and r["errors"] == 0)
def early_turn_defers(sim):
    # 첫 구간 2.1m — 방향 안내가 출발과 거의 동시(걷기 시작 2초 전)에 나온다. 뒤 12.6m 직선.
    # 10-29 오후부터 안내 시점이 보낸 속도를 따르므로 현장 조건(저속 명령·실제 약 0.33 m/s)으로 맞춘다 —
    # 보통 명령(1.0~1.2 m/s)이면 12.6m 는 약 10초라 곧 도착(6.5초 전) 앞에 넣을 틈이 없는 것이 맞다.
    st = _stage(sim, vias=[(-8.5, 1.5)], progress=P, robot_at=(-10.0, 0.0))
    sim.robot.p.speed = 0.35
    db = st.Session()
    try:
        db.query(st.M.VoiceScenario).filter(st.M.VoiceScenario.id == 1).first().GuideSpeed = "slow"
        db.commit()
    finally:
        db.close()
    return _guide_move(st, sim)


@scenario("[경계] 2.8m 마다 꺾는 지그재그 → 여유 있는 구간이 없어 생략(붙이지 않음), 방향 안내는 그대로",
          expect=lambda r: not any(P in h for h in r.get("heard", [])) and r.get("heard") and r["heard"][-1] == SOON
          and r["errors"] == 0, limit_sec=600)
def zigzag_skips(sim):
    zig = [(-10.0, 1.0), (-8.0, -1.0), (-6.0, 1.0), (-4.0, -1.0), (-2.0, 1.0), (0.0, -1.0), (2.0, 1.0)]
    st = _stage(sim, vias=zig, progress=P)
    return _guide_move(st, sim)


@scenario("[경계] 8m · 출발 때 꺾고 남은 건 곧 도착뿐(가까움) → 둘 다는 안 들어가 '곧 도착'을 빼고 진행 멘트(11-02 결정)",
          expect=lambda r: r.get("heard") == [RIGHT, P] and r["errors"] == 0)
def short_route(sim):
    st = _stage(sim, vias=[(0.0, 3.0)], progress=P, robot_at=(0.0, 0.0))
    return _guide_move(st, sim)


@scenario("[경계] 직선 4m(< 5m) → 진행 멘트 없음",
          expect=lambda r: r.get("heard") == [] and r.get("pos") == [4, 0] and r["errors"] == 0)
def too_short(sim):
    st = _stage(sim, progress=P, robot_at=(0.0, 0.0))
    return _guide_move(st, sim)


@scenario("[끄기] 진행 멘트 칸 '-' · 16m → 아무 말 없음 (10-29 부터 빈 칸은 기본 문장 — progress_cue_default.py)",
          expect=lambda r: r.get("heard") == [] and r["errors"] == 0)
def empty_text(sim):
    st = _stage(sim, progress="-")
    return _guide_move(st, sim)


@scenario("[회귀] 안내 아닌 장소 이동(관제 화면) 16m → 진행 멘트 없음",
          expect=lambda r: r.get("heard") == [] and r.get("pos") == [4, 0] and r["errors"] == 0)
def plain_place_move(sim):
    st = _stage(sim, progress=P)
    start = (sim.robot.x, sim.robot.y)
    db = st.Session()
    try:
        sim.sm.start_place_move(2, db)
    finally:
        db.close()
    out = _listen(sim, start)
    st.drive(until=lambda: not sim.sm.is_navigating)
    return _result(out, sim)


@scenario("[경계] 다음 이동에 새지 않음 — 원무과(멘트 있음) 뒤 편의점 20m(칸 '-')",
          expect=lambda r: r.get("first") == [P] and r.get("second") == [] and r["errors"] == 0, limit_sec=900)
def no_leak(sim):
    st = _stage(sim, progress=P)
    first = _guide_move(st, sim)["heard"]
    db = st.Session()
    try:
        (db.query(st.M.VoiceScenarioStep)
         .filter(st.M.VoiceScenarioStep.ScenarioId == 1, st.M.VoiceScenarioStep.StepCode == "store_move")
         .first()).ProgressText = "-"
        db.commit()
    finally:
        db.close()
    sim.robot.place(-12.0, 4.0)
    start = (sim.robot.x, sim.robot.y)
    db = st.Session()
    try:
        s = db.query(st.M.ServiceSession).order_by(st.M.ServiceSession.id.desc()).first()
        s.CurrentStepCode = "store_move"
        db.commit()
        st.vs.session_move(s.id, place_id=4, db=db, _="sim")
    finally:
        db.close()
    out = _listen(sim, start)
    st.drive(until=lambda: not sim.sm.is_navigating)
    return {"first": first, "second": _result(out, sim)["heard"]}


def _editor(st):
    import app.database.voice_scenario_editor as ed
    db = st.Session()
    sc = db.query(st.M.VoiceScenario).filter(st.M.VoiceScenario.id == 1).first()
    return ed, db, sc, ed.current_steps(db, sc)


@scenario("[편집기] 이동 단계에 진행 멘트 저장 → DB·편집기·엔진 묶음에 그대로, 말만 단계에 넣은 값은 지워짐",
          expect=lambda r: r.get("db") == P and r.get("editor") == P and r.get("bundle") == P
          and r.get("greet_db") is None)
def editor_save(sim):
    st = _stage(sim)
    import app.database.routes.voice_scenario_editor as ve
    ed, db, sc, steps = _editor(st)
    try:
        for s in steps:
            if s["stepCode"] == "ticket_move":
                s["progressText"] = f"  {P} "
            if s["stepCode"] == "greet":
                s["progressText"] = "인사에 넣은 값"
        try:
            ve._save_steps(db, sc, steps, sc.Version or 0, "sim", gp.USER)
            db.commit()
        except Exception as e:      # noqa: BLE001 — 현행·변경 비교용
            return {"err": repr(e)[:200]}
        rows = {r.StepCode: r for r in db.query(st.M.VoiceScenarioStep).filter(st.M.VoiceScenarioStep.ScenarioId == 1)}
        got = getattr(rows["ticket_move"], "ProgressText", "칸 없음")
        greet = getattr(rows["greet"], "ProgressText", None)
        editor = next((s.get("progressText", "칸 없음") for s in ed.current_steps(db, sc)
                       if s["stepCode"] == "ticket_move"), None)
    finally:
        db.close()
    db = st.Session()
    try:
        b = st.vs.get_bundle(gp.ROBOT, db=db, _="sim")
    finally:
        db.close()
    sc_b = next(x for x in b.scenarios if x.code == "hospital")
    bundle = next((getattr(x, "progressText", "칸 없음") for x in sc_b.steps if x.stepCode == "ticket_move"), None)
    return {"db": got, "editor": editor, "bundle": bundle, "greet_db": greet}


@scenario("[편집기·경계] 진행 멘트 101자 → 저장 거부(422)",
          expect=lambda r: str(r.get("res")).startswith("422") and "진행 멘트" in str(r.get("res")))
def editor_too_long(sim):
    st = _stage(sim)
    import app.database.routes.voice_scenario_editor as ve
    from fastapi import HTTPException
    ed, db, sc, steps = _editor(st)
    try:
        for s in steps:
            if s["stepCode"] == "ticket_move":
                s["progressText"] = "가" * 101
        try:
            ve._save_steps(db, sc, steps, sc.Version or 0, "sim", gp.USER)
            db.commit()
            return {"res": "저장됨"}
        except HTTPException as e:
            return {"res": f"{e.status_code} {e.detail}"}
    finally:
        db.close()
