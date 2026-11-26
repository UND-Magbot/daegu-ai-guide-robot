"""다지점 묶음 끝 도착을 통신 두절 중에 놓침(2026-10-27) — 재전송 한도 초과.

실기(10-27 12:44·15:00·16:01): 묶음 '마지막 점으로 가는 중' → 로봇이 끝점에 서는 순간 Wi-Fi 1s 두절 →
'통신 복구 — 도착 판정 상태 리셋' → 상태 0(정지)인데 이동한 적 없음으로 보고 같은 점 재전송 3회 → 한도 초과.
묶음 중에는 '이미 목표 근처' 검사를 건너뛰어(한 바퀴 묶음 보호) 서 있는 로봇을 도착으로 못 봤다.

변경: polling 이동 미감지 — 로봇이 묶음 마지막 점으로 가는 중이라고 보고한 뒤(send_move.multi_at_last)에는
'이미 목표 근처' 검사를 한다. 출발 직후(2번째 점으로 가는 중)는 그대로 건너뛴다.
"""
from harness import scenario

ON = {"NAV_MULTIPOINT_ENABLED": "true"}


def _drop_at_final_arrival(sim, final, sec=3.0):
    """가짜 로봇이 마지막 점에 서는 그 틱에 통신을 끊는다 — 관제는 3→0 전이를 못 본다."""
    robot = sim.robot
    n0 = len(robot.arrivals)
    done = []

    def tick(dt):
        if done:
            return
        if any((a[1], a[2]) == final for a in robot.arrivals[n0:]):
            robot.drop_comm(sec)
            done.append(sim.clock.elapsed())
    sim.clock.add_ticker(tick)
    return done


def _res(sim, dropped):
    log = sim.log()
    return {"dropped_at": dropped[0] if dropped else None,
            "resend": log.count("NAV 이동 미감지 — 재전송 ("), "limit": "재전송 한도 초과" in log,
            "near": "이미 목표 근처" in log,
            "held": any(k == "nav_unreached_hold" for _, k, _m in sim.events)}


def _ok(r):
    good = r["dropped_at"] is not None and r["resend"] == 0 and not r["limit"] and not r["held"] and r["errors"] == 0
    return good or (f"두절={r['dropped_at']} 재전송={r['resend']} 한도초과={r['limit']} 근처={r['near']} "
                    f"대기창={r['held']} 위치={r['pos']}")


@scenario("N1 [재현] 3점 묶음 끝에 서는 순간 통신 3s 두절 → 재전송 없이 도착으로 처리", env=ON, limit_sec=200,
          expect=_ok)
def n1(sim):
    dropped = _drop_at_final_arrival(sim, (6, 3))
    sim.move([(3, 0, True), (3, 3, True), (6, 3)])
    return _res(sim, dropped)


@scenario("N2 [재현] 4점 묶음(한도) 끝에서 두절, 뒤 묶음 남음 → 재전송 없이 다음 묶음으로 끝까지", env=ON, limit_sec=300,
          expect=lambda r: (_ok(r) is True and r["pos"] == [8, 6]) or _ok(r))
def n2(sim):
    dropped = _drop_at_final_arrival(sim, (6, 2))
    sim.move([(2, 0, True), (4, 0, True), (6, 0, True), (6, 2, True), (6, 4, True), (8, 4, True), (8, 6)])
    return _res(sim, dropped)


@scenario("N3 [회귀] 한 바퀴 묶음(끝점 = 출발점) → 출발 직후 '이미 근처'로 끝내지 않고 실제로 돈다", env=ON, limit_sec=200,
          expect=lambda r: (r["visited_far"] and r["pos"] == [0, 0] and not r["near_early"])
          or f"멀리감={r['visited_far']} 위치={r['pos']} 바로끝={r['near_early']}")
def n3(sim):
    sim.robot.place(0, 0)
    sim.move([(3, 0, True), (3, 3, True), (0, 0)])
    far = any((a[1], a[2]) == (3, 3) for a in sim.robot.arrivals) or "3/3" in sim.log()
    early = sim.log().split("다지점 진행 — 2/")[0].count("이미 목표 근처") > 0
    return {"visited_far": far, "near_early": early}
