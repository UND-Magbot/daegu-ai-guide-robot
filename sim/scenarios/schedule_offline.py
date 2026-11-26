"""작업 등록 — 로봇이 끊겨 있을 때 (현행 vs 변경, 10-22).

변경: ① 등록하자마자 실행될 작업(지금 실행·허용 지연 안의 지난 회차·운영 시간 안의 상시 대기)인데
      로봇이 끊겨 있으면 409 로 거절한다. 나중에 실행될 작업은 그대로 받는다.
      ② 실행 시각에 로봇이 끊겨 있으면 회차마다 한 번 "HH:MM까지 연결되면 실행합니다" 알림.
      허용 지연을 넘기면 기존처럼 '오프라인으로 스킵' 기록·알림.
무대는 standby_scenario_api.py 의 ApiStage(로봇 10 = SIM). 온라인 여부는 polling._was_online 으로 바꾼다.
"""
import importlib.util
import os
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "standby_scenario_api_base", os.path.join(os.path.dirname(__file__), "standby_scenario_api.py"))
_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api)
del REGISTRY[_n:]
ApiStage, ROBOT, USER = _api.ApiStage, _api.ROBOT, _api.USER


def _count(sim, kind):
    return sum(1 for _t, k, _m in sim.events if k == kind)


def _msgs(sim, kind):
    return [m for _t, k, m in sim.events if k == kind]


def _ok(r):
    return r["errors"] == 0 and not r.get("crash")


def _hm(dt):
    return f"{dt.hour:02d}:{dt.minute:02d}"


class OfflineStage(ApiStage):
    def __init__(self, sim, online=False):
        is_online = sim.lp._is_target_robot_online        # Stage 가 True 로 덮기 전 원본
        super().__init__(sim)
        self.lp._is_target_robot_online = is_online
        from app.robot_io import polling
        self.polling = polling
        self.set_online(online)

    def set_online(self, on):
        self.polling._was_online[ROBOT] = on

    def add(self, **over):
        """화면 '작업 등록' — 순회 작업(once/weekly) 또는 상시 대기."""
        body = dict(RobotName="SIM", TaskName="오프라인 시험", TaskType="patrol", WorkStatus="대기",
                    WayName="경로1", ScheduleMode="once")
        body.update(over)
        db = self.Session()
        try:
            res = self.sc.insert_schedule(self.sc.ScheduleInsertReq(**body), MagicMock(), db=db,
                                          current_user=USER)
            return {"status": 200, "id": res["id"]}
        except Exception as e:
            return {"status": getattr(e, "status_code", "예외"), "detail": str(getattr(e, "detail", e))}
        finally:
            db.close()

    def add_standby(self, start, end):
        return self.add(TaskType="standby", ScheduleMode="standby", WayName=None,
                        ActiveStartTime=start, ActiveEndTime=end, SeriesStartDate=date.today().isoformat(),
                        StandbyTrigger="button", ReturnPlaceId=1)

    def add_weekly(self, hm):
        return self.add(ScheduleMode="weekly", ExecutionTime=hm, RepeatDays="월,화,수,목,금,토,일",
                        SeriesStartDate=date.today().isoformat())

    # 스케줄러 한 틱(실행 판단·만료 기록)을 정해진 시각으로
    def tick(self, sid, now):
        db = self.Session()
        try:
            sched = db.query(self.M.ScheduleInfo).filter(self.M.ScheduleInfo.id == sid).first()
            grace = self.lp.runs.grace_minutes(sched)
            self.lp._record_expired(db, sched, now, grace)
            started = False
            if self.lp._active_schedule_id is None:
                started = self.lp._try_execute(db, sched, now, grace)
            return started
        finally:
            db.close()

    def run_results(self, sid):
        db = self.Session()
        try:
            return [r.Result for r in db.query(self.M.ScheduleRun).filter(self.M.ScheduleRun.ScheduleId == sid).all()]
        finally:
            db.close()


# ── ① 등록 ──────────────────────────────────────────

@scenario("R1 끊긴 로봇에 '지금 실행' 단일 작업 → 409 (현행: 등록됨, 실행 안 되고 10분 뒤 스킵)",
          expect=lambda r: _ok(r) and r["res"]["status"] == 409 and "연결되어 있지 않아" in r["res"]["detail"])
def r1(sim):
    st = OfflineStage(sim)
    return {"res": st.add(StartTime=datetime.now())}


@scenario("R2 연결된 로봇에 '지금 실행' 단일 작업 → 등록됨(회귀)",
          expect=lambda r: _ok(r) and r["res"]["status"] == 200)
def r2(sim):
    st = OfflineStage(sim, online=True)
    return {"res": st.add(StartTime=datetime.now())}


@scenario("R3 끊긴 로봇에 2시간 뒤 단일 작업 → 등록됨",
          expect=lambda r: _ok(r) and r["res"]["status"] == 200)
def r3(sim):
    st = OfflineStage(sim)
    return {"res": st.add(StartTime=datetime.now() + timedelta(hours=2))}


@scenario("R4 끊긴 로봇에 5분 전 시각 반복 작업(허용 지연 안이라 곧바로 실행됨) → 409",
          expect=lambda r: _ok(r) and r["res"]["status"] == 409)
def r4(sim):
    st = OfflineStage(sim)
    return {"res": st.add_weekly(_hm(datetime.now() - timedelta(minutes=5)))}


@scenario("R5 끊긴 로봇에 30분 전 시각 반복 작업(오늘 회차는 이미 지남) → 등록됨",
          expect=lambda r: _ok(r) and r["res"]["status"] == 200)
def r5(sim):
    st = OfflineStage(sim)
    return {"res": st.add_weekly(_hm(datetime.now() - timedelta(minutes=30)))}


@scenario("R6 끊긴 로봇에 운영 시간 안의 상시 대기 → 409 / 운영 시간이 나중이면 등록됨",
          expect=lambda r: _ok(r) and r["now"]["status"] == 409 and r["later"]["status"] == 200)
def r6(sim):
    st = OfflineStage(sim)
    now = datetime.now()
    return {"now": st.add_standby("00:00", "23:59"),
            "later": st.add_standby(_hm(now + timedelta(hours=1)), _hm(now + timedelta(hours=2)))}


# ── ② 실행 시각 ──────────────────────────────────────

PLANNED_HM = "23:30"


def _planned():
    return datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=23, minutes=30)


@scenario("R7 실행 시각에 끊김 → 연결 대기 알림 1건(23:40까지) → 10분 넘으면 스킵 기록·알림",
          expect=lambda r: _ok(r) and r["wait"] == 1 and "23:40까지 연결되면 실행합니다" in r["wait_msg"]
          and r["skip"] == 1 and r["results"] == ["skipped_offline"] and not r["started"])
def r7(sim):
    st = OfflineStage(sim)
    st.lp._watch_since = _planned() - timedelta(hours=1)
    sid = st.add_weekly(PLANNED_HM)["id"]
    p = _planned()
    started = any(st.tick(sid, p + timedelta(seconds=s)) for s in (10, 40, 70, 300, 590))
    st.tick(sid, p + timedelta(minutes=10, seconds=20))
    msgs = _msgs(sim, "schedule_wait_offline")
    return {"wait": len(msgs), "wait_msg": msgs[0] if msgs else "", "skip": _count(sim, "schedule_skip_offline"),
            "results": st.run_results(sid), "started": started}


@scenario("R8 실행 시각에 끊김 → 알림 1건 → 3분 뒤 연결되면 그 회차 실행(회귀: 늦게라도 실행)",
          expect=lambda r: _ok(r) and r["wait"] == 1 and r["started"] and r["skip"] == 0)
def r8(sim):
    st = OfflineStage(sim)
    st.lp._watch_since = _planned() - timedelta(hours=1)
    st.lp.execute_schedule = lambda s: (True, "")
    sid = st.add_weekly(PLANNED_HM)["id"]
    p = _planned()
    st.tick(sid, p + timedelta(seconds=10))
    st.tick(sid, p + timedelta(seconds=40))
    st.set_online(True)
    started = st.tick(sid, p + timedelta(minutes=3))
    return {"wait": _count(sim, "schedule_wait_offline"), "started": started,
            "skip": _count(sim, "schedule_skip_offline")}


@scenario("R9 연결된 로봇은 실행 시각 알림 없이 바로 실행(회귀)",
          expect=lambda r: _ok(r) and r["wait"] == 0 and r["started"])
def r9(sim):
    st = OfflineStage(sim, online=True)
    st.lp._watch_since = _planned() - timedelta(hours=1)
    st.lp.execute_schedule = lambda s: (True, "")
    sid = st.add_weekly(PLANNED_HM)["id"]
    started = st.tick(sid, _planned() + timedelta(seconds=10))
    return {"wait": _count(sim, "schedule_wait_offline"), "started": started}
