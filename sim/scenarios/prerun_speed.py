"""사전 주행 속도 선택(2026-10-27) — 층 이동 시험 탭의 1회 주행 테스트에서 저속·보통·고속을 고른다.

  sim\\run.bat sim\\scenarios\\prerun_speed.py --cand sim\\out\\cand_prerun_speed
무대는 guide_prerun.py 의 병원 시나리오를 그대로 쓴다.
현행은 speed 를 받지 않아(요청 모델이 모르는 칸은 버림) 늘 고속으로 돈다 — 저속·보통·잘못된 값 시나리오는
현행에서 실패하는 것이 정상이다. 속도를 안 보내는 경로(안내 시작 창 '고속 1바퀴')는 둘 다 고속이어야 한다.
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

MP = {"NAV_MULTIPOINT_ENABLED": "true"}


def _start(st, speed=None, code="hospital", start=1):
    """화면이 보내는 요청 그대로. 현행에는 PrerunStartReq 가 없어 speed 를 버린다."""
    from pydantic import ValidationError
    req_cls = getattr(st.va, "PrerunStartReq", st.va.SessionOpenReq)
    body = {"scenarioCode": code, "startLocationId": start}
    if speed is not None:
        body["speed"] = speed
    try:
        req = req_cls(**body)
    except ValidationError:
        return "422 속도 값 오류"
    return st.call(st.va.start_voice_prerun, gp.ROBOT, req, gp.REQ)


def _lap(sim, speed):
    st, ok = gp._stage(sim)
    if not ok:
        return gp.NO_FEATURE
    res = _start(st, speed)
    st.drive()
    return st.summary({"start": res if isinstance(res, str) else "ok", "visited": gp._visited(st.navs),
                       "snap_speed": (st.state() or {}).get("speed", "칸 없음")})


def _lap_ok(want):
    return lambda r: (r.get("status") == "done" and r.get("visited") == gp.HOSPITAL_B[1:]
                      and r.get("nav_speeds") == [want] and r.get("snap_speed") == want
                      and r.get("pos") == [0, 0] and r["errors"] == 0)


@scenario("[속도] 저속 선택 → 한 바퀴 모든 NAV 가 slow, 완료·출발 장소 복귀", expect=_lap_ok("slow"),
          limit_sec=2400)
def lap_slow(sim):
    return _lap(sim, "slow")


@scenario("[속도] 보통 선택 → 한 바퀴 모든 NAV 가 normal", expect=_lap_ok("normal"), limit_sec=2000)
def lap_normal(sim):
    return _lap(sim, "normal")


@scenario("[속도] 고속 선택 → fast (기존과 같음)", expect=_lap_ok("fast"), limit_sec=1500)
def lap_fast(sim):
    return _lap(sim, "fast")


@scenario("[호환] 속도 안 보냄(안내 시작 창 '고속 1바퀴') → fast",
          expect=lambda r: r.get("status") == "done" and r.get("nav_speeds") == ["fast"], limit_sec=1500)
def lap_default(sim):
    return _lap(sim, None)


@scenario("[경계] 잘못된 속도 값 → 422, 로봇 안 움직임",
          expect=lambda r: str(r.get("start")).startswith("422") and r.get("nav_n") == 0)
def bad_speed(sim):
    st, ok = gp._stage(sim)
    if not ok:
        return gp.NO_FEATURE
    res = _start(st, "turbo")
    sim.run(sim.pl.nav_thread, until=lambda: sim.clock.elapsed() > 10, grace=1.0)
    return {"start": res if isinstance(res, str) else "ok(시작됨)", "nav_n": len(st.navs)}


@scenario("[다시] 저속으로 돌다 측위 상실 30초 넘김 → 멈춤 → '실패 지점부터 다시' 도 저속",
          expect=lambda r: r.get("first") == "failed" and r.get("status") == "done"
          and r.get("before") == ["slow"] and r.get("after") == ["slow"] and r["errors"] == 0,
          limit_sec=2600)
def resume_keeps_speed(sim):
    st, ok = gp._stage(sim)
    if not ok:
        return gp.NO_FEATURE
    _start(st, "slow")
    st.lose_loc(at=40, sec=45)
    st.drive()
    first = st.state()["status"]
    n = len(st.navs)
    sim.run(sim.pl.nav_thread, until=lambda: sim.clock.elapsed() > 100, grace=0.5)   # 회복 뒤 다시
    st.resume(True)
    st.drive()
    return st.summary({"first": first,
                       "before": sorted({str(s) for _, _, s in st.navs[:n]}),
                       "after": sorted({str(s) for _, _, s in st.navs[n:]})})


@scenario("[다시] 저속 한 바퀴 뒤 중지 → '처음부터 다시' 도 저속",
          expect=lambda r: r.get("status") == "done" and r.get("after") == ["slow"] and r["errors"] == 0,
          limit_sec=4000)
def restart_keeps_speed(sim):
    st, ok = gp._stage(sim)
    if not ok:
        return gp.NO_FEATURE
    _start(st, "slow")
    sim.at(30, st.stop)
    st.drive()
    n = len(st.navs)
    st.resume(False)
    st.drive()
    return st.summary({"after": sorted({str(s) for _, _, s in st.navs[n:]})})


@scenario("[다지점 켬] 보통 선택 → 묶음 안 점마다 normal", expect=_lap_ok("normal"), env=MP, limit_sec=2000)
def lap_normal_mp(sim):
    return _lap(sim, "normal")


@scenario("[회귀] 저속 사전 주행 뒤 안내 열기 → 안내 이동은 시나리오 설정(normal)",
          expect=lambda r: r.get("guide_speed") == "normal" and r.get("stored") == "normal", limit_sec=2400)
def guide_after_slow_prerun(sim):
    st, ok = gp._stage(sim)
    if ok:
        _start(st, "slow")
        st.drive()
        st.pr.dismiss()
    res = st.open_guide()
    sid = res["session"]["id"]
    db = st.Session()
    try:
        s = db.query(st.M.ServiceSession).filter(st.M.ServiceSession.id == sid).first()
        s.CurrentStepCode = "ticket_move"
        db.commit()
        n = len(st.navs)
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
        stored = db.query(st.M.VoiceScenario).filter(st.M.VoiceScenario.id == 1).first().GuideSpeed
    finally:
        db.close()
    st.drive(until=lambda: not sim.sm.is_navigating)
    return {"guide_speed": st.navs[n][2] if len(st.navs) > n else "NAV 없음", "stored": stored}
