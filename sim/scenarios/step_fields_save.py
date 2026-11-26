"""편집 화면 저장에서 이동 단계 부르는 이름·진행 멘트가 빠지던 문제(10-29).

StepIn 에 두 칸이 없어 요청에서 버려지고, 전체 저장이 NULL 로 덮었다(운영 병원 v11→v12 진행 멘트 4개 사라짐).
무대는 editor_s1.py 의 Stage(관제 dev DB 읽기 전용 덤프).
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("editor_s1_base", os.path.join(os.path.dirname(__file__), "editor_s1.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
_s2 = importlib.util.spec_from_file_location("editor_s2_base", os.path.join(os.path.dirname(__file__), "editor_s2_api.py"))
_s2m = importlib.util.module_from_spec(_s2)
_s2.loader.exec_module(_s2m)
del REGISTRY[_n:]
Stage, _new_flow, _place_on, _made = _base.Stage, _base._new_flow, _base._place_on, _base._made
_save = _s2m._save


def _row(st, sid, code):
    db = st.Session()
    try:
        s = db.query(st.M.VoiceScenarioStep).filter_by(ScenarioId=sid, StepCode=code).first()
        return {"spoken": s.SpokenPlaceName, "progress": s.ProgressText} if s else None   # 저장 거절이면 단계 없음
    finally:
        db.close()


def _flow(st, **move):
    f = _new_flow(_place_on(st, 47))
    f[2].update(move)
    return f


def _run(sim, move, extra=None):
    st = Stage(sim)
    sc = _made(st)
    if not sc:
        return {"res": {"status": "API 없음"}}
    f = _flow(st, **move)
    if extra:
        f[0].update(extra)
    r = _save(st, sc.id, f, 1)
    return {"res": r, "move": _row(st, sc.id, "s_002"), "greet": _row(st, sc.id, "greet")}


@scenario("F1 이동 단계에 부르는 이름·진행 멘트 넣고 저장 → 그대로 남음",
          expect=lambda r: r["res"].get("status") == 200
          and r["move"] == {"spoken": "원무과", "progress": "원무과 방향으로 이동하고 있습니다."})
def f1(sim):
    return _run(sim, {"spokenPlaceName": "원무과", "progressText": "원무과 방향으로 이동하고 있습니다."})


@scenario("F2 진행 멘트 '-'(말하지 않음) → '-' 로 남음",
          expect=lambda r: r["res"].get("status") == 200 and r["move"]["progress"] == "-")
def f2(sim):
    return _run(sim, {"progressText": "-"})


@scenario("F3 비워 저장 → NULL(지금 동작 유지)",
          expect=lambda r: r["res"].get("status") == 200 and r["move"] == {"spoken": None, "progress": None})
def f3(sim):
    return _run(sim, {"spokenPlaceName": "", "progressText": None})


@scenario("F4 말만 단계에 두 칸을 넣어도 비워짐(normalize 유지)",
          expect=lambda r: r["res"].get("status") == 200 and r["greet"] == {"spoken": None, "progress": None})
def f4(sim):
    return _run(sim, {}, extra={"spokenPlaceName": "x", "progressText": "y"})


@scenario("F5 부르는 이름 31자 → 검사 오류로 저장 거절(422 + spokenPlaceName 오류), 형식 오류 아님",
          expect=lambda r: r["res"].get("status") == 422 and isinstance(r["res"].get("detail"), dict)
          and any(e.get("field") == "spokenPlaceName" for e in r["res"]["detail"].get("errors", [])))
def f5(sim):
    return _run(sim, {"spokenPlaceName": "가" * 31})
