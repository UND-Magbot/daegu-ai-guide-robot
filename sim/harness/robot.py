"""가짜 로봇(receiver + 본체) — 실기에서 확인한 동작만 규칙으로 넣는다.

규칙 출처(메모리·분석 보고서)
- 요청→출발 1.6s (실측, 경유지 연속 주행 분석)
- 리셋 NAV: receiver Mode 0 → 0x1001 확인 → NAV, 내비 보행이면 전환 후 출발까지 3.1s
- 리셋 없는 NAV(continue): 실행 중이면 거부, 끝난 뒤면 수락
- NAV 로 출발하면 펌웨어가 Navigation 모드(1) + 내비 보행 0x3002
- GAIT_BASIC: 실행 중이 아니면 0x1001, 제어 모드는 그대로
- MODE_REGULAR: 최근 5s 안에 NAV 를 받았으면 receiver 가드가 버린다.
  실행 중이면 작업 끝에 적용, 출발 대기 중이면 0xa328 경합으로 센다.
- 충전 중 MODE_REGULAR 는 동작 미확인 → 위험 횟수로 센다.
- 다지점(NAV_MULTI, 10-20 1F 실기 7회): receiver 가 첫 점 앞에서만 리셋하고 1건으로 보낸다.
  로봇은 점을 차례로 가며 상태 3 을 끝까지 유지하고, 1007 Value 로 지금 향하는 점 번호를 보고한다.
  경유점에서도 감속·방향 전환으로 transit_dwell(실측 자율 2.5s, 직선 3.1~3.6s) 머문다. 끝점에서 0.
여기에 없는 동작(벤더 재정렬, 측위 품질, Wi-Fi 타이밍)은 재현하지 않는다. 보고서에 그렇게 적는다.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

from .clock import Clock

GAIT_BASIC = 0x1001
GAIT_NAV = 0x3002
MODE_NAME = {0: "Regular", 1: "Navigation"}


@dataclass
class RobotParams:
    speed: float = 0.5               # m/s
    start_delay: float = 1.6         # NAV 수락 → 출발
    reset_delay: float = 3.1         # 내비 보행에서 리셋 NAV → 출발(보행 전환 포함)
    mode0_guard_sec: float = 5.0     # receiver: 최근 NAV 뒤 MODE_REGULAR 버림
    old_receiver: bool = False       # MODE_REGULAR 를 모르는 옛 receiver
    transit_dwell: float = 2.5       # 다지점 경유점 감속·방향 전환(실측 자율 2.5s)
    multi_supported: bool = True     # False 면 옛 receiver — NAV_MULTI 거부


class FakeRobot:
    def __init__(self, clock: Clock, params: RobotParams | None = None):
        self.clock = clock
        self.p = params or RobotParams()
        self.x = self.y = self.yaw = 0.0
        self.gait, self.mode = GAIT_BASIC, 0
        self.status = 0                 # NAV_STATUS: 0 대기, 3 실행 중
        self.target: tuple[float, float, float] | None = None
        self.queue: list[tuple[float, float, float]] = []   # 다지점 남은 점(target 다음부터)
        self.value = 1                  # 1007 Value — 다지점에서 지금 향하는 점 번호
        self.dwell_until = -1e9
        self.multi_sent: list[int] = []  # 받은 다지점의 점 개수
        self.executing = False
        self.start_at: float | None = None
        self.pending_mode0 = False
        self.charging = False
        self.posture = "STAND"
        # receiver 가드 상태
        self.live = False
        self.nav_t = -1e9
        self.run_since: float | None = None
        self.seen_running = False
        # 장애 주입
        self.comm_down_until = -1e9
        self.hold_until = -1e9
        # 계측
        self.cmds: list[str] = []
        self.timeline: list[tuple[float, str]] = []
        self.switches = 0               # 보행 전환(다리 재정렬)
        self.rejects = 0                # 실행 중 NAV 거부
        self.races = 0                  # 0xa328 경합
        self.charge_mode0 = 0           # 충전 중 MODE_REGULAR
        self.dropped = 0                # receiver 가드가 버린 MODE_REGULAR
        self.arrivals: list[tuple[float, float, float]] = []
        clock.add_ticker(self.advance)

    # ── 기록 ──
    def _log(self, what: str) -> None:
        self.cmds.append(what)
        self.timeline.append((round(self.clock.elapsed(), 2), what))

    def set_gait(self, g: int) -> None:
        if g != self.gait:
            self.switches += 1
            self.gait = g

    def _regular(self) -> None:
        self.set_gait(GAIT_BASIC)
        self.mode = 0

    def place(self, x: float, y: float, yaw: float = 0.0) -> None:
        self.x, self.y, self.yaw = x, y, yaw

    # ── 장애 주입 ──
    def drop_comm(self, sec: float) -> None:
        """NAV_STATUS 응답을 sec 초 동안 끊는다(Wi-Fi 두절)."""
        self.comm_down_until = self.clock.now() + sec

    def hold(self, sec: float) -> None:
        """실행 중 상태로 sec 초 동안 제자리(장애물 앞 대기 등)."""
        self.hold_until = self.clock.now() + sec

    def comm_ok(self) -> bool:
        return self.clock.now() >= self.comm_down_until

    # ── 명령 수신 ──
    def nav(self, x: float, y: float, yaw: float, continuing: bool = False) -> None:
        self._log("NAV(cont)" if continuing else "NAV")
        now = self.clock.now()
        self.live, self.seen_running, self.nav_t, self.run_since = True, False, now, None
        if continuing and self.gait == GAIT_NAV:
            if self.executing:
                self.rejects += 1
                self._log("  └ 거부(실행 중)")
                return
            delay = self.p.start_delay
        else:
            self.pending_mode0 = False
            if self.executing or self.gait == GAIT_NAV:
                self.executing = False
                self.status = 0
                self._regular()
                delay = self.p.reset_delay
            else:
                self.mode = 0
                delay = self.p.start_delay
        self.target = (x, y, yaw)
        self.queue, self.value = [], 1
        self.start_at = now + delay
        self.executing = True

    def nav_multi(self, points: list[tuple[float, float, float]], continuing: bool = False) -> bool:
        """receiver NAV_MULTI — 첫 점 앞에서 리셋(서 있거나 내비 보행이면 단일 리셋 NAV 와 같은 지연)."""
        if not self.p.multi_supported:
            self._log("NAV_MULTI(거부: 옛 receiver)")
            return False
        self._log(f"NAV_MULTI({len(points)}점)")
        self.multi_sent.append(len(points))
        x, y, yaw = points[0]
        # 이어가기(continue)면 단일 이어가기 NAV 와 같다: 내비 보행이면 리셋 없이 출발(가정, 10-27 실측 전)
        self.nav(x, y, yaw, continuing)
        self.cmds.pop()                 # nav() 가 남긴 "NAV" 기록은 빼고 NAV_MULTI 하나로 센다
        self.timeline.pop()
        self.queue = list(points[1:])
        return True

    def cmd(self, c: str) -> None:
        self._log(c)
        now = self.clock.now()
        if c == "GAIT_BASIC" and not self.executing:
            self.set_gait(GAIT_BASIC)
        elif c == "MODE_REGULAR":
            if self.p.old_receiver:
                self.cmds[-1] = "MODE_REGULAR(옛 receiver 무시)"
                return
            if self.live and now - self.nav_t < self.p.mode0_guard_sec:
                self.dropped += 1
                self.cmds[-1] = "MODE_REGULAR(버림)"
                return
            if self.charging:
                self.charge_mode0 += 1
            if self.executing and self.start_at is not None and now < self.start_at:
                self.races += 1
            elif self.executing:
                self.pending_mode0 = True
            else:
                self._regular()
        elif c in ("CANCEL_NAV", "STOP"):
            self.live = False
            self.executing = False
            self.status = 0
            self.start_at = None
            self.queue = []
            if self.pending_mode0:
                self.pending_mode0 = False
                self._regular()
        elif c in ("SIT", "STAND"):
            self.posture = c

    def nav_status(self) -> dict:
        return {"status": self.status, "age": 0.1, "value": self.value}

    # ── 시간 진행 ──
    def advance(self, dt: float) -> None:
        now = self.clock.now()
        if not (self.executing and self.start_at is not None and now >= self.start_at):
            return
        self.set_gait(GAIT_NAV)
        self.mode = 1
        self.status = 3
        if self.run_since is None:
            self.run_since = now
        if now - self.run_since >= 1.0:
            self.seen_running = True
        if now < self.hold_until or now < self.dwell_until:
            return
        tx, ty, tyaw = self.target
        d = math.hypot(tx - self.x, ty - self.y)
        step = self.p.speed * dt
        if d <= step and self.queue:
            # 다지점 경유점 — 상태 3 을 유지한 채 감속·방향 전환 뒤 다음 점으로
            self.x, self.y, self.yaw = tx, ty, tyaw
            self.arrivals.append((round(self.clock.elapsed() + dt, 2), tx, ty))
            self.target = self.queue.pop(0)
            self.value += 1
            self.dwell_until = now + self.p.transit_dwell
            return
        if d <= step:
            self.x, self.y, self.yaw = tx, ty, tyaw
            self.status = 0
            self.executing = False
            self.start_at = None
            self.arrivals.append((round(self.clock.elapsed() + dt, 2), tx, ty))
            if self.seen_running:
                self.live = False
            if self.pending_mode0:
                self.pending_mode0 = False
                self._regular()
        else:
            self.x += (tx - self.x) / d * step
            self.y += (ty - self.y) / d * step

    def summary(self) -> dict:
        return {
            "pos": [round(self.x, 2), round(self.y, 2)],
            "gait": f"0x{self.gait:04x}",
            "mode": MODE_NAME.get(self.mode, str(self.mode)),
            "posture": self.posture,
            "charging": self.charging,
            "executing": self.executing,
            "switches": self.switches,
            "rejects": self.rejects,
            "races": self.races,
            "charge_mode0": self.charge_mode0,
            "dropped": self.dropped,
            "arrivals": len(self.arrivals),
            "multi_sent": list(self.multi_sent),
            "cmds": [c for c in self.cmds if c != "STOP"],
        }


class FakeSocket:
    """polling 모듈의 socket 을 대신한다. NAV_STATUS 요청에 가짜 로봇 상태로 답한다."""

    timeout = TimeoutError

    def __init__(self, robot: FakeRobot):
        self.robot = robot

    def socket(self, *a, **k):
        robot = self.robot

        class _Sock:
            def settimeout(self, t): pass
            def setsockopt(self, *a): pass
            def bind(self, *a): pass
            def sendto(self, m, addr): return len(m)
            def close(self): pass

            def recvfrom(self, n):
                if not robot.comm_ok():
                    raise TimeoutError("sim: comm down")
                return json.dumps(robot.nav_status()).encode(), ("sim", 0)

            def __enter__(self): return self
            def __exit__(self, *a): self.close()

        return _Sock()

    AF_INET = 2
    SOCK_DGRAM = 2
    SOCK_STREAM = 1
    SOL_SOCKET = 1
    SO_REUSEADDR = 4
