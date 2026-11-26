"""대시보드 작업 통계·작업 일정에 안내 시나리오 세션을 '길 안내' 작업으로 합치기 — 현행 vs 변경.

로봇 이동은 없다. 임시 SQLite 에 일정·세션을 넣고 통계 서비스(StatisticsService)와
작업 일정 API 함수(/DB/schedule/guide-sessions)를 직접 부른다.
"""
from datetime import datetime, timedelta

from harness import scenario


def _stage(sessions=(), schedules=()):
    from app.database.database import SessionLocal
    from app.database import models as M

    db = SessionLocal()
    db.add(M.RobotInfo(id=10, RobotName="SIM", BusinessId=1, RobotType="QUADRUPED"))
    db.add(M.RobotInfo(id=11, RobotName="OTHER", BusinessId=2, RobotType="QUADRUPED"))
    db.add(M.VoiceScenario(id=1, RobotId=10, Code="hospital", DisplayName="병원 안내",
                           MapId=1, FloorId=1, IsActive=1))
    now = datetime.now()
    for i, (robot, result, ago_min, test, sched_id, *single) in enumerate(sessions, start=1):
        started = now - timedelta(minutes=ago_min)
        db.add(M.ServiceSession(       # single=True → 상시 대기 단발 응대(ScenarioId 없음)
            id=i, RobotName=robot, ScenarioId=None if single else 1, StartedAt=started, Result=result,
            EndedAt=None if result in ("running", "paused") else started + timedelta(minutes=3),
            IsTest=1 if test else 0, ScheduleId=sched_id, DestinationName="내과"))
    for j, (mode, status) in enumerate(schedules, start=1):
        start = now.replace(hour=0, minute=1, second=0, microsecond=0)
        db.add(M.ScheduleInfo(id=j, RobotName="SIM", WorkName=f"작업{j}", TaskType="순찰·점검",
                              TaskStatus=status, StartDate=start, EndDate=start + timedelta(hours=1),
                              ScheduleMode=mode))
    db.commit()
    return db, M


def _measure(sessions=(), schedules=(), admin=True):
    from app.statistics.service import StatisticsService
    import app.database.routes.schedule as sr

    db, M = _stage(sessions, schedules)
    st = StatisticsService(db).get_all(robot_name="SIM")
    pr = st.per_robot[0]
    out = {"done": pr.tasks_completed, "total": pr.tasks_total,
           "t_done": st.tasks.completed, "t_fail": st.tasks.failed, "t_cancel": st.tasks.cancelled}
    fn = getattr(sr, "get_today_guide_sessions", None)
    if fn is None:
        out["rows"] = None          # 현행: API 없음
    else:
        user = M.UserInfo(Permission=1 if admin else 3, BusinessId=None if admin else 1)
        rows = fn(db=db, current_user=user)
        out["rows"] = [(r["id"], r["RobotName"], r["TaskType"], r["TaskStatus"]) for r in rows]
        out["titles"] = [r["WorkName"] for r in rows]
        out["sched"] = [r.get("ScheduleId", "없음") for r in rows]
    db.close()
    return out


# 오늘 SIM: 완료·진행·운영자 중단·측위 이탈 각 1, 테스트 1, 어제 완료 1 / OTHER: 완료 1
TODAY = [
    ("SIM", "completed", 50, False, None),
    ("SIM", "running", 2, False, None),
    ("SIM", "aborted_operator", 30, False, None),
    ("SIM", "aborted_follow_lost", 20, False, None),
    ("SIM", "test_done", 10, True, None),
    ("SIM", "completed", 60 * 24 + 30, False, None),
    ("OTHER", "completed", 15, False, None),
]


def _today_ok(r):
    # 작업 일정에 오늘 SIM 4건 + OTHER 1건(관리자). 테스트·어제 것은 빠진다.
    statuses = sorted(x[3] for x in r["rows"] or [])
    return (r["rows"] is not None and len(r["rows"]) == 5
            and statuses == sorted(["완료", "진행중", "취소", "오류", "완료"])
            and all(x[0] < 0 and x[2] == "길 안내" for x in r["rows"]))


@scenario("G1 안내만 한 날 — 작업 통계 0/0 재현 → 1/4",
          expect=lambda r: (r["done"], r["total"]) == (1, 4) or f"작업 {r['done']}/{r['total']}")
def g1(sim):
    return _measure(TODAY)


@scenario("G2 작업 일정 — 오늘 안내 세션 5건(테스트·어제 제외, 음수 id, 길 안내)",
          expect=lambda r: _today_ok(r) or f"rows={r['rows']}")
def g2(sim):
    return _measure(TODAY)


@scenario("G3 통계 탭 합계 — 완료·오류·취소에 안내 반영",
          expect=lambda r: (r["t_done"], r["t_fail"], r["t_cancel"]) == (1, 1, 1)
          or f"완료/오류/취소 {r['t_done']}/{r['t_fail']}/{r['t_cancel']}")
def g3(sim):
    # SIM 만 집계(robot_name 필터) — 어제 완료·테스트는 빠지고, OTHER 는 다른 로봇
    return _measure(TODAY)


@scenario("G4 회귀 — 안내 없는 날 일정 통계는 그대로(완료 1 / 전체 2)",
          expect=lambda r: (r["done"], r["total"], r["t_done"]) == (1, 2, 1)
          or f"{r['done']}/{r['total']} 합계완료 {r['t_done']}")
def g4(sim):
    return _measure(schedules=[("once", "완료"), ("once", "대기")])


@scenario("G5 일정+안내 섞인 날 — 일정 2 + 안내 1 = 3, 완료 2",
          expect=lambda r: (r["done"], r["total"]) == (2, 3) or f"{r['done']}/{r['total']}")
def g5(sim):
    return _measure([("SIM", "completed", 5, False, None)],
                    schedules=[("once", "완료"), ("once", "대기")])


@scenario("G6 상시 대기 근무 중 안내 2건 — 근무 일정은 안 세고 안내 2건만",
          expect=lambda r: (r["done"], r["total"]) == (1, 2) or f"{r['done']}/{r['total']}")
def g6(sim):
    return _measure([("SIM", "completed", 40, False, 1), ("SIM", "running", 3, False, 1)],
                    schedules=[("standby", "진행중")])


@scenario("G7 다른 사업장 사용자 — 자기 사업장 로봇 세션만 (SIM 4건)",
          expect=lambda r: (r["rows"] is not None and len(r["rows"]) == 4
                            and all(x[1] == "SIM" for x in r["rows"])) or f"rows={r['rows']}")
def g7(sim):
    return _measure(TODAY, admin=False)


@scenario("G8 상시 대기 — 단발 응대 2건(완료·못 알아들음) + 다단계 안내 1건 = 각 1건, 근무 일정은 안 셈",
          expect=lambda r: ((r["done"], r["total"]) == (2, 3)
                            and r["rows"] is not None and len(r["rows"]) == 3
                            and sum(t.startswith("상시 대기 응대") for t in r["titles"]) == 2)
          or f"{r['done']}/{r['total']} titles={r.get('titles')}")
def g8(sim):
    return _measure([("SIM", "completed", 40, False, 1, True),
                     ("SIM", "no_match", 30, False, 1, True),
                     ("SIM", "completed", 10, False, 1)],
                    schedules=[("standby", "진행중")])


@scenario("G9 못 알아들음·목적지 모름 = 업무 실패('오류'), 운영자 중단 = '취소'",
          expect=lambda r: (r["rows"] is not None
                            and sorted(x[3] for x in r["rows"]) == sorted(["오류", "오류", "취소"])
                            and (r["t_fail"], r["t_cancel"], r["total"]) == (2, 1, 3))
          or f"rows={r['rows']} 오류/취소 {r['t_fail']}/{r['t_cancel']} 총 {r['total']}")
def g9(sim):
    return _measure([("SIM", "no_match", 30, False, 1, True),
                     ("SIM", "unknown_dest", 20, False, 1, True),
                     ("SIM", "aborted_operator", 10, False, None)],
                    schedules=[("standby", "진행중")])


@scenario("G10 상시 대기 근무에서 연 세션은 행에 근무 일정 id — 화면이 근무 카드에 접는다(11-05), 통계는 그대로",
          expect=lambda r: (r["rows"] is not None and sorted(r["sched"], key=str) == sorted([1, None], key=str)
                            and (r["done"], r["total"]) == (0, 2))
          or f"ScheduleId={r.get('sched')} {r['done']}/{r['total']}")
def g10(sim):
    return _measure([("SIM", "running", 3, False, 1), ("SIM", "aborted_operator", 10, False, None)],
                    schedules=[("standby", "진행중")])
