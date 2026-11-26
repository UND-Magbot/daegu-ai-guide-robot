"""언도킹 재정렬 도착 SIT 보완(2026-10-23).

변경:
- charge._undock_internal: 도킹 포인트 재정렬 목표에 undock_realign 표시를 단다.
- send_move 도착 처리: 언도킹 재정렬로 도착했을 때만
  ① 도킹 포인트에서 0.5 m 넘게 떨어져 끝났으면 SIT 하지 않는다.
  ② 안내 세션(단계 엔진)이 진행 중이면 SIT 하지 않는다(곧 다음 이동이 나간다).
  모터 과열 보호의 도킹 포인트 SIT 는 그대로 둔다.

언도킹은 _undock_internal 을 그대로 돌린다. 충전 상태 패킷·자세 상태·도킹 포인트 조회만 가짜로 둔다.
"""
import datetime

from harness import scenario

DOCK = {"x": 2.0, "y": 1.0, "yaw": 0.0, "name": "충전소-1", "map_id": None, "floor_id": None}
START = (0.6, 1.0)   # 도크 이탈 직후 위치


def _stage(sim, short_m: float = 0.0, stand_after: float = 0.0):
    """언도킹 무대. short_m: 로봇이 NAV 목표보다 그만큼 앞(x-)에서 멈춘다(가로막힘·도착 판정 어긋남)."""
    ch, rt, robot = sim.ch, sim.rt, sim.robot
    robot.place(*START)
    robot.charging = True
    state = {"v": 2}

    def stop_charge():
        state["v"] = 0
        robot.charging = False
        return True
    ch._send_stop_charge_packet = stop_charge
    ch._send_start_charge_packet = lambda: True
    ch._send_charge_clear_packet = lambda: True
    rt.get_charge_state = lambda rid: state["v"]
    rt.get_motion_state = lambda rid: 1 if sim.clock.elapsed() >= stand_after else 0
    rt.clear_charge_state = lambda rid: None
    ch._get_dock_point_waypoint = lambda: dict(DOCK)
    if short_m:
        nav = robot.nav
        robot.nav = lambda x, y, yaw, continuing=False: nav(x - short_m, y, yaw, continuing)


def _guide_session(sim):
    """진행 중인 안내 세션 한 건(이동 전 단계에서 멘트·대답을 기다리는 중)."""
    from app.database.database import SessionLocal
    from app.database.models import ServiceSession
    db = SessionLocal()
    try:
        db.add(ServiceSession(RobotName="SIM", StartedAt=datetime.datetime.now(), Result="running",
                              ScenarioId=1, CurrentStepCode="greet"))
        db.commit()
    finally:
        db.close()
    import app.database.routes.voice_scenario  # noqa: F401  도착 처리가 부르는 모듈을 가상 시계에 건다
    sim.rewire_late_modules()


def _undock(sim):
    def go():
        sim.ch._undock_internal()
        sim.pl.nav_thread()
    sim.run(go, until=lambda: not sim.sm.is_navigating and sim.clock.elapsed() > 2, grace=3)


def _out(sim, **extra):
    s = sim.summary()
    return {"sat": "SIT" in s["cmds"], "skipped": any("undock_sit_skipped" in e for e in s["events"]), **extra}


@scenario("U1 [회귀] 언도킹 → 도킹 포인트 정상 도착, 안내 없음 → SIT",
          expect=lambda r: r["sat"] and r["posture"] == "SIT" and r["pos"] == [2.0, 1.0] and r["errors"] == 0)
def u1(sim):
    _stage(sim)
    _undock(sim)
    return _out(sim)


@scenario("U2 [변경] 언도킹 재정렬이 도킹 포인트 0.8 m 앞에서 끝남 → 앉지 않고 기본 보행으로 서서 대기",
          expect=lambda r: not r["sat"] and r["posture"] == "STAND" and r["skipped"] and r["gait"] == "0x1001"
          and r["errors"] == 0)
def u2(sim):
    _stage(sim, short_m=0.8)
    _undock(sim)
    return _out(sim)


@scenario("U3 [경계] 0.45 m 앞에서 끝남(기준 0.5 m 안) → SIT",
          expect=lambda r: r["sat"] and r["posture"] == "SIT" and not r["skipped"] and r["errors"] == 0)
def u3(sim):
    _stage(sim, short_m=0.45)
    _undock(sim)
    return _out(sim)


@scenario("U4 [변경] 안내 세션 진행 중(이동 전 단계)에 언도킹 → 서서 대기, 내비 보행 유지",
          expect=lambda r: not r["sat"] and r["posture"] == "STAND" and r["skipped"] and r["gait"] == "0x3002"
          and r["errors"] == 0)
def u4(sim):
    _stage(sim)
    _guide_session(sim)
    _undock(sim)
    return _out(sim)


@scenario("U8 [변경] 안내 세션 진행 중 + 0.8 m 앞에서 끝남 → 앉지 않고 내비 보행 유지(기본 보행 복귀 안 함)",
          expect=lambda r: not r["sat"] and r["posture"] == "STAND" and r["skipped"] and r["gait"] == "0x3002"
          and "GAIT_BASIC" not in r["cmds"] and r["errors"] == 0)
def u8(sim):
    _stage(sim, short_m=0.8)
    _guide_session(sim)
    _undock(sim)
    return _out(sim)


@scenario("U5 [회귀] 언도킹 도중 안내 이동이 들어옴 → 재정렬·SIT 생략, 안내 목적지에서도 SIT 없음",
          expect=lambda r: not r["sat"] and r["pos"] == [5.0, 3.0] and r["errors"] == 0)
def u5(sim):
    _stage(sim, stand_after=4.0)   # 이탈 뒤 서기까지 4 s — 그 사이 안내 이동
    sm = sim.sm

    def guide_move():
        sm.clear_arrival_actions()
        sm.waypoints_list = [{"x": 5.0, "y": 3.0, "yaw": 0.0, "name": "원무과", "map_id": None}]
        sm.current_wp_index = 0
        sm.is_navigating = True
        sm._signal_nav_reset(full=True)
        sm.navigation_send_next()
    sim.at(2.5, guide_move)   # STAND 확인 전에 들어온 안내 이동
    def go():
        sim.ch._undock_internal()
        sim.pl.nav_thread()
    sim.run(go, until=lambda: not sm.is_navigating and sim.clock.elapsed() > 3, grace=3)
    return _out(sim)


def _overheat(sim, short_m: float = 0.0, session: bool = False):
    sim.robot.place(*START)
    sim.ch.build_overheat_return_route = lambda: [dict(DOCK)]
    if short_m:
        nav = sim.robot.nav
        sim.robot.nav = lambda x, y, yaw, continuing=False: nav(x - short_m, y, yaw, continuing)
    if session:
        _guide_session(sim)

    def go():
        sim.ch.trigger_overheat_protection()
        sim.pl.nav_thread()
    sim.run(go, until=lambda: not sim.sm.is_navigating and sim.clock.elapsed() > 2, grace=3)
    return _out(sim)


@scenario("U6 [회귀] 모터 과열 보호 · 안내 세션 진행 중 → 도킹 포인트에서 SIT 유지",
          expect=lambda r: r["sat"] and r["posture"] == "SIT" and not r["skipped"] and r["errors"] == 0)
def u6(sim):
    return _overheat(sim, session=True)


@scenario("U7 [회귀] 모터 과열 보호 · 0.8 m 앞에서 끝남 → SIT 유지(냉각 우선)",
          expect=lambda r: r["sat"] and r["posture"] == "SIT" and not r["skipped"] and r["errors"] == 0)
def u7(sim):
    return _overheat(sim, short_m=0.8)
