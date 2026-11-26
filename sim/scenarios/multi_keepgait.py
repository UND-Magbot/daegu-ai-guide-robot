"""다지점 내비 보행 유지 + 묶음 '못 감' 표시(2026-10-27).

실기(10-27):
- 안내 도착에서 내비 보행을 유지했는데 다음 이동이 노드 2점 이상 묶음이면 receiver 가 늘 모드 리셋
  → 작업지마다 기본 보행 전환·다리 재정렬(단일 NAV 는 continue 로 리셋 생략).
- 12:32 엘리베이터행 7점 묶음을 로봇이 받지 않아 재전송 한도 초과 → '못 감' 표시가 묶음 첫 점에만 붙어
  /robot/nav arrived=True → 음성 엔진이 수납 앞에서 도착 멘트·completed 로 종료.

변경:
- send_move._send_bundle/sender.send_nav_multi_to_robot: 내비 보행 유지 출발이면 NAV_MULTI 에 continue.
  유지 출발 감시(keep_gait_probe)도 묶음에 건다. receiver 는 continue + 내비 보행이면 리셋 생략(단독 확인).
- mark_current_wp_unreached: 묶음 중이면 향하던 점부터 묶음 끝까지 '못 감'.

가정(실측 아님): 하네스 다지점 이어가기 = 단일 이어가기 NAV 와 같음(내비 보행이면 리셋 없이 출발).
"""
from harness import scenario

ON = {"NAV_MULTIPOINT_ENABLED": "true", "NAV_KEEP_GAIT_ON_ARRIVAL": "true"}
LEG1 = [(3, 0)]                                   # 작업지 1 (안내 도착 → 내비 보행 유지)
LEG2 = [(3, 3, True), (6, 3)]                     # 노드 1 + 작업지 2 → 2점 묶음


def _guide(sim):
    """안내 이동으로 본다(재전송 한도 초과 시 '못 감' 처리 경로)."""
    sim.pl._guide_move_active = lambda: True


def _nav_cmds(sim, since):
    return [c for c in sim.robot.cmds[since:] if c.startswith("NAV")]


@scenario("K1 [재현] 안내 도착(내비 보행 유지) → 다음 이동이 2점 묶음 → 리셋 없이 출발(보행 전환 없음)",
          env=ON,
          expect=lambda r: (r["leg2_switches"] == 0 and r["leg2_multi"] == [2] and r["pos"] == [6, 3]
                            and r["errors"] == 0) or f"전환={r['leg2_switches']} 다지점={r['leg2_multi']} 위치={r['pos']}")
def k1(sim):
    sim.move(LEG1, keep_gait=True)
    sw, ms, n = sim.robot.switches, len(sim.robot.multi_sent), len(sim.robot.cmds)
    sim.move(LEG2, keep_gait=True)
    return {"leg2_switches": sim.robot.switches - sw, "leg2_multi": sim.robot.multi_sent[ms:],
            "leg2_cmds": _nav_cmds(sim, n)}


@scenario("K2 [회귀] 서 있다가(유지 아님) 2점 묶음 출발 → 지금처럼 리셋 후 출발", env=ON,
          expect=lambda r: (r["multi_sent"] == [2] and r["pos"] == [6, 3] and r["errors"] == 0)
          or f"다지점={r['multi_sent']} 위치={r['pos']}")
def k2(sim):
    sim.move(LEG2)


@scenario("K3 [회귀] 단일 장소 이동(점 1개) 유지 출발 → 지금처럼 이어가기 단일 NAV", env=ON,
          expect=lambda r: (r["leg2_switches"] == 0 and r["pos"] == [6, 3] and r["errors"] == 0)
          or f"전환={r['leg2_switches']} 위치={r['pos']}")
def k3(sim):
    sim.move(LEG1, keep_gait=True)
    sw = sim.robot.switches
    sim.move([(6, 3)], keep_gait=True)
    return {"leg2_switches": sim.robot.switches - sw}


def _ignore_multi(sim):
    """로봇이 다지점을 받고도 출발하지 않음(10-27 12:32 엘리베이터행 7점)."""
    robot = sim.robot

    def ignored(points, map_id=None, continuing=False):
        robot._log(f"NAV_MULTI({len(points)}점, 무시)")
        return True
    sim.sender.send_nav_multi_to_robot = ignored


def _nav_state(sim):
    from app.robot_control.status import get_nav
    st = get_nav()
    return {"arrived": st["arrived"], "unreached": st["unreached"], "moved": sim.robot.x != 0 or sim.robot.y != 0,
            "alerts": [k for _, k, _m in sim.events if k == "nav_hang_skip"]}


def _run_until_idle(sim, points):
    sm = sim.sm
    import math
    wps = []
    for i, p in enumerate(points):
        nxt = points[i + 1] if i + 1 < len(points) else None
        w = {"x": p[0], "y": p[1], "yaw": math.atan2(nxt[1] - p[1], nxt[0] - p[0]) if nxt else 0.0,
             "name": f"P{i}", "map_id": None}
        if len(p) > 2 and p[2]:
            w["pass_through"] = True
        wps.append(w)
    sm.waypoints_list, sm.current_wp_index, sm.is_navigating = wps, 0, True
    sm._signal_nav_reset(full=True)

    def go():
        sm.navigation_send_next()
        sim.pl.nav_thread()
    sim.run(go, until=lambda: not sm.is_navigating and sim.clock.elapsed() > 5)


@scenario("U1 [재현] 안내 중 7점 묶음을 로봇이 안 받음 → 재전송 한도 초과 → '도착' 아님(못 감) + 운영자 알림", env=ON,
          expect=lambda r: (r["arrived"] is False and r["unreached"] is True and not r["moved"]
                            and r["alerts"] == ["nav_hang_skip"])
          or f"arrived={r['arrived']} unreached={r['unreached']} 알림={r['alerts']}")
def u1(sim):
    _guide(sim)
    _ignore_multi(sim)
    _run_until_idle(sim, [(2, 0, True), (4, 0, True), (6, 0, True), (6, 2, True), (6, 4, True), (8, 4, True), (8, 6)])
    return _nav_state(sim)


@scenario("U2 안내 중 단일 이동을 로봇이 안 받음 → 못 감 + 운영자 알림", env=ON,
          expect=lambda r: (r["arrived"] is False and r["unreached"] is True and r["alerts"] == ["nav_hang_skip"])
          or f"arrived={r['arrived']} unreached={r['unreached']} 알림={r['alerts']}")
def u2(sim):
    _guide(sim)
    robot = sim.robot
    from harness.backend import replace_everywhere   # 하네스 가짜 단일 NAV 를 '무시' 로 바꾼다
    import app.robot_io.sender as s
    replace_everywhere(s.send_nav_to_robot, lambda *a, **k: robot._log("NAV(무시)"))
    _run_until_idle(sim, [(6, 3)])
    return _nav_state(sim)
