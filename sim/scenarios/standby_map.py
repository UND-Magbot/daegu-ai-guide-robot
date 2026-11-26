"""상시 대기 시작 전 맵 검사 (현행 vs 변경).

로봇은 스스로 층을 바꾸지 못한다. 대기 장소가 다른 층(맵)인데 근무를 시작하면
그 좌표를 지금 층 지도에서 찾아간다. 변경: 시작을 보류하고 한 번 알리고, 로봇을 옮기면 시작한다.
무대는 standby_scenario.py 의 Stage(로봇 10 = 1F 맵 1, 로비 대기 (6,0)) + 3F 맵 3 의 '3F 로봇대기장소' (2,9).
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "standby_scenario_base_map", os.path.join(os.path.dirname(__file__), "standby_scenario.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
Stage, ROBOT, _no_err = _base.Stage, _base.ROBOT, _base._no_err

UPSTAIRS = (2.0, 9.0)


class MapStage(Stage):
    def __init__(self, sim, place_map=3, robot_map=1):
        super().__init__(sim)
        M = self.M
        db = self.Session()
        try:
            db.add(M.RobotMapInfo(id=1, BusinessId=1, FloorId=1, MapName="UND_1F"))
            db.add(M.RobotMapInfo(id=3, BusinessId=1, FloorId=2, MapName="UND_3F"))
            db.add(M.LocationInfo(id=3, RobotName="SIM", LacationName="3F 로봇대기장소", FloorId=2, MapId=3,
                                  LocationX=UPSTAIRS[0], LocationY=UPSTAIRS[1], Yaw=0.0, Category="work"))
            sched = db.query(M.ScheduleInfo).filter(M.ScheduleInfo.id == 1).first()
            sched.ReturnPlaceId = 3 if place_map == 3 else 1
            db.query(M.RobotInfo).filter(M.RobotInfo.id == ROBOT).update({M.RobotInfo.CurrentMapId: robot_map})
            db.commit()
        finally:
            db.close()
        # 하네스 log_event 는 본문만 남긴다 — 이 알림의 상세(detail)까지 받아 둔다
        lp = self.lp
        inner = lp.log_event
        self.details = []

        def rec(*a, **k):
            if len(a) > 1 and a[1] == "standby_map_mismatch":
                self.details.append(k.get("detail"))
            return inner(*a, **k)
        lp.log_event = rec

    def start_shift(self):
        """실제 스케줄러처럼 같은 근무 시간대(시작 시각 고정)로 다시 시도한다."""
        from datetime import datetime, timedelta
        if not hasattr(self, "_window"):
            now = datetime.now()
            self._window = (now - timedelta(hours=1), now + timedelta(hours=8))
        db = self.Session()
        try:
            sched = db.query(self.M.ScheduleInfo).filter(self.M.ScheduleInfo.id == 1).first()
            return self.lp._enter_standby(db, sched, datetime.now(), self._window)
        finally:
            db.close()

    def move_robot_to(self, map_id):
        db = self.Session()
        try:
            db.query(self.M.RobotInfo).filter(self.M.RobotInfo.id == ROBOT).update(
                {self.M.RobotInfo.CurrentMapId: map_id})
            db.commit()
        finally:
            db.close()

    def holds(self):
        return [m for _t, k, m in self.sim.events if k == "standby_map_mismatch"]

    def res(self, started, **extra):
        return self.out(started=bool(started), phase=self.phase(), holds=self.holds(),
                        sent=[(round(c.get("x", 0), 1), round(c.get("y", 0), 1)) for c in self.sim.robot.nav_targets()]
                        if hasattr(self.sim.robot, "nav_targets") else None,
                        navigating=self.sm.is_navigating, **extra)


@scenario("M1 대기 장소 3F·로봇 1F → 시작 보류, 로봇 안 움직임, 알림에 두 층 이름 (현행: 1F 지도에서 3F 좌표로 출발)",
          expect=lambda r: _no_err(r) and not r["started"] and r["phase"] is None and not r["navigating"]
          and len(r["holds"]) == 1 and "3F" in r["holds"][0] and "1F" in r["holds"][0])
def m1(sim):
    st = MapStage(sim)
    return st.res(st.start_shift())


@scenario("M6 보류 알림 상세 = 화면 팝업이 읽는 JSON(작업·대기 장소·두 층)",
          expect=lambda r: _no_err(r) and r["detail"] == {
              "scheduleId": 1, "workName": "로비 상시 대기",
              "place": "3F 로봇대기장소", "placeFloor": "3F", "robotFloor": "1F"})
def m6(sim):
    import json
    st = MapStage(sim)
    st.start_shift()
    try:
        detail = json.loads(st.details[-1]) if st.details else None
    except ValueError:
        detail = st.details[-1]
    return st.res(False, detail=detail)


@scenario("M2 보류 중 여러 번 다시 시도해도 알림은 한 번",
          expect=lambda r: _no_err(r) and not r["started"] and len(r["holds"]) == 1)
def m2(sim):
    st = MapStage(sim)
    for _ in range(4):
        started = st.start_shift()
        sim.wait(5)
    return st.res(started)


@scenario("M3 보류 뒤 로봇을 3F 로 옮기면 바로 시작해 3F 대기 장소로",
          expect=lambda r: _no_err(r) and r["started"] and r["phase"] == "moving" and r["navigating"])
def m3(sim):
    st = MapStage(sim)
    st.start_shift()
    st.move_robot_to(3)
    return st.res(st.start_shift())


@scenario("M4 같은 층(1F 대기 장소·로봇 1F) → 그대로 시작 (회귀)",
          expect=lambda r: _no_err(r) and r["started"] and r["phase"] == "moving" and not r["holds"])
def m4(sim):
    st = MapStage(sim, place_map=1)
    return st.res(st.start_shift())


@scenario("M5 로봇 맵을 모르면 판단하지 않고 예전처럼 시작 (회귀)",
          expect=lambda r: _no_err(r) and r["started"] and not r["holds"])
def m5(sim):
    st = MapStage(sim, robot_map=None)
    return st.res(st.start_shift())
