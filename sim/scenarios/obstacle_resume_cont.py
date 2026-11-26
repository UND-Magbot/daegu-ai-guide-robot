"""장애물 재출발을 리셋 없이(이어가기) — 로봇이 받지 않을 때 떨어지는 길(2026-10-30).

  sim\\run.bat sim\\scenarios\\obstacle_resume_cont.py --cand sim\\out\\cand_resumecont
변경: 장애물이 풀린 재출발만 navigation_resend_current(continuing=True) — 리셋하면 꺾는 점 앞에서 보행이 풀렸다가
다시 잡혔다(10-30 15:10 1F N243). 이동 취소(CANCEL_NAV) 뒤 로봇이 리셋 없는 NAV 를 받는지는 실기 미확인이라
(10-29 장애물 사전 시뮬 4절), 안 떠나면 출발 확인 감시가 4초(RESUME_START_TIMEOUT_SEC, 10-30 실측 0.8~3.1초 위) 뒤 리셋 재전송한다.
2차(10-30): 실패해도 그 세션의 '도착 뒤 내비 보행 유지'는 끄지 않고, 이어가기만 끈다(다음 재출발은 바로 리셋).
가정(실측 없음): R 시나리오의 가짜 로봇은 CANCEL_NAV 뒤 첫 이어가기 NAV 를 말없이 버린다 — 가장 나쁜 경우.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("obstacle_hold_base_rc", os.path.join(os.path.dirname(__file__), "obstacle_hold.py"))
oh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oh)
del REGISTRY[_n:]


def _ignore_cont_after_cancel(sim):
    """CANCEL_NAV 뒤 첫 이어가기 NAV 를 로봇이 버린다(가정)."""
    robot = sim.robot
    state = {"cancelled": False, "ignored": 0}
    cmd, nav = robot.cmd, robot.nav

    def cmd2(c):
        if c == "CANCEL_NAV":
            state["cancelled"] = True
        return cmd(c)

    def nav2(x, y, yaw, continuing=False):
        if continuing and state["cancelled"]:
            state["cancelled"] = False
            state["ignored"] += 1
            robot._log("NAV(cont) — 취소 뒤라 버림(가정)")
            return None
        state["cancelled"] = False
        return nav(x, y, yaw, continuing)
    robot.cmd, robot.nav = cmd2, nav2
    return state


def _arrive_t(sim):
    for t, kind, _ in sim.events:
        if kind == "nav_complete":
            return round(t, 1)
    return None


@scenario("R1 [기준] 3초 막힘 뒤 재출발 — 로봇이 이어가기를 받음(시뮬 기본)",
          env=oh.ON, expect=lambda r: r["arrived_clinic"] or str(r))
def r1(sim):
    out = oh._out(*oh._guide(sim, 3.0))
    out["arrive_t"] = _arrive_t(sim)
    return out


@scenario("R2 [위험] 3초 막힘 뒤 재출발 — 로봇이 취소 뒤 이어가기를 버림 → 4초 뒤 리셋 재전송 → 도착, 보행 유지는 안 꺼짐",
          env=oh.ON, expect=lambda r: (r["arrived_clinic"] and r["rejects_noticed"] and not r.get("keep_gait_off")) or str(r))
def r2(sim):
    st = _ignore_cont_after_cancel(sim)
    out = oh._out(*oh._guide(sim, 3.0))
    out["arrive_t"] = _arrive_t(sim)
    out["ignored"] = st["ignored"]
    log = sim.log()
    out["rejects_noticed"] = (st["ignored"] == 0) or ("출발 미확인" in log)
    out["keep_gait_off"] = bool(getattr(sim.sm, "_keep_gait_off", False))
    out["resume_cont_off"] = getattr(sim.sm, "_resume_cont_off", None)
    out["fallback_line"] = next((l for l in log.splitlines() if "출발 미확인" in l), None)
    return out


@scenario("R3 [위험] 다지점 묶음 중간 5초 막힘 — 이어가기를 버림 → 리셋 재전송으로 끝점 도착",
          env=oh.ON_MP, expect=lambda r: r["pos"] == [9, 0] or str(r))
def r3(sim):
    st = _ignore_cont_after_cancel(sim)
    sim.pl._guide_move_active = lambda: True
    sim.pl._scenario_move_active = lambda: True
    guard = oh.FakeGuard(sim, oh.Block(sim, lambda: sim.robot.x >= 4.0, 5.0))
    sim.move([(3, 0, True), (6, 0, True), (9, 0)])
    return {"cancels": guard.cancels, "ignored": st["ignored"], "arrive_t": _arrive_t(sim)}
