"""편집기 S2 서버 보완 — 맵 변경을 단계 저장과 한 번에 · 말 없이 이동하는 단계는 빈 멘트 경고 안 함.

무대는 editor_s1.py 의 Stage 를 그대로 쓴다(관제 dev DB 읽기 전용 덤프).
"""
import importlib.util
import os
from unittest.mock import MagicMock

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("editor_s1_base", os.path.join(os.path.dirname(__file__), "editor_s1.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
Stage, ADMIN, _new_flow, _place_on, _made = _base.Stage, _base.ADMIN, _base._new_flow, _base._place_on, _base._made


def _save(st, sid, steps, base, map_id=None):
    def run(db):
        kw = {"mapId": map_id} if map_id is not None else {}
        req = st.ve.StepsSaveReq(steps=[st.ve.StepIn(**s) for s in steps], baseVersion=base, **kw)
        return st.ve.save_voice_scenario_steps(sid, req, MagicMock(), db=db, current_user=ADMIN)
    return st.call(run)


def _scen(st, sid):
    db = st.Session()
    try:
        sc = db.query(st.M.VoiceScenario).get(sid)
        return {"MapId": sc.MapId, "FloorId": sc.FloorId, "StartLocationId": sc.StartLocationId, "Version": sc.Version}
    finally:
        db.close()


@scenario("M1 맵을 43으로 바꾸며 저장, 장소도 43 것 → 200, 맵·층 바뀜, 옛 시작 장소는 비워짐",
          expect=lambda r: r["res"].get("status") == 200 and r["after"]["MapId"] == 43
          and r["after"]["StartLocationId"] is None and r["after"]["FloorId"] == r["floor43"])
def m1(sim):
    st = Stage(sim)
    sc = _made(st)
    if not sc:
        return {"res": {"status": "API 없음"}, "after": {}, "floor43": None}
    db = st.Session()
    db.query(st.M.VoiceScenario).filter(st.M.VoiceScenario.id == sc.id).update(
        {"StartLocationId": _place_on(st, 47)})
    floor43 = db.query(st.M.RobotMapInfo).get(43).FloorId
    db.commit()
    db.close()
    r = _save(st, sc.id, _new_flow(_place_on(st, 43)), 1, map_id=43)
    return {"res": r, "after": _scen(st, sc.id), "floor43": floor43}


@scenario("M2 맵을 43으로 바꾸는데 장소는 47 것 → 422, 맵도 그대로(한쪽만 저장 안 됨)",
          expect=lambda r: r["res"].get("status") == 422 and r["after"]["MapId"] == 47 and r["after"]["Version"] == 1)
def m2(sim):
    st = Stage(sim)
    sc = _made(st)
    if not sc:
        return {"res": {"status": "API 없음"}, "after": {}}
    r = _save(st, sc.id, _new_flow(_place_on(st, 47)), 1, map_id=43)
    return {"res": r, "after": _scen(st, sc.id)}


@scenario("M3 다른 사업장 맵으로 바꾸며 저장 → 422",
          expect=lambda r: r["res"].get("status") == 422)
def m3(sim):
    st = Stage(sim)
    sc = _made(st)
    if not sc:
        return {"res": {"status": "API 없음"}}
    db = st.Session()
    db.add(st.M.RobotMapInfo(id=9999, BusinessId=2, FloorId=1, MapName="다른 사업장 맵"))
    db.commit()
    db.close()
    return {"res": _save(st, sc.id, _new_flow(_place_on(st, 47)), 1, map_id=9999)}


@scenario("M4 검사만 — 바꾼 맵(43) 기준으로 47 장소를 오류로 알림, 저장·맵 그대로",
          expect=lambda r: r["res"].get("status") == 200 and r["res"]["body"]["errors"] and r["after"]["MapId"] == 47)
def m4(sim):
    st = Stage(sim)
    sc = _made(st)
    if not sc:
        return {"res": {"status": "API 없음"}, "after": {}}
    r = st.call(lambda db: st.ve.validate_voice_scenario(
        sc.id, st.ve.StepsReq(steps=[st.ve.StepIn(**s) for s in _new_flow(_place_on(st, 47))], mapId=43),
        db=db, current_user=ADMIN))
    return {"res": r, "after": _scen(st, sc.id)}


@scenario("W1 주차장 그대로 저장 — 말 없이 이동(greet_move)은 빈 멘트 경고 안 함",
          expect=lambda r: r["res"].get("status") == 200 and not any("greet_move" in w for w in r["warnings"]))
def w1(sim):
    st = Stage(sim)
    if not st.ve:
        return {"res": {"status": "API 없음"}, "warnings": []}
    sc = st.sc("parking")
    cur, ver = st.current(sc.id)
    r = st.save(sc.id, cur, ver)
    return {"res": r, "warnings": [w["message"] for w in (r.get("body") or {}).get("warnings", [])]}


@scenario("W2 말만 단계의 빈 멘트는 그대로 경고 (회귀)",
          expect=lambda r: r["res"].get("status") == 200 and any("멘트가 비어" in w for w in r["warnings"]))
def w2(sim):
    st = Stage(sim)
    sc = _made(st)
    if not sc:
        return {"res": {"status": "API 없음"}, "warnings": []}
    f = _new_flow(_place_on(st, 47))
    f[0]["sayText"] = ""
    r = st.save(sc.id, f, 1)
    return {"res": r, "warnings": [w["message"] for w in (r.get("body") or {}).get("warnings", [])]}
