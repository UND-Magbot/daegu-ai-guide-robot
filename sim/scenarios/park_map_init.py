"""맵 초기 위치를 주차 자리로(2026-10-27, B안) — 장소 이름·env 없이 도크 밖 재부팅 자동 위치.

변경(auto_init_pose._resolve_park_pose):
- 지금: 주차 자리 = env AUTO_INIT_POSE_PARK_PLACES 이름의 현재 맵 장소뿐. 장소 이름은 사업장 안에서
  유일해야 해서 새 맵마다 이름이 달라지고 env 를 고쳐야 한다.
- 변경: 이름 장소 중 맞는 게 없으면 현재 맵의 초기 위치(map_init_pose, 로봇별)를 같은 1m·30° 기준으로
  본다. 스위치 AUTO_INIT_POSE_PARK_MAP_INIT(기본 켬).

가짜로 둔 것(park_seed.py 와 같음, 실측 아님):
  - 부팅 직후 로봇은 원점·측위 상실(Location=1)을 보고한다.
  - 주입 좌표가 실제 자세에서 1m·30° 안이면 정합이 실제 자세로 끌어와 Location=0, 아니면 상실 유지.
값: 맵 47 은 운영 DB 실좌표(10-27), 새 맵 48 좌표는 임의.
"""
import math

from harness import scenario

RID, OTHER_RID = 10, 13
FLOOR = 3
M47, M48 = 47, 48
DOCK = (-0.932, 2.374, 1.5720)
PARK = (-0.83, 0.515, 1.5859)          # 맵 47 로봇충전소-1
INIT48 = (2.10, -1.40, 0.0)            # 새 맵 48 초기 위치(위치 재조정 창에서 저장했다고 가정)
NAMES = {"AUTO_INIT_POSE_PARK_PLACES": "로봇충전소-1"}   # 운영 .env 그대로
OFF = {**NAMES, "AUTO_INIT_POSE_PARK_MAP_INIT": "0"}


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def _r(p):
    return tuple(round(v, 3) for v in p)


class Stage:
    def __init__(self, sim, true_pose, last_pose, map_id=M48, init_poses=(), docked=False):
        """init_poses: [(map_id, robot_id, (x, y, yaw)), ...] map_init_pose 행."""
        import app.robot_control.auto_init_pose as aip
        from app.database.database import SessionLocal
        from app.database.models import LocationInfo, MapInitPose, RobotLastStatus
        sim.rewire_late_modules()
        self.sim, self.aip = sim, aip
        self.true = true_pose
        self.belief = [0.0, 0.0, 0.0]
        self.location = 1
        self.sent = []
        sim.robot.charging = docked

        db = SessionLocal()
        # 맵 47: 충전소+로봇충전소-1. 새 맵 48: 충전소 없음, 일반 장소 하나(이름은 겹칠 수 없어 다름)
        db.add_all([
            LocationInfo(LacationName="로봇충전소", Category="charge", MapId=M47, FloorId=FLOOR,
                         LocationX=DOCK[0], LocationY=DOCK[1], Yaw=DOCK[2]),
            LocationInfo(LacationName="로봇충전소-1", Category="waypoint", MapId=M47, FloorId=FLOOR,
                         LocationX=PARK[0], LocationY=PARK[1], Yaw=PARK[2]),
            LocationInfo(LacationName="1F_주차", Category="waypoint", MapId=M48, FloorId=FLOOR,
                         LocationX=INIT48[0], LocationY=INIT48[1], Yaw=INIT48[2]),
        ])
        for mid, rid, p in init_poses:
            db.add(MapInitPose(MapId=mid, RobotId=rid, PosX=p[0], PosY=p[1], Yaw=p[2]))
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
        seed = (pose["PosX"], pose["PosY"], pose["Yaw"])
        self.sent.append(_r(seed))
        d = math.hypot(seed[0] - self.true[0], seed[1] - self.true[1])
        if d <= 1.0 and abs(_wrap(seed[2] - self.true[2])) <= math.radians(30):
            self.belief, self.location = list(self.true), 0
        else:
            self.belief, self.location = list(seed), 1
        return "ok"

    def run(self):
        done = []
        # 재부팅 확인(부팅 번호 바뀜) 경로 — 10-27 실기와 같은 진입
        self.sim.run(lambda: (self.aip._inject_worker(RID, True), done.append(1)),
                     until=lambda: bool(done), grace=0.0)
        rt = self.sim.rt
        return {
            "pending": rt.is_initpose_pending(RID),
            "manual": sum(1 for _, k, _m in self.sim.events if k == "robot_initpose_manual_needed"),
            "seeds": self.sent,
            "sec": round(self.sim.clock.elapsed(), 1),
        }


def confirmed_with(seed):
    def chk(r):
        if r["pending"] or r["manual"]:
            return f"pending={r['pending']} manual={r['manual']}"
        if r["seeds"][:1] != [_r(seed)]:
            return f"첫 주입 {r['seeds'][:1]} ≠ {_r(seed)}"
        return True
    return chk


def manual(r):
    return (r["pending"] and r["manual"] == 1) or f"pending={r['pending']} manual={r['manual']}"


def _near(p, dx=0.0, dy=0.0, dyaw=0.0):
    return (p[0] + dx, p[1] + dy, p[2] + dyaw)


INIT48_ROW = [(M48, RID, INIT48)]


@scenario("M1 [변경 대상] 새 맵·이름 목록 비움·초기 위치 자리에서 끔(0.25m 흔들림) → 초기 위치 주입·확정",
          expect=confirmed_with(INIT48))
def m1(sim):
    return Stage(sim, INIT48, _near(INIT48, 0.2, -0.15, 0.1), init_poses=INIT48_ROW).run()


@scenario("M2 [변경 대상] 운영 .env 그대로(로봇충전소-1)·새 맵 → 이름 장소 없음 → 맵 초기 위치로 확정",
          env=NAMES, expect=confirmed_with(INIT48))
def m2(sim):
    return Stage(sim, INIT48, INIT48, init_poses=INIT48_ROW).run()


@scenario("M3 초기 위치에서 1.5m 떨어져 끔 → 운영자 재조정", env=NAMES, expect=manual)
def m3(sim):
    p = _near(INIT48, 1.5)
    return Stage(sim, p, p, init_poses=INIT48_ROW).run()


@scenario("M4 초기 위치 자리지만 방향 90° 다름 → 운영자 재조정", env=NAMES, expect=manual)
def m4(sim):
    p = _near(INIT48, dyaw=math.pi / 2)
    return Stage(sim, p, p, init_poses=INIT48_ROW).run()


@scenario("M5 초기 위치에서 끄고 꺼진 동안 4m 옮겨짐 → 주입·수렴 실패 → 운영자 재조정", env=NAMES, expect=manual)
def m5(sim):
    return Stage(sim, _near(INIT48, 4.0), INIT48, init_poses=INIT48_ROW).run()


@scenario("M6 새 맵 초기 위치 미등록(장소만 있음) → 운영자 재조정", env=NAMES, expect=manual)
def m6(sim):
    return Stage(sim, INIT48, INIT48).run()


@scenario("M7 다른 로봇(13)의 초기 위치만 있음 → 운영자 재조정", env=NAMES, expect=manual)
def m7(sim):
    return Stage(sim, INIT48, INIT48, init_poses=[(M48, OTHER_RID, INIT48)]).run()


@scenario("M8 다른 맵(47)의 초기 위치 좌표와 우연히 가까움 → 현재 맵 것만 봄 → 운영자 재조정", env=NAMES,
          expect=manual)
def m8(sim):
    return Stage(sim, PARK, PARK, init_poses=[(M47, RID, PARK)]).run()


@scenario("M9 스위치 끔(AUTO_INIT_POSE_PARK_MAP_INIT=0) → 지금과 같음(운영자 재조정)", env=OFF, expect=manual)
def m9(sim):
    return Stage(sim, INIT48, INIT48, init_poses=INIT48_ROW).run()


@scenario("M10 저장된 마지막 위치가 원점 → 운영자 재조정", env=NAMES, expect=manual)
def m10(sim):
    return Stage(sim, INIT48, (0.05, 0.02, 0.0), init_poses=INIT48_ROW).run()


@scenario("K1 회귀 · 맵 47 로봇충전소-1 자리(초기 위치는 다른 곳) → 이름 장소 우선",
          env=NAMES, expect=confirmed_with(PARK))
def k1(sim):
    return Stage(sim, PARK, _near(PARK, 0.1), map_id=M47, init_poses=[(M47, RID, DOCK)]).run()


@scenario("K2 [동작 변화] 맵 47 초기 위치=충전소, 도크 옆 0.5m 비충전으로 켬 → 충전소 좌표 주입·확정",
          env=NAMES, expect=confirmed_with(DOCK))
def k2(sim):
    p = _near(DOCK, 0.0, -0.5)
    return Stage(sim, p, p, map_id=M47, init_poses=[(M47, RID, DOCK)]).run()


@scenario("R1 회귀 · 도크 위 재부팅(맵 47) → 충전소 좌표로 확정", env=NAMES, expect=confirmed_with(DOCK))
def r1(sim):
    return Stage(sim, DOCK, PARK, map_id=M47, init_poses=[(M47, RID, PARK)], docked=True).run()


@scenario("R2 회귀 · 새 맵 도크 위 재부팅인데 충전소 없음 → 맵 초기 위치 폴백(기존 동작)", env=NAMES,
          expect=confirmed_with(INIT48))
def r2(sim):
    return Stage(sim, INIT48, INIT48, init_poses=INIT48_ROW, docked=True).run()
