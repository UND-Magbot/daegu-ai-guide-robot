"""장애물 정지 중 감지 신호 끊김·판단 전 상태(unknown) — 현행 vs 변경 (2026-11-02 세션 521).

실기(11-02 09:54~09:56, 669 로봇, 맵 49):
    09:54:58.5 감지 노드 정지 → 09:55:05.5 풀림 → 09:55:06.7 '준비됐어요' 질문 → 09:55:13.4 감지 노드 '관제 신호 끊김'으로 꺼짐
    → 09:55:17.5 손님 yes 받음(처리 안 됨) → 09:55:31.7 감지 다시 켬(unknown) → 관제 '묻는 중 다시 막힘' + '아직 앞이 막혀'
    → 09:55:34 다시 질문 → 49.8 yes → 출발. (관제 '통신 복구' 기록 0건 = 응답 시간 초과가 아니라 stale 로 건너뜀)
가정(추정, 본체 로그 미확인)
    장애물 이동 취소 뒤 receiver 의 NAV_STATUS 가 갱신되지 않는다 — 취소 after 초 뒤부터 age 가 5초를 넘고,
    revive 초 뒤(실기 약 33초) 또는 다음 NAV 까지 그대로다. 그래서 하네스 robot.py 에는 넣지 않고 여기서만 쓴다.
운영 설정(control-backend env, 11-02): GUARD_MODE=on · ANNOUNCE 0 · ASK_AFTER 7 · SETTLE 1.5 · STAFF 120.
"""
import os
import importlib.util

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "obstacle_hold_base_stale", os.path.join(os.path.dirname(__file__), "obstacle_hold.py"))
_ob = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ob)
del REGISTRY[_n:]

STOP, RESUME, WAIT, ASK = _ob.STOP, _ob.RESUME, _ob.WAIT, _ob.ASK
OPS = {"OBSTACLE_GUARD_MODE": "on", "OBSTACLE_ANNOUNCE_SEC": "0", "OBSTACLE_ASK_AFTER_SEC": "7",
       "OBSTACLE_SETTLE_SEC": "1.5", "OBSTACLE_STAFF_SEC": "120"}


class StaleAfterCancel:
    """CANCEL_NAV 뒤 after 초부터 NAV_STATUS age 가 5초를 넘는다(revive 초 뒤 또는 다음 NAV 에서 회복)."""

    def __init__(self, sim, after=7.0, revive=None, on_nav=False):
        """on_nav: 취소가 아니라 NAV 를 기준으로 센다(평소 이동 중 stale — 회귀 확인용)."""
        r, self.sim, self.after, self.revive = sim.robot, sim, after, revive
        self.cancel_t, self.stale_polls = None, 0
        log, status = r._log, r.nav_status

        def _log(c):
            if c == ("NAV" if on_nav else "CANCEL_NAV") or (on_nav and c.startswith("NAV")):
                self.cancel_t = sim.clock.now()
            elif c.startswith("NAV"):
                self.cancel_t = None
            return log(c)

        def nav_status():
            d = status()
            if self.cancel_t is None:
                return d
            el = sim.clock.now() - self.cancel_t
            if el >= self.after and (self.revive is None or el < self.revive):
                self.stale_polls += 1
                return {**d, "age": round(el - (self.after - 5.0) + 0.1, 2)}
            return d
        r._log, r.nav_status = _log, nav_status


def _run(sim, block_sec, answers, delay, stale=None, events=(), max_sec=200):
    st = _ob.Stage(sim)
    st.arrive_standby()
    blk = _ob.Block(sim, lambda: sim.robot.y >= 2.0, block_sec)
    guard = _ob.FakeGuard(sim, blk)
    _ob.FakeVoice(sim, answers, delay=delay)
    stl = StaleAfterCancel(sim, **stale) if stale else None
    sid = st.operator_start()
    db = st.Session()
    try:
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
    finally:
        db.close()
    pending = sorted(events, key=lambda e: e[0])

    def check():
        while pending and blk.t0 is not None and sim.clock.now() - blk.t0 >= pending[0][0]:
            pending.pop(0)[1](sim)
        if pending:
            sim.clock.after(0.1, check)
    check()
    st.drive(until=lambda: not st.sm.is_navigating, max_sec=max_sec)
    out = _ob._out(st, sid, guard, blk)
    t0 = out["block_t0"] or 0.0
    out["stale_polls"] = stl.stale_polls if stl else 0
    out["notice_t"] = [(round(t - t0, 1), x[:12]) for t, x in guard.notice_t]
    return out


def _ok(r):
    return _ob._no_err(r) and r["arrived_clinic"] and r["staff_alert"] == 0


# ── 재현: 11-02 세션 521 ───────────────────────────────

@scenario("T1 [재현 11-02 #521] 7초 막힘 → 질문 → 정지 중 상태 stale(취소 12초 뒤~33초, 실측 맞춤) → 11초 뒤 '준비됐어요'"
          " — 변경: 대답 받은 즉시 '다시 출발'·도착, '아직 앞이 막혀' 없음, 질문 1번",
          env=OPS,
          expect=lambda r: _ok(r) and r["notices"] == [STOP, ASK, RESUME] and r["resends"] == 1)
def t1(sim):
    return _run(sim, 7.0, ["yes", "yes"], delay=11.0, stale={"after": 12.0, "revive": 33.0})


@scenario("T2 [재현 악화] T1 과 같은데 stale 이 다음 NAV 까지 계속(회복 없음) — 변경: 대답에 출발·도착 / 현행: 출발 못 함",
          env=OPS,
          expect=lambda r: _ok(r) and r["notices"] == [STOP, ASK, RESUME] and r["resends"] == 1)
def t2(sim):
    return _run(sim, 7.0, ["yes", "yes"], delay=11.0, stale={"after": 12.0}, max_sec=150)


@scenario("T3 질문 뒤 통신 4초 두절(감지 노드 3초 뒤 꺼짐) 중 '준비됐어요' 도착 → 다시 켜진 직후 unknown"
          " — 변경: 대답 들고 있다가 비면 출발(질문 1번), 막힘 판정 아님",
          env=OPS,
          expect=lambda r: _ok(r) and r["notices"] == [STOP, ASK, RESUME] and r["resends"] == 1)
def t3(sim):
    # 막힘 7초 → 9초 clear → 10.5초 질문. 13초부터 4초 두절, 대답은 질문 4초 뒤(두절 중)
    return _run(sim, 7.0, ["yes", "yes"], delay=4.0, events=[(13.0, lambda s: s.robot.drop_comm(4.0))])


@scenario("T4 '다시 출발하겠습니다' 직후 통신 3.5초 두절 → 재전송 전 unknown — 변경: 출발 거두지 않고 비면 바로 재전송(질문 1번)",
          env=OPS,
          expect=lambda r: _ok(r) and r["notices"] == [STOP, ASK, RESUME] and r["resends"] == 1)
def t4(sim):
    # 질문 10.5초 + 대답 3초 = 13.5초 '다시 출발' → 13.6초부터 3.5초 두절
    return _run(sim, 7.0, ["yes", "yes"], delay=3.0, events=[(13.6, lambda s: s.robot.drop_comm(3.5))])


# ── 회귀: 바뀌면 안 되는 것 ─────────────────────────────

@scenario("T5 [회귀] 질문 중 진짜 다시 막힘(4초) → 대답 버리고 말없이 정지 → 다시 풀리면 다시 질문 → '네' → 출발 (현행과 같아야 함)",
          env=OPS,
          expect=lambda r: _ok(r) and r["notices"].count(ASK) == 2 and r["notices"][-1] == RESUME
          and r["notices"].count(STOP) == 1 and r["resends"] == 1)
def t5(sim):
    st = _ob.Stage(sim)
    st.arrive_standby()
    fl = _ob.Flicker2(sim, lambda: sim.robot.y >= 2.0, 7.0, 4.0)
    guard = _ob.FakeGuard(sim, fl)
    fl.guard = guard
    _ob.FakeVoice(sim, ["yes", "yes"], delay=8.0)
    sid = st.operator_start()
    db = st.Session()
    try:
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
    finally:
        db.close()
    st.drive(until=lambda: not st.sm.is_navigating, max_sec=200)
    blk = _ob.Block(sim, lambda: False, None)
    blk.t0 = fl.t0
    return _ob._out(st, sid, guard, blk)


@scenario("T6 [회귀] stale 없음 · 7초 막힘 → 질문 → 3초 뒤 '네' → 출발·도착 (현행과 같아야 함)",
          env=OPS,
          expect=lambda r: _ok(r) and r["notices"] == [STOP, ASK, RESUME] and r["resends"] == 1)
def t6(sim):
    return _run(sim, 7.0, ["yes"], delay=3.0)


@scenario("T7 [회귀] stale 있음 · 막힘 없는 평소 안내 이동 — 정지 0·멘트 0·도착 (장애물 주기를 앞으로 옮긴 부작용 확인)",
          env=OPS,
          expect=lambda r: _ob._no_err(r) and r["arrived_clinic"] and r["notices"] == [] and r["cancels"] == 0)
def t7(sim):
    # 출발 2초 뒤부터 6초 동안 stale(age>5) — 장애물 주기는 돌지만 막힘이 없으니 평소 판정(stale 건너뜀)과 같아야 한다
    return _run(sim, 0.0, [], delay=3.0, stale={"after": 2.0, "revive": 8.0, "on_nav": True})
