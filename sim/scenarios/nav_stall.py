"""주행 멈춤 판단(hang) 변경 P1(10-22) — 현행 vs 변경.

현행: 보낸 뒤 60초가 지나면 hang → 리셋 재전송 3회 → 도착으로 처리(건너뜀).
변경: 60초 동안 0.2m 도 못 움직였을 때만 hang. 안내 이동이면 재전송·건너뛰기 없이 막힘 알림만,
      순회 등은 재전송 3회 뒤 건너뛰고 '도착 못 함' 알림. 단발 응대는 '못 감' 주행에 도착 멘트를 하지 않는다.
보고서: docs/계획보고/2026-10-22/계획보고_2026-10-22_주행멈춤판단·막힘공유_개선.md
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "standby_single_base_stall", os.path.join(os.path.dirname(__file__), "standby_single_no_limit.py"))
_single = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_single)
del REGISTRY[_n:]
SingleStage, Stage, CLINIC, _no_err = _single.SingleStage, _single.Stage, _single.CLINIC, _single._no_err

ARRIVE_SPEECH = "내과에 도착했습니다"


def _count(sim, kind):
    return sum(1 for _t, k, _m in sim.events if k == kind)


def _nav_counts(sim):
    return {k: _count(sim, k) for k in ("nav_hang_retry", "nav_hang_skip", "guide_nav_blocked", "guide_nav_unblocked",
                                        "nav_unreached_hold", "nav_hold_resumed")}


def _arrived_speech(st):
    return any(ARRIVE_SPEECH in t for t in st.speech)


# ── 순회(일반) 이동 ──────────────────────────────────

@scenario("N1 순회 긴 이동(30m, 막힘 없음 · 약 150초) → 재전송 없이 도착 (현행: 60초마다 재전송)",
          params={"speed": 0.2},
          expect=lambda r: _no_err(r) and r["pos"] == [30, 0] and r["n"]["nav_hang_retry"] == 0)
def n1(sim):
    sim.move([(30, 0)])
    return {"n": _nav_counts(sim)}


@scenario("N2 순회 이동 중 막힘(7분) → 재전송 3회 뒤 건너뛰지 않고 멈춤(P2) → 막힘이 풀리면 이어서 도착 — P1 단독일 땐 건너뜀",
          expect=lambda r: _no_err(r) and r["n"]["nav_hang_retry"] == 3 and r["n"]["nav_hang_skip"] == 0
          and r["n"]["nav_unreached_hold"] == 1 and r["n"]["nav_hold_resumed"] == 1 and not r["unreached"])
def n2(sim):
    sim.at(4.0, lambda: sim.robot.hold(420))
    sim.move([(20, 0)])
    try:
        from app.navigation import nav_hold
        held = nav_hold.get_hold() is not None
    except ImportError:
        held = False
    return {"n": _nav_counts(sim), "unreached": sim.sm.last_wp_unreached(), "held": held}


# ── 안내 이동 ────────────────────────────────────────

@scenario("N3 단발 응대 막힘 2분 뒤 풀림 → 재전송 없이 막힘 알림·풀림 기록 → 내과에서 도착 멘트",
          expect=lambda r: _no_err(r) and r["n"]["nav_hang_retry"] == 0 and r["n"]["guide_nav_blocked"] == 1
          and r["n"]["guide_nav_unblocked"] == 1 and r["arrived_at"] == list(CLINIC) and r["sess"]["Result"] == "completed")
def n3(sim):
    st = SingleStage(sim, limit_min=20)
    st.arrive_standby()
    sid = st.call().get("sessionId")
    st.drive(until=lambda: sim.robot.y >= 2.0, max_sec=60)
    sim.robot.hold(120)
    arrived_at = {}
    st.run_until(lambda: _arrived_speech(st) and arrived_at.setdefault("p", st.pos()) is not None, max_sec=400)
    st.run_until(lambda: st.sess_row(sid)["ended"], max_sec=600)
    return st.result(sid, n=_nav_counts(sim), arrived_at=arrived_at.get("p"))


@scenario("N4 단발 응대 막힘이 안 풀림(15분) → 도착 멘트 없음, 응대 유지, 운영자 종료 (현행: 4분쯤 '도착했습니다')",
          expect=lambda r: _no_err(r) and not r["said_arrived"] and r["mid_result"] == "running"
          and r["n"]["guide_nav_blocked"] == 1 and r["n"]["nav_hang_skip"] == 0
          and r["sess"]["Result"] == "aborted_operator")
def n4(sim):
    st = SingleStage(sim, limit_min=20)
    st.arrive_standby()
    sid = st.call().get("sessionId")
    st.drive(until=lambda: sim.robot.y >= 2.0, max_sec=60)
    sim.robot.hold(900)
    st.run_until(lambda: st.sess_row(sid)["ended"], max_sec=480)
    mid = st.sess_row(sid)["Result"]
    said = _arrived_speech(st)
    st.ss.abort(st.ss.RESULT_ABORTED_OPERATOR, "운영자 종료")
    return st.result(sid, n=_nav_counts(sim), mid_result=mid, said_arrived=said)


@scenario("N5 안내 시나리오 이동 막힘(15분) → 6분 시점에도 주행 유지·'못 감' 표시 없음·막힘 알림 1건 (현행: 건너뛰고 못 감)",
          expect=lambda r: _no_err(r) and r["navigating"] and not r["unreached"]
          and r["n"]["nav_hang_retry"] == 0 and r["n"]["guide_nav_blocked"] == 1)
def n5(sim):
    st = Stage(sim)
    st.arrive_standby()
    sid = st.operator_start()
    db = st.Session()
    try:
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
    finally:
        db.close()
    st.drive(until=lambda: sim.robot.y >= 2.0, max_sec=60)
    sim.robot.hold(900)
    st.drive(until=lambda: False, max_sec=360)
    return {"n": _nav_counts(sim), "navigating": bool(st.sm.is_navigating), "unreached": st.sm.last_wp_unreached()}


@scenario("N6 단발 응대 주행이 '못 감'으로 끝남(출발 못 함 등) → 도착 멘트 대신 안내 종료 (현행: '도착했습니다')",
          expect=lambda r: _no_err(r) and not r["said_arrived"] and r["sess"]["Result"] == "nav_failed")
def n6(sim):
    st = SingleStage(sim, limit_min=20)
    st.arrive_standby()
    sid = st.call().get("sessionId")
    sim.wait(2.0)
    st.sm.mark_current_wp_unreached("이동 미감지 — 재전송 한도 초과")
    st.lp._on_standby_arrival()
    said = _arrived_speech(st)
    st.sm.stop_navigation_internal("시험 정지", keep_schedule=True)
    sim.wait(1.0)
    return st.result(sid, said_arrived=said)
