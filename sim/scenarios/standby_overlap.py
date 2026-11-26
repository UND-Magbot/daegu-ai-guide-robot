"""상시 대기 근무끼리 시간대 겹침 — 등록·수정에서 막기 (현행 vs 변경, 11-05).

변경: 같은 로봇의 상시 대기 근무끼리 운영 시간이 겹치면 층과 관계없이 409 로 막는다.
      요일·근무 기간·예외일·자정 넘김은 스케줄러(standby_window)와 같은 판정으로 본다.
      같은 시리즈(날짜를 나눠 가진 같은 근무)·취소된 근무·다른 로봇은 비교하지 않는다.
      예약 작업(once/weekly/interval)과의 겹침은 예전처럼 막지 않고 알린다(S8).
현행: 상시 대기끼리는 검사가 없어 다 등록된다(schedule.py insert_schedule standby 갈래).
무대는 standby_scenario_api.py 의 ApiStage(로봇 10 = SIM, 1F 대기 id=1 '로비 대기', 3F 대기 id=3 '3층 대기').
"""
import importlib.util
import os
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "standby_scenario_api_base_overlap", os.path.join(os.path.dirname(__file__), "standby_scenario_api.py"))
_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api)
del REGISTRY[_n:]
ApiStage, ROBOT, USER = _api.ApiStage, _api.ROBOT, _api.USER

TODAY = date.today()
TOMORROW = TODAY + timedelta(days=1)
DAYS = ["월", "화", "수", "목", "금", "토", "일"]


def _ok(r):
    return r["errors"] == 0 and not r.get("crash")


class OverlapStage(ApiStage):
    def add(self, start, end, place=1, name="근무", **over):
        """화면 '작업 등록' — 상시 대기."""
        body = dict(RobotName="SIM", TaskName=name, TaskType="standby", WorkStatus="대기",
                    ScheduleMode="standby", ActiveStartTime=start, ActiveEndTime=end,
                    SeriesStartDate=TOMORROW.isoformat(), StandbyTrigger="button", ReturnPlaceId=place)
        body.update(over)
        db = self.Session()
        try:
            res = self.sc.insert_schedule(self.sc.ScheduleInsertReq(**body), MagicMock(), db=db,
                                          current_user=USER)
            return {"status": 200, "id": res["id"], "warnings": res.get("warnings", [])}
        except Exception as e:
            return {"status": getattr(e, "status_code", "예외"), "detail": str(getattr(e, "detail", e))}
        finally:
            db.close()

    def edit(self, sid, **fields):
        db = self.Session()
        try:
            res = self.sc.update_schedule(sid, self.sc.ScheduleUpdateReq(**fields), MagicMock(), db=db,
                                          current_user=USER)
            return {"status": 200, "created": res.get("created_id")}
        except Exception as e:
            return {"status": getattr(e, "status_code", "예외"), "detail": str(getattr(e, "detail", e))}
        finally:
            db.close()

    def put_row(self, **kw):
        """DB 에 근무를 직접 넣는다(다른 로봇·취소된 근무·예외일 근무 재현용)."""
        M = self.M
        base = dict(RobotName="SIM", WorkName="기존", TaskType="standby", TaskStatus="대기",
                    ScheduleMode="standby", Repeat="Y", ActiveStartTime="09:00", ActiveEndTime="12:00",
                    SeriesStartDate=TOMORROW, ReturnPolicy="place", ReturnPlaceId=1,
                    StartDate=datetime.combine(TOMORROW, datetime.min.time()) + timedelta(hours=9),
                    SeriesGroupId=str(uuid.uuid4()))
        base.update(kw)
        base["EndDate"] = base["StartDate"]
        db = self.Session()
        try:
            row = M.ScheduleInfo(**base)
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()

    def row(self, sid):
        db = self.Session()
        try:
            r = db.query(self.M.ScheduleInfo).filter(self.M.ScheduleInfo.id == sid).first()
            return None if r is None else {"start": r.ActiveStartTime, "end": r.ActiveEndTime}
        finally:
            db.close()

    def standby_count(self):
        db = self.Session()
        try:
            return db.query(self.M.ScheduleInfo).filter(self.M.ScheduleInfo.ScheduleMode == "standby").count()
        finally:
            db.close()


def _blocked(res, *words):
    return res["status"] == 409 and all(w in res.get("detail", "") for w in words)


# ── 등록 ──────────────────────────────────────────────

@scenario("O1 [재현] 같은 층(1F) 근무 09~12 와 10~11 → 두 번째 409, 문구에 시간·대기 장소·근무명 (현행: 등록됨)",
          expect=lambda r: _ok(r) and r["a"]["status"] == 200
          and _blocked(r["b"], "09:00~12:00", "로비 대기", "1F 오전", "시간대가 겹칩니다") and r["n"] == 1)
def o1(sim):
    st = OverlapStage(sim)
    a = st.add("09:00", "12:00", name="1F 오전")
    b = st.add("10:00", "11:00", name="1F 중간")
    return {"a": a, "b": b, "n": st.standby_count()}


@scenario("O2 [재현] 다른 층(1F·3F) 근무가 겹쳐도 409 (현행: 등록됨)",
          expect=lambda r: _ok(r) and _blocked(r["b"], "로비 대기", "1F 근무"))
def o2(sim):
    st = OverlapStage(sim)
    st.add("10:00", "10:40", place=1, name="1F 근무")
    return {"b": st.add("10:30", "11:30", place=3, name="3F 근무")}


@scenario("O3 끝·시작이 맞닿음(10:00~10:40 / 10:40~11:30) → 둘 다 등록 (회귀)",
          expect=lambda r: _ok(r) and r["a"]["status"] == 200 and r["b"]["status"] == 200)
def o3(sim):
    st = OverlapStage(sim)
    return {"a": st.add("10:00", "10:40", place=1), "b": st.add("10:40", "11:30", place=3)}


@scenario("O4 요일이 다름(월·수 / 화·목) → 등록 (회귀)",
          expect=lambda r: _ok(r) and r["b"]["status"] == 200)
def o4(sim):
    st = OverlapStage(sim)
    st.add("09:00", "18:00", RepeatDays="월,수")
    return {"b": st.add("09:00", "18:00", RepeatDays="화,목")}


@scenario("O5 근무 기간이 다름(내일 하루 / 모레부터) → 등록 (회귀)",
          expect=lambda r: _ok(r) and r["b"]["status"] == 200)
def o5(sim):
    st = OverlapStage(sim)
    st.add("09:00", "18:00", SeriesEndDate=TOMORROW.isoformat())
    return {"b": st.add("09:00", "18:00", SeriesStartDate=(TOMORROW + timedelta(days=1)).isoformat())}


@scenario("O6 다른 로봇 근무와 겹침 → 등록 (회귀)",
          expect=lambda r: _ok(r) and r["b"]["status"] == 200)
def o6(sim):
    st = OverlapStage(sim)
    st.put_row(RobotName="OTHER")
    return {"b": st.add("09:00", "12:00")}


@scenario("O7 자정 넘김 근무 22:00~06:00 과 다음 날 05:00~07:00 → 409",
          expect=lambda r: _ok(r) and _blocked(r["b"], "22:00~06:00"))
def o7(sim):
    st = OverlapStage(sim)
    st.add("22:00", "06:00", name="야간")
    return {"b": st.add("05:00", "07:00", SeriesStartDate=(TOMORROW + timedelta(days=1)).isoformat())}


@scenario("O8 기존 근무가 그날을 예외일로 뺐으면(운영 DB 185·187 모양) → 등록",
          expect=lambda r: _ok(r) and r["b"]["status"] == 200)
def o8(sim):
    st = OverlapStage(sim)
    st.put_row(SeriesEndDate=TOMORROW, SeriesExceptions=TOMORROW.isoformat())
    return {"b": st.add("10:28", "12:00", SeriesEndDate=TOMORROW.isoformat())}


@scenario("O9 취소된 근무와 겹침 → 등록 (회귀)",
          expect=lambda r: _ok(r) and r["b"]["status"] == 200)
def o9(sim):
    st = OverlapStage(sim)
    st.put_row(TaskStatus="취소")
    return {"b": st.add("09:00", "12:00")}


@scenario("O10 예약 작업(매주 10:00)과 겹침 → 막지 않고 알림 (회귀, S8)",
          expect=lambda r: _ok(r) and r["b"]["status"] == 200 and any("양보" in w for w in r["b"]["warnings"]))
def o10(sim):
    st = OverlapStage(sim)
    db = st.Session()
    db.add(st.M.ScheduleInfo(RobotName="SIM", WorkName="순찰", TaskType="patrol", TaskStatus="대기",
                             ScheduleMode="weekly", Repeat="Y", Repeat_Day=",".join(DAYS), ExecutionTime="10:00",
                             SeriesStartDate=TODAY, WayName="경로1",
                             StartDate=datetime.combine(TODAY, datetime.min.time()) + timedelta(hours=10)))
    db.commit()
    db.close()
    return {"b": st.add("09:00", "12:00")}


# ── 수정 ──────────────────────────────────────────────

@scenario("O11 [재현] 수정(전체)으로 시간을 옮겨 다른 근무와 겹치게 → 409, 값 그대로 (현행: 저장됨)",
          expect=lambda r: _ok(r) and _blocked(r["res"], "1F 오전") and r["row"] == {"start": "13:00", "end": "15:00"})
def o11(sim):
    st = OverlapStage(sim)
    st.add("09:00", "12:00", name="1F 오전")
    b = st.add("13:00", "15:00", place=3, name="3F 오후")["id"]
    res = st.edit(b, ActiveStartTime="11:00", ActiveEndTime="15:00", RepeatScope="all")
    return {"res": res, "row": st.row(b)}


@scenario("O12 수정(전체)으로 겹치지 않는 시간으로 → 저장 (회귀)",
          expect=lambda r: _ok(r) and r["res"]["status"] == 200 and r["row"] == {"start": "12:00", "end": "16:00"})
def o12(sim):
    st = OverlapStage(sim)
    st.add("09:00", "12:00")
    b = st.add("13:00", "15:00", place=3)["id"]
    return {"res": st.edit(b, ActiveStartTime="12:00", ActiveEndTime="16:00", RepeatScope="all"), "row": st.row(b)}


@scenario("O13 자기 근무 시간만 늘림(다른 근무 없음) → 저장 (회귀: 자기 자신과 비교 안 함)",
          expect=lambda r: _ok(r) and r["res"]["status"] == 200)
def o13(sim):
    st = OverlapStage(sim)
    a = st.add("09:00", "12:00")["id"]
    return {"res": st.edit(a, ActiveEndTime="13:00", RepeatScope="all")}


@scenario("O14 [재현] '이날만' 수정으로 그날 시간이 다른 근무와 겹치게 → 409 (현행: 저장됨)",
          expect=lambda r: _ok(r) and _blocked(r["res"], "1F 오전"))
def o14(sim):
    st = OverlapStage(sim)
    st.add("09:00", "12:00", name="1F 오전")
    b = st.add("13:00", "15:00", place=3)["id"]
    return {"res": st.edit(b, ActiveStartTime="10:00", RepeatScope="this", TargetDate=TOMORROW.isoformat())}


@scenario("O15 이름만 수정 → 겹침 검사 안 함, 저장 (회귀)",
          expect=lambda r: _ok(r) and r["res"]["status"] == 200)
def o15(sim):
    st = OverlapStage(sim)
    a = st.add("09:00", "12:00")["id"]
    st.put_row(ActiveStartTime="10:00", ActiveEndTime="11:00")   # 예전에 겹치게 들어간 근무
    return {"res": st.edit(a, WorkName="새 이름", RepeatScope="all")}


# ── 실행: 앞 근무 시간이 끝나면 로봇을 놓아주는가 ─────────────────

def _set_map(st, mid, floor_id):
    db = st.Session()
    try:
        r = db.query(st.M.RobotInfo).filter(st.M.RobotInfo.id == ROBOT).first()
        r.CurrentMapId, r.CurrentFloorId = mid, floor_id
        db.commit()
    finally:
        db.close()


@scenario("W1 1F 근무 중 로봇을 3F 로 옮김 → 1F 시간 안에는 3F 근무 못 잡음 → 1F 끝나는 시각에 놓아주고 3F 근무 시작",
          expect=lambda r: _ok(r) and r["away"] == "away" and not r["early"] and r["late"]
          and r["sid"] == r["sid3"])
def w1(sim):
    st = OverlapStage(sim)
    st.arrive_standby()                                   # 1F 근무(id 1) 대기 장소 도착
    now = datetime.now()
    with st.lp._lock:
        st.lp._standby["ends_at"] = now + timedelta(minutes=10)
    _set_map(st, 2, 2)                                    # 직원이 3F 로 데려감
    st.lp._return_to_post(dict(st.lp.get_standby_state()))
    away = st.phase()
    sid3 = st.put_row(WorkName="3F 근무", ReturnPlaceId=3, SeriesStartDate=TODAY,
                      ActiveStartTime="00:00", ActiveEndTime="23:59")
    win = (now, now + timedelta(hours=2))

    def enter(at):
        # 스케줄러 루프와 같은 문: 다른 작업·근무가 로봇을 쓰는 동안은 상시 대기에 들어가지 않는다(loop.py:1492)
        if st.lp._active_schedule_id is not None:
            return False
        db = st.Session()
        try:
            sched = db.query(st.M.ScheduleInfo).filter(st.M.ScheduleInfo.id == sid3).first()
            return bool(st.lp._enter_standby(db, sched, at, win))
        finally:
            db.close()
    early = enter(now + timedelta(minutes=5))
    end_at = now + timedelta(minutes=10, seconds=1)
    st.lp._standby_watchdog(end_at)                       # 스케줄러 틱: 근무 감시가 먼저 돈다
    late = enter(end_at)
    state = st.lp.get_standby_state() or {}
    return {"away": away, "early": early, "late": late, "sid": state.get("schedule_id"), "sid3": sid3}
