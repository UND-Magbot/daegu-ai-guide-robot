"""진행 멘트 기본 문장(2026-10-29, f2 세션 사용자 요청) — 칸이 비면 "○○ 방향으로 이동하고 있습니다."

  sim\\run.bat sim\\scenarios\\progress_cue_default.py --cand sim\\out\\cand_progress_default
무대·경로는 progress_cue.py 와 같다(직선 16m, 가짜 로봇 0.5 m/s). 이름은 부르는 이름 → 장소 이름(밑줄·층 표시·끝 번호 정리).
칸에 "-" 를 넣으면 그 단계는 말하지 않는다. 현행은 칸이 비면 말하지 않는다 — 기본 문장 시나리오는 현행에서 실패하는 것이 정상이다.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("progress_cue_base",
                                               os.path.join(os.path.dirname(__file__), "progress_cue.py"))
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)
del REGISTRY[_n:]

P = pc.P


def _stage(sim, progress=None, spoken=None, place_name=None, say=None, **kw):
    st = pc._stage(sim, progress=progress, **kw)
    db = st.Session()
    try:
        M = st.M
        row = (db.query(M.VoiceScenarioStep)
               .filter(M.VoiceScenarioStep.ScenarioId == 1, M.VoiceScenarioStep.StepCode == "ticket_move").first())
        if spoken is not None:
            row.SpokenPlaceName = spoken
        if say is not None:
            row.SayText = say
        if place_name is not None:
            db.query(M.LocationInfo).filter(M.LocationInfo.id == 2).first().LacationName = place_name
        db.commit()
    finally:
        db.close()
    return st


@scenario("[기본] 칸 비움 · 부르는 이름 없음 → 장소 이름으로 '원무과 접수 방향으로 이동하고 있습니다.'",
          expect=lambda r: r.get("heard") == ["원무과 접수 방향으로 이동하고 있습니다."] and r["errors"] == 0)
def default_place_name(sim):
    return pc._guide_move(_stage(sim), sim)


@scenario("[기본] 칸 비움 · 부르는 이름 '원무과' → '원무과 방향으로 이동하고 있습니다.'",
          expect=lambda r: r.get("heard") == ["원무과 방향으로 이동하고 있습니다."] and r["errors"] == 0)
def default_spoken_name(sim):
    return pc._guide_move(_stage(sim, spoken="원무과"), sim)


@scenario("[기본·이름 정리] 장소 이름 '1F_엘리베이터입구_01' → '엘리베이터입구 방향으로 이동하고 있습니다.'",
          expect=lambda r: r.get("heard") == ["엘리베이터입구 방향으로 이동하고 있습니다."] and r["errors"] == 0)
def default_name_cleanup(sim):
    return pc._guide_move(_stage(sim, place_name="1F_엘리베이터입구_01"), sim)


@scenario("[끄기] 칸 '-' → 말하지 않음",
          expect=lambda r: r.get("heard") == [] and r["errors"] == 0)
def off_dash(sim):
    return pc._guide_move(_stage(sim, progress="-", spoken="원무과"), sim)


@scenario("[회귀] 칸에 적은 문구는 그대로 (기본 문장보다 우선)",
          expect=lambda r: r.get("heard") == [P] and r["errors"] == 0)
def explicit_wins(sim):
    return pc._guide_move(_stage(sim, progress=P, spoken="다른 이름"), sim)


@scenario("[회귀] 멘트 없는 이동 단계(따라오는 사람 없음) → 기본 문장도 없음",
          expect=lambda r: r.get("heard") == [] and r["errors"] == 0)
def silent_move(sim):
    return pc._guide_move(_stage(sim, say=""), sim)


@scenario("[회귀] 안내 아닌 장소 이동(관제 화면) → 없음",
          expect=lambda r: r.get("heard") == [] and r["errors"] == 0)
def plain_move(sim):
    st = _stage(sim)
    start = (sim.robot.x, sim.robot.y)
    db = st.Session()
    try:
        sim.sm.start_place_move(2, db)
    finally:
        db.close()
    out = pc._listen(sim, start)
    st.drive(until=lambda: not sim.sm.is_navigating)
    return pc._result(out, sim)


@scenario("[회귀] 짧은 구간 4m → 기본 문장도 생략",
          expect=lambda r: r.get("heard") == [] and r["errors"] == 0)
def short_default(sim):
    return pc._guide_move(_stage(sim, robot_at=(0.0, 0.0)), sim)


def _bundle_step(st, code):
    import app.database.routes.voice_scenario as vs
    db = st.Session()
    try:
        b = vs.get_bundle(pc.gp.ROBOT, db=db, _="sim")
    finally:
        db.close()
    sc = next(x for x in b.scenarios if x.code == "hospital")
    return {x.stepCode: getattr(x, "progressText", "칸 없음") for x in sc.steps}.get(code, "단계 없음")


@scenario("[묶음] Jetson 미리 합성 — 고정 장소 단계는 기본 문장, '-' 는 빈 값, 고객 목적지 단계는 빈 값",
          expect=lambda r: r.get("fixed") == "원무과 방향으로 이동하고 있습니다." and r.get("off") is None
          and r.get("slot") is None and r.get("talk") is None)
def bundle_defaults(sim):
    st = _stage(sim, spoken="원무과")
    got = {"fixed": _bundle_step(st, "ticket_move"), "talk": _bundle_step(st, "greet")}
    db = st.Session()
    try:
        M = st.M
        rows = {r.StepCode: r for r in db.query(M.VoiceScenarioStep).filter(M.VoiceScenarioStep.ScenarioId == 1)}
        slot = next((r for r in rows.values() if r.ActionType == "navigate" and r.SlotName), None)
        rows["seat_move"].ProgressText = "-"
        db.commit()
        slot_code = slot.StepCode if slot else None
    finally:
        db.close()
    got["off"] = _bundle_step(st, "seat_move")
    got["slot"] = _bundle_step(st, slot_code) if slot_code else "고객 목적지 단계 없음"
    return got
