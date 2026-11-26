"""다지점 ACK 늦음(2026-10-27) — 로봇 준비가 늦어 ack:False 가 와도 다지점을 끄지 않는다.

실기(10-27 11:55): 앉은 로봇에 2점 묶음 → receiver 가 모드 리셋 뒤 보행 전환을 4.5s 만 기다리고 {'ack': False,
'idx': 1, 'n': 2} → 관제가 '다지점 모름'으로 보고 같은 구간 단일 NAV 재전송(receiver 는 묶음 작업 버림) →
관제 재시작 전까지 모든 이동 단일(경로테스트 지점마다 기본 보행 전환).

변경: sender._multi_accepted — n 이 있고 형식 오류가 없으면 받은 것으로 본다(단일 NAV 의 늦은 ACK 와 같음).
형식 오류(error)·무응답은 지금처럼 단일 폴백 + 이후 다지점 끔.

하네스는 send_nav_multi_to_robot 을 통째로 가짜로 바꾸므로, 여기서는 진짜 sender 모듈 사본을 불러
ACK 판정 코드를 그대로 돌리고 네트워크(_tcp_send_nav/_udp_send)만 가짜로 둔다.
가짜 receiver: ok/late 면 로봇이 묶음을 실행(late 는 ACK 만 False), error/none 이면 실행하지 않는다.
"""
import importlib.util
import itertools

from harness import scenario

ON = {"NAV_MULTIPOINT_ENABLED": "true"}
ROUTE = [(3, 0, True), (3, 3), (6, 3, True), (6, 6)]     # 통과·작업지·통과·작업지 → 2점 묶음 2건
_seq = itertools.count()


def _real_sender(sim, replies):
    """진짜 sender 사본 + 가짜 네트워크. replies: 다지점 전송마다 쓸 응답 종류 목록(끝나면 마지막 값 반복)."""
    spec = importlib.util.spec_from_file_location(f"sim_real_sender_{next(_seq)}", sim.sender.__file__)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    robot = sim.robot
    sent = []

    def fake_tcp(msg, *a, **k):
        if msg.get("action") != "NAV_MULTI":
            return None
        kind = replies[min(len(sent), len(replies) - 1)]
        sent.append(kind)
        pts = msg["points"]
        if kind in ("ok", "late"):
            robot.nav_multi([(p["x"], p["y"], p.get("yaw", 0.0)) for p in pts])
        if kind == "ok":
            return {"ack": True, "idx": 1, "n": len(pts)}
        if kind == "late":
            return {"ack": False, "idx": 1, "n": len(pts)}
        if kind == "error":
            return {"ack": False, "idx": 1, "error": "points 형식 오류"}
        return None                                      # none: 옛 receiver — 응답 없음

    real._tcp_send_nav = fake_tcp
    real._udp_send = lambda *a, **k: None
    sim.sender.send_nav_multi_to_robot = real.send_nav_multi_to_robot
    return sent


def _result(sim, sent):
    singles = [c for c in sim.robot.cmds if c == "NAV"]
    return {"multi_sent": list(sim.robot.multi_sent), "singles": len(singles), "replies": sent,
            "unsupported": bool(sim.sm._multi_unsupported)}


def _expect(multi, singles, unsupported):
    def ok(r):
        good = (r["multi_sent"] == multi and r["singles"] == singles and r["unsupported"] == unsupported
                and r["pos"] == [6, 6] and r["errors"] == 0)
        return good or (f"다지점={r['multi_sent']} 단일={r['singles']} 끔={r['unsupported']} "
                        f"위치={r['pos']} 응답={r['replies']}")
    return ok


@scenario("A1 [재현] 첫 묶음 ACK 늦음(ack False, n=2) → 다지점 유지, 두 묶음 모두 다지점, 단일 재전송 없음",
          env=ON, expect=_expect([2, 2], 0, False))
def a1(sim):
    sent = _real_sender(sim, ["late", "ok"])
    sim.move(ROUTE)
    return _result(sim, sent)


@scenario("A2 두 묶음 모두 ACK 늦음 → 둘 다 다지점으로 진행", env=ON, expect=_expect([2, 2], 0, False))
def a2(sim):
    sent = _real_sender(sim, ["late"])
    sim.move(ROUTE)
    return _result(sim, sent)


@scenario("A3 [회귀] 정상 ACK → 다지점 2건", env=ON, expect=_expect([2, 2], 0, False))
def a3(sim):
    sent = _real_sender(sim, ["ok"])
    sim.move(ROUTE)
    return _result(sim, sent)


@scenario("A4 형식 오류 응답(error) → 구간마다 단일 폴백, 다지점은 끄지 않음(10-27 늘 켜짐)", env=ON,
          expect=_expect([], 4, False))
def a4(sim):
    sent = _real_sender(sim, ["error"])
    sim.move(ROUTE)
    return _result(sim, sent)


@scenario("A5 응답 없음(옛 receiver·통신 순단) → 구간마다 단일 폴백, 다지점은 끄지 않음", env=ON,
          expect=_expect([], 4, False))
def a5(sim):
    sent = _real_sender(sim, ["none"])
    sim.move(ROUTE)
    return _result(sim, sent)


@scenario("A6 [재현] 10-27 15:17 첫 묶음 무응답(순단) → 그 구간만 단일, 다음 묶음은 다시 다지점", env=ON,
          expect=_expect([2], 2, False))
def a6(sim):
    sent = _real_sender(sim, ["none", "ok"])
    sim.move(ROUTE)
    return _result(sim, sent)
