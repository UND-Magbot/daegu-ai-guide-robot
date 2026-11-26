"""자동 재측위 자동 확정(11-06) — 기준을 넘으면 운영자 '완료' 없이 확정.

재현: 11-06 17:02 대기장소 복귀 → 자동 재측위 (-3.87, 6.43) 맞음 89%·차 28·넣은 뒤 85% 인데 '완료' 대기.
기준(변경안): 세 장 최저 80%↑ · 다른 자리 점수 차 15↑ · 넣은 뒤 80%↑ · 확정 시 Location=0·신선한 좌표.
근거 자료: sim/out/auto_confirm/neg_eval.py(실제 스캔 30장 84.6~98.2%·차 17~37 / 다른 층 지도 최고 71.4%).

흐름·가짜는 auto_reloc_1106.py 의 Stage 를 그대로 쓴다(실제 스캔의 global_locate 결과·정답 자세 점수).
경계 사례는 실제 대기장소 스캔 결과를 복사해 점수·차·넣은 뒤 점수만 바꾼 가짜 항목(syn#*)으로 만든다.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("auto_reloc_1106_base", os.path.join(os.path.dirname(__file__), "auto_reloc_1106.py"))
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)
del REGISTRY[_n:]                      # 기존 A1~A23 은 그 파일에서 돌린다

RID = B.RID
DONE = B.DONE
_BASE_KEY = B.STANDBY[0][0]


def _syn(name, score=None, gap=None, at_truth=None):
    """대기장소 실제 결과 복사본에서 점수·차·넣은 뒤 점수만 바꾼다."""
    key = f"syn#{name}"
    r = dict(B.FITS[_BASE_KEY])
    r["fit"] = dict(r["fit"])
    if score is not None:
        r["fit"]["score"] = score
    if gap is not None:
        r["gap"] = gap
    if at_truth is not None:
        r["at_truth"] = at_truth
    B.FITS[key] = r
    return [key], B.STANDBY[1]


def _exp(pending, reason=None, injects=1, alerts=0, auto_log=None, recovered=None):
    def ok(r):
        good = r["pending"] == pending and len(r["injects"]) == injects and r["alerts"] == alerts
        if reason is not None:
            good &= r["reason"] == reason
        if auto_log is not None:
            good &= r["auto_confirm_logs"] == auto_log
        if recovered is not None:
            good &= r["recovered"] == recovered
        return good or (f"pending={r['pending']} reason={r['reason']!r} injects={r['injects']} alerts={r['alerts']} "
                        f"자동확정기록={r['auto_confirm_logs']} 복구기록={r['recovered']} err={r['err_m']}")
    return ok


def _go(sim, st, sec=90, confirm_at=None):
    r = st.go(sec=sec, confirm_at=confirm_at)
    r["auto_confirm_logs"] = sum(1 for _, k, m in sim.events if k == "robot_initpose_auto_reloc" and "확정" in str(m))
    r["recovered"] = sum(1 for _, k, _m in sim.events if k == "robot_initpose_recovered")
    return r


@scenario("C1 [재현] 대기장소(실제 스캔 85~86%·차 25·넣은 뒤 정답 점수) → 자동 확정",
          expect=_exp(pending=False, auto_log=1, recovered=1))
def c1(sim):
    return _go(sim, B.Stage(sim, *B.STANDBY))


@scenario("C2 측위 상실 보고(Location=1)·진료대기장소(94%) → 넣고 수렴 뒤 자동 확정",
          expect=_exp(pending=False, auto_log=1))
def c2(sim):
    return _go(sim, B.Stage(sim, *B.CLINIC, reason="측위 상실(로봇 보고)", report_loc=1))


@scenario("C3 원무과 접수(실제 차 17~20, 옆 2.2m 75~78%) → 차 15 이상이라 자동 확정",
          expect=_exp(pending=False, auto_log=1))
def c3(sim):
    return _go(sim, B.Stage(sim, *B.DESK))


@scenario("C4 경계: 점수 80.0 → 확정", expect=_exp(pending=False, auto_log=1))
def c4(sim):
    return _go(sim, B.Stage(sim, *_syn("s80", score=80.0)))


@scenario("C5 경계: 점수 79.9(채택 75 는 넘음) → 넣고 '완료' 대기(지금과 같음)",
          expect=_exp(pending=True, reason=DONE, auto_log=0))
def c5(sim):
    return _go(sim, B.Stage(sim, *_syn("s79", score=79.9)))


@scenario("C6 경계: 다른 자리와 차 14.9(채택 10 은 넘음) → '완료' 대기",
          expect=_exp(pending=True, reason=DONE, auto_log=0))
def c6(sim):
    return _go(sim, B.Stage(sim, *_syn("g14", gap=14.9)))


@scenario("C7 경계: 차 15.0 → 확정", expect=_exp(pending=False, auto_log=1))
def c7(sim):
    return _go(sim, B.Stage(sim, *_syn("g15", gap=15.0)))


@scenario("C8 넣은 뒤 그 자리 79%(확인 75 는 넘음) → '완료' 대기",
          expect=_exp(pending=True, reason=DONE, auto_log=0))
def c8(sim):
    return _go(sim, B.Stage(sim, *_syn("a79", at_truth=79.0)))


@scenario("C9 확정 직전 로봇이 다시 측위 상실(Location=1) → 확정 검사 실패로 '완료' 대기",
          expect=_exp(pending=True, reason=DONE, auto_log=0))
def c9(sim):
    st = B.Stage(sim, *B.STANDBY)
    orig = B.Stage._score_at

    def score_then_lost(f, key, x, y, yaw):
        out = orig(st, f, key, x, y, yaw)
        if st.sent:                       # 넣은 뒤 잰 점수 → 그 직후 상실
            st.location = 1
        return out
    from app.robot_control import scan_match as sm
    sm.score_pose = score_then_lost
    return _go(sim, st)


@scenario("C10 넣은 뒤 수렴 안 함 → 지금처럼 '자동 재측위 실패' 알림(확정 없음)",
          expect=_exp(pending=True, reason=B.FAIL, alerts=1, auto_log=0))
def c10(sim):
    st = B.Stage(sim, *B.STANDBY)
    st.no_converge = True
    return _go(sim, st, sec=150)


@scenario("C11 넣은 직후 운영자가 재조정 창을 엶 → 손 떼고 확정하지 않음",
          expect=_exp(pending=True, auto_log=0))
def c11(sim):
    from app.robot_control import relocalize
    st = B.Stage(sim, *B.STANDBY)
    st.on_inject = lambda: relocalize._add_viewer(1)
    return _go(sim, st)


@scenario("C12 AUTO_RELOCALIZE_AUTO_CONFIRM=0 → 지금처럼 '완료' 대기", env={"AUTO_RELOCALIZE_AUTO_CONFIRM": "0"},
          expect=_exp(pending=True, reason=DONE, auto_log=0))
def c12(sim):
    return _go(sim, B.Stage(sim, *B.STANDBY))


@scenario("C13 지도 밖(아무 점) → 넣지도 확정하지도 않음", expect=_exp(pending=True, injects=0, reason=B.LOST_REASON))
def c13(sim):
    return _go(sim, B.Stage(sim, ["junk#0", "junk#1", "junk#2"], B.STANDBY[1]), sec=40)


@scenario("C14 층 이동 미확정 → 자동 재측위 자체가 안 함(확정 없음)",
          expect=_exp(pending=True, injects=0, reason="층 이동 중(사전 맵 전환)"))
def c14(sim):
    return _go(sim, B.Stage(sim, *B.STANDBY, reason="층 이동 중(사전 맵 전환)"), sec=40)


@scenario("C15 자동 확정 뒤 운영자가 '완료'를 또 누름 → 문제없이 성공(복구 기록은 1번)",
          expect=_exp(pending=False, auto_log=1, recovered=1))
def c15(sim):
    return _go(sim, B.Stage(sim, *B.STANDBY), sec=90, confirm_at=70)
