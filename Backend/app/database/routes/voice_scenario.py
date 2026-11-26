"""다단계 음성 시나리오 — Jetson 판정 구조의 관제 측 엔드포인트.

관제는 **데이터를 주고 실행을 받는다.** 판정(STT -> 의도 -> 목적지)은 Jetson 이 한다.
번들을 로컬 캐시에 들고 있으므로 관제가 끊겨도 대부분의 단계가 계속 돈다 —
`WaitFor=operator`(대기번호·창구) 단계만 네트워크가 필수다.

기존 단발 응대(`scheduler/standby_session.py`)와 `service_session` 테이블을 공유하되
`ScenarioId` 로 구분한다. NULL 이면 단발 FSM 이, 값이 있으면 단계 엔진이 세션을 소유한다.

설계: 02_Backend/음성-시나리오-단계엔진-설계.md
인증: X-Agent-Token (agent_bridge 와 같은 토큰)
"""

import os
import threading
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import voice_follow
from app.database.database import get_db
from app.database.models import (
    LocationInfo,
    RobotInfo,
    ServiceSession,
    VoiceConfig,
    VoiceScenario,
    VoiceScenarioStep,
    VoiceSlotValue,
)
from app.database.routes.agent_bridge import require_agent
from app.database.voice_scenario_defaults import WAIT_OPERATOR
from app.database.voice_slot_links import links_by_slot

router = APIRouter(prefix="/voice", tags=["Voice Scenario"])

# 운영자 입력 자동화 — 로봇이 운영자 입력 단계(WaitFor=operator)에서 기다리기 시작하면
# 운영자 대신 값을 넣고 다음 단계로 넘긴다. 병원·관공서 모두 적용된다.
#   번호표(TICKET)       바로 AUTO_TICKET_NO 를 넣는다 — "대기번호는 10번입니다"
#   창구(COUNTER)        AUTO_OPERATOR_SEC 뒤 AUTO_COUNTER_NO 를 넣는다 — "1번 창구로 …"
#   재개(RESUME)         AUTO_OPERATOR_SEC 뒤 넘긴다 — 순서 도래 · 진료 후 재개
# 그 전에 운영자가 관제에서 입력하면 운영자 값이 쓰이고 자동 진행은 하지 않는다.
# 끄기: 번호·창구는 빈 값, 대기 시간은 음수(예 -1)면 그 단계는 예전처럼 운영자를 기다린다.
# 단계마다 따로 정할 수 있다(VoiceScenarioStep.AutoAdvanceSec) — 정한 단계는 위 대기 시간 대신 그 값을 쓴다.
#   예) 편의점 대기: -1(운영자가 분기를 고른다) · 진료실 입구: -1(진료 연출) · 대기 자리: 20
AUTO_TICKET_NO = (os.getenv("VOICE_AUTO_TICKET_NO", "10") or "").strip()
AUTO_COUNTER_NO = (os.getenv("VOICE_AUTO_COUNTER_NO", "1") or "").strip()


def _env_sec(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


# 창구·재개 단계에서 자동으로 넘기기까지 기다리는 시간(초). 로봇이 "순서가 되면 다시
# 안내해 드리겠습니다" 를 말한 뒤부터 잰다. 로봇은 그 뒤 세션 폴링(약 3초)에서 알아챈다.
AUTO_OPERATOR_SEC = _env_sec("VOICE_AUTO_OPERATOR_SEC", 5.0)

# 단계 엔진이 소유한 세션만 다룬다. 단발 FSM 세션(ScenarioId IS NULL)은 건드리지 않는다.
RESULT_RUNNING = "running"
RESULT_PAUSED = "paused"
RESULT_TEST_DONE = "test_done"   # 구간 테스트가 종료 단계까지 마쳤다

# 호출 위치 이동 — 시나리오 단계가 아니라 세션 앞에 붙는 가상 단계다. 안내 시작 시 호출 위치가
# 정해지면 세션이 이 단계로 열리고, Jetson 엔진이 StartLocationId 로 이동한 뒤 첫 단계(인사)로 넘긴다.
# 단계 표에 넣지 않는 이유: 위치가 세션마다 다르고, 운영자가 흐름을 편집하는 대상도 아니다.
CALL_MOVE_STEP = "call_move"

# 관제가 스스로 닫는 기준(2026-10-07). 페이지를 닫아도 안내는 이어지므로 아무도 끝내지 않는 세션이
# 생긴다 — 그대로 두면 로봇이 복도에 묶이고 새 안내도 409 로 막힌다.
#   동행 이탈: 운영자를 부른 뒤 재개 없이 이만큼 지나면 방문자는 떠난 것으로 본다
#   엔진 끊김: Jetson 이 세션을 이만큼 폴링하지 않으면 닫는다 — 남겨 두면 Jetson 이 다시 켜질 때
#             몇 시간 전 안내를 멈춘 단계부터 이어 간다
FOLLOW_LOST_CLOSE_SEC = 600
ENGINE_SILENT_CLOSE_SEC = 180
# 폴링(3초)마다 DB 에 쓰지 않는다. 기준(180초)보다 충분히 짧으면 된다.
ENGINE_SEEN_WRITE_SEC = 30
STALE_SWEEP_SEC = 15
RESULT_FOLLOW_LOST = "aborted_follow_lost"
RESULT_ENGINE_OFFLINE = "aborted_offline"

# 운영자 일시 정지(2026-10-09) — 누른 즉시 세우지 않고, 다음 이동 단계 앞에서 멈춘다.
# 지금 가는 목적지의 도착 안내·질문까지 마치고 서므로, 재개하면 다음 단계 멘트("○○로 안내하겠습니다")부터 잇는다.
#   requested: 운영자가 눌렀다 — Jetson 이 다음 이동 단계 직전에 보고 멈춘다
#   holding:   Jetson 이 멈췄다고 알렸다 — 운영자가 재개할 때까지 기다린다
# 재개 없이 HOLD_CLOSE_SEC 가 지나면 닫는다(방문자를 세워 둔 채 잊힌 안내).
HOLD_REQUESTED = "requested"
HOLD_HOLDING = "holding"
HOLD_CLOSE_SEC = 1200
RESULT_HOLD_TIMEOUT = "aborted_hold_timeout"
# 배터리 최소 기준 이하 — 관제가 안내를 바로 닫고 충전소로 보낸다(2026-10-22).
# ABORT_BATTERY·BATTERY_ABORT_CLOSE_SEC 는 예전 방식(다음 이동 전 중단 요청)으로 남은 요청 정리용.
RESULT_BATTERY = "aborted_battery"
ABORT_BATTERY = "battery"
BATTERY_ABORT_CLOSE_SEC = 180
# 화면 '반복 실행' 회차 사이(10초) 동안 근무·예약 작업이 로봇을 잡지 않게 막는 시간 (D11)
LOOP_HOLD_SEC = 30
_loop_hold_until: dict[str, float] = {}
HOLD_REASON = "운영자 일시 정지 — 재개를 기다립니다"
# 관제가 재시작되면 그동안 Jetson 은 폴링하지 못했다 — 재시작 시각부터 다시 잰다
_PROCESS_STARTED_AT = datetime.now()

# 안내 이동 속도. slow = 사람이 따라오는 속도(로봇 NAV Speed 필드로 전달, receiver 가 코드로 바꾼다)
# fast 는 receiver 가 High(2) 로 보낸다. fast 를 모르는 옛 receiver 는 보통(0)으로 보낸다.
GUIDE_SPEEDS = ("slow", "normal", "fast")

# 목표가 이 거리(m) 안이면 움직이지 않고 도착으로 본다. 처음에는 1.0 이었다 — 로봇이 가까운 목표에는
# NAV 를 받아도 움직이지 않아 '이동 미감지 → 재전송 한도 초과' 로 실패했기 때문이다(2026-09-28 실측:
# 0.6m 에서 실패). 그런데 대기장소 ↔ 원무과_01 처럼 0.8m 떨어진 안내 장소는 아예 가지 않고(방향도
# 돌지 않고) 도착 멘트가 나왔다(10-11 실기). 그래서 0.5 로 줄여 일단 보내 보고, 로봇이 받지 않으면
# polling.GUIDE_NO_START_NEAR_M 안에서는 실패 대신 도착으로 처리한다(재전송 한도까지 서 있지 않게).
GUIDE_NEAR_M = _env_sec("GUIDE_NEAR_M", 0.5)
# 시작 위치(호출 이동)만 더 넓게 본다. 층 이동 직후에는 측위가 상실돼 '장소 이동'을 못 쓰므로
# 운영자가 수동으로 시작 위치에 세우고 위치를 재조정한다 — 그 오차(0.5~1m)로 안내 시작 때 짧은
# 이동·방향 전환이 붙어 다닥거렸다(10-11 실기). 다음 안내 단계가 어차피 방향을 잡고 출발한다.
# 1.5m 면 3F 로비입구→로봇대기위치(1.49m)가 생략됐다(10-30) — 손으로 세운 오차(0.5~1m)만 덮는다.
GUIDE_CALL_NEAR_M = _env_sec("GUIDE_CALL_NEAR_M", 1.0)
# 서 있는 자세(MotionState). 1=Stand, 17=RL Control(Stand) — ModeSpeedControl 과 같은 해석
STAND_STATES = (1, 17)
# 앉은 자세에서 일어서는 데 ~8초 걸린다(2026-09-30 실측). Jetson 의 이동 요청 대기(MOVE_TIMEOUT)는
# 이 값 + 기립 안정화 1초 + NAV 전송보다 길어야 한다 — 짧으면 Jetson 은 실패로 알고 로봇은 출발한다.
STAND_WAIT_SEC = 15.0


# ── 응답 모델 ─────────────────────────────────────────
class StepOut(BaseModel):
    stepCode: str
    stepOrder: int
    sayText: Optional[str]
    waitFor: str
    expectedIntent: Optional[str]
    retryCount: int
    responseTimeoutSec: int
    actionType: str
    slotName: Optional[str]
    targetLocationId: Optional[int]
    targetName: Optional[str]
    nextStepCode: Optional[str]
    onNoStepCode: Optional[str]
    onTimeoutStepCode: Optional[str]
    # 도착 접근 안내의 부르는 이름 — 엔진이 "곧 ○○에 도착합니다" 를 미리 합성한다
    spokenPlaceName: Optional[str] = None
    # 이동 중 진행 멘트 — 관제가 /robot/nav cue 로 내보내고 엔진은 미리 합성해 둔다
    progressText: Optional[str] = None
    # 운영자 단계 음성 출구 — 엔진이 운영자 입력을 기다리며 듣다가 말 목록 중 하나가 들리면 넘어간다
    voiceExitKeywords: list[str] = []
    voiceExitStepCode: Optional[str] = None


class PlaceOut(BaseModel):
    id: int
    name: str


class FollowLegOut(BaseModel):
    locationId: int
    avgSec: float          # 과거 평균 이동 시간 — 도착 직전에는 묻지 않는 데 쓴다
    samples: int
    intervalSec: int       # 0 = 이 구간은 묻지 않는다


class FollowCheckOut(BaseModel):
    """이동 중 동행 확인 — 계산 규칙은 app/database/voice_follow.py."""
    enabled: bool
    intervalSec: int       # 기록 없는 구간의 기본 간격
    source: str            # manual | history | default
    maxWaitSec: int        # 멈춰서 기다리다 운영자를 호출하기까지
    legs: list[FollowLegOut] = []


class ScenarioOut(BaseModel):
    id: int
    code: str
    displayName: str
    floorId: Optional[int]
    mapId: Optional[int] = None
    # 운영 맵의 장소 — 추가 목적지 판정(DESTINATION)에서 사전에 없는 곳(화장실 등)도 고르게 한다
    places: list[PlaceOut] = []
    steps: list[StepOut]
    followCheck: Optional[FollowCheckOut] = None
    # 경유지 방향 안내를 말할지 — 꺼져 있으면 엔진이 /robot/nav 의 cue 를 말하지 않는다
    turnCues: bool = True


class SlotLocationOut(BaseModel):
    locationId: int
    x: float
    y: float
    yaw: Optional[float] = None
    floorId: Optional[int] = None
    mapId: int


class SlotOut(BaseModel):
    value: str
    synonyms: list[str]
    # 최상위 좌표 = 로봇이 지금 있는 맵의 연결(맵별 연결 이전 엔진 호환용).
    # 엔진은 locations 에서 시나리오 운영 맵의 것을 골라 쓴다.
    locationId: Optional[int]
    x: Optional[float] = None
    y: Optional[float] = None
    yaw: Optional[float] = None
    floorId: Optional[int] = None
    mapId: Optional[int] = None
    locations: list[SlotLocationOut] = []
    # 도착 멘트 {도착안내} — 목적지와 도착 장소가 다를 때의 안내("엘리베이터를 타고 3층으로 …")
    arrivalText: Optional[str] = None
    # 도착지 음성명 — {도착지}에 장소 이름 대신 말할 이름. 없으면 장소 이름
    spokenName: Optional[str] = None


class ConfigOut(BaseModel):
    matchThreshold: float
    confirmOnAction: bool
    fallbackResponse: str
    slotFuzzyThreshold: float
    slotFuzzyMinLen: int
    silenceMs: int
    ttsSpeed: float
    sttModel: str


class BundleOut(BaseModel):
    robotId: int
    robotName: Optional[str]
    generatedAt: datetime
    config: Optional[ConfigOut]
    slots: list[SlotOut]
    scenarios: list[ScenarioOut]


class SessionOut(BaseModel):
    """목적지는 두 가지다 — 헷갈리지 않게 이름으로 가른다.

    destination*  고객 목적지: 고객이 말해 확정한 곳. 멘트 {목적지} · '고객 목적지' 이동 단계가 쓴다.
    navTarget*    현재 이동 목표: 마지막 이동 명령의 장소(발급기·대기석 …). 관제 표시용이다.
    """
    id: int
    robotName: Optional[str]
    scenarioId: Optional[int]
    scenarioCode: Optional[str]
    currentStepCode: Optional[str]
    currentFloorId: Optional[int]
    destinationId: Optional[int]
    destinationName: Optional[str]
    navTargetId: Optional[int] = None
    navTargetName: Optional[str] = None
    ticketNo: Optional[str]
    counterNo: Optional[str]
    startLocationId: Optional[int] = None
    followState: Optional[str] = None
    holdState: Optional[str] = None
    abortRequest: Optional[str] = None
    isTest: bool = False
    testEndStepCode: Optional[str] = None
    result: str
    closedBy: Optional[str]
    startedAt: datetime
    endedAt: Optional[datetime]


# ── 요청 모델 ─────────────────────────────────────────
class SessionStartReq(BaseModel):
    robotId: int
    scenarioCode: str = Field(description="hospital | public_office")
    floorId: Optional[int] = None


class SessionPatchReq(BaseModel):
    currentStepCode: Optional[str] = None
    destinationId: Optional[int] = None
    destinationName: Optional[str] = None
    currentFloorId: Optional[int] = None
    heard: Optional[str] = None
    matchedIntent: Optional[str] = None
    matchedSlot: Optional[str] = None
    confidence: Optional[float] = None
    retryCount: Optional[int] = None
    result: Optional[str] = Field(default=None, description="running | paused")
    # 조건부 단계 이동 — 세션이 아직 이 단계일 때만 currentStepCode 를 바꾼다(음성 출구).
    # 그 사이 운영자가 입력해 단계가 바뀌었으면 아무것도 바꾸지 않고 지금 세션을 돌려준다.
    fromStepCode: Optional[str] = None


class FollowReq(BaseModel):
    state: str = Field(description="waiting | lost | ok")


class HoldReq(BaseModel):
    state: str = Field(description="holding — 다음 이동 단계 앞에서 멈췄다")


class ObstacleAnswerReq(BaseModel):
    askId: int = Field(description="/robot/nav obstacle.ask.id")
    answer: str = Field(description="yes | no | none")


class TtsFailedReq(BaseModel):
    text: str = Field(default="", description="말하려던 문장")
    error: Optional[str] = None
    critical: bool = Field(default=False, description="질문처럼 못 들으면 안내가 진행되지 않는 발화")


class MoveCheckOut(BaseModel):
    ok: bool
    reason: Optional[str] = None
    # 지금 위치에서 그 장소까지 경유지를 따라간 거리(m) — 출발 멘트의 {거리}. 모르면 None
    distanceM: Optional[float] = None
    # 리셋·준비 대기 없이 바로 출발한다(내비 보행 유지 중) — 음성 엔진이 이동을 미리 요청하는 시간을 줄인다
    fastStart: bool = False
    # 출발하자마자 제자리에서 도는 경우의 방향 안내("왼쪽으로 방향을 바꾸겠습니다.") — 엔진이 회전 직전에 말한다
    departTurn: Optional[str] = None


class SessionCloseReq(BaseModel):
    result: str = Field(description="completed | no_match | unknown_dest | nav_failed | aborted_*")
    closedBy: str = Field(description="voice | operator | timeout | emergency")
    reason: Optional[str] = None


# ── 헬퍼 ──────────────────────────────────────────────
def _robot_or_404(db: Session, robot_id: int) -> RobotInfo:
    robot = db.query(RobotInfo).filter(RobotInfo.id == robot_id).first()
    if not robot:
        raise HTTPException(status_code=404, detail=f"로봇 {robot_id} 을(를) 찾을 수 없습니다.")
    return robot


def _active_session(db: Session, robot_name: str) -> Optional[ServiceSession]:
    """단계 엔진이 소유한 진행 중 세션. 단발 FSM 세션은 제외한다."""
    return (
        db.query(ServiceSession)
        .filter(
            ServiceSession.RobotName == robot_name,
            ServiceSession.ScenarioId.isnot(None),
            ServiceSession.Result.in_([RESULT_RUNNING, RESULT_PAUSED]),
        )
        .order_by(ServiceSession.id.desc())
        .first()
    )


def scenario_busy(robot_name: Optional[str]) -> bool:
    """이 로봇이 다단계 안내 중인가. 스케줄러·단발 응대가 끼어들지 않게 하는 데 쓴다.

    운영자 입력을 기다리는 동안(paused) 로봇은 멈춰 있어 `is_navigating` 으로는 안 보인다.
    그 사이 스케줄이 로봇을 끌고 가면 환자를 대기석에 두고 떠나게 된다.
    """
    if not robot_name:
        return False
    from app.database.database import SessionLocal

    db = SessionLocal()
    try:
        return _active_session(db, robot_name) is not None
    finally:
        db.close()


def open_session(db: Session, robot: RobotInfo, scenario_code: str,
                 floor_id: Optional[int] = None,
                 start_location_id: Optional[int] = None,
                 test: Optional[dict] = None,
                 allow_inactive: bool = False) -> ServiceSession:
    """시나리오 세션을 연다. Jetson 경로와 운영자 경로가 같이 쓴다. 로봇당 활성 세션은 하나다.

    start_location_id 가 있으면 호출 위치로 먼저 이동하는 단계(CALL_MOVE_STEP)부터 시작한다.
    장소 검증(운영 맵의 장소인가)은 호출하는 쪽이 한다.
    test: 구간 테스트 — {startStepCode, endStepCode, destinationId, destinationName, ticketNo,
    counterNo}. 검증(도달 가능·필수 값)은 호출하는 쪽이 한다. 한 번에 넣어야 Jetson 이 중간 상태
    (첫 단계부터 시작)를 이어받지 않는다.
    """
    existing = _active_session(db, robot.RobotName)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"이미 진행 중인 세션이 있습니다(#{existing.id}, {existing.CurrentStepCode}).",
        )

    # 이 관제가 물고 있는 로봇이면 다른 작업과 겹치지 않는지 본다(순환 import 라 늦게 가져온다).
    from app.user_cache import get_robot_id
    if robot.id == get_robot_id():
        from app.scheduler import standby_session as sess_mod
        from app.scheduler.loop import get_active_schedule_id, get_standby_state
        if sess_mod.is_active():
            raise HTTPException(status_code=409, detail="단발 응대가 진행 중입니다. 끝난 뒤 시작하세요.")
        from app.database import voice_prerun
        if voice_prerun.is_active():
            raise HTTPException(status_code=409, detail="사전 주행 중입니다. 끝나거나 멈춘 뒤 안내를 시작하세요.")
        # 상시 대기 근무는 '사람을 기다리는 중' 이라 안내를 시작해도 된다. 순회 같은 작업만 막는다.
        if get_active_schedule_id() is not None and get_standby_state() is None:
            raise HTTPException(status_code=409, detail="스케줄 작업이 진행 중입니다. 끝난 뒤 시작하세요.")
        standby = get_standby_state()
        if standby and standby.get("phase") == "away" and not test:
            raise HTTPException(status_code=409,
                                detail=standby.get("away_label") or "대기 장소로 돌아오지 못한 상태입니다.")

    # 꺼진 시나리오는 상시 대기·로봇 마이크 호출에 쓰지 않는다. 운영자 안내 시작·구간 시험만 연다(allow_inactive)
    q = db.query(VoiceScenario).filter(VoiceScenario.RobotId == robot.id, VoiceScenario.Code == scenario_code)
    if not allow_inactive:
        q = q.filter(VoiceScenario.IsActive == 1)
    sc = q.first()
    if not sc:
        raise HTTPException(status_code=404, detail=f"시나리오 '{scenario_code}' 이(가) 없습니다.")

    first = (db.query(VoiceScenarioStep)
             .filter(VoiceScenarioStep.ScenarioId == sc.id, VoiceScenarioStep.IsActive == 1)
             .order_by(VoiceScenarioStep.StepOrder, VoiceScenarioStep.id).first())
    if not first:
        raise HTTPException(status_code=409, detail=f"시나리오 '{sc.DisplayName}' 에 단계가 없습니다.")

    s = ServiceSession(
        RobotName=robot.RobotName,
        StartedAt=datetime.now(),
        Result=RESULT_RUNNING,
        ScenarioId=sc.id,
        CurrentStepCode=CALL_MOVE_STEP if start_location_id else first.StepCode,
        CurrentFloorId=floor_id if floor_id is not None else sc.FloorId,
        StartLocationId=start_location_id,
    )
    if test:
        s.CurrentStepCode = test["startStepCode"]
        s.IsTest = 1
        s.TestEndStepCode = test["endStepCode"]
        s.DestinationId = test.get("destinationId")
        s.DestinationName = test.get("destinationName")
        s.TicketNo = test.get("ticketNo")
        s.CounterNo = test.get("counterNo")
    db.add(s)
    db.commit()
    db.refresh(s)
    if robot.id == get_robot_id():
        if not test:
            # 근무 중이면 회차에 붙이고, 대기 장소로 가던 중이면 그 자리에 세운다(D9)
            from app.scheduler.loop import attach_scenario_session
            attach_scenario_session(db, s)
        from app.navigation.send_move import begin_keep_gait_session
        begin_keep_gait_session()
        if not start_location_id and not test:
            _prime_in_background(robot.id)
    return s


def _prime_in_background(robot_id: int) -> None:
    """현재 위치에서 바로 시작 — 내비 모드로 미리 바꾼다. 시작 요청을 붙잡지 않게 따로 돌린다.

    서 있을 때만 한다(앉은 로봇에 NAV 를 보내지 않는다 — 기립은 첫 이동의 _ensure_standing 이 한다).
    첫 이동이 먼저 오면 prime 쪽이 _gait_kept 를 남기지 않아 지금처럼 리셋 출발한다.
    """
    from app.robot_io import runtime
    if runtime.get_motion_state(robot_id) not in STAND_STATES:
        return
    from app.navigation.send_move import prime_nav_gait_in_place
    threading.Thread(target=prime_nav_gait_in_place, args=("현재 위치에서 시작",),
                     daemon=True, name="nav_prime").start()


def close(db: Session, s: ServiceSession, result: str, closed_by: str,
          reason: Optional[str] = None) -> ServiceSession:
    s.Result = result[:24]
    s.ClosedBy = closed_by[:20]
    # 사유 없이 닫히면 앞서 남긴 사유(이동 실패 원인 등)를 지우지 않는다
    if reason:
        s.Reason = reason[:200]
    s.EndedAt = datetime.now()
    db.commit()
    db.refresh(s)
    # 도착 후 내비 보행으로 세워 뒀으면 안내가 끝났으니 기본 보행으로 돌린다
    from app.user_cache import get_robot_name
    if s.RobotName == get_robot_name():
        from app.navigation.send_move import end_keep_gait_session
        end_keep_gait_session()
        if not s.IsTest:
            # 근무 복귀·충전 판단. Jetson 의 close 요청을 붙잡지 않게 따로 돌린다.
            from app.scheduler.loop import on_scenario_closed
            threading.Thread(target=on_scenario_closed, args=(s.id,), daemon=True,
                             name="scenario-closed").start()
    return s


def abort_for_battery(robot_name: Optional[str], battery_pct: int) -> bool:
    """진행 중 안내를 배터리 부족으로 바로 닫는다. 반환: 닫았으면 True(안내 중이 아니면 False).

    가던 이동이 남아 있으면 세우고 aborted_battery 로 닫는다 — 종료 후처리(on_scenario_closed)가
    알림을 남기고 충전소로 보낸다. Jetson 은 다음 폴링에서 닫힌 것을 보고 진행을 멈춘다.
    """
    from app.database.database import SessionLocal
    from app.navigation import send_move

    if not robot_name:
        return False
    db = SessionLocal()
    try:
        s = _active_session(db, robot_name)
        if not s:
            return False
        if send_move.is_navigating:
            send_move.stop_navigation_internal("배터리 부족 — 안내 중단", keep_schedule=True)
        close(db, s, RESULT_BATTERY, "battery",
              f"배터리 {battery_pct}% — 최소 기준 이하라 안내를 멈추고 충전소로 갑니다")
        return True
    finally:
        db.close()


def hold_for_loop(robot_name: Optional[str]) -> None:
    """화면 '반복 실행'이 다음 회차를 열기로 했다 — 그 틈에 근무가 로봇을 잡지 않게 한다."""
    if robot_name:
        _loop_hold_until[robot_name] = time.time() + LOOP_HOLD_SEC


def loop_holding(robot_name: Optional[str]) -> bool:
    return bool(robot_name) and time.time() < _loop_hold_until.get(robot_name, 0.0)


def _session_out(db: Session, s: ServiceSession) -> SessionOut:
    code = None
    if s.ScenarioId:
        sc = db.query(VoiceScenario).filter(VoiceScenario.id == s.ScenarioId).first()
        code = sc.Code if sc else None
    return SessionOut(
        id=s.id, robotName=s.RobotName, scenarioId=s.ScenarioId, scenarioCode=code,
        currentStepCode=s.CurrentStepCode, currentFloorId=s.CurrentFloorId,
        destinationId=s.DestinationId, destinationName=s.DestinationName,
        navTargetId=s.NavTargetId, navTargetName=s.NavTargetName,
        ticketNo=s.TicketNo, counterNo=s.CounterNo,
        startLocationId=s.StartLocationId, followState=s.FollowState, holdState=s.HoldState,
        abortRequest=s.AbortRequest,
        isTest=bool(s.IsTest), testEndStepCode=s.TestEndStepCode,
        result=s.Result, closedBy=s.ClosedBy,
        startedAt=s.StartedAt, endedAt=s.EndedAt,
    )


# ── 번들 (Jetson 이 당겨가는 데이터) ──────────────────
@router.get("/bundle/{robot_id}", response_model=BundleOut)
def get_bundle(robot_id: int, db: Session = Depends(get_db), _: str = Depends(require_agent)):
    """목적지 사전 · 시나리오 단계 · 튜닝값을 한 번에 내려준다.

    Jetson 은 이걸 로컬 캐시에 저장하고 주기적으로 갱신한다. 관제가 끊겨도
    마지막 번들로 계속 동작한다.
    """
    robot = _robot_or_404(db, robot_id)

    # 목적지 — 좌표는 LocationInfo 에서 끌어온다(복사본이 아니라 조인).
    slots: list[SlotOut] = []
    rows = db.query(VoiceSlotValue).filter(VoiceSlotValue.RobotId == robot_id).all()
    links = links_by_slot(db, [r.id for r in rows])
    for r in rows:
        locs = [lo for _link, lo in links[r.id]]
        here = next((lo for lo in locs if lo.MapId == robot.CurrentMapId), None)
        slots.append(SlotOut(
            value=r.Value,
            synonyms=[x.strip() for x in (r.Synonyms or "").split(",") if x.strip()],
            locationId=here.id if here else None,
            x=here.LocationX if here else None,
            y=here.LocationY if here else None,
            yaw=here.Yaw if here else None,
            floorId=here.FloorId if here else None,
            mapId=here.MapId if here else None,
            locations=[SlotLocationOut(locationId=lo.id, x=lo.LocationX, y=lo.LocationY, yaw=lo.Yaw,
                                       floorId=lo.FloorId, mapId=lo.MapId) for lo in locs],
            arrivalText=r.ArrivalText or None,
            spokenName=r.SpokenName or None,
        ))

    # 시나리오 + 단계
    durations = voice_follow.load_durations(db, robot_id)
    scenarios: list[ScenarioOut] = []
    # 꺼진 시나리오도 싣는다 — 운영자가 연 시험·안내 세션을 엔진이 코드로 찾아 돌린다.
    # 엔진이 스스로 고르는 시나리오는 없다(세션의 scenarioCode 만 본다), 켜짐 판단은 세션을 여는 관제가 한다.
    for sc in (db.query(VoiceScenario)
               .filter(VoiceScenario.RobotId == robot_id)
               .order_by(VoiceScenario.SortOrder, VoiceScenario.id).all()):
        steps_q = (db.query(VoiceScenarioStep)
                   .filter(VoiceScenarioStep.ScenarioId == sc.id,
                           VoiceScenarioStep.IsActive == 1)
                   .order_by(VoiceScenarioStep.StepOrder, VoiceScenarioStep.id).all())
        target_ids = [st.TargetLocationId for st in steps_q if st.TargetLocationId]
        tnames = {}
        if target_ids:
            for lo in db.query(LocationInfo).filter(LocationInfo.id.in_(target_ids)).all():
                tnames[lo.id] = lo.LacationName
        map_places = []
        if sc.MapId:
            map_places = [PlaceOut(id=lo.id, name=lo.LacationName)
                          for lo in (db.query(LocationInfo)
                                     .filter(LocationInfo.MapId == sc.MapId)
                                     .order_by(LocationInfo.LacationName).all())
                          if lo.LacationName]
        # 이 시나리오가 갈 수 있는 곳 — 고정 이동 장소 · 고객 목적지(사전, 운영 맵 연결분) · 운영 맵 장소
        slot_loc_ids = [lo.id for pairs in links.values() for link, lo in pairs if link.MapId == sc.MapId]
        reachable = voice_follow.place_names(
            db, [*target_ids, *slot_loc_ids, *(p.id for p in map_places)])
        plan = voice_follow.follow_plan(durations, reachable, sc.FollowIntervalSec)
        follow = FollowCheckOut(
            enabled=bool(sc.FollowCheckEnabled), intervalSec=plan["intervalSec"],
            source=plan["source"], maxWaitSec=sc.FollowMaxWaitSec or 60,
            legs=[FollowLegOut(**leg) for leg in plan["legs"]],
        )
        scenarios.append(ScenarioOut(
            id=sc.id, code=sc.Code, displayName=sc.DisplayName, floorId=sc.FloorId,
            mapId=sc.MapId, places=map_places, followCheck=follow,
            turnCues=bool(sc.TurnCuesEnabled),
            steps=[StepOut(
                stepCode=st.StepCode, stepOrder=st.StepOrder, sayText=st.SayText,
                waitFor=st.WaitFor, expectedIntent=st.ExpectedIntent,
                retryCount=st.RetryCount, responseTimeoutSec=st.ResponseTimeoutSec,
                actionType=st.ActionType, slotName=st.SlotName,
                targetLocationId=st.TargetLocationId,
                targetName=tnames.get(st.TargetLocationId),
                nextStepCode=st.NextStepCode, onNoStepCode=st.OnNoStepCode,
                onTimeoutStepCode=st.OnTimeoutStepCode,
                spokenPlaceName=st.SpokenPlaceName,
                progressText=_bundle_progress(st, tnames),
                **_bundle_voice_exit(st),
            ) for st in steps_q],
        ))

    cfg = db.query(VoiceConfig).filter(VoiceConfig.RobotId == robot_id).first()
    config = ConfigOut(
        matchThreshold=cfg.MatchThreshold, confirmOnAction=bool(cfg.ConfirmOnAction),
        fallbackResponse=cfg.FallbackResponse, slotFuzzyThreshold=cfg.SlotFuzzyThreshold,
        slotFuzzyMinLen=cfg.SlotFuzzyMinLen, silenceMs=cfg.SilenceMs,
        ttsSpeed=cfg.TtsSpeed, sttModel=cfg.SttModel,
    ) if cfg else None

    return BundleOut(
        robotId=robot_id, robotName=robot.RobotName, generatedAt=datetime.now(),
        config=config, slots=slots, scenarios=scenarios,
    )


# ── 세션 ──────────────────────────────────────────────
@router.post("/session", response_model=SessionOut)
def start_session(req: SessionStartReq, db: Session = Depends(get_db),
                  _: str = Depends(require_agent)):
    """시나리오 세션을 연다. 로봇당 활성 세션은 하나다."""
    robot = _robot_or_404(db, req.robotId)
    return _session_out(db, open_session(db, robot, req.scenarioCode, req.floorId))


@router.get("/session/current", response_model=Optional[SessionOut])
def current_session(robot_id: int = Query(...), db: Session = Depends(get_db),
                    _: str = Depends(require_agent)):
    """진행 중 세션. Jetson 이 폴링해 운영자 입력값과 종료를 감지한다."""
    robot = _robot_or_404(db, robot_id)
    s = _active_session(db, robot.RobotName)
    if not s:
        return None
    now = datetime.now()
    if s.EngineSeenAt is None or (now - s.EngineSeenAt).total_seconds() >= ENGINE_SEEN_WRITE_SEC:
        s.EngineSeenAt = now
        db.commit()
        db.refresh(s)
    return _session_out(db, s)


@router.patch("/session/{session_id}", response_model=SessionOut)
def patch_session(session_id: int, req: SessionPatchReq, db: Session = Depends(get_db),
                  _: str = Depends(require_agent)):
    """단계 진행·목적지 확정·판정 결과 기록."""
    s = db.query(ServiceSession).filter(ServiceSession.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    if s.ScenarioId is None:
        raise HTTPException(status_code=409, detail="단발 응대 세션은 단계 엔진이 수정할 수 없습니다.")
    if s.Result not in (RESULT_RUNNING, RESULT_PAUSED):
        # 운영자가 종료한 세션을 Jetson 의 늦은 갱신이 'running' 으로 되살리지 않게 한다.
        # 그대로 돌려주면 엔진이 종료를 알아챈다.
        return _session_out(db, s)

    # 구간 테스트: 엔진이 종료 단계를 마치고 다음 단계로 넘어가는 순간 닫는다.
    # 닫힌 세션을 돌려주면 엔진은 운영자 종료로 알고 말없이 멈춘다.
    if (s.IsTest and s.TestEndStepCode and req.currentStepCode
            and s.CurrentStepCode == s.TestEndStepCode
            and req.currentStepCode != s.TestEndStepCode):
        return _session_out(db, close(db, s, RESULT_TEST_DONE, "operator", "구간 테스트 종료 단계 완료"))

    if req.fromStepCode and s.CurrentStepCode != req.fromStepCode:
        # 운영자 입력(또는 자동 진행)이 먼저 단계를 넘겼다 — 엔진은 돌려받은 단계를 따른다
        print(f"[GUIDE] 안내 세션 #{s.id} 음성 출구 무시 — 이미 {s.CurrentStepCode} (요청 기준 {req.fromStepCode})")
        return _session_out(db, s)

    for field, col in (
        ("currentStepCode", "CurrentStepCode"), ("destinationId", "DestinationId"),
        ("destinationName", "DestinationName"), ("currentFloorId", "CurrentFloorId"),
        ("heard", "Heard"), ("matchedIntent", "MatchedIntent"),
        ("matchedSlot", "MatchedSlot"), ("confidence", "Confidence"),
        ("retryCount", "RetryCount"),
    ):
        v = getattr(req, field)
        if v is not None:
            setattr(s, col, v)

    if req.result in (RESULT_RUNNING, RESULT_PAUSED):
        s.Result = req.result

    # 구간 테스트의 종료 단계가 운영자 입력 단계면, 거기 닿아 입력을 기다리기 시작할 때 닫는다
    # — 운영자가 입력하면 다음 단계로 한 걸음 더 가 버린다.
    if (s.IsTest and req.result == RESULT_PAUSED
            and s.TestEndStepCode and s.CurrentStepCode == s.TestEndStepCode):
        return _session_out(db, close(db, s, RESULT_TEST_DONE, "operator", "구간 테스트 종료 단계 도달"))

    if req.result == RESULT_PAUSED:
        _auto_operator_input(db, s)

    db.commit()
    db.refresh(s)
    return _session_out(db, s)


def _operator_step(db: Session, s: ServiceSession) -> Optional[VoiceScenarioStep]:
    """세션이 지금 서 있는 운영자 입력 단계. 아니면 None."""
    step = (db.query(VoiceScenarioStep)
            .filter(VoiceScenarioStep.ScenarioId == s.ScenarioId,
                    VoiceScenarioStep.StepCode == s.CurrentStepCode).first())
    return step if step and step.WaitFor == WAIT_OPERATOR else None


def _auto_value(kind: str) -> Optional[str]:
    """자동으로 넣을 값. 그 종류의 값이 비어 있으면 None, 재개는 값 없이 ''.
    언제 넣을지(꺼짐 포함)는 _auto_delay 가 정한다."""
    if kind == "TICKET":
        return AUTO_TICKET_NO or None
    if kind == "COUNTER":
        return AUTO_COUNTER_NO or None
    return ""


def _auto_delay(step: VoiceScenarioStep) -> Optional[float]:
    """자동 진행까지 기다릴 초. None 이면 자동 진행하지 않는다.

    단계 값(AutoAdvanceSec)이 있으면 그것을, 없으면 전역 규칙(번호표 바로 · 창구·재개 AUTO_OPERATOR_SEC).
    음성 출구가 있는 단계는 전역 규칙으로 넘기지 않는다 — 방문자가 말할 틈 없이 5초 만에 다음으로
    가 버린다(11-02 세션 527: 편의점 앞 "대기 자리로 안내해 줘" 를 듣지 않고 진료실로 출발)."""
    own = step.AutoAdvanceSec
    if own is not None:
        return None if own < 0 else float(own)
    if (step.ExpectedIntent or "RESUME") == "TICKET":
        return 0.0
    if _bundle_voice_exit(step)["voiceExitStepCode"]:
        return None
    return None if AUTO_OPERATOR_SEC < 0 else AUTO_OPERATOR_SEC


def _auto_advance(db: Session, s: ServiceSession, step: VoiceScenarioStep) -> bool:
    """운영자 입력(voice_scenario_admin.operator_input)처럼 값을 넣고 **단계를 넘긴다** —
    엔진은 '단계가 바뀌었는가' 로 진행을 안다. 다음 단계가 없으면 넘기지 않는다(운영자 입력은
    세션을 닫지만 자동으로 닫지는 않는다)."""
    kind = step.ExpectedIntent or "RESUME"
    value = _auto_value(kind)
    if value is None or not step.NextStepCode:
        return False
    if kind == "TICKET":
        s.TicketNo = value
    elif kind == "COUNTER":
        s.CounterNo = value
    s.CurrentStepCode = step.NextStepCode
    s.Result = RESULT_RUNNING
    print(f"[GUIDE] 안내 세션 #{s.id} 운영자 입력 자동 처리 — {step.StepCode} {kind}"
          f"{' ' + value if value else ''} -> {step.NextStepCode}")
    return True


def _auto_operator_input(db: Session, s: ServiceSession) -> None:
    """엔진이 운영자 입력 대기를 시작했다(result=paused) — 그 순간이 입력 타이밍이다.

    번호표는 바로 넣는다("번호표를 발급해 주세요" 를 이미 말했으므로 다음 폴링에 "10번" 이 나온다).
    창구·재개는 AUTO_OPERATOR_SEC 뒤에 넘긴다 — 바로 넘기면 "순서가 되면 다시 안내해 드리겠습니다"
    직후 곧바로 "순서가 되었습니다" 가 나와 어색하다. 단계에 AutoAdvanceSec 가 있으면 그 값을 쓴다.
    """
    step = _operator_step(db, s)
    if not step or _auto_value(step.ExpectedIntent or "RESUME") is None:
        return
    delay = _auto_delay(step)
    if delay is None:
        return
    if delay <= 0:
        _auto_advance(db, s, step)
        return
    t = threading.Timer(delay, _auto_advance_later, args=(s.id, step.StepCode))
    t.daemon = True
    t.start()


def _auto_advance_later(session_id: int, step_code: str) -> None:
    """예약된 자동 진행. 그 사이 운영자가 입력했거나(단계가 바뀜) 세션이 닫혔으면 하지 않는다.
    서버가 재시작되면 예약은 사라진다 — 그때는 운영자가 입력하면 된다."""
    from app.database.database import SessionLocal

    db = SessionLocal()
    try:
        s = db.query(ServiceSession).filter(ServiceSession.id == session_id).first()
        if (not s or s.Result not in (RESULT_RUNNING, RESULT_PAUSED)
                or s.CurrentStepCode != step_code):
            return
        step = _operator_step(db, s)
        if step and _auto_advance(db, s, step):
            db.commit()
    except Exception as e:  # noqa: BLE001 — 타이머 스레드라 올려 줄 곳이 없다
        print(f"[GUIDE] 안내 세션 #{session_id} 자동 진행 실패: {e}")
    finally:
        db.close()


@router.get("/session/{session_id}/move/check", response_model=MoveCheckOut)
def session_move_check(session_id: int, place_id: int = Query(...), db: Session = Depends(get_db),
                       _: str = Depends(require_agent)):
    """이동 전 확인 — 로봇을 움직이지 않고 지금 이 장소로 출발할 수 있는지만 본다.

    Jetson 이 "안내하겠습니다" 를 말하기 전에 부른다. 측위 상실·위치 미초기화·통신 두절·맵상 길 없음은
    출발 전에 알 수 있는 실패라, 멘트를 듣고 따라나선 방문자를 제자리에 세워 두지 않는다.
    실제 이동(/move)은 같은 검사를 다시 한다 — 확인과 이동 사이에 상태가 바뀔 수 있다.
    """
    s = db.query(ServiceSession).filter(ServiceSession.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    place = db.query(LocationInfo).filter(LocationInfo.id == place_id).first()
    reason, distance = _move_blocker(db, s, place)
    from app.navigation.send_move import departs_without_reset
    return MoveCheckOut(ok=reason is None, reason=reason,
                        distanceM=round(distance, 1) if distance is not None else None,
                        fastStart=reason is None and departs_without_reset(),
                        departTurn=_depart_turn_cue(db, place) if reason is None and distance is not None else None)


def _depart_turn_cue(db: Session, place: Optional[LocationInfo]) -> Optional[str]:
    """출발 제자리 회전 방향 안내. 길을 잰 경우(위치·경로를 앎)만 부른다. 실패해도 이동 확인은 그대로다."""
    from app.navigation import send_move
    from app.robot_io import runtime
    from app.user_cache import get_robot_id
    rid = get_robot_id()
    if rid is None:
        return None
    try:
        return send_move.depart_turn_preview(db, runtime.get_position(rid), place)
    except Exception as e:  # noqa: BLE001 — 안내 문장 때문에 출발을 막지 않는다
        print(f"[GUIDE] 출발 회전 안내 계산 실패(안내 없이 출발): {e}")
        return None


def _move_blocker(db: Session, s: ServiceSession,
                  place: Optional[LocationInfo]) -> tuple[Optional[str], Optional[float]]:
    """(지금 출발할 수 없는 사유, 경유지를 따라간 거리 m). 출발할 수 있으면 사유는 None.
    거리는 위치를 알고 길을 찾았을 때만 준다."""
    from app.navigation import send_move
    from app.robot_io import runtime
    from app.user_cache import get_robot_id

    if s.Result not in (RESULT_RUNNING, RESULT_PAUSED):
        return "안내가 종료돼 이동하지 않습니다.", None
    if not place:
        return "이동할 장소를 찾을 수 없습니다.", None
    rid = get_robot_id()
    if rid is None:
        return None, None
    if runtime.get_network(rid) in ("Offline", "Error"):
        return "로봇과 통신이 끊겨 있어 이동할 수 없습니다.", None
    if send_move._initpose_blocked():
        return send_move._initpose_block_msg(), None
    if _near_target(rid, place, _near_m_for(s)) or runtime.is_charging(rid):
        # 근처면 움직이지 않는다. 충전 중이면 출발점이 도킹 포인트라(언도킹 경유) 여기서 길을 볼 수 없다
        return None, None
    pos = runtime.get_position(rid)
    if not pos.get("timestamp"):
        return None, None
    from app.navigation.turn_cues import path_length
    from app.navigation.via_route import plan_via_waypoints

    start = (pos["x"], pos["y"])
    from app.robot_io.config import NODE_ROUTING_ENABLED
    if NODE_ROUTING_ENABLED:
        # 실제 이동과 같은 노드 경로로 길이를 잰다(navigation_send_next 가 같은 계산으로 펼친다)
        from app.navigation import node_route
        refused = send_move.node_entry_check(db, [place])
        if refused:
            return refused, None
        goal = {"x": place.LocationX, "y": place.LocationY, "name": place.LacationName}
        plan = node_route.plan_leg(db, int(place.MapId), start, goal)
        if plan.error:
            return plan.error, None
        return None, node_route.leg_length(start, plan.waypoints, goal)
    via, why = plan_via_waypoints(db, place, start)
    if via is None:
        return f"'{place.LacationName}'까지 맵상 안전 우회 경로를 찾지 못했습니다({why}).", None
    pts = [start] + [(w["x"], w["y"]) for w in via] + [(place.LocationX, place.LocationY)]
    return None, path_length(pts)


@router.post("/session/{session_id}/tts-failed")
def session_tts_failed(session_id: int, req: TtsFailedReq, db: Session = Depends(get_db),
                       _: str = Depends(require_agent)):
    """로봇 스피커 발화 실패 보고. 운영자 알림은 세션마다 한 번만 띄우고 이후는 로그만 남긴다."""
    from app.logs.service import log_event
    from app.user_cache import get_robot_business_id, get_robot_id

    s = db.query(ServiceSession).filter(ServiceSession.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    first = session_id not in _tts_alerted_sessions
    _tts_alerted_sessions.add(session_id)
    kind = "질문" if req.critical else "안내 멘트"
    log_event("schedule", "guide_tts_failed" if first else "guide_tts_failed_repeat",
              f"{s.RobotName} 안내 음성 출력 실패({kind})",
              detail=f"session={s.id} step={s.CurrentStepCode} text={req.text[:80]!r} error={(req.error or '')[:120]}",
              robot_id=get_robot_id(), robot_name=s.RobotName, business_id=get_robot_business_id())
    return {"ok": True, "alerted": first}


# 발화 실패 알림을 이미 띄운 세션. 프로세스 재시작으로 비면 한 번 더 뜰 뿐이다.
_tts_alerted_sessions: set[int] = set()


def _current_step(db: Session, s: ServiceSession) -> Optional[VoiceScenarioStep]:
    return (db.query(VoiceScenarioStep)
            .filter(VoiceScenarioStep.ScenarioId == s.ScenarioId,
                    VoiceScenarioStep.StepCode == s.CurrentStepCode).first())


def _is_silent_move(st: Optional[VoiceScenarioStep]) -> bool:
    """멘트 없는 이동 단계인가 — 다음 단계를 말할 장소로 먼저 가는 이동(주차장: 안내 장소로 마중).
    "안내하겠습니다" 없이 출발하므로 따라오는 사람이 없다 — 호출 위치 이동처럼 기본 속도로 간다."""
    return bool(st and st.ActionType == "navigate" and not (st.SayText or "").strip())


def _bundle_voice_exit(st: VoiceScenarioStep) -> dict:
    """운영자 단계의 음성 출구 — 말과 넘어갈 단계가 함께 있어야 싣는다(하나만 있으면 듣지 않는다)."""
    from app.database.voice_scenario_editor import voice_exit_words
    words = voice_exit_words(getattr(st, "VoiceExitKeywords", None))
    to = (getattr(st, "VoiceExitStepCode", None) or "").strip() or None
    if st.WaitFor != WAIT_OPERATOR or not words or not to:
        return {"voiceExitKeywords": [], "voiceExitStepCode": None}
    return {"voiceExitKeywords": words, "voiceExitStepCode": to}


def _bundle_progress(st: VoiceScenarioStep, tnames: dict) -> Optional[str]:
    """엔진이 미리 합성할 진행 멘트 — 이동 때 관제가 낼 문장과 같게 만든다(session_move).
    고객 목적지로 가는 단계는 장소가 세션마다 달라 칸에 적은 문구만 싣는다."""
    from app.navigation.turn_cues import progress_phrase
    if st.ActionType != "navigate" or _is_silent_move(st):
        return None
    place = tnames.get(st.TargetLocationId) if st.TargetLocationId else None
    if not place and not (st.ProgressText or "").strip():
        return None
    return progress_phrase(st.ProgressText, st.SpokenPlaceName, place)


def _slot_spoken_name(db: Session, robot_id: Optional[int], dest: Optional[str],
                      step: VoiceScenarioStep) -> Optional[str]:
    """고객 목적지 단계(SlotName)의 도착지 음성명. 없거나 목적지 단계가 아니면 None."""
    if not step.SlotName or not dest or robot_id is None:
        return None
    slot = (db.query(VoiceSlotValue)
            .filter(VoiceSlotValue.RobotId == robot_id, VoiceSlotValue.Value == dest)
            .first())
    return ((slot.SpokenName or "").strip() or None) if slot else None


@router.post("/session/{session_id}/move")
def session_move(session_id: int, place_id: int = Query(...), db: Session = Depends(get_db),
                 _: str = Depends(require_agent)):
    """세션 맥락에서 이동을 지시한다. 기존 NAV 흐름을 그대로 탄다.

    이동 장소는 '현재 이동 목표'(NavTarget)에만 남긴다. 고객 목적지(Destination)를 덮으면
    발급기로 간 뒤 '고객 목적지' 단계(진료실)가 발급기로 되돌아가고 {목적지} 멘트도 바뀐다.

    속도: 안내 단계는 시나리오의 안내 속도(기본 저속)로, 호출 위치로 가는 이동은 따라올 사람이
    없으므로 기본 속도로 간다.
    """
    s = db.query(ServiceSession).filter(ServiceSession.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    place = db.query(LocationInfo).filter(LocationInfo.id == place_id).first()
    if not place:
        _fail_move(db, s, "이동할 장소를 찾을 수 없습니다.")
    s.NavTargetId = place.id
    s.NavTargetName = place.LacationName
    db.commit()

    speed = None
    arrive_name = None
    progress_text = None
    step = _current_step(db, s) if s.CurrentStepCode != CALL_MOVE_STEP and s.ScenarioId else None
    if step and not _is_silent_move(step):
        sc = db.query(VoiceScenario).filter(VoiceScenario.id == s.ScenarioId).first()
        speed = sc.GuideSpeed if sc and sc.GuideSpeed in GUIDE_SPEEDS else None
        # 단계의 부르는 이름이 없고 고객 목적지로 가는 단계면 목적지 사전의 도착지 음성명을 쓴다
        # ("간호실데스크" 대신 "진료실", "1F_엘리베이터입구" 대신 "엘리베이터 입구")
        arrive_name = step.SpokenPlaceName or _slot_spoken_name(
            db, sc.RobotId if sc else None, s.DestinationName, step)
        from app.navigation.turn_cues import progress_phrase
        # 칸이 비면 "○○ 방향으로 이동하고 있습니다."(부르는 이름 → 장소 이름), "-" 면 말하지 않음
        progress_text = progress_phrase(step.ProgressText, arrive_name, place.LacationName)

    # 이동은 관제가 물고 있는 로봇(send_move 전역)이 한다 — 자세·위치도 그 로봇 기준으로 본다
    from app.user_cache import get_robot_id
    rid = get_robot_id()

    why = _ensure_standing(rid)
    if why:
        _fail_move(db, s, why)

    # 기립을 기다리는 동안 운영자가 안내를 종료했을 수 있다 — 닫힌 세션으로 로봇을 출발시키지 않는다
    db.refresh(s)
    if s.Result not in (RESULT_RUNNING, RESULT_PAUSED):
        raise HTTPException(status_code=409, detail="안내가 종료돼 이동하지 않습니다.")

    if _near_target(rid, place, _near_m_for(s)):
        from app.navigation.send_move import (face_yaw_in_place, mark_arrived_in_place,
                                              prime_nav_gait_in_place)
        mark_arrived_in_place(place.LacationName, place.LocationX, place.LocationY, place.Yaw or 0.0)
        if s.CurrentStepCode == CALL_MOVE_STEP:
            # 시작 위치 생략 — 그 자리에서 시작하되 장소 방향(yaw)으로 먼저 돈다(11-03 메모).
            # 회전 NAV 가 내비 모드로 바꾸므로 이어지는 미리 전환은 바로 끝난다(첫 출발의 보행 전환을 여기로)
            face_yaw_in_place(place.Yaw, "시작 위치 생략")
            prime_nav_gait_in_place("시작 위치 생략")
        return {"status": "ok", "msg": f"이미 {place.LacationName} 근처라 이동하지 않습니다."}

    # send_move 를 재사용한다 — 도착 감지·IsWorking 연동·충전 해제가 따라온다.
    from app.navigation.send_move import start_place_move

    result = start_place_move(place_id, db, speed=speed, keep_gait=True, arrive_name=arrive_name,
                              progress_text=progress_text)
    # move_to_place 는 실패도 200 + status=error 로 돌려준다(위치 미초기화·장소 없음).
    # 그대로 넘기면 Jetson 은 출발한 줄 알고 직전 이동의 '도착' 상태를 읽어 다음 단계로 넘어간다.
    if (result or {}).get("status") != "ok":
        _fail_move(db, s, (result or {}).get("msg") or "이동을 시작하지 못했습니다.")
    return result


FOLLOW_WAITING = "waiting"
FOLLOW_LOST = "lost"
FOLLOW_RESUME = "resume"     # 운영자가 관제에서 재개를 눌렀다 — Jetson 이 보고 다시 출발한다


@router.post("/session/{session_id}/follow", response_model=SessionOut)
def session_follow(session_id: int, req: FollowReq, db: Session = Depends(get_db),
                   _: str = Depends(require_agent)):
    """이동 중 동행 확인 결과를 반영한다.

    - waiting: 방문자가 따라오지 못한다(아니오·무응답) → 주행을 멈추고 기다린다
    - lost:    기다려도 오지 않는다 → 세션을 '입력 대기' 로 두고 관제에 알림을 띄운다
    - ok:      다시 따라온다 → 상태를 지운다. 재출발은 Jetson 이 /move 로 따로 보낸다
    """
    s = db.query(ServiceSession).filter(ServiceSession.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    if s.Result not in (RESULT_RUNNING, RESULT_PAUSED):
        return _session_out(db, s)   # 이미 닫혔다 — 엔진이 종료를 알아챈다

    if req.state == FOLLOW_WAITING:
        from app.navigation.send_move import stop_navigation_internal
        stop_navigation_internal("안내 동행 확인 — 방문자 대기")
        s.FollowState = FOLLOW_WAITING
    elif req.state == FOLLOW_LOST:
        if s.FollowState != FOLLOW_LOST:     # 알림은 한 번만
            from app.logs.service import log_event
            from app.user_cache import get_robot_business_id, get_robot_id
            where = f"{s.NavTargetName} 가는 길" if s.NavTargetName else "이동 중"
            log_event("schedule", "guide_follow_lost",
                      f"{s.RobotName} 안내 중 방문자가 따라오지 않습니다 ({where})",
                      detail=f"session={s.id} step={s.CurrentStepCode} target={s.NavTargetId}",
                      robot_id=get_robot_id(), robot_name=s.RobotName,
                      business_id=get_robot_business_id())
            s.FollowLostAt = datetime.now()
        s.FollowState = FOLLOW_LOST
        s.Result = RESULT_PAUSED
        s.Reason = "방문자가 따라오지 않아 운영자 확인을 기다립니다"
    elif req.state == "ok":
        if s.FollowState and s.Result == RESULT_PAUSED:
            s.Result = RESULT_RUNNING
        s.FollowState = None
        s.FollowLostAt = None
    else:
        raise HTTPException(status_code=422, detail="state 는 waiting | lost | ok 입니다.")

    db.commit()
    db.refresh(s)
    return _session_out(db, s)


def _fail_move(db: Session, s: ServiceSession, why: str) -> None:
    """이동 실패 — 사유를 세션에 남기고(운영자 화면 표시용) Jetson 에 409 를 준다."""
    s.Reason = why[:200]
    db.commit()
    raise HTTPException(status_code=409, detail=why)


def _ensure_standing(robot_id: Optional[int], failed_to: str = "이동하지") -> Optional[str]:
    """로봇이 서 있지 않으면 일으킨다. 실패하면 그 사유.

    앉아 있거나 전원 투입 직후(자세 0)인 로봇은 NAV 를 받아도 출발하지 않는다.
    충전 중이면 건드리지 않는다 — 이동이 충전 해제(도크 이탈)를 먼저 한다.

    Args:
        failed_to: 실패 사유 문장의 동사구("이동하지", "안내를 시작하지").
    """
    import time
    from app.robot_io import runtime
    from app.robot_io.sender import send_to_robot

    if robot_id is None or runtime.is_charging(robot_id):
        return None
    state = runtime.get_motion_state(robot_id)
    if state in STAND_STATES:
        return None

    send_to_robot("STAND", robot_id=robot_id)
    deadline = time.time() + STAND_WAIT_SEC
    while time.time() < deadline:
        time.sleep(0.5)
        state = runtime.get_motion_state(robot_id)
        if state in STAND_STATES:
            time.sleep(1.0)   # 기립 직후 바로 NAV 를 보내면 무시될 수 있다
            return None
    return (f"로봇이 일어서지 않아 {failed_to} 못했습니다(자세 코드 {state}). "
            "원격 제어에서 로봇 상태를 확인하세요.")


def _near_m_for(s: ServiceSession) -> float:
    """이 단계에서 '이미 도착'으로 볼 거리(m). 시작 위치 이동만 넓게 본다."""
    return GUIDE_CALL_NEAR_M if s.CurrentStepCode == CALL_MOVE_STEP else GUIDE_NEAR_M


def _near_target(robot_id: Optional[int], place: LocationInfo, near_m: float = GUIDE_NEAR_M) -> bool:
    """로봇이 이미 목표 근처인가. 위치를 믿을 수 없으면(미초기화·미수신) 아니라고 본다."""
    from app.robot_io import runtime

    if robot_id is None or runtime.is_initpose_pending(robot_id):
        return False
    pos = runtime.get_position(robot_id)
    if not pos or not pos.get("timestamp") or place.LocationX is None or place.LocationY is None:
        return False
    dist = ((pos["x"] - place.LocationX) ** 2 + (pos["y"] - place.LocationY) ** 2) ** 0.5
    return dist < near_m


@router.post("/session/{session_id}/obstacle-answer")
def session_obstacle_answer(session_id: int, req: ObstacleAnswerReq, db: Session = Depends(get_db),
                            _: str = Depends(require_agent)):
    """장애물 긴 정지 뒤 '준비됐어요' 질문에 Jetson 이 들은 대답(navigation/obstacle_hold)."""
    if req.answer not in ("yes", "no", "none"):
        raise HTTPException(status_code=422, detail="answer 는 yes | no | none 입니다.")
    s = db.query(ServiceSession).filter(ServiceSession.id == session_id).first()
    if not s or s.Result not in (RESULT_RUNNING, RESULT_PAUSED):
        return {"accepted": False}
    from app.navigation import obstacle_hold
    return {"accepted": obstacle_hold.submit_answer(req.askId, req.answer)}


@router.post("/session/{session_id}/hold", response_model=SessionOut)
def session_hold(session_id: int, req: HoldReq, db: Session = Depends(get_db),
                 _: str = Depends(require_agent)):
    """Jetson 이 운영자 일시 정지 예약을 보고 다음 이동 단계 앞에서 멈췄다.

    그 사이 운영자가 예약을 취소했으면(HoldState 없음) 바꾸지 않고 돌려준다 — 엔진은 그대로 잇는다.
    도착 후 내비 보행으로 세워 뒀으면 기본 보행·Regular 로 돌린다. 멈춤은 몇 분씩 갈 수 있고,
    재개하면 다음 이동이 출발 리셋과 함께 나간다.
    """
    if req.state != HOLD_HOLDING:
        raise HTTPException(status_code=422, detail="state 는 holding 입니다.")
    s = db.query(ServiceSession).filter(ServiceSession.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    if s.Result not in (RESULT_RUNNING, RESULT_PAUSED) or s.HoldState is None:
        return _session_out(db, s)

    if s.HoldState != HOLD_HOLDING:
        s.HoldState = HOLD_HOLDING
        s.HoldAt = datetime.now()
        s.Result = RESULT_PAUSED
        s.Reason = HOLD_REASON
        db.commit()
        db.refresh(s)
        print(f"[GUIDE] 안내 세션 #{s.id} 일시 정지 시작 (step={s.CurrentStepCode})")

    from app.user_cache import get_robot_name
    if s.RobotName == get_robot_name():
        from app.navigation.send_move import release_kept_gait
        release_kept_gait("안내 일시 정지")
    return _session_out(db, s)


@router.post("/session/{session_id}/close", response_model=SessionOut)
def close_session(session_id: int, req: SessionCloseReq, db: Session = Depends(get_db),
                  _: str = Depends(require_agent)):
    """세션 종료. Result(무엇으로 끝났나) 와 ClosedBy(누가 끝냈나) 를 함께 남긴다."""
    s = db.query(ServiceSession).filter(ServiceSession.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    if s.Result not in (RESULT_RUNNING, RESULT_PAUSED):
        # 운영자가 먼저 닫았다. Jetson 의 늦은 종료 보고로 '누가 끝냈나' 를 덮어쓰지 않는다.
        return _session_out(db, s)
    reason = req.reason
    if not reason and not s.Reason and req.result == "nav_failed":
        # 출발은 했는데 목표에 못 간 경우 — 주행 모듈이 남긴 실패 사유를 옮겨 둔다
        from app.navigation.send_move import last_nav_error_message
        reason = last_nav_error_message() or "로봇이 목표 지점까지 이동하지 못했습니다."
    if req.result != "completed":
        _stop_session_move(s, req.result)
    return _session_out(db, close(db, s, req.result, req.closedBy, reason))


def _stop_session_move(s: ServiceSession, result: str) -> None:
    """실패로 끝난 세션이 시킨 이동이 아직 가고 있으면 세운다.

    Jetson 이 도착을 못 기다리고(도착 대기 초과·이동 명령 응답 지연) 세션을 닫아도 관제 주행은
    계속된다 — 안내가 끝났는데 로봇 혼자 목적지로 가는 상태를 남기지 않는다.
    다른 작업(순찰 등)의 주행은 건드리지 않도록 이 세션의 이동 목표로 가는 중일 때만 세운다.
    """
    from app.navigation import send_move

    if not send_move.is_nav_active() or not s.NavTargetName:
        return
    wps = send_move.waypoints_list
    if not wps or wps[-1].get("name") != s.NavTargetName:
        return
    # 근무에 붙은 안내면 근무(회차)는 남기고 주행만 세운다
    send_move.stop_navigation_internal(f"안내 세션 종료({result})", keep_schedule=True)


def _stale_reason(s: ServiceSession, now: datetime) -> Optional[tuple[str, str, str]]:
    """관제가 닫아야 하는 세션이면 (Result, 로그 action, 사유). action 이 None 이면 종료 후처리가 알린다."""
    if (s.AbortRequest == ABORT_BATTERY and s.AbortRequestAt
            and (now - s.AbortRequestAt).total_seconds() >= BATTERY_ABORT_CLOSE_SEC):
        return (RESULT_BATTERY, None,
                f"배터리 위급으로 안내 중단을 요청한 뒤 {BATTERY_ABORT_CLOSE_SEC // 60}분 안에 "
                "로봇이 안내를 끝내지 않아 관제가 종료했습니다")
    if (s.HoldState == HOLD_HOLDING and s.HoldAt
            and (now - s.HoldAt).total_seconds() >= HOLD_CLOSE_SEC):
        return (RESULT_HOLD_TIMEOUT, "guide_hold_timeout_closed",
                f"안내를 일시 정지한 뒤 {HOLD_CLOSE_SEC // 60}분 동안 재개가 없어 안내를 종료했습니다")
    if (s.FollowState == FOLLOW_LOST and s.FollowLostAt
            and (now - s.FollowLostAt).total_seconds() >= FOLLOW_LOST_CLOSE_SEC):
        return (RESULT_FOLLOW_LOST, "guide_follow_lost_closed",
                f"방문자가 따라오지 않아 직원을 부른 뒤 {FOLLOW_LOST_CLOSE_SEC // 60}분 동안 "
                "재개가 없어 안내를 종료했습니다")
    seen = max(t for t in (s.StartedAt, s.EngineSeenAt, _PROCESS_STARTED_AT) if t)
    if (now - seen).total_seconds() >= ENGINE_SILENT_CLOSE_SEC:
        return (RESULT_ENGINE_OFFLINE, "guide_engine_offline",
                f"로봇 음성 엔진(Jetson) 응답이 {ENGINE_SILENT_CLOSE_SEC // 60}분 넘게 없어 "
                "안내를 종료했습니다")
    return None


def close_stale_sessions(now: Optional[datetime] = None) -> list[tuple[int, str]]:
    """아무도 끝내지 않는 안내 세션(동행 이탈 방치·엔진 끊김)을 닫는다. 닫은 (세션 id, Result) 목록."""
    from app.database.database import SessionLocal
    from app.logs.service import log_event

    now = now or datetime.now()
    closed: list[tuple[int, str]] = []
    db = SessionLocal()
    try:
        candidates = (
            db.query(ServiceSession)
            .filter(ServiceSession.ScenarioId.isnot(None),
                    ServiceSession.Result.in_([RESULT_RUNNING, RESULT_PAUSED]))
            .all()
        )
        for s in candidates:
            hit = _stale_reason(s, now)
            if not hit:
                continue
            db.refresh(s)   # 그 사이 Jetson·운영자가 닫았거나 재개했을 수 있다
            if s.Result not in (RESULT_RUNNING, RESULT_PAUSED) or not _stale_reason(s, now):
                continue
            result, action, reason = hit
            _stop_session_move(s, result)
            close(db, s, result, "timeout", reason)
            robot = db.query(RobotInfo).filter(RobotInfo.RobotName == s.RobotName).first()
            if action:
                log_event("schedule", action, f"{s.RobotName} {reason}",
                          detail=f"session={s.id} step={s.CurrentStepCode} target={s.NavTargetId}",
                          robot_id=robot.id if robot else None, robot_name=s.RobotName,
                          business_id=robot.BusinessId if robot else None)
            closed.append((s.id, result))
    finally:
        db.close()
    return closed


def start_stale_session_watch() -> None:
    """close_stale_sessions 를 주기적으로 돌린다. 서버 시작 시 한 번 부른다."""
    def _loop() -> None:
        while True:
            time.sleep(STALE_SWEEP_SEC)
            try:
                for sid, result in close_stale_sessions():
                    print(f"[GUIDE] 안내 세션 #{sid} 자동 종료 — {result}")
            except Exception as e:  # noqa: BLE001 — 감시 스레드는 죽지 않는다
                print(f"[GUIDE ERR] 오래된 안내 세션 정리 실패: {e}")

    threading.Thread(target=_loop, daemon=True, name="guide-stale-watch").start()
