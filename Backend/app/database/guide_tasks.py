"""응대 세션을 '길 안내' 작업 1건으로 보는 규칙 (대시보드 작업 통계·작업 일정 공통).

안내는 작업관리 일정(ScheduleInfo)이 아니라 세션(ServiceSession)으로만 남는다. 작업 유형 분류에서는
'길 안내' 이므로 통계·일정에 같이 보여야 한다. 세션 1건 = 작업 1건이다.

  - 다단계 시나리오 세션(ScenarioId 있음)과 상시 대기 단발 응대(ScenarioId 없음) 모두 1건씩 센다.
  - 구간 테스트(IsTest=1)는 운영 실적이 아니므로 뺀다.
  - 상시 대기 근무 일정(standby) 자체는 세지 않는다 — 근무 안의 응대가 실적이고, 근무를 따로 세면
    같은 일을 두 번 센다.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.database.models import ServiceSession, VoiceScenario

GUIDE_TASK_TYPE = "길 안내"

# 세션 Result → 작업 상태(ScheduleInfo.TaskStatus 와 같은 말). 목록에 없는 끝 상태는 '취소'.
# 못 알아들음(no_match)·목적지 모름(unknown_dest)은 '오류' — 방문자가 불렀는데 안내를 해내지 못한
# 업무 실패다. '취소'는 운영자 중단·일시 정지 만료처럼 사람이 그만둔 경우만.
_RUNNING = {"running", "paused"}
_DONE = {"completed", "completed_delayed"}
_FAILED = {"nav_failed", "aborted_offline", "aborted_follow_lost", "aborted_battery", "aborted_obstacle",
           "aborted_no_progress", "no_match", "unknown_dest"}


def guide_task_status(result: str | None) -> str:
    """세션 Result 를 작업 상태(진행중/완료/오류/취소)로 바꾼다."""
    if result in _RUNNING:
        return "진행중"
    if result in _DONE:
        return "완료"
    if result in _FAILED:
        return "오류"
    return "취소"


def query_guide_sessions(
    db: Session,
    start: datetime,
    end: datetime,
    robot_names: set[str] | list[str] | None = None,
) -> list[ServiceSession]:
    """기간 안에 시작한 응대 세션(테스트 제외)."""
    q = (db.query(ServiceSession)
         .filter(ServiceSession.IsTest == 0,
                 ServiceSession.StartedAt >= start,
                 ServiceSession.StartedAt <= end))
    if robot_names is not None:
        q = q.filter(ServiceSession.RobotName.in_(list(robot_names)))
    return q.order_by(ServiceSession.StartedAt.asc()).all()


def guide_session_rows(db: Session, sessions: list[ServiceSession]) -> list[dict]:
    """세션을 /DB/schedule 행과 같은 모양으로 바꾼다. id 는 일정과 겹치지 않게 음수(-세션 id)."""
    ids = {s.ScenarioId for s in sessions if s.ScenarioId}
    names = {sc.id: sc.DisplayName for sc in
             db.query(VoiceScenario).filter(VoiceScenario.id.in_(ids)).all()} if ids else {}
    rows = []
    for s in sessions:
        if s.ScenarioId:
            title = names.get(s.ScenarioId) or "안내 시나리오"
        else:
            title = "상시 대기 응대"
        if s.DestinationName:
            title = f"{title} · {s.DestinationName}"
        started = s.StartedAt.isoformat()
        ended = (s.EndedAt or s.StartedAt).isoformat()
        rows.append({
            "id": -s.id,
            "SessionId": s.id,
            # 상시 대기 근무 안에서 연 세션이면 그 근무 일정 id — 화면이 근무 카드에 접는다(11-05)
            "ScheduleId": s.ScheduleId,
            "Kind": "guide",
            "RobotName": s.RobotName,
            "WorkName": title,
            "TaskType": GUIDE_TASK_TYPE,
            "TaskStatus": guide_task_status(s.Result),
            "StartDate": started,
            "EndDate": ended,
            "WayName": s.NavTargetName or "",
            "Repeat": "N",
            "Repeat_Day": None,
            "Repeat_End": None,
            "ScheduleMode": "once",
            "RunCount": 1,
            "LastRunDate": started,
        })
    return rows
