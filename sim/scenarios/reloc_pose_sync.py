"""위치 재조정 화면 — 스캔 오버레이에 쓰는 로봇 위치의 지연·신선도.

실제 관제 코드: polling.position_thread(위치 수집), runtime.update_position/get_position,
auto_init_pose.confirm_current_position_manual('완료' 서버 판정).
가짜로 둔 것(시나리오 안):
  - receiver: 로봇을 0.5s 마다 폴링(POS_POLL_MS 기본), 로봇 응답이 없으면 마지막 좌표 유지.
    현행 receiver 는 age 없이, 변경 receiver 는 age(마지막 로봇 응답 뒤 경과 초)를 실어 준다.
  - 화면: /robot/position 을 현행 1.0s·변경 0.5s 로 폴링, 위치 시각이 3s 넘으면 '통신 확인 불가'.
  - 스캔: relay 10Hz 중 최신을 0.5s 마다 받음 = 그 순간 실제 자세 기준 body 점군.
오버레이 오차 = 화면이 쓰는 위치와 실제 자세 차이를 5m 앞 벽점에서 잰 어긋남(위치차 + 5m×각도차).
"""
import math

from harness import scenario

RID = 10
RANGE_M = 5.0


def _is_cand(sim) -> bool:
    return hasattr(sim.pl, "_apply_position_sample")


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class Stage:
    """가짜 receiver + 가짜 재조정 화면 + 계측."""

    def __init__(self, sim, receiver_age: bool | None = None, viewer: bool = True):
        self.sim, self.clock, self.robot = sim, sim.clock, sim.robot
        rt = sim.rt
        self.cand = _is_cand(sim)
        self.receiver_age = self.cand if receiver_age is None else receiver_age
        self.front_poll = 0.5 if self.cand else 1.0
        self.location = 0          # 로봇이 보고하는 측위 상태
        # 로봇이 '믿는' 자세(측위 결과). 실제 자세는 robot.x/y/yaw.
        self.belief = [0.0, 0.0, 0.0]
        self.latest = {"x": 0.0, "y": 0.0, "yaw": 0.0, "timestamp": 0}
        self.latest_mono = None
        self.requests = 0
        self.live = None           # 화면 liveOdom
        self.stale = True
        self.stale_marks: list[tuple[float, bool]] = []
        self.samples: list[tuple[float, float]] = []   # (t, 오버레이 오차 m)
        self.alive = True

        # runtime 항목 + 실제 get_position(하네스가 가짜로 바꿔 둔 것을 원래 내용으로)
        with rt._lock:
            rt._runtime[RID] = {
                "robot_id": RID, "robot_name": "SIM", "robot_type": "", "robot_ip": "sim",
                "robot_port": 30000, "current_floor_id": None, "current_map_id": None,
                "limit_battery": 30,
                "position": {"x": 0.0, "y": 0.0, "yaw": 0.0, "timestamp": 0},
                "battery": {}, "charge_state": {"state": 0, "error_code": 0, "timestamp": 0},
                "device_temp": {}, "basic_status": {}, "abnormal_codes": set(),
                "last_heartbeat": 0, "nav": {"arrived": False, "last_state": None, "timestamp": 0},
            }

        def real_get_position(robot_id):
            with rt._lock:
                entry = rt._runtime.get(robot_id)
                if not entry:
                    return {"x": 0.0, "y": 0.0, "yaw": 0.0, "timestamp": 0}
                return {"robot_id": entry["robot_id"], "robot_name": entry["robot_name"],
                        "initpose_pending": bool(entry.get("_initpose_pending", False)),
                        **entry["position"]}
        rt.get_position = real_get_position
        sim.sender.request_receiver_tcp = self._receiver
        # 시나리오에서 처음 부르는 모듈도 가상 시계를 쓰게 한다('완료' 판정의 신선도 계산)
        import app.robot_control.auto_init_pose  # noqa: F401
        import app.robot_control.relocalize as rl
        sim.rewire_late_modules()
        if viewer and self.cand:
            rl._add_viewer(1)
        self._every(0.5, self._receiver_poll_robot)
        self._every(self.front_poll, self._front_poll)
        self._every(0.5, self._scan_frame)

    # ── 주기 작업 ──
    def _every(self, dt, fn):
        def tick():
            if not self.alive:
                return
            fn()
            self.clock.after(dt, tick)
        self.clock.after(dt, tick)

    def _receiver_poll_robot(self):
        if not self.robot.comm_ok():
            return   # 로봇 무응답 — receiver 는 마지막 값을 그대로 둔다
        self.latest = {"x": self.belief[0], "y": self.belief[1], "yaw": self.belief[2],
                       "location": self.location, "timestamp": self.clock.now()}
        self.latest_mono = self.clock.now()

    def _receiver(self, msg, **k):
        if msg.get("action") != "POSITION":
            return None
        self.requests += 1
        p = dict(self.latest)
        if self.receiver_age and self.latest_mono is not None:
            p["age"] = round(self.clock.now() - self.latest_mono, 3)
        return p

    def _front_poll(self):
        d = self.sim.rt.get_position(RID)
        if not d.get("timestamp"):
            return
        age = max(0.0, self.clock.now() - d["timestamp"])
        if age > 3.0:
            self._set_stale(True)
            return
        self.live = (d["x"], d["y"], d["yaw"])
        self._set_stale(False)

    def _set_stale(self, v):
        if v != self.stale:
            self.stale_marks.append((round(self.clock.elapsed(), 2), v))
        self.stale = v

    def _scan_frame(self):
        if self.live is None:
            return
        tx, ty, tyaw = self.robot.x, self.robot.y, self.robot.yaw
        lx, ly, lyaw = self.live
        err = math.hypot(lx - tx, ly - ty) + RANGE_M * abs(_wrap(lyaw - tyaw))
        self.samples.append((self.clock.elapsed(), err))

    # ── 실행 ── 위치 폴링 스레드는 한 번만 띄워 끝까지 돈다(다시 띄우면 폴링 위상이 초기화된다)
    def at(self, t, fn):
        """시작 후 t 초에 fn 실행."""
        self.clock.at(self.clock.t0 + t, fn)

    def run(self, sec):
        self.sim.run(self.sim.pl.position_thread, until=lambda: self.clock.elapsed() >= sec,
                     grace=0.0)
        self.alive = False

    def set_true(self, x, y, yaw):
        """측위가 맞는 상태: 실제 자세 = 로봇이 믿는 자세."""
        self.robot.place(x, y, yaw)
        self.belief = [x, y, yaw]

    def recover_time(self, t0, tol=0.05):
        """t0 이후 오버레이 오차가 tol 아래로 처음 내려온 시각까지(초)."""
        for t, e in self.samples:
            if t >= t0 and e <= tol:
                return round(t - t0, 2)
        return None


def _stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"avg": None, "max": None}
    return {"avg": round(sum(xs) / len(xs), 2), "max": round(max(xs), 2)}


WRONG = [2.6, 1.4, 0.35]   # 측위가 틀어진 상태(0.7m·20°)
RIGHT = [2.0, 1.0, 0.0]


@scenario("주입 직후 스캔이 맞아 보이기까지(정지 로봇, 주입 시점 10가지)",
          expect=lambda r: r["recover_max_s"] is not None and r["recover_max_s"] <= 1.6
          or f"최대 {r['recover_max_s']}s")
def inject_recover(sim):
    st = Stage(sim)
    st.robot.place(*RIGHT)
    st.belief = list(WRONG)
    inject_at = [5.0 + 9.0 * i + 0.23 * i for i in range(10)]   # 폴링 위상이 매번 다르게
    for t in inject_at:
        st.at(t, lambda: setattr(st, "belief", list(RIGHT)))      # 주입 = 믿는 자세가 맞아짐
        st.at(t + 6.0, lambda: setattr(st, "belief", list(WRONG)))  # 다음 회차 준비
    st.run(inject_at[-1] + 7.0)
    times = [st.recover_time(t) for t in inject_at]
    s = _stats(times)
    return {"recover_avg_s": s["avg"], "recover_max_s": s["max"], "recover_each": times}


@scenario("제자리 회전 중 오버레이 어긋남(0.5rad/s, 6초, 관찰)")
def rotate_overlay(sim):
    st = Stage(sim)
    st.set_true(*RIGHT)
    t0 = 5.0

    def spin(dt):
        if t0 <= st.clock.elapsed() < t0 + 6.0:
            st.robot.yaw = _wrap(st.robot.yaw + 0.5 * dt)
            st.belief[2] = st.robot.yaw
    st.clock.add_ticker(spin)
    st.run(t0 + 9.0)
    during = _stats([e for t, e in st.samples if t0 + 1.0 <= t < t0 + 6.0])
    settle = st.recover_time(t0 + 6.0)
    return {"rot_err_avg_m": during["avg"], "rot_err_max_m": during["max"],
            "after_stop_settle_s": settle}


def _confirm(st, out, key):
    from app.robot_control.auto_init_pose import confirm_current_position_manual
    ok, why = confirm_current_position_manual(RID)
    out[key], out[key + "_why"] = ok, why
    out[key + "_stale_shown"] = st.stale


def _drop_case(sim, receiver_age=None):
    st = Stage(sim, receiver_age=receiver_age)
    st.set_true(*RIGHT)
    out = {"cand": st.cand}
    st.at(6.0, lambda: st.robot.drop_comm(12.0))       # 6~18초 로봇 무응답
    st.at(5.5, lambda: _confirm(st, out, "before"))
    st.at(14.0, lambda: _confirm(st, out, "during"))   # 두절 8초째 '완료'
    st.at(21.0, lambda: _confirm(st, out, "after"))    # 복구 3초 뒤 '완료'
    st.run(23.0)
    out["stale_marks"] = st.stale_marks
    return out


@scenario("로봇↔receiver 12초 두절 중 '완료'(두절 전 측위 정상)",
          expect=lambda r: (r["before"] and r["during_stale_shown"] and not r["during"]
                            and r["after"]) or
          f"두절 전 완료={r['before']}, 두절 중 통신확인불가={r['during_stale_shown']}, "
          f"두절 중 완료={r['during']}, 복구 후 완료={r['after']}")
def drop_confirm(sim):
    return _drop_case(sim)


@scenario("구버전 receiver(age 없음) 호환 — 현행 동작 그대로(두절 중 완료 통과)",
          expect=lambda r: r["before"] and r["during"] and r["after"]
          or f"{r['before']}/{r['during']}/{r['after']}")
def old_receiver_compat(sim):
    return _drop_case(sim, receiver_age=False)


@scenario("측위 상실 보고 → 미확정, 회복 보고 → 해제(회귀)",
          expect=lambda r: r["pending_after_lost"] and not r["pending_after_ok"]
          or f"상실 뒤 미확정={r['pending_after_lost']}, 회복 뒤={r['pending_after_ok']}")
def loc_lost_regression(sim):
    st = Stage(sim)
    st.set_true(*RIGHT)
    out = {}
    st.at(4.0, lambda: setattr(st, "location", 1))
    st.at(24.0, lambda: out.update(pending_after_lost=sim.rt.is_initpose_pending(RID)))
    st.at(24.5, lambda: setattr(st, "location", 0))
    st.at(44.0, lambda: out.update(pending_after_ok=sim.rt.is_initpose_pending(RID),
                                   reason=sim.rt.get_initpose_reason(RID)))
    st.run(45.0)
    return out


@scenario("두절 중 측위 상실이 굳지 않음 — 두절 직전 상실 1회 보고 뒤 20초 무응답(관찰)")
def lost_then_drop(sim):
    st = Stage(sim)
    st.set_true(*RIGHT)
    out = {}
    st.at(4.0, lambda: setattr(st, "location", 1))
    st.at(4.6, lambda: st.robot.drop_comm(20.0))
    st.at(24.0, lambda: out.update(pending_during_drop=sim.rt.is_initpose_pending(RID)))
    st.run(25.0)
    return out


@scenario("위치 폴링 횟수 — 재조정 창 닫힘 60초(회귀: 2s 주기 유지)",
          expect=lambda r: 27 <= r["requests"] <= 33 or f"{r['requests']}회")
def poll_closed(sim):
    st = Stage(sim, viewer=False)
    st.set_true(*RIGHT)
    st.run(60.0)
    return {"requests": st.requests}


@scenario("위치 폴링 횟수 — 재조정 창 열림 60초(관찰)")
def poll_open(sim):
    st = Stage(sim, viewer=True)
    st.set_true(*RIGHT)
    st.run(60.0)
    return {"requests": st.requests}
