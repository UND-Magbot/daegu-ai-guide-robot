"""진행 멘트와 '곧 도착'이 겹치면 '곧 도착'을 뺀다(2026-11-02 사용자 결정) — 현행 vs 변경.

  sim\\run.bat sim\\scenarios\\progress_soon_drop.py --cand sim\\out\\cand_soon_drop
실기(11-02 09:26 병원 #519 clinic_move, 편의점 입구 → 간호실데스크): 출발 17.1 → 진행 멘트 22.2 → '곧 도착' 25.25
(진행 멘트 3초 뒤) → 정지 31.4. Jetson 이 진행 멘트(합성 1.8초 + 3.55초)를 말하는 동안 밀려 '곧 도착'이 도착 0.4초 전에 들렸다.
현행 규칙: 마지막 구간에서 진행 멘트 + '곧 도착' 둘 다 들어갈 시간이 없으면 진행 멘트를 뺀다.
변경: 진행 멘트만 들어갈 시간이 있으면 낸다. 진행 멘트 뒤 6초 안에 '곧 도착' 차례가 오면 '곧 도착'을 뺀다.
무대는 progress_cue.py(병원, 원무과 접수 (4,0)). 경로는 직선 + 출발 2m 앞 경유점 하나(출발점 0.8m 앞 경유점은 하네스에서 빠져 꺾임 재현 안 됨).
가정(실기 닮음): accel=True 면 처음 1m 는 0.35 m/s(출발 구간 저속), 그 뒤 1.0 m/s. 아니면 하네스 기본 0.5 m/s 일정.
시간은 걷기 시작(0.5m) 기준, 도착은 로봇이 목적지에 닿은 순간.
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("progress_cue_base_soon",
                                               os.path.join(os.path.dirname(__file__), "progress_cue.py"))
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)
del REGISTRY[_n:]

SOON = pc.SOON
P = "원무과 접수 방향으로 이동하고 있습니다."      # 칸 비움 → 장소 이름 기본 문장(progress_cue_default)
SOON_AFTER = 6.0
SAY_SEC = 3.5                                      # 진행 멘트 길이(11-02 캐시 파일 3.55초)
DEST = (4.0, 0.0)


def _accel(sim, start, slow=0.35, fast=1.0, after_m=1.0):
    sim.robot.p.speed = slow

    def tick():
        if math.hypot(sim.robot.x - start[0], sim.robot.y - start[1]) >= after_m:
            sim.robot.p.speed = fast
            return
        sim.at(0.1, tick)
    sim.at(0.1, tick)


def _run(sim, length, progress="", accel=False):
    start = (DEST[0] - length, 0.0)
    # 경유점 하나(출발 2m 앞, 직선 위) — 노드 경로처럼 '곧 도착'이 생기고 꺾는 안내는 없다(30° 미만)
    st = pc._stage(sim, vias=[(start[0] + 2.0, 0.0)], progress=progress, robot_at=start)
    if accel:
        _accel(sim, start)
    res = st.open_guide()
    sid = res["session"]["id"]
    db = st.Session()
    try:
        s = db.query(st.M.ServiceSession).filter(st.M.ServiceSession.id == sid).first()
        s.CurrentStepCode = "ticket_move"
        db.commit()
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
    finally:
        db.close()
    out = pc._listen(sim, start)
    st.drive(until=lambda: not sim.sm.is_navigating)
    r = pc._result(out, sim)
    d = out["depart"] or 0.0
    hit = [t for t, x, y in sim.robot.arrivals if math.hypot(x - DEST[0], y - DEST[1]) < 0.05]
    r["arrive_t"] = round(hit[-1] - d, 1) if hit else None
    r["route_m"] = length
    return r


def _t(r, text):
    return next((t for h, t in zip(r.get("heard", []), r.get("t", [])) if h == text), None)


def _no_clash(r):
    """진행 멘트 뒤 6초 안에 '곧 도착'이 없고, 진행 멘트는 도착 SAY_SEC 초 전에 시작한다."""
    tp, ts = _t(r, P), _t(r, SOON)
    if tp is not None and ts is not None and ts - tp < SOON_AFTER:
        return False
    return tp is None or r["arrive_t"] is None or r["arrive_t"] - tp >= SAY_SEC


def _ok(r, want):
    return r["errors"] == 0 and r.get("heard") == want and _no_clash(r)


# ── 실기 걸음 닮음(저속 출발 → 보통 1.0 m/s) ─────────────────────────

@scenario("A1 [재현 11-02 #519] 가속 · 직선 7m — 현행: 진행 멘트 뒤 6초 안에 '곧 도착' / 변경: '곧 도착' 뺌·진행 멘트 남김",
          expect=lambda r: _ok(r, [P]))
def a1(sim):
    return _run(sim, 7.0, accel=True)


@scenario("A2 가속 · 직선 9m — 겹치면 '곧 도착' 뺌, 아니면 둘 다 6초 넘게 떨어짐",
          expect=lambda r: r["errors"] == 0 and r.get("heard", [])[:1] == [P] and _no_clash(r))
def a2(sim):
    return _run(sim, 9.0, accel=True)


@scenario("A3 가속 · 직선 5m — 진행 멘트가 도착 3.5초 전에 들어갈 때만 진행 멘트, 아니면 '곧 도착'만",
          expect=lambda r: r["errors"] == 0 and (r.get("heard") in ([P], [SOON])) and _no_clash(r))
def a3(sim):
    return _run(sim, 5.0, accel=True)


@scenario("A4 [회귀] 가속 · 직선 20m — 진행 멘트·'곧 도착' 둘 다, 6초 넘게 떨어짐(현행과 같아야 함)",
          expect=lambda r: _ok(r, [P, SOON]))
def a4(sim):
    return _run(sim, 20.0, accel=True)


@scenario("A5 [회귀] 가속 · 칸 '-' · 직선 7m — 진행 멘트 없음, '곧 도착' 그대로(현행과 같아야 함)",
          expect=lambda r: r["errors"] == 0 and r.get("heard") == [SOON])
def a5(sim):
    return _run(sim, 7.0, progress="-", accel=True)


# ── 하네스 기본 걸음(0.5 m/s 일정) — 회귀 ─────────────────────────

@scenario("B1 [회귀] 일정 0.5 m/s · 직선 7m — 겹치지 않으면 현행과 같음",
          expect=lambda r: r["errors"] == 0 and _no_clash(r))
def b1(sim):
    return _run(sim, 7.0)


@scenario("B2 [회귀] 일정 0.5 m/s · 직선 16m — 진행 멘트·'곧 도착' 둘 다(현행과 같아야 함)",
          expect=lambda r: _ok(r, [P, SOON]))
def b2(sim):
    return _run(sim, 16.0)


@scenario("B3 [회귀] 일정 0.5 m/s · 직선 3.5m(< 4m) — 진행 멘트 없음(짧아 '곧 도착'도 없음, 현행과 같아야 함)",
          expect=lambda r: r["errors"] == 0 and r.get("heard") == [])
def b3(sim):
    return _run(sim, 3.5)
