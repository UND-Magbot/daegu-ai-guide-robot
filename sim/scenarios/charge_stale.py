"""충전 상태 수신 끊김(2026-11-02 669) — 현행 vs 변경.

  sim\\run.bat sim\\scenarios\\charge_stale.py --cand sim\\out\\cand_charge_stale
실기(11-02): 제조사 charge_manager 가 NOS 09:56:44 부터 /CHARGE_STATUS 발행을 멈춤(relay-charge 재시작으로 안 풀림).
  · 관제 charge_state 0 이 81분 고정 → 실제 충전 중인데 대시보드 '충전소 이동' 버튼
  · 10:11 충전소 이동: 제조사는 10:12:41 state 1→2(충전 성공), 관제는 '3회 시도 모두 실패'
  · 이 상태에서 대시보드 '충전 해제'(_undock_internal)를 누르면: 멈춘 0 을 '이탈 완료'로 읽고 STAND 뒤 도킹 포인트 NAV
    → 로봇이 아직 도크를 빠져나오는 중에 이동 명령(10-28 이탈 중 NAV 로 엎드림·전복과 같은 모양) — 추론
  · 수동 해제: Charge=0 한 번 → 제조사 2→3→0, 14초.
변경: 충전 값 시각이 BasicStatus 시각(둘 다 NOS 시계)보다 30초 넘게 뒤처지면 '수신 끊김'.
  끊김이면 충전소 이동은 Charge=1 한 번 + 도크에 있다고 가정, 충전 해제는 Charge=0 만 보내고 25초 대기(재정렬·SIT 생략).
가짜 상태 공급(Feed): 1초마다 runtime.update_status 를 부른다. relay 살아 있으면 충전 시각이 갱신(period 초마다).
가짜 제조사 이탈(가정, 11-02 실측 닮음): Charge=0 뒤 2초부터 서서(MotionState 1) 나오고 14초에 끝(상태 3 → 0, relay 살아 있을 때만 전달).
"""
import ast
import types

from harness import scenario

NOS_OFFSET = 66.0
BATT = {"BatteryLevelLeft": 100, "BatteryLevelRight": 98, "chargeLeft": False, "chargeRight": False,
        "VoltageLeft": 83, "VoltageRight": 83}


def _fresh_fn(mod, name):
    """하네스가 가짜로 바꾼 함수를 이 변형의 원본 코드로 다시 만든다(모듈 전역·가짜 시계 그대로)."""
    src = open(mod.__file__, encoding="utf-8").read()
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == name)
    code = compile(ast.Module(body=[node], type_ignores=[]), mod.__file__, "exec")
    ns = {}
    exec(code, mod.__dict__, ns)
    return ns[name]


class Feed:
    """receiver STATUS 를 흉내 낸 1초 상태 공급."""

    def __init__(self, sim, state=2, alive=True, period=1.0, motion=4):
        self.sim, self.state, self.alive, self.period, self.motion = sim, state, alive, period, motion
        self.cts = sim.clock.now() + NOS_OFFSET
        self.sent_state = state                 # receiver 가 들고 있는 마지막 값 — relay 가 끊기면 고정
        self.exit_at = None
        rt = sim.rt
        rt.init_runtime([types.SimpleNamespace(
            id=10, RobotName="SIM", RobotType="M20", RobotIP="10.0.0.1", RobotPort=30000, CurrentFloorId=1,
            CurrentMapId=1, BusinessId=1, LimitBattery=30)])
        self.tick()

    def nos(self):
        return self.sim.clock.now() + NOS_OFFSET

    def tick(self):
        now = self.nos()
        if self.exit_at is not None:            # 제조사 이탈 동작(가정)
            d = self.sim.clock.now() - self.exit_at
            if d >= 14.0:
                self.state, self.motion, self.exit_at = 0, 1, None
            elif d >= 2.0:
                self.state, self.motion = 3, 1
        if self.alive and (now - self.cts >= self.period - 1e-6 or self.state != self.sent_state):
            self.cts, self.sent_state = now, self.state
        self.sim.rt.update_status(10, dict(BATT), self.sim.clock.now(),
                                  charge_state={"state": self.sent_state, "error_code": 0, "timestamp": self.cts},
                                  basic_status={"Sleep": 0, "PowerManagement": 0, "MotionState": self.motion,
                                                "timestamp": now})
        self.sim.clock.after(1.0, self.tick)

    def status(self):
        rt = self.sim.rt
        return rt._build_status(rt._runtime[10])


def _wire_packets(sim, feed):
    """Charge 패킷을 세고, Charge=0 이면 제조사 이탈 동작을 시작한다(가정)."""
    ch, sent = sim.ch, []

    def start():
        sent.append((round(sim.clock.elapsed(), 1), "Charge=1"))
        return True

    def stop():
        sent.append((round(sim.clock.elapsed(), 1), "Charge=0"))
        if feed.state == 2:
            feed.exit_at = sim.clock.now()
        return True
    ch._send_start_charge_packet = start
    ch._send_stop_charge_packet = stop
    ch._send_charge_clear_packet = lambda: sent.append((round(sim.clock.elapsed(), 1), "Charge=2")) or True
    ch._get_dock_point_waypoint = lambda: {"x": 2.0, "y": 1.0, "yaw": 0.0, "name": "충전소-1",
                                           "map_id": None, "floor_id": None}
    return sent


def _events(sim, kind):
    return sum(1 for _t, k, _m in sim.events if k == kind)


def _first_nav_after_exit_start(sim, sent):
    """Charge=0 뒤 처음 나간 NAV 시각 - Charge=0 시각(초). 14초 미만이면 이탈 중 NAV."""
    t0 = next((t for t, c in sent if c == "Charge=0"), None)
    if t0 is None:
        return None
    navs = [round(t, 1) for t, c in sim.robot.timeline if c.startswith("NAV") and t >= t0 - 0.01]
    return round(navs[0] - t0, 1) if navs else None


# ── 재현 ───────────────────────────────────────────

@scenario("C1 [재현] 충전 값 81분 고정(0) 중 실제로 도크에 붙어 충전 — 변경: '수신 끊김' 표시·알림 1번, 충전 해제 버튼(도크 가정)",
          expect=lambda r: r["errors"] == 0 and r["label"] == "수신 끊김" and r["stale_alert"] == 1 and r["at_dock"]
          and r["charge1"] == 1 and r["fail_alert"] == 0 and r["unverified"] == 1)
def c1(sim):
    feed = Feed(sim, state=0, alive=False)         # 발행 멈춤, 마지막 값 0
    sent = _wire_packets(sim, feed)
    sim.wait(40)                                   # 끊김 판정(30초)
    feed.state = 2                                 # 실제로는 도킹·충전(관제에 안 옴)
    retry = _fresh_fn(sim.ch, "start_charge_with_retry")
    sim.run(retry, until=lambda: False, grace=60)
    st = feed.status()
    return {"label": st["charge_state_label"], "at_dock": st["at_dock"], "stale_alert": _events(sim, "charge_state_stale"),
            "charge1": sum(1 for _t, c in sent if c == "Charge=1"), "fail_alert": _events(sim, "robot_charge_failed"),
            "unverified": _events(sim, "robot_charging_unverified"), "sent": sent}


@scenario("C2 [재현] 수신 끊김 중 대시보드 '충전 해제' — 현행: 이탈 중(14초 전) 도킹 포인트 NAV / 변경: Charge=0 만, NAV·SIT 없음",
          expect=lambda r: r["errors"] == 0 and r["nav_after_exit"] is None and not r["sat"] and r["charge1"] == 0
          and r["unverified"] == 1, limit_sec=300)
def c2(sim):
    feed = Feed(sim, state=0, alive=False, motion=4)
    sent = _wire_packets(sim, feed)
    sim.wait(40)
    feed.state = 2                                 # 실제는 충전 중(관제는 0)

    def go():
        sim.ch._undock_internal()
        sim.pl.nav_thread()
    sim.run(go, until=lambda: not sim.sm.is_navigating and sim.clock.elapsed() > 120, grace=5)
    s = sim.summary()
    return {"nav_after_exit": _first_nav_after_exit_start(sim, sent), "sat": "SIT" in s["cmds"],
            "charge1": sum(1 for _t, c in sent if c == "Charge=1"), "unverified": _events(sim, "undock_unverified"),
            "sent": sent}


# ── 회귀 ───────────────────────────────────────────

@scenario("C3 [회귀] 충전 값 정상(1초) · 충전 중 '충전 해제' → 도킹 포인트 NAV·SIT (현행과 같아야 함 · NAV 시각 관찰)",
          expect=lambda r: r["errors"] == 0 and r["nav_after_exit"] is not None and r["sat"],
          limit_sec=300)
def c3(sim):
    feed = Feed(sim, state=2, alive=True, motion=4)
    sent = _wire_packets(sim, feed)
    sim.wait(5)

    def go():
        sim.ch._undock_internal()
        sim.pl.nav_thread()
    sim.run(go, until=lambda: not sim.sm.is_navigating and sim.clock.elapsed() > 30, grace=5)
    s = sim.summary()
    return {"nav_after_exit": _first_nav_after_exit_start(sim, sent), "sat": "SIT" in s["cmds"], "sent": sent}


@scenario("C4 [회귀] 충전 값 정상 · 충전소 이동 뒤 1→2 → 첫 시도 성공, 충전 해제 버튼, 끊김 알림 없음 (현행과 같아야 함)",
          expect=lambda r: r["errors"] == 0 and r["charge1"] == 1 and r["fail_alert"] == 0 and r["at_dock"]
          and r["stale_alert"] == 0)
def c4(sim):
    feed = Feed(sim, state=0, alive=True)
    sent = _wire_packets(sim, feed)
    sim.wait(3)
    sim.at(3.0, lambda: setattr(feed, "state", 1))
    sim.at(8.0, lambda: setattr(feed, "state", 2))
    retry = _fresh_fn(sim.ch, "start_charge_with_retry")
    sim.run(retry, until=lambda: False, grace=30)
    st = feed.status()
    return {"charge1": sum(1 for _t, c in sent if c == "Charge=1"), "fail_alert": _events(sim, "robot_charge_failed"),
            "at_dock": st["at_dock"], "stale_alert": _events(sim, "charge_state_stale"), "label": st["charge_state_label"]}


@scenario("C5 [회귀] 새 relay(값 같으면 5초마다 다시 보냄) · 2분 충전 → 끊김 아님",
          expect=lambda r: r["errors"] == 0 and r["stale_alert"] == 0 and r["label"] != "수신 끊김" and r["at_dock"])
def c5(sim):
    feed = Feed(sim, state=2, alive=True, period=5.0)
    _wire_packets(sim, feed)
    sim.wait(120)
    st = feed.status()
    return {"stale_alert": _events(sim, "charge_state_stale"), "label": st["charge_state_label"], "at_dock": st["at_dock"]}


@scenario("C6 [변경] 끊겼다 다시 들어옴 → 끊김 알림 1번·복구 알림 1번, 표시는 실제 값(충전 중)",
          expect=lambda r: r["errors"] == 0 and r["stale_alert"] == 1 and r["recovered"] == 1 and r["label"] == "충전 중"
          and r["at_dock"])
def c6(sim):
    feed = Feed(sim, state=0, alive=False)
    _wire_packets(sim, feed)
    sim.wait(40)
    feed.state, feed.alive = 2, True
    sim.wait(5)
    st = feed.status()
    return {"stale_alert": _events(sim, "charge_state_stale"), "recovered": _events(sim, "charge_state_recovered"),
            "label": st["charge_state_label"], "at_dock": st["at_dock"]}


@scenario("C7 [변경] 끊김 중 충전소 이동(도크 가정) 뒤 장소 이동 시작 → 도크 가정 지움(충전 해제 버튼 안 보임)",
          expect=lambda r: r["errors"] == 0 and r["before"] and not r["after"])
def c7(sim):
    feed = Feed(sim, state=0, alive=False)
    _wire_packets(sim, feed)
    sim.wait(40)
    retry = _fresh_fn(sim.ch, "start_charge_with_retry")
    sim.run(retry, until=lambda: False, grace=20)
    before = feed.status()["at_dock"]
    sim.sm.is_navigating = True
    sim.wait(3)
    after = feed.status()["at_dock"]
    sim.sm.is_navigating = False
    return {"before": before, "after": after}
