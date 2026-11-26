"""다단계 안내 시나리오 — 관제 운영자 경로(로그인 인증).

Jetson 경로(`voice_scenario.py`, X-Agent-Token)와 같은 테이블을 쓰되 하는 일이 다르다.
    - 시나리오 조회·편집 (멘트 · 이동 장소 · 대기 시간)
    - 세션 운영: 안내 시작 · 대기번호/창구 입력 · 진료 후 재개 · 종료   (사양 9.3 운영자 수동 입력)

**운영자 입력은 단계를 넘기는 것으로 전달한다.** 입력값(TicketNo/CounterNo)만 넣으면 두 번째
운영자 단계(창구)에서 이미 들어 있는 대기번호 때문에 엔진이 바로 통과해 버린다. 그래서 입력과
함께 CurrentStepCode 를 다음 단계로 옮기고, 엔진은 '단계가 바뀌었는가' 로 진행을 안다.
"""

from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.audit import get_client_ip, write_audit
from app.auth.dependencies import require_permission
from app.database.models import (
    FloorInfo,
    LocationInfo,
    RobotInfo,
    RobotMapInfo,
    ScheduleInfo,
    ServiceSession,
    UserInfo,
    VoiceScenario,
    VoiceScenarioStep,
    VoiceSlotValue,
)
from app.database.routes import database, get_db
from app.database.routes import voice_scenario as vs
from app.database.routes.voice import NO_BUSINESS_DETAIL, SLOT_NAME, _business_maps, _get_robot_or_404
from app.database import voice_prerun as prerun
from app.database import voice_scenario_editor as ed
from app.database import voice_segment_test as seg
from app.database.voice_loop import judge_next_round
from app.database.voice_slot_links import location_on_map, slot_ids_on_map
from app.database.voice_scenario_defaults import (
    DEFAULT_BY_CODE,
    OPERATOR_INPUTS,
    WAIT_OPERATOR,
    apply_defaults,
    default_step,
)

MAX_SAY_TEXT = 400
MAX_TICKET = 30
MAX_COUNTER = 20
TIMEOUT_RANGE = (3, 600)
FOLLOW_INTERVAL_RANGE = (10, 300)   # 0 은 '자동' 이라 범위 밖이어도 받는다
FOLLOW_WAIT_RANGE = (15, 600)
# 직전 안내 결과를 세션 카드에 보여 주는 기간 — 그보다 오래된 실패는 다시 띄우지 않는다
LAST_ENDED_WINDOW = timedelta(minutes=30)
RETRY_RANGE = (0, 5)
# 운영자 단계 자동 진행(초). -1 은 '자동 진행 안 함', 비우면 전역 규칙(voice_scenario.AUTO_OPERATOR_SEC)
AUTO_ADVANCE_RANGE = (0, 1800)
AUTO_ADVANCE_OFF = -1


# ── 요청 모델 ─────────────────────────────────────────
class ScenarioPatchReq(BaseModel):
    displayName: Optional[str] = Field(default=None, max_length=50)
    floorId: Optional[int] = None
    clearFloor: bool = False          # floorId=None 과 '안 바꿈' 을 구분한다
    mapId: Optional[int] = None
    clearMap: bool = False
    startLocationId: Optional[int] = None   # 초기 대기(호출) 위치 기본값
    clearStartLocation: bool = False
    guideSpeed: Optional[str] = None         # slow | normal | fast
    # 이동 중 동행 확인. 간격 0 = 구간별 과거 평균으로 자동
    followCheckEnabled: Optional[bool] = None
    followIntervalSec: Optional[int] = None
    followMaxWaitSec: Optional[int] = None
    turnCuesEnabled: Optional[bool] = None   # 경유지 방향 안내
    isActive: Optional[bool] = None


class StepPatchReq(BaseModel):
    sayText: Optional[str] = Field(default=None, max_length=MAX_SAY_TEXT)
    targetLocationId: Optional[int] = None
    clearTarget: bool = False
    responseTimeoutSec: Optional[int] = None
    retryCount: Optional[int] = None
    autoAdvanceSec: Optional[int] = None   # 운영자 단계만. -1 = 자동 진행 안 함
    clearAutoAdvance: bool = False         # 자동 진행을 전역 규칙으로 되돌림(비움)
    isActive: Optional[bool] = None
    restoreDefault: bool = False      # 멘트·시간을 기본값으로 (장소 연결은 유지)


class DefaultsReq(BaseModel):
    codes: Optional[list[str]] = None
    reset: bool = False


class SessionOpenReq(BaseModel):
    scenarioCode: str
    # 호출 위치 — 로봇이 먼저 여기로 간 뒤 인사한다. 없으면 제자리에서 시작한다.
    startLocationId: Optional[int] = None


class SegmentReq(BaseModel):
    startStepCode: str = Field(max_length=40)
    endStepCode: str = Field(max_length=40)


class SegmentTestReq(SegmentReq):
    scenarioCode: str
    # 구간 안에서 정해지지 않는 값 — 필요한 것만 받는다(segment-check 의 requiredInputs)
    destinationSlotId: Optional[int] = None
    ticketNo: Optional[str] = Field(default=None, max_length=MAX_TICKET)
    counterNo: Optional[str] = Field(default=None, max_length=MAX_COUNTER)


class OperatorInputReq(BaseModel):
    ticketNo: Optional[str] = Field(default=None, max_length=MAX_TICKET)
    counterNo: Optional[str] = Field(default=None, max_length=MAX_COUNTER)


class SessionCloseReq(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=200)


# ── 직렬화 ────────────────────────────────────────────
def _step_row(st: VoiceScenarioStep, places: dict[int, dict], map_id: Optional[int]) -> dict:
    needs_target = st.ActionType == "navigate" and not st.SlotName
    target = places.get(st.TargetLocationId or 0)
    return {
        "id": st.id,
        "stepCode": st.StepCode,
        "stepLabel": st.StepLabel,
        "stepOrder": st.StepOrder,
        "sayText": st.SayText,
        "waitFor": st.WaitFor,
        "expectedIntent": st.ExpectedIntent,
        "retryCount": st.RetryCount,
        "responseTimeoutSec": st.ResponseTimeoutSec,
        "actionType": st.ActionType,
        "slotName": st.SlotName,
        "targetLocationId": st.TargetLocationId,
        "targetName": target["name"] if target else None,
        "needsTarget": needs_target,
        # 고정 장소로 가야 하는데 연결이 없으면 그 단계에서 안내가 끊긴다 — 화면에서 경고한다
        "targetMissing": needs_target and not st.TargetLocationId,
        # 시나리오 맵이 아닌 맵의 장소 — 좌표계가 달라 엉뚱한 곳에 선다(맵을 바꾼 뒤 남은 연결)
        "targetOffMap": bool(target and map_id and target["mapId"] != map_id),
        "nextStepCode": st.NextStepCode,
        "onNoStepCode": st.OnNoStepCode,
        "onTimeoutStepCode": st.OnTimeoutStepCode,
        "autoAdvanceSec": st.AutoAdvanceSec,
        "spokenPlaceName": st.SpokenPlaceName,
        "progressText": st.ProgressText,
        "voiceExitKeywords": st.VoiceExitKeywords,
        "voiceExitStepCode": st.VoiceExitStepCode,
        "isActive": bool(st.IsActive),
    }


def _scenario_row(db: Session, sc: VoiceScenario, places: dict[int, dict],
                  slots: list[VoiceSlotValue]) -> dict:
    steps = (db.query(VoiceScenarioStep)
             .filter(VoiceScenarioStep.ScenarioId == sc.id)
             .order_by(VoiceScenarioStep.StepOrder, VoiceScenarioStep.id).all())
    rows = [_step_row(st, places, sc.MapId) for st in steps]
    start = places.get(sc.StartLocationId or 0)
    # 운영 맵에 장소가 없는 목적지는 이 시나리오에서 알아듣지 않는다(엔진이 뺀다)
    on_map = slot_ids_on_map(db, sc.RobotId, sc.MapId)
    # 준비 점검 — 목록의 '꺼짐 · 준비됨 / 작성 중' 과 '사용' 켜기 조건
    checked = ed.check([ed.step_to_dict(st) for st in steps], map_id=sc.MapId,
                       map_place_ids=[p["id"] for p in places.values() if p["mapId"] == sc.MapId])
    return {
        "id": sc.id,
        "code": sc.Code,
        "displayName": sc.DisplayName,
        "floorId": sc.FloorId,
        "mapId": sc.MapId,
        "startLocationId": sc.StartLocationId,
        "startLocationName": start["name"] if start else None,
        "guideSpeed": sc.GuideSpeed or "slow",
        "followCheckEnabled": bool(sc.FollowCheckEnabled),
        "followIntervalSec": sc.FollowIntervalSec,      # NULL = 자동(구간별 평균)
        "followMaxWaitSec": sc.FollowMaxWaitSec,
        "turnCuesEnabled": bool(sc.TurnCuesEnabled),
        "offMapTargets": sum(1 for r in rows if r["targetOffMap"] and r["isActive"]),
        "isActive": bool(sc.IsActive),
        "version": sc.Version or 0,
        "checkErrors": checked["errors"],
        "checkWarnings": checked["warnings"],
        "ready": bool(sc.MapId) and not checked["errors"],
        "hasDefault": sc.Code in DEFAULT_BY_CODE,
        "missingTargets": sum(1 for r in rows if r["targetMissing"] and r["isActive"]),
        "destinationsOnMap": sum(1 for s in slots if s.id in on_map),
        "destinationsOffMap": [s.Value for s in slots if s.id not in on_map] if sc.MapId else [],
        "steps": rows,
    }


def _session_row(db: Session, s: ServiceSession) -> dict:
    out = vs._session_out(db, s).model_dump()
    step = None
    if s.ScenarioId and s.CurrentStepCode:
        step = (db.query(VoiceScenarioStep)
                .filter(VoiceScenarioStep.ScenarioId == s.ScenarioId,
                        VoiceScenarioStep.StepCode == s.CurrentStepCode).first())
    sc = db.query(VoiceScenario).filter(VoiceScenario.id == s.ScenarioId).first() if s.ScenarioId else None
    out["scenarioName"] = sc.DisplayName if sc else None
    # 상시 대기 근무에 붙은 안내 — 음성관리(일회성 시연용) 화면은 시작·종료를 숨기고 로봇 상세에서 관리한다
    work = db.query(ScheduleInfo).filter(ScheduleInfo.id == s.ScheduleId).first() if s.ScheduleId else None
    out["scheduleId"] = s.ScheduleId
    out["scheduleName"] = work.WorkName if work else None
    start = (db.query(LocationInfo).filter(LocationInfo.id == s.StartLocationId).first()
             if s.StartLocationId else None)
    out["startLocationName"] = start.LacationName if start else None
    out["heard"] = s.Heard
    out["step"] = {
        "stepCode": step.StepCode,
        "sayText": step.SayText,
        "waitFor": step.WaitFor,
        "expectedIntent": step.ExpectedIntent,
    } if step else None
    # 지금 운영자가 넣어야 하는 값 — 화면은 이것만 보고 입력칸을 고른다
    out["operatorInput"] = (step.ExpectedIntent or "RESUME") if step and step.WaitFor == WAIT_OPERATOR else None
    return out


def _places(db: Session, map_ids: list[int]) -> list[dict]:
    """사업장 맵들의 장소. 화면은 시나리오 맵으로 걸러 보여 준다."""
    if not map_ids:
        return []
    q = db.query(LocationInfo).filter(
        LocationInfo.MapId.in_(map_ids),
    )
    return [{"id": p.id, "name": p.LacationName, "floorId": p.FloorId, "mapId": p.MapId}
            for p in q.order_by(LocationInfo.LacationName).all()]


def map_mismatch(db: Session, robot, sc: VoiceScenario) -> Optional[str]:
    """안내를 시작할 수 없는 맵 상태면 그 이유.

    이동 장소가 시나리오 맵의 좌표라서 로봇이 다른 층·맵에 있으면 첫 이동부터 엉뚱한 곳으로 간다.
    """
    def floor_name(fid):
        f = db.query(FloorInfo).filter(FloorInfo.id == fid).first() if fid else None
        return f.FloorName if f else "층 미지정"

    def map_name(mid):
        m = db.query(RobotMapInfo).filter(RobotMapInfo.id == mid).first() if mid else None
        return m.MapName if m else "맵 미지정"

    if not sc.MapId:
        return (f"'{sc.DisplayName}'의 운영 맵이 지정되지 않았습니다. "
                "안내 시나리오에서 층과 맵을 먼저 설정하세요.")
    if robot.CurrentMapId != sc.MapId:
        return (f"로봇이 {floor_name(robot.CurrentFloorId)} / {map_name(robot.CurrentMapId)}에 있습니다. "
                f"'{sc.DisplayName}' 시나리오는 {floor_name(sc.FloorId)} / {map_name(sc.MapId)} 기준입니다. "
                "맵 관리에서 로봇 맵을 먼저 전환한 뒤 시작하세요.")
    return None


def _map_place_or_422(db: Session, sc: VoiceScenario, place_id: int) -> LocationInfo:
    """시나리오 운영 맵의 장소인지 확인한다 — 다른 맵의 좌표로 가면 엉뚱한 곳에 선다."""
    place = db.query(LocationInfo).filter(LocationInfo.id == place_id).first()
    if not place:
        raise HTTPException(status_code=422, detail="장소를 찾을 수 없습니다")
    if not sc.MapId:
        raise HTTPException(status_code=422, detail="시나리오의 운영 맵을 먼저 지정하세요")
    if place.MapId != sc.MapId:
        raise HTTPException(status_code=422, detail="시나리오 운영 맵의 장소가 아닙니다")
    return place


def _scenario_or_404(db: Session, scenario_id: int, user: UserInfo) -> VoiceScenario:
    sc = db.query(VoiceScenario).filter(VoiceScenario.id == scenario_id).first()
    if not sc:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    _get_robot_or_404(db, sc.RobotId, user)
    return sc


def _session_or_404(db: Session, session_id: int, user: UserInfo) -> tuple[ServiceSession, object]:
    s = db.query(ServiceSession).filter(ServiceSession.id == session_id).first()
    if not s or s.ScenarioId is None:
        raise HTTPException(status_code=404, detail="안내 세션을 찾을 수 없습니다")
    sc = db.query(VoiceScenario).filter(VoiceScenario.id == s.ScenarioId).first()
    if not sc:
        raise HTTPException(status_code=404, detail="세션의 시나리오가 삭제되었습니다")
    robot = _get_robot_or_404(db, sc.RobotId, user)
    return s, robot


def _require_open(s: ServiceSession) -> None:
    if s.Result not in (vs.RESULT_RUNNING, vs.RESULT_PAUSED):
        raise HTTPException(status_code=409, detail="이미 종료된 세션입니다")


def _standby_tasks_by_code(db: Session, robot_name: str) -> dict[str, list[dict]]:
    """이 로봇의 살아 있는 상시 대기 작업을 응대 시나리오 Code 별로 — 끄거나 고치기 전에 보이게."""
    rows = (db.query(ScheduleInfo)
            .filter(ScheduleInfo.RobotName == robot_name,
                    ScheduleInfo.ScheduleMode == "standby",
                    ScheduleInfo.TaskStatus.in_(["대기", "진행중"]),
                    ScheduleInfo.StandbyScenarioCode.isnot(None))
            .order_by(ScheduleInfo.id).all())
    out: dict[str, list[dict]] = {}
    for s in rows:
        out.setdefault(s.StandbyScenarioCode, []).append({"id": s.id, "name": s.WorkName})
    return out


# ── 시나리오 조회·편집 ────────────────────────────────
@database.get("/robots/{robot_id}/voice-scenarios")
def list_voice_scenarios(
    robot_id: int,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    robot = _get_robot_or_404(db, robot_id, current_user)
    maps = _business_maps(db, robot)
    places = _places(db, [m.id for m in maps])
    place_map = {p["id"]: p for p in places}

    floors_q = db.query(FloorInfo).filter(FloorInfo.BusinessId == robot.BusinessId)

    scenarios = (db.query(VoiceScenario).filter(VoiceScenario.RobotId == robot_id)
                 .order_by(VoiceScenario.SortOrder, VoiceScenario.id).all())
    slots = (db.query(VoiceSlotValue)
             .filter(VoiceSlotValue.RobotId == robot_id, VoiceSlotValue.SlotName == SLOT_NAME)
             .order_by(VoiceSlotValue.Value).all())
    used_by = _standby_tasks_by_code(db, robot.RobotName)
    rows = [_scenario_row(db, sc, place_map, slots) for sc in scenarios]
    for row, sc in zip(rows, scenarios):
        row["standbyTasks"] = used_by.get(sc.Code, [])
    return {
        "scenarios": rows,
        "places": places,
        "floors": [{"id": f.id, "name": f.FloorName} for f in floors_q.order_by(FloorInfo.id).all()],
        "maps": [{"id": m.id, "name": m.MapName, "floorId": m.FloorId} for m in maps],
        # 안내 시작 전에 화면이 '로봇 맵이 다르다' 를 미리 보여 줄 수 있게
        "robot": {"currentFloorId": robot.CurrentFloorId, "currentMapId": robot.CurrentMapId},
        "missingDefaults": [c for c in DEFAULT_BY_CODE if c not in {sc.Code for sc in scenarios}],
    }


@database.post("/robots/{robot_id}/voice-scenarios/defaults")
def load_default_scenarios(
    robot_id: int,
    req: DefaultsReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """기본 시나리오 불러오기. reset=False 면 없는 것만 만들고, True 면 단계를 기본값으로 되돌린다."""
    robot = _get_robot_or_404(db, robot_id, current_user)
    if req.reset and vs.scenario_busy(robot.RobotName):
        raise HTTPException(status_code=409, detail="안내가 진행 중이라 시나리오를 되돌릴 수 없습니다")
    if req.reset:
        for sc in db.query(VoiceScenario).filter(VoiceScenario.RobotId == robot_id).all():
            if not req.codes or sc.Code in req.codes:
                ed.ensure_baseline(db, sc, current_user.id)
    result = apply_defaults(db, robot_id, req.codes, reset=req.reset)
    db.flush()
    for code in result.get("reset", []):
        sc = db.query(VoiceScenario).filter(VoiceScenario.RobotId == robot_id, VoiceScenario.Code == code).first()
        if sc:
            ed.record_version(db, sc, ed.current_steps(db, sc), "기본값으로 되돌림", current_user.id)
    db.commit()
    write_audit(db, current_user.id, "voice_scenario_defaults", "voice", robot_id,
                detail=f"created={result['created']} reset={result['reset']}",
                ip_address=get_client_ip(request))
    return {"status": "ok", **result}


def _scenario_business(db: Session, sc: VoiceScenario) -> int:
    """시나리오 로봇의 사업장. 없으면 층·맵을 고를 기준이 없어 거절한다."""
    robot = db.query(RobotInfo).filter(RobotInfo.id == sc.RobotId).first()
    if not robot or not robot.BusinessId:
        raise HTTPException(status_code=409, detail=NO_BUSINESS_DETAIL)
    return robot.BusinessId


def _require_ready(db: Session, sc: VoiceScenario) -> None:
    """'사용' 을 켜려면 운영 맵이 있고 준비 점검 오류가 없어야 한다 — 상시 대기 응대에 바로 쓰인다."""
    if not sc.MapId:
        raise HTTPException(status_code=422, detail="운영 맵을 지정해야 켤 수 있습니다")
    db.flush()
    steps = (db.query(VoiceScenarioStep).filter(VoiceScenarioStep.ScenarioId == sc.id)
             .order_by(VoiceScenarioStep.StepOrder, VoiceScenarioStep.id).all())
    ids = [r.id for r in db.query(LocationInfo.id).filter(LocationInfo.MapId == sc.MapId).all()]
    errors = ed.check([ed.step_to_dict(st) for st in steps], map_id=sc.MapId, map_place_ids=ids)["errors"]
    if errors:
        raise HTTPException(status_code=422, detail=(
            f"준비 점검을 통과해야 켤 수 있습니다 — {errors[0]['message']}"
            + (f" 외 {len(errors) - 1}건" if len(errors) > 1 else "")))


@database.put("/voice-scenarios/{scenario_id}")
def update_voice_scenario(
    scenario_id: int,
    req: ScenarioPatchReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    sc = _scenario_or_404(db, scenario_id, current_user)
    biz = _scenario_business(db, sc)
    if req.displayName is not None:
        name = req.displayName.strip()
        if not name:
            raise HTTPException(status_code=422, detail="시나리오 이름을 입력하세요")
        sc.DisplayName = name
    if req.clearFloor:
        sc.FloorId = None
        sc.MapId = None
    elif req.floorId is not None and req.floorId != sc.FloorId:
        if not db.query(FloorInfo).filter(FloorInfo.id == req.floorId, FloorInfo.BusinessId == biz).first():
            raise HTTPException(status_code=422, detail="이 로봇 사업장의 층이 아닙니다")
        sc.FloorId = req.floorId
        sc.MapId = None           # 층이 바뀌면 이전 층의 맵은 의미가 없다
    if req.clearMap:
        sc.MapId = None
    elif req.mapId is not None:
        m = db.query(RobotMapInfo).filter(RobotMapInfo.id == req.mapId, RobotMapInfo.BusinessId == biz).first()
        if not m:
            raise HTTPException(status_code=422, detail="이 로봇 사업장의 맵이 아닙니다")
        if sc.FloorId and m.FloorId != sc.FloorId:
            raise HTTPException(status_code=422, detail="운영 층에 속한 맵이 아닙니다")
        sc.FloorId = m.FloorId
        sc.MapId = m.id
    if req.clearStartLocation:
        sc.StartLocationId = None
    elif req.startLocationId is not None:
        sc.StartLocationId = _map_place_or_422(db, sc, req.startLocationId).id
    # 맵이 바뀌어 초기 대기 위치가 다른 맵의 장소가 됐으면 비운다 — 남겨 두면 안내 시작 때 엉뚱한 곳으로 간다
    if sc.StartLocationId:
        start = db.query(LocationInfo).filter(LocationInfo.id == sc.StartLocationId).first()
        if not start or start.MapId != sc.MapId:
            sc.StartLocationId = None
    if req.guideSpeed is not None:
        if req.guideSpeed not in vs.GUIDE_SPEEDS:
            raise HTTPException(status_code=422, detail="안내 속도는 slow, normal, fast 중 하나여야 합니다")
        sc.GuideSpeed = req.guideSpeed
    if req.followCheckEnabled is not None:
        sc.FollowCheckEnabled = 1 if req.followCheckEnabled else 0
    if req.followIntervalSec is not None:
        v = req.followIntervalSec
        if v and not FOLLOW_INTERVAL_RANGE[0] <= v <= FOLLOW_INTERVAL_RANGE[1]:
            raise HTTPException(status_code=422, detail="동행 확인 간격은 10~300초(0 = 자동)입니다")
        sc.FollowIntervalSec = v or None
    if req.followMaxWaitSec is not None:
        if not FOLLOW_WAIT_RANGE[0] <= req.followMaxWaitSec <= FOLLOW_WAIT_RANGE[1]:
            raise HTTPException(status_code=422, detail="운영자 호출까지 대기는 15~600초입니다")
        sc.FollowMaxWaitSec = req.followMaxWaitSec
    if req.turnCuesEnabled is not None:
        sc.TurnCuesEnabled = 1 if req.turnCuesEnabled else 0
    if req.isActive is not None:
        if req.isActive and not sc.IsActive:
            _require_ready(db, sc)
        sc.IsActive = 1 if req.isActive else 0
    db.commit()
    write_audit(db, current_user.id, "voice_scenario_updated", "voice", sc.id,
                detail=(f"{sc.Code} name={sc.DisplayName} floor={sc.FloorId} map={sc.MapId} "
                        f"start={sc.StartLocationId} speed={sc.GuideSpeed} active={sc.IsActive}"),
                ip_address=get_client_ip(request))
    return {"status": "ok"}


@database.put("/voice-scenario-steps/{step_id}")
def update_voice_scenario_step(
    step_id: int,
    req: StepPatchReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """멘트 · 이동 장소 · 대기 시간 편집. 단계 흐름(다음 단계)은 바꾸지 않는다 — 엔진과 맞물린 구조다."""
    st = db.query(VoiceScenarioStep).filter(VoiceScenarioStep.id == step_id).first()
    if not st:
        raise HTTPException(status_code=404, detail="단계를 찾을 수 없습니다")
    sc = _scenario_or_404(db, st.ScenarioId, current_user)
    ed.ensure_baseline(db, sc, current_user.id)

    if req.restoreDefault:
        tpl = default_step(sc.Code, st.StepCode)
        if not tpl:
            raise HTTPException(status_code=404, detail="이 단계에는 기본값이 없습니다")
        st.SayText = tpl["SayText"]
        st.ResponseTimeoutSec = tpl["ResponseTimeoutSec"]
        st.RetryCount = tpl["RetryCount"]
        st.AutoAdvanceSec = None

    if req.sayText is not None:
        text = req.sayText.strip()
        if not text:
            raise HTTPException(status_code=422, detail="멘트를 입력하세요")
        st.SayText = text
    if req.clearTarget:
        st.TargetLocationId = None
    elif req.targetLocationId is not None:
        st.TargetLocationId = _map_place_or_422(db, sc, req.targetLocationId).id
    if req.responseTimeoutSec is not None:
        lo, hi = TIMEOUT_RANGE
        if not lo <= req.responseTimeoutSec <= hi:
            raise HTTPException(status_code=422, detail=f"응답 대기는 {lo}~{hi}초여야 합니다")
        st.ResponseTimeoutSec = req.responseTimeoutSec
    if req.retryCount is not None:
        lo, hi = RETRY_RANGE
        if not lo <= req.retryCount <= hi:
            raise HTTPException(status_code=422, detail=f"재질문은 {lo}~{hi}회여야 합니다")
        st.RetryCount = req.retryCount
    if req.clearAutoAdvance:
        st.AutoAdvanceSec = None
    elif req.autoAdvanceSec is not None:
        if st.WaitFor != WAIT_OPERATOR:
            raise HTTPException(status_code=422, detail="자동 진행은 운영자 입력 단계에만 정할 수 있습니다")
        lo, hi = AUTO_ADVANCE_RANGE
        if req.autoAdvanceSec != AUTO_ADVANCE_OFF and not lo <= req.autoAdvanceSec <= hi:
            raise HTTPException(status_code=422, detail=f"자동 진행은 {lo}~{hi}초 또는 끔(-1)이어야 합니다")
        st.AutoAdvanceSec = req.autoAdvanceSec
    if req.isActive is not None:
        st.IsActive = 1 if req.isActive else 0

    db.flush()
    ed.record_version(db, sc, ed.current_steps(db, sc), f"단계 수정 — {st.StepLabel or st.StepCode}", current_user.id)
    db.commit()
    write_audit(db, current_user.id, "voice_scenario_step_updated", "voice", st.id,
                detail=f"{sc.Code}/{st.StepCode}", ip_address=get_client_ip(request))
    return {"status": "ok"}


# ── 세션 운영 (사양 9.3) ──────────────────────────────
@database.get("/robots/{robot_id}/voice-sessions/current")
def current_voice_session(
    robot_id: int,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    robot = _get_robot_or_404(db, robot_id, current_user)
    s = vs._active_session(db, robot.RobotName)
    return {"session": _session_row(db, s) if s else None,
            "lastEnded": None if s else _last_ended(db, robot.RobotName),
            "standbyWork": _standby_work(robot)}


def _standby_work(robot) -> Optional[dict]:
    """이 로봇이 상시 대기 근무 중이면 {scheduleId, workName} — 음성관리 '안내 시작' 을 숨기는 데 쓴다."""
    from app.user_cache import get_robot_id
    if robot.id != get_robot_id():
        return None
    from app.scheduler.loop import get_standby_state
    st = get_standby_state()
    return {"scheduleId": st["schedule_id"], "workName": st.get("work_name")} if st else None


class KeepGaitReq(BaseModel):
    enabled: bool


def _nav_robot_or_409(db: Session, robot_id: int, current_user: UserInfo):
    """보행 유지는 이 관제가 주행시키는 로봇에만 걸린다(send_move 는 로봇 하나를 맡는다)."""
    robot = _get_robot_or_404(db, robot_id, current_user)
    from app.user_cache import get_robot_id
    if robot.id != get_robot_id():
        raise HTTPException(status_code=409, detail="이 관제가 주행시키는 로봇이 아닙니다.")
    return robot


@database.get("/robots/{robot_id}/keep-gait")
def get_keep_gait(
    robot_id: int,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """안내 도착 후 내비 보행 유지 — 현재 켜짐 여부, .env 기본값, 이번 세션 실패로 꺼졌는지."""
    _nav_robot_or_409(db, robot_id, current_user)
    from app.navigation.send_move import keep_gait_state
    return keep_gait_state()


@database.put("/robots/{robot_id}/keep-gait")
def put_keep_gait(
    robot_id: int,
    req: KeepGaitReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """재배포 없이 켜고 끈다. 백엔드를 재시작하면 .env 값으로 돌아간다."""
    _nav_robot_or_409(db, robot_id, current_user)
    from app.navigation.send_move import keep_gait_state, set_keep_gait_enabled
    set_keep_gait_enabled(req.enabled)
    write_audit(db, current_user.id, "keep_gait_toggled", "voice", robot_id,
                detail="켬" if req.enabled else "끔", ip_address=get_client_ip(request))
    return keep_gait_state()


def _last_ended(db: Session, robot_name: str) -> Optional[dict]:
    """직전에 끝난 안내 — 왜 끝났는지(이동 실패 사유 등)를 운영자가 볼 수 있게 한다."""
    s = (db.query(ServiceSession)
         .filter(ServiceSession.RobotName == robot_name,
                 ServiceSession.ScenarioId.isnot(None),
                 ServiceSession.EndedAt.isnot(None),
                 ServiceSession.EndedAt >= datetime.now() - LAST_ENDED_WINDOW)
         .order_by(ServiceSession.id.desc()).first())
    if not s:
        return None
    sc = db.query(VoiceScenario).filter(VoiceScenario.id == s.ScenarioId).first()
    return {
        "id": s.id,
        "scenarioName": sc.DisplayName if sc else None,
        "stepCode": s.CurrentStepCode,
        "result": s.Result,
        "closedBy": s.ClosedBy,
        "reason": s.Reason,
        "endedAt": s.EndedAt,
    }


@database.post("/robots/{robot_id}/voice-sessions")
def open_voice_session(
    robot_id: int,
    req: SessionOpenReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """안내 시작. Jetson 이 세션을 폴링하다 이어받아 첫 단계부터 돈다.

    호출 위치를 주면 로봇이 먼저 그곳으로 이동한 뒤 인사한다(고객이 호출한 자리로 간다).
    """
    robot = _get_robot_or_404(db, robot_id, current_user)
    sc = (db.query(VoiceScenario)
          .filter(VoiceScenario.RobotId == robot_id, VoiceScenario.Code == req.scenarioCode).first())
    start_id = None
    if sc:
        why = map_mismatch(db, robot, sc)
        if why:
            raise HTTPException(status_code=409, detail=why)
        if req.startLocationId:
            start_id = _map_place_or_422(db, sc, req.startLocationId).id

    # 앉은 채로 인사를 시작하지 않게 먼저 일으킨다. 이미 안내 중이면 일으키지 않고 거절한다.
    existing = vs._active_session(db, robot.RobotName)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"이미 진행 중인 세션이 있습니다(#{existing.id}, {existing.CurrentStepCode}).",
        )
    why = vs._ensure_standing(robot_id, failed_to="안내를 시작하지")
    if why:
        raise HTTPException(status_code=409, detail=why)

    s = vs.open_session(db, robot, req.scenarioCode, start_location_id=start_id, allow_inactive=True)
    write_audit(db, current_user.id, "voice_session_opened", "voice", s.id,
                detail=f"robot={robot_id} scenario={req.scenarioCode} start={start_id or '-'}",
                ip_address=get_client_ip(request))
    return {"session": _session_row(db, s)}


def _active_steps(db: Session, scenario_id: int) -> list[VoiceScenarioStep]:
    """엔진이 받는 단계(번들과 같은 기준 — 켜진 단계만)."""
    return (db.query(VoiceScenarioStep)
            .filter(VoiceScenarioStep.ScenarioId == scenario_id, VoiceScenarioStep.IsActive == 1)
            .order_by(VoiceScenarioStep.StepOrder, VoiceScenarioStep.id).all())


def _check_segment(steps: list[VoiceScenarioStep], start: str, end: str) -> list[str]:
    """구간이 돌 수 있는지 보고 미리 넣어야 할 값을 돌려준다. 못 돌면 422."""
    codes = {st.StepCode for st in steps}
    for code in (start, end):
        if code not in codes:
            raise HTTPException(status_code=422, detail=f"'{code}' 단계가 없거나 꺼져 있습니다")
    if not seg.reachable(steps, start, end):
        raise HTTPException(
            status_code=422,
            detail=f"'{start}' 에서 '{end}' 로 이어지지 않습니다. 종료 단계를 시작 단계 뒤에서 고르세요.")
    return seg.required_inputs(steps, start, end)


class LoopCheckReq(BaseModel):
    sessionId: int                 # 방금 끝난 회차
    round: int = Field(ge=1)       # 몇 번째 회차였나(알림 문구용)


def _battery_snapshot(robot_id: int) -> tuple[Optional[int], Optional[int]]:
    """(잔량 %, 최소 배터리 기준 %) — 자동 복귀(auto_return)와 같은 값을 본다."""
    import app.robot_io.runtime as runtime
    with runtime._lock:
        entry = runtime._runtime.get(robot_id) or {}
        battery = dict(entry.get("battery") or {})
        robot_type = entry.get("robot_type", "")
        limit = entry.get("limit_battery")
    return runtime.battery_percent(battery, robot_type), limit


@database.post("/robots/{robot_id}/voice-sessions/loop-check")
def check_voice_loop(
    robot_id: int,
    req: LoopCheckReq,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """안내 반복 — 회차가 끝날 때 화면이 묻는다. 다음 회차를 열어도 되는가(voice_loop.judge_next_round).

    배터리 기준 이하로 멈추면 관제 알림(guide_loop_battery_low)을 남긴다. 다른 사유는 화면에만 보인다.
    """
    import app.robot_io.runtime as runtime
    from app.logs.service import log_event

    robot = _get_robot_or_404(db, robot_id, current_user)
    s = (db.query(ServiceSession)
         .filter(ServiceSession.id == req.sessionId, ServiceSession.RobotName == robot.RobotName).first())
    if not s:
        raise HTTPException(status_code=404, detail="회차 세션을 찾을 수 없습니다.")
    if s.EndedAt is None:
        # 화면이 새 회차를 아직 폴링하지 못해 끝난 줄 알았다 — 잠시 뒤 다시 묻게 한다
        return {"ok": False, "kind": "running", "reason": None, "battery": None, "limit": None,
                "result": s.Result}

    battery, limit = _battery_snapshot(robot.id)
    ok, kind, reason = judge_next_round(s.Result, s.Heard, battery, limit,
                                        runtime.is_initpose_pending(robot.id))
    if kind == "battery":
        sc = db.query(VoiceScenario).filter(VoiceScenario.id == s.ScenarioId).first()
        log_event("schedule", "guide_loop_battery_low",
                  f"{sc.DisplayName if sc else '안내'} 반복 {req.round}회차 후 중지 — {reason}",
                  robot_id=robot.id, robot_name=robot.RobotName, business_id=robot.BusinessId)
    if ok:
        from app.database.routes.voice_scenario import hold_for_loop
        hold_for_loop(robot.RobotName)
    return {"ok": ok, "kind": kind or None, "reason": reason or None,
            "battery": battery, "limit": limit, "result": s.Result}


@database.post("/voice-scenarios/{scenario_id}/segment-check")
def check_voice_segment(
    scenario_id: int,
    req: SegmentReq,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """구간 테스트 미리 보기 — 이어지는가, 미리 넣어야 할 값은 무엇인가."""
    sc = _scenario_or_404(db, scenario_id, current_user)
    steps = _active_steps(db, sc.id)
    codes = {st.StepCode for st in steps}
    if req.startStepCode not in codes or req.endStepCode not in codes:
        return {"reachable": False, "requiredInputs": []}
    if not seg.reachable(steps, req.startStepCode, req.endStepCode):
        return {"reachable": False, "requiredInputs": []}
    return {"reachable": True,
            "requiredInputs": seg.required_inputs(steps, req.startStepCode, req.endStepCode)}


@database.post("/robots/{robot_id}/voice-sessions/test")
def open_voice_test_session(
    robot_id: int,
    req: SegmentTestReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """구간 테스트 시작. 로봇은 시작 단계부터 돌고, 종료 단계를 마치면 관제가 세션을 닫는다.

    이동 단계에서 시작하면 로봇이 지금 자리에서 출발한다 — 앞 단계의 도착 장소에 먼저 세워 둔다.
    응대 통계에는 들어가지 않는다.
    """
    robot = _get_robot_or_404(db, robot_id, current_user)
    sc = (db.query(VoiceScenario)
          .filter(VoiceScenario.RobotId == robot_id, VoiceScenario.Code == req.scenarioCode).first())
    if not sc:
        raise HTTPException(status_code=404, detail=f"시나리오 '{req.scenarioCode}' 이(가) 없습니다.")
    why = map_mismatch(db, robot, sc)
    if why:
        raise HTTPException(status_code=409, detail=why)

    needs = _check_segment(_active_steps(db, sc.id), req.startStepCode, req.endStepCode)
    test = {"startStepCode": req.startStepCode, "endStepCode": req.endStepCode}
    if seg.INPUT_DESTINATION in needs:
        slot = (db.query(VoiceSlotValue)
                .filter(VoiceSlotValue.id == req.destinationSlotId,
                        VoiceSlotValue.RobotId == robot_id).first()) if req.destinationSlotId else None
        if not slot:
            raise HTTPException(status_code=422, detail="이 구간은 고객 목적지가 필요합니다. 목적지를 고르세요.")
        place = location_on_map(db, slot.id, sc.MapId)
        if not place:
            raise HTTPException(status_code=422,
                                detail=f"'{slot.Value}' 은(는) 이 시나리오 맵에 연결된 장소가 없습니다.")
        test.update(destinationId=place.id, destinationName=slot.Value)
    for need, field, label in ((seg.INPUT_TICKET, "ticketNo", "대기번호"),
                               (seg.INPUT_COUNTER, "counterNo", "창구 번호")):
        value = (getattr(req, field) or "").strip()
        if need in needs and not value:
            raise HTTPException(status_code=422, detail=f"이 구간은 {label}가 필요합니다.")
        if value:
            test[field] = value

    existing = vs._active_session(db, robot.RobotName)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"이미 진행 중인 세션이 있습니다(#{existing.id}, {existing.CurrentStepCode}).",
        )
    why = vs._ensure_standing(robot_id, failed_to="구간 테스트를 시작하지")
    if why:
        raise HTTPException(status_code=409, detail=why)

    s = vs.open_session(db, robot, req.scenarioCode, test=test, allow_inactive=True)
    write_audit(db, current_user.id, "voice_session_test_opened", "voice", s.id,
                detail=f"robot={robot_id} scenario={req.scenarioCode} "
                       f"{req.startStepCode}~{req.endStepCode}",
                ip_address=get_client_ip(request))
    return {"session": _session_row(db, s)}


@database.post("/voice-sessions/{session_id}/operator")
def operator_input(
    session_id: int,
    req: OperatorInputReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """운영자 입력 — 대기번호 · 창구 번호 · 재개. 입력과 함께 다음 단계로 넘긴다."""
    s, _robot = _session_or_404(db, session_id, current_user)
    _require_open(s)

    step = (db.query(VoiceScenarioStep)
            .filter(VoiceScenarioStep.ScenarioId == s.ScenarioId,
                    VoiceScenarioStep.StepCode == s.CurrentStepCode).first())
    if not step or step.WaitFor != WAIT_OPERATOR:
        raise HTTPException(status_code=409,
                            detail="지금은 운영자 입력 단계가 아닙니다. 로봇이 해당 단계에 도착하면 입력하세요.")

    kind = step.ExpectedIntent if step.ExpectedIntent in OPERATOR_INPUTS else "RESUME"
    ticket = (req.ticketNo or "").strip()
    counter = (req.counterNo or "").strip()
    if kind == "TICKET":
        if not ticket:
            raise HTTPException(status_code=422, detail="대기번호를 입력하세요")
        s.TicketNo = ticket
    elif kind == "COUNTER":
        if not counter:
            raise HTTPException(status_code=422, detail="창구 번호를 입력하세요")
        s.CounterNo = counter
        if ticket:            # 번호가 바뀌었으면 함께 고칠 수 있게 한다
            s.TicketNo = ticket

    detail = f"{step.StepCode} {kind} ticket={s.TicketNo or '-'} counter={s.CounterNo or '-'}"
    if s.IsTest and step.StepCode == s.TestEndStepCode:
        # 구간 테스트의 종료 단계 — 다음 단계로 넘기지 않고 끝낸다
        vs.close(db, s, vs.RESULT_TEST_DONE, "operator", "구간 테스트 종료 단계 도달")
    elif step.NextStepCode:
        s.CurrentStepCode = step.NextStepCode
        s.Result = vs.RESULT_RUNNING
        db.commit()
    else:
        vs.close(db, s, "completed", "operator", "운영자 입력으로 종료")

    write_audit(db, current_user.id, "voice_session_operator", "voice", s.id,
                detail=detail, ip_address=get_client_ip(request))
    db.refresh(s)
    return {"session": _session_row(db, s)}


@database.post("/voice-sessions/{session_id}/follow-resume")
def follow_resume(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """동행 이탈로 멈춘 안내를 운영자가 재개한다. 로봇(Jetson)이 세션을 폴링하다 보고 다시 출발한다."""
    s, _robot = _session_or_404(db, session_id, current_user)
    _require_open(s)
    if s.FollowState not in (vs.FOLLOW_WAITING, vs.FOLLOW_LOST):
        raise HTTPException(status_code=409, detail="동행 확인으로 멈춘 안내가 아닙니다.")
    s.FollowState = vs.FOLLOW_RESUME
    db.commit()
    write_audit(db, current_user.id, "voice_session_follow_resume", "voice", s.id,
                detail=f"step={s.CurrentStepCode} target={s.NavTargetId}",
                ip_address=get_client_ip(request))
    db.refresh(s)
    return {"session": _session_row(db, s)}


@database.post("/voice-sessions/{session_id}/hold")
def hold_voice_session(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """안내 일시 정지 예약. 바로 세우지 않는다 — 로봇(Jetson)이 다음 이동 단계 앞에서 멈춘다.

    이동 중이면 그 목적지의 도착 안내까지, 운영자 입력 대기 중이면 입력(자동 입력 포함)으로 넘어간 뒤
    다음 이동 직전에 멈춘다.
    """
    s, _robot = _session_or_404(db, session_id, current_user)
    _require_open(s)
    if s.HoldState:
        raise HTTPException(status_code=409, detail="이미 일시 정지했거나 예약된 안내입니다.")
    s.HoldState = vs.HOLD_REQUESTED
    db.commit()
    write_audit(db, current_user.id, "voice_session_hold", "voice", s.id,
                detail=f"step={s.CurrentStepCode} target={s.NavTargetId}",
                ip_address=get_client_ip(request))
    db.refresh(s)
    return {"session": _session_row(db, s)}


@database.post("/voice-sessions/{session_id}/hold-release")
def release_voice_session_hold(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """일시 정지 재개(또는 아직 멈추기 전이면 예약 취소). 로봇이 세션을 폴링하다 보고 다음 단계를 잇는다."""
    s, _robot = _session_or_404(db, session_id, current_user)
    _require_open(s)
    if not s.HoldState:
        raise HTTPException(status_code=409, detail="일시 정지한 안내가 아닙니다.")
    was = s.HoldState
    s.HoldState = None
    s.HoldAt = None
    if was == vs.HOLD_HOLDING:
        s.Result = vs.RESULT_RUNNING
        if s.Reason == vs.HOLD_REASON:
            s.Reason = None
    db.commit()
    write_audit(db, current_user.id, "voice_session_hold_release", "voice", s.id,
                detail=f"{was} step={s.CurrentStepCode}", ip_address=get_client_ip(request))
    db.refresh(s)
    return {"session": _session_row(db, s)}


@database.post("/voice-sessions/{session_id}/close")
def close_voice_session(
    session_id: int,
    req: SessionCloseReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """작업 종료. 이동 중이면 로봇도 세운다 — 안내할 사람이 없는데 목적지로 계속 걸어가면 안 된다."""
    s, _robot = _session_or_404(db, session_id, current_user)
    _require_open(s)

    import app.navigation.send_move as nav_mod
    if nav_mod.is_navigating:
        # 주행만 세운다 — 상시 대기 근무에 붙은 안내라도 근무는 남기고 종료 후처리가 대기 장소로 보낸다
        nav_mod.stop_navigation_internal("운영자 안내 종료", keep_schedule=True)

    vs.close(db, s, "aborted_operator", "operator", req.reason or "운영자 종료")
    write_audit(db, current_user.id, "voice_session_closed", "voice", s.id,
                detail=req.reason or "운영자 종료", ip_address=get_client_ip(request))
    return {"session": _session_row(db, s)}


# ── 안내 사전 주행 (voice_prerun) ─────────────────────
class PrerunStartReq(SessionOpenReq):
    # 안 보내면 고속(안내 시작 창의 '고속 1바퀴'). 층 이동 시험 탭은 저속·보통도 고른다.
    speed: Literal["slow", "normal", "fast"] = prerun.PRERUN_SPEED
    # 1회 주행 테스트의 '갈 곳 고르기' — 동선 미리보기(plan) 구간 번호(0부터). 안 보내면 동선 전체
    legIndexes: Optional[list[int]] = None
    # 고를 때 본 동선 구간 수 — 그 사이 시나리오가 바뀌어 번호가 어긋나면 거절한다
    legCount: Optional[int] = None


class PrerunResumeReq(BaseModel):
    # True = 멈춘 구간부터 · False = 처음부터
    fromFailed: bool = True


def _prerun_plan(db: Session, robot_id: int, req: SessionOpenReq) -> tuple[VoiceScenario, dict]:
    sc = (db.query(VoiceScenario)
          .filter(VoiceScenario.RobotId == robot_id, VoiceScenario.Code == req.scenarioCode).first())
    if not sc:
        raise HTTPException(status_code=404, detail=f"시나리오 '{req.scenarioCode}' 이(가) 없습니다.")
    if not sc.MapId:
        raise HTTPException(status_code=422, detail="시나리오의 운영 맵을 먼저 지정하세요")
    start = _map_place_or_422(db, sc, req.startLocationId) if req.startLocationId else None
    return sc, prerun.plan_route(db, sc, start)


def _prerun_blocker(db: Session, robot) -> Optional[str]:
    """사전 주행을 걸 수 없는 상태면 그 이유 — 안내·다른 이동·스케줄과 겹치지 않게 한다."""
    import app.navigation.send_move as nav_mod
    from app.scheduler import standby_session as sess_mod
    from app.scheduler.loop import get_active_schedule_id

    existing = vs._active_session(db, robot.RobotName)
    if existing:
        return f"진행 중인 안내가 있습니다(#{existing.id}). 끝난 뒤 사전 주행하세요."
    if nav_mod.is_navigating:
        return "로봇이 다른 이동 중입니다. 이동이 끝난 뒤 사전 주행하세요."
    if sess_mod.is_active() or get_active_schedule_id() is not None:
        return "스케줄 작업(상시 대기 포함)이 진행 중입니다. 끝낸 뒤 사전 주행하세요."
    if nav_mod._initpose_blocked():
        return nav_mod._initpose_block_msg()
    return vs._ensure_standing(robot.id, failed_to="사전 주행을 시작하지")


@database.post("/robots/{robot_id}/voice-prerun/plan")
def preview_voice_prerun(
    robot_id: int,
    req: SessionOpenReq,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """사전 주행 동선 미리 보기 — 안내 시작 창이 경로 한 줄을 그린다."""
    _get_robot_or_404(db, robot_id, current_user)
    _sc, plan = _prerun_plan(db, robot_id, req)
    return plan


@database.get("/robots/{robot_id}/voice-prerun")
def current_voice_prerun(
    robot_id: int,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """사전 주행 진행 상태. 이 관제가 주행시키는 로봇이 아니면 늘 없음."""
    robot = _get_robot_or_404(db, robot_id, current_user)
    from app.user_cache import get_robot_id
    snap = prerun.snapshot() if robot.id == get_robot_id() else None
    return {"prerun": snap}


@database.post("/robots/{robot_id}/voice-prerun")
def start_voice_prerun(
    robot_id: int,
    req: PrerunStartReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """사전 주행 시작 — 멘트 없이 고른 속도(기본 고속)로 시나리오 동선을 한 바퀴 돈다. 안내는 따로 연다."""
    robot = _nav_robot_or_409(db, robot_id, current_user)
    sc, plan = _prerun_plan(db, robot_id, req)
    why = map_mismatch(db, robot, sc)
    if why:
        raise HTTPException(status_code=409, detail=why)
    if not plan["legs"]:
        raise HTTPException(status_code=422, detail="이 시나리오에는 사전 주행할 이동 장소가 없습니다.")
    # 안내 시작 창의 '고속 1바퀴'는 고르기 칸 없는 요청(SessionOpenReq)으로 온다 — 그때는 동선 전체
    leg_indexes = getattr(req, "legIndexes", None)
    leg_count = getattr(req, "legCount", None)
    if leg_indexes is not None:
        if leg_count is not None and leg_count != len(plan["legs"]):
            raise HTTPException(status_code=409, detail="동선이 바뀌었습니다. 갈 곳 고르기 창을 다시 열어 주세요.")
        if any(i < 0 or i >= len(plan["legs"]) for i in leg_indexes):
            raise HTTPException(status_code=422, detail="고른 구간 번호가 동선 밖입니다.")
        plan = prerun.select_legs(plan, leg_indexes)
        if not [lg for lg in plan["legs"] if lg["role"] != prerun.ROLE_START]:
            raise HTTPException(status_code=422, detail="갈 곳을 하나 이상 고르세요.")
    why = _prerun_blocker(db, robot)
    if why:
        raise HTTPException(status_code=409, detail=why)
    speed = getattr(req, "speed", prerun.PRERUN_SPEED)   # 속도 칸 없는 예전 요청은 고속
    try:
        snap = prerun.start(robot, sc, req.startLocationId, plan, speed)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    write_audit(db, current_user.id, "voice_prerun_started", "voice", sc.id,
                detail=f"robot={robot_id} scenario={sc.Code} legs={len(plan['legs'])} speed={speed}",
                ip_address=get_client_ip(request))
    return {"prerun": snap}


@database.post("/robots/{robot_id}/voice-prerun/resume")
def resume_voice_prerun(
    robot_id: int,
    req: PrerunResumeReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """멈춘 사전 주행을 멈춘 구간부터 또는 처음부터 다시 돈다."""
    robot = _nav_robot_or_409(db, robot_id, current_user)
    why = _prerun_blocker(db, robot)
    if why:
        raise HTTPException(status_code=409, detail=why)
    try:
        snap = prerun.resume(req.fromFailed)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    write_audit(db, current_user.id, "voice_prerun_resumed", "voice", robot_id,
                detail="멈춘 구간부터" if req.fromFailed else "처음부터", ip_address=get_client_ip(request))
    return {"prerun": snap}


@database.post("/robots/{robot_id}/voice-prerun/stop")
def stop_voice_prerun(
    robot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    _nav_robot_or_409(db, robot_id, current_user)
    snap = prerun.stop()
    write_audit(db, current_user.id, "voice_prerun_stopped", "voice", robot_id,
                ip_address=get_client_ip(request))
    return {"prerun": snap}


@database.post("/robots/{robot_id}/voice-prerun/dismiss")
def dismiss_voice_prerun(
    robot_id: int,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """끝난 사전 주행 기록을 닫는다(안내를 시작했거나 그냥 닫았다)."""
    _nav_robot_or_409(db, robot_id, current_user)
    try:
        prerun.dismiss()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"prerun": None}
