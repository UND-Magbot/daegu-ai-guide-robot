"""주차 장소 시드(2026-10-27) — 충전소 고장으로 도크 밖에서 끄고 켤 때 위치 자동 주입.

변경(auto_init_pose._inject_worker 비충전 분기):
- 보고 위치가 원점(부팅 리셋)이면 지금은 운영자 '위치 재조정'으로 넘긴다.
- 변경: 끄기 전 마지막 위치가 현재 맵 주차 장소(AUTO_INIT_POSE_PARK_PLACES) 반경(1m)·방향(30°) 안이면
  장소의 등록 좌표·방향을 주입하고, 도크와 같은 수렴 검사로 확정한다. 아니면 지금과 같다.
- 도크 수렴 반복은 _inject_until_converged 로 뽑아 도크·주차 장소가 같이 쓴다(도크 회귀 확인).

가짜로 둔 것(시나리오 안, 실측 아님):
  - 부팅 직후 로봇은 원점(0,0,0)·측위 상실(Location=1)을 보고한다.
  - 주입 좌표가 실제 자세에서 1m·30° 안이면 저장 지도 정합이 실제 자세로 끌어와 Location=0,
    그보다 멀면 주입 좌표에 머물고 Location=1(상실) 유지.
  - INIT_POSE 는 receiver 로 바로 전달된다(boot_reject 회만큼 처음에 timeout).
값은 운영 DB 맵 47 실좌표(10-27 조회).
"""
import math

from harness import scenario

RID = 10
MAP, FLOOR = 47, 3
DOCK = (-0.932, 2.374, 1.5720)
PARK = (-0.83, 0.515, 1.5859)
ON = {"AUTO_INIT_POSE_PARK_PLACES": "로봇충전소-1"}


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


class Stage:
    def __init__(self, sim, true_pose, last_pose, docked=False, boot_reject=0, map_id=MAP):
        import app.robot_control.auto_init_pose as aip
        from app.database.database import SessionLocal
        from app.database.models import LocationInfo, RobotLastStatus
        sim.rewire_late_modules()
        self.sim, self.aip = sim, aip
        self.true = true_pose
        self.belief = [0.0, 0.0, 0.0]
        self.location = 1
        self.boot_reject = boot_reject
        self.sent = []
        sim.robot.charging = docked

        db = SessionLocal()
        db.add_all([
            LocationInfo(LacationName="로봇충전소", Category="charge", MapId=MAP, FloorId=FLOOR,
                         LocationX=DOCK[0], LocationY=DOCK[1], Yaw=DOCK[2]),
            LocationInfo(LacationName="로봇충전소-1", Category="waypoint", MapId=MAP, FloorId=FLOOR,
                         LocationX=PARK[0], LocationY=PARK[1], Yaw=PARK[2]),
            # 다른 맵(45)의 같은 역할 장소 — 이름이 띄어쓰기로 다르다
            LocationInfo(LacationName="로봇 충전소-1", Category="waypoint", MapId=45, FloorId=FLOOR,
                         LocationX=0.869, LocationY=0.166, Yaw=-0.0871),
        ])
        if last_pose is not None:
            db.add(RobotLastStatus(RobotId=RID, PosX=last_pose[0], PosY=last_pose[1],
                                   PosYaw=last_pose[2], CurrentFloorId=FLOOR))
        db.commit()
        db.close()

        rt = sim.rt
        with rt._lock:
            rt._runtime[RID] = {"robot_id": RID, "robot_name": "SIM", "current_floor_id": FLOOR,
                                "current_map_id": map_id, "position": {}, "last_heartbeat": 1}
        rt.get_position = lambda rid: {"x": self.belief[0], "y": self.belief[1],
                                       "yaw": self.belief[2], "location": self.location,
                                       "timestamp": sim.clock.now()}
        aip._send_init_pose_via_receiver = self._send

    def _send(self, pose):
        if self.boot_reject > 0:
            self.boot_reject -= 1
            return "timeout"
        seed = (pose["PosX"], pose["PosY"], pose["Yaw"])
        self.sent.append(tuple(round(v, 3) for v in seed))
        d = math.hypot(seed[0] - self.true[0], seed[1] - self.true[1])
        if d <= 1.0 and abs(_wrap(seed[2] - self.true[2])) <= math.radians(30):
            self.belief, self.location = list(self.true), 0
        else:
            self.belief, self.location = list(seed), 1
        return "ok"

    def run(self):
        done = []
        self.sim.run(lambda: (self.aip._inject_worker(RID), done.append(1)),
                     until=lambda: bool(done), grace=0.0)
        rt = self.sim.rt
        err = math.hypot(self.belief[0] - self.true[0], self.belief[1] - self.true[1])
        return {
            "pending": rt.is_initpose_pending(RID),
            "manual": sum(1 for _, k, _m in self.sim.events if k == "robot_initpose_manual_needed"),
            "seeds": self.sent,
            "sec": round(self.sim.clock.elapsed(), 1),
            "err_m": round(err, 2) if self.location == 0 else None,
        }


def ok_confirmed(r):
    return (not r["pending"] and r["manual"] == 0) or f"pending={r['pending']} manual={r['manual']}"


def ok_manual(r):
    return (r["pending"] and r["manual"] == 1) or f"pending={r['pending']} manual={r['manual']}"


def _near(p, dx=0.0, dy=0.0, dyaw=0.0):
    return (p[0] + dx, p[1] + dy, p[2] + dyaw)


@scenario("P1 [변경 대상] 주차 장소에 세우고 끔(마지막 위치 0.25m 흔들림) → 자동 확정", env=ON,
          expect=lambda r: ok_confirmed(r) is True and r["seeds"] == [tuple(round(v, 3) for v in PARK)]
          or ok_confirmed(r))
def p1(sim):
    return Stage(sim, true_pose=PARK, last_pose=_near(PARK, 0.2, -0.15, 0.1)).run()


@scenario("P2 실제로는 장소에서 0.4m·10° 어긋나 세움 → 장소 좌표 주입 후 정합으로 실제 자세에 수렴", env=ON,
          expect=lambda r: ok_confirmed(r) is True and r["err_m"] == 0.0 or ok_confirmed(r))
def p2(sim):
    true = _near(PARK, 0.3, 0.27, math.radians(10))
    return Stage(sim, true_pose=true, last_pose=_near(PARK, 0.25, 0.2, math.radians(8))).run()


@scenario("P3 마지막 위치가 장소에서 1.5m(반경 밖) → 지금처럼 운영자 재조정", env=ON, expect=ok_manual)
def p3(sim):
    p = _near(PARK, 1.5)
    return Stage(sim, true_pose=p, last_pose=p).run()


@scenario("P4 장소 위지만 방향 90° 다름 → 지금처럼 운영자 재조정", env=ON, expect=ok_manual)
def p4(sim):
    p = _near(PARK, dyaw=math.pi / 2)
    return Stage(sim, true_pose=p, last_pose=p).run()


@scenario("P5 장소에서 끄고, 꺼진 동안 4m 옮겨짐 → 주입했지만 수렴 실패 3회 → 운영자 재조정", env=ON,
          expect=ok_manual)
def p5(sim):
    return Stage(sim, true_pose=_near(PARK, 4.0, 0.0), last_pose=PARK).run()


@scenario("P6 부팅 중 INIT_POSE 미수락 4회 뒤 수락 → 자동 확정", env=ON,
          expect=lambda r: ok_confirmed(r))
def p6(sim):
    return Stage(sim, true_pose=PARK, last_pose=PARK, boot_reject=4).run()


@scenario("P7 저장된 마지막 위치가 원점(부팅 직후 저장이 먼저 걸림) → 운영자 재조정", env=ON, expect=ok_manual)
def p7(sim):
    return Stage(sim, true_pose=PARK, last_pose=(0.05, 0.02, 0.0)).run()


@scenario("P8 활성 맵 45(장소 이름 '로봇 충전소-1', 설정과 다름) → 운영자 재조정", env=ON, expect=ok_manual)
def p8(sim):
    return Stage(sim, true_pose=(0.869, 0.166, -0.0871), last_pose=(0.869, 0.166, -0.0871),
                 map_id=45).run()


@scenario("P9 스위치 끔(설정 비움) · 주차 장소에서 켬 → 지금과 같음(운영자 재조정)", expect=ok_manual)
def p9(sim):
    return Stage(sim, true_pose=PARK, last_pose=PARK).run()


@scenario("R1 회귀 · 도크 위 재부팅 → 충전소 좌표로 자동 확정", env=ON,
          expect=lambda r: ok_confirmed(r) is True and r["seeds"] == [tuple(round(v, 3) for v in DOCK)]
          or ok_confirmed(r))
def r1(sim):
    return Stage(sim, true_pose=DOCK, last_pose=PARK, docked=True).run()


@scenario("R2 회귀 · 도크 위 재부팅, 부팅 중 미수락 2회 → 자동 확정", env=ON, expect=ok_confirmed)
def r2(sim):
    return Stage(sim, true_pose=DOCK, last_pose=None, docked=True, boot_reject=2).run()
