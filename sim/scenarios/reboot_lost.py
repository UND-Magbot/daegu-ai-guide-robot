"""재부팅 감지 — NOS 부팅 번호(BootId)가 바뀌면 재부팅으로 본다(2026-10-27, B 방안).

실기(10-27 10:52~10:56): 로봇충전소-1 에서 끄고 켬 → NOS 는 (0,0,0)에서 시작했지만 첫 정합이
(0.50,-0.31)로 끌려가 보고됨. 원점 0.3m 기준에 안 걸려 재부팅 감지가 안 됐고 주차 장소 주입도 안 됨.

변경:
- receiver: BasicStatus 에 BootId(/proc/sys/kernel/random/boot_id) 를 싣는다(receiver 재시작만으론 안 바뀜).
- runtime: BasicStatus 병합 키에 BootId 추가. check_and_init_pose: 이전 BootId 와 다르면 재부팅.
- _noncharge_position_stable: Location≠0 이면 '안정'으로 보지 않는다(끌려간 좌표 조용히 확정 방지).
- persistence: Location≠0 좌표는 마지막 위치로 저장하지 않는다.

가짜로 둔 것: heartbeat 1초마다 check_and_init_pose. 위치·측위 상태는 runtime 항목에 직접 쓴다.
INIT_POSE 수렴 규칙은 park_seed.py 와 같다(1m·30° 안이면 실제 자세로 정합, Location=0).
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "park_seed_base_rl", os.path.join(os.path.dirname(__file__), "park_seed.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
BaseStage, RID, PARK, ON = _base.Stage, _base.RID, _base.PARK, _base.ON

LAST_REAL = (-0.898, 1.198, 1.369)      # 10-27 끄기 전 저장된 마지막 위치
BOOT_REAL = (0.57, -0.21, -0.49)        # 10-27 재접속 직후 관제가 받은 위치(측위 상실)


class Stage(BaseStage):
    """park_seed Stage + heartbeat 감지 루프."""

    def __init__(self, sim, true_pose, last_pose, boot_reject=2):
        super().__init__(sim, true_pose=true_pose, last_pose=last_pose, boot_reject=boot_reject)
        self.online = True
        self.sleep_val = 0
        self.boot_id = "boot-A"
        self.belief = list(last_pose)
        self.location = 0
        self.triggers = 0
        self._sync()
        aip = self.aip
        orig = aip._inject_worker

        def counted(rid, *a, **k):
            self.triggers += 1
            return orig(rid, *a, **k)
        aip._inject_worker = counted
        self.sim.rt.get_position = lambda rid: (self._sync(), self._pos())[1]
        self._beat()

    def _pos(self):
        return {"x": self.belief[0], "y": self.belief[1], "yaw": self.belief[2],
                "location": self.location, "timestamp": self.sim.clock.now()}

    def _sync(self):
        rt = self.sim.rt
        with rt._lock:
            e = rt._runtime[RID]
            e["position"] = self._pos()
            e["basic_status"] = {"Sleep": self.sleep_val}
            if self.boot_id is not None:
                e["basic_status"]["BootId"] = self.boot_id
            e["charge_state"] = {"state": 0}

    def _beat(self):
        if self.online:
            self._sync()
            self.aip.check_and_init_pose(RID)
        self.sim.clock.after(1.0, self._beat)

    def power_cycle(self, off_at, on_at, boot_pose, boot_loc=1, new_boot="boot-B"):
        def off():
            self.online = False
        def offline():                     # 35초 끊김 → polling 이 오프라인 확정
            self.aip.mark_offline(RID)
        def on():
            self.online = True
            self.belief, self.location = list(boot_pose), boot_loc
            if new_boot is not None and self.boot_id is not None:
                self.boot_id = new_boot
        self.sim.clock.at(self.sim.clock.t0 + off_at, off)
        self.sim.clock.at(self.sim.clock.t0 + off_at + 35, offline)
        self.sim.clock.at(self.sim.clock.t0 + on_at, on)

    def go(self, sec=240):
        self.sim.run(lambda: self.sim.clock.sleep(sec), grace=0.0)
        r = {
            "pending": self.sim.rt.is_initpose_pending(RID),
            "manual": sum(1 for _, k, _m in self.sim.events if k == "robot_initpose_manual_needed"),
            "seeds": self.sent,
            "triggers": self.triggers,
            "loc": self.location,
        }
        return r


SEED_PARK = [tuple(round(v, 3) for v in PARK)]


def _trig(n, seeds=None, pending=None, manual=None):
    def ok(r):
        good = r["triggers"] == n
        if seeds is not None:
            good &= r["seeds"] == seeds
        if pending is not None:
            good &= r["pending"] == pending
        if manual is not None:
            good &= r["manual"] == manual
        return good or f"trig={r['triggers']} seeds={r['seeds']} pending={r['pending']} manual={r['manual']}"
    return ok


@scenario("B1 [재현] 10-27 실제: 주차 장소에서 끄고 켬(NOS 재부팅), 부팅 위치 (0.57,-0.21) 상실 → 장소 좌표로 자동 확정",
          env=ON, expect=_trig(1, SEED_PARK, pending=False, manual=0))
def b1(sim):
    st = Stage(sim, true_pose=LAST_REAL, last_pose=LAST_REAL)
    st.power_cycle(10, 80, BOOT_REAL)
    return st.go()


@scenario("B2 통신만 40초 끊김, 재접속 직후 측위 상실 → NOS 는 그대로라 재부팅 아님(C 방안의 오인 경우)", env=ON,
          expect=_trig(0, []))
def b2(sim):
    st = Stage(sim, true_pose=LAST_REAL, last_pose=LAST_REAL)
    st.power_cycle(10, 60, BOOT_REAL, boot_loc=1, new_boot=None)
    return st.go()


@scenario("B3 주차 장소 아닌 곳에서 끄고 켬 → 감지하되 주입 없이 운영자 재조정", env=ON,
          expect=_trig(1, [], pending=True, manual=1))
def b3(sim):
    far = (5.0, 3.0, 0.0)
    st = Stage(sim, true_pose=far, last_pose=far)
    st.power_cycle(10, 80, BOOT_REAL)
    return st.go()


@scenario("B4 부팅 위치가 원점 그대로(0.05,0.02) → 지금처럼 감지·장소 좌표로 확정", env=ON,
          expect=_trig(1, SEED_PARK, pending=False))
def b4(sim):
    st = Stage(sim, true_pose=LAST_REAL, last_pose=LAST_REAL)
    st.power_cycle(10, 80, (0.05, 0.02, 0.0))
    return st.go()


@scenario("B5 receiver 만 재시작(관제 배포 등, 부팅 번호 같음) · 측위 정상 → 재부팅 아님", env=ON,
          expect=_trig(0, []))
def b5(sim):
    st = Stage(sim, true_pose=LAST_REAL, last_pose=LAST_REAL)
    st.power_cycle(10, 20, LAST_REAL, boot_loc=0, new_boot=None)
    return st.go()


@scenario("B6 구버전 receiver(부팅 번호 없음) 재부팅, 부팅 위치 원점 아님 → 지금과 같음(감지 안 됨)", env=ON,
          expect=_trig(0, []))
def b6(sim):
    st = Stage(sim, true_pose=LAST_REAL, last_pose=LAST_REAL)
    st.boot_id = None
    st.power_cycle(10, 80, BOOT_REAL)
    return st.go()


@scenario("B7 접속 유지 중 운행하다 측위 상실 → 재부팅 아님", env=ON, expect=_trig(0, []))
def b7(sim):
    st = Stage(sim, true_pose=LAST_REAL, last_pose=LAST_REAL)
    sim.clock.at(sim.clock.t0 + 30, lambda: (setattr(st, "belief", list(BOOT_REAL)),
                                            setattr(st, "location", 1)))
    return st.go()


@scenario("S1 주기 저장 · 측위 상실 좌표(부팅 직후 끌려간 값)는 마지막 위치로 저장 안 함",
          expect=lambda r: (r["persist_lost"] == 0 and r["persist_ok"] == 1)
          or f"lost={r['persist_lost']} ok={r['persist_ok']}")
def s1(sim):
    import app.robot_io.persistence as ps
    rt = sim.rt
    out = {}
    for key, loc in (("persist_lost", 1), ("persist_ok", 0)):
        with rt._lock:
            rt._runtime[RID] = {"robot_id": RID, "robot_name": "SIM", "robot_type": "",
                                "last_heartbeat": sim.clock.now(), "battery": {},
                                "position": {"x": 0.57, "y": -0.21, "yaw": 0.0, "location": loc,
                                             "timestamp": sim.clock.now()},
                                "current_floor_id": 3}
        rows = ps._snapshot_runtime()
        out[key] = rows[0]["PersistPosition"]
    return out


@scenario("S2 runtime · STATUS 의 BootId 가 basic_status 에 병합돼 남는다",
          expect=lambda r: r["boot_id"] == "boot-X" or f"boot_id={r['boot_id']}")
def s2(sim):
    rt = sim.rt
    with rt._lock:
        rt._runtime[RID] = {"robot_id": RID, "robot_name": "SIM", "robot_type": "", "battery": {},
                            "basic_status": {}, "charge_state": {"state": 0}, "last_heartbeat": 0,
                            "position": {}, "abnormal_codes": set()}
    rt.update_status(RID, {}, sim.clock.now(), basic_status={"Sleep": 0, "BootId": "boot-X"})
    with rt._lock:
        return {"boot_id": rt._runtime[RID]["basic_status"].get("BootId")}


def _backend_restart(st):
    """관제 재시작 = 감지 루프 메모리 초기화(DB 는 남는다)."""
    with st.aip._lock:
        st.aip._state.clear()


@scenario("B8 로봇이 꺼진 동안 관제 재시작 → 켜면 DB 의 부팅 번호와 달라 재부팅 감지·장소 좌표 확정", env=ON,
          expect=_trig(1, SEED_PARK, pending=False))
def b8(sim):
    st = Stage(sim, true_pose=LAST_REAL, last_pose=LAST_REAL)
    st.power_cycle(10, 80, BOOT_REAL)
    sim.clock.at(sim.clock.t0 + 50, lambda: _backend_restart(st))
    return st.go()


@scenario("B9 관제만 재시작(로봇은 계속 켜짐, 부팅 번호 같음) → 재부팅 아님", env=ON, expect=_trig(0, []))
def b9(sim):
    st = Stage(sim, true_pose=LAST_REAL, last_pose=LAST_REAL)
    sim.clock.at(sim.clock.t0 + 30, lambda: _backend_restart(st))
    return st.go()


@scenario("B10 관제 처음 설치(DB 에 부팅 번호 없음)·로봇 켜져 있음 → 기준값만 저장, 재부팅 아님", env=ON,
          expect=lambda r: (r["triggers"] == 0 and r["db_boot"] == "boot-A") or f"trig={r['triggers']} db={r['db_boot']}")
def b10(sim):
    st = Stage(sim, true_pose=LAST_REAL, last_pose=LAST_REAL)
    r = st.go(30)
    from app.database.database import SessionLocal
    from app.database.models import RobotLastStatus
    db = SessionLocal()
    try:
        row = db.query(RobotLastStatus).filter(RobotLastStatus.RobotId == RID).first()
        r["db_boot"] = getattr(row, "BootId", None) if row else None
    finally:
        db.close()
    return r


BOOT_REAL2 = (0.44, -0.29, -0.49)       # 10-27 2차 실기: 부팅 직후 Location=0 으로 보고된 좌표


@scenario("B11 [재현] 10-27 2차 실기: 부팅 직후 본체가 측위 전 Location=0 보고(0.44,-0.29) → 조용히 확정하지 않고 장소 좌표",
          env=ON, expect=_trig(1, SEED_PARK, pending=False))
def b11(sim):
    st = Stage(sim, true_pose=LAST_REAL, last_pose=LAST_REAL)
    st.power_cycle(10, 80, BOOT_REAL2, boot_loc=0)

    def lost_if_untouched():               # 측위가 뜨면 상실 보고 — 그 전에 주입됐으면 그대로
        if not st.sent:
            st.location = 1
    sim.clock.at(sim.clock.t0 + 101, lost_if_untouched)
    r = st.go()
    r["err_m"] = round(math.hypot(st.belief[0] - LAST_REAL[0], st.belief[1] - LAST_REAL[1]), 2)
    return r


@scenario("B12 10-27 실기 부팅 순서 묶음: Location=0 끌린 좌표 20초 → 지도 로드 중 주입 거부 5회 → 측위 상실 → 장소 좌표 확정",
          env=ON, expect=_trig(1, SEED_PARK, pending=False, manual=0))
def b12(sim):
    st = Stage(sim, true_pose=LAST_REAL, last_pose=LAST_REAL, boot_reject=5)
    st.power_cycle(10, 80, BOOT_REAL2, boot_loc=0)

    def lost_if_untouched():
        if not st.sent:
            st.location = 1
    sim.clock.at(sim.clock.t0 + 101, lost_if_untouched)
    r = st.go()
    r["err_m"] = round(math.hypot(st.belief[0] - LAST_REAL[0], st.belief[1] - LAST_REAL[1]), 2)
    return r
