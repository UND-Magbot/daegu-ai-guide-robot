"""안내 시나리오 편집기 API — 새로 만들기 · 검사 · 단계 전체 저장 · 버전 · 삭제 · 엔진 지원 목록.

멘트·장소 같은 작은 수정은 그대로 voice_scenario_admin 의 단계 수정 API 를 쓴다.
검사 규칙은 app.database.voice_scenario_editor 에 있다.
"""

import json
from typing import Optional

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.auth.audit import get_client_ip, write_audit
from app.auth.dependencies import require_permission
from app.database import voice_scenario_editor as ed
from app.database.models import (
    LocationInfo,
    RobotMapInfo,
    ServiceSession,
    UserInfo,
    VoiceScenario,
    VoiceScenarioStep,
    VoiceScenarioVersion,
)
from app.database.routes import database, get_db
from app.database.routes.voice import NO_BUSINESS_DETAIL, _get_robot_or_404
from app.database.routes.voice_scenario_admin import (
    _scenario_business,
    _scenario_or_404,
    _standby_tasks_by_code,
)

AUTO_CODE_PREFIX = "scenario_"
NEW_STEPS = (
    {"stepCode": "greet", "stepLabel": "인사", "sayText": "안녕하세요. 안내 로봇입니다.",
     "waitFor": ed.WAIT_NONE, "nextStepCode": ed.FINISH_CODE},
    {"stepCode": ed.FINISH_CODE, "stepLabel": "종료", "sayText": "안녕히 가세요.", "waitFor": ed.WAIT_NONE},
)


# ── 요청 모델 ─────────────────────────────────────────
class StepIn(BaseModel):
    stepCode: Optional[str] = Field(default=None, max_length=40)   # 비우면 서버가 s_NNN 을 붙인다
    stepLabel: Optional[str] = Field(default=None, max_length=ed.MAX_LABEL)
    sayText: Optional[str] = Field(default=None, max_length=1000)   # 400자 넘김은 검사가 알려 준다
    waitFor: str = Field(max_length=20)
    expectedIntent: Optional[str] = Field(default=None, max_length=40)
    retryCount: Optional[int] = None
    responseTimeoutSec: Optional[int] = None
    slotName: Optional[str] = Field(default=None, max_length=20)
    targetLocationId: Optional[int] = None
    nextStepCode: Optional[str] = Field(default=None, max_length=40)
    onNoStepCode: Optional[str] = Field(default=None, max_length=40)
    onTimeoutStepCode: Optional[str] = Field(default=None, max_length=40)
    autoAdvanceSec: Optional[int] = None
    # 이동 단계 칸 — 여기 없으면 화면 저장 때 빠져 NULL 로 덮인다(10-29). 길이 넘김은 검사가 알려 준다
    spokenPlaceName: Optional[str] = Field(default=None, max_length=200)
    progressText: Optional[str] = Field(default=None, max_length=1000)
    # 운영자 단계 음성 출구 — 말 목록(쉼표 구분)과 넘어갈 단계. 길이 넘김은 검사가 알려 준다
    voiceExitKeywords: Optional[str] = Field(default=None, max_length=1000)
    voiceExitStepCode: Optional[str] = Field(default=None, max_length=40)
    isActive: Optional[bool] = True


class StepsReq(BaseModel):
    steps: list[StepIn] = Field(max_length=ed.MAX_STEPS + 1)
    # 운영 맵을 바꾸면서 검사·저장 — 장소 검사가 새 맵 기준이라 단계와 한 번에 바꾼다(따로 하면 한쪽만 저장될 수 있다)
    mapId: Optional[int] = None


class StepsSaveReq(StepsReq):
    baseVersion: int
    note: Optional[str] = Field(default=None, max_length=200)


class RestoreReq(BaseModel):
    baseVersion: int


class ScenarioCreateReq(BaseModel):
    displayName: str = Field(min_length=1, max_length=50)
    mapId: int


# ── 공용 ──────────────────────────────────────────────
def _map_place_ids(db: Session, map_id: Optional[int]) -> list[int]:
    if not map_id:
        return []
    return [r.id for r in db.query(LocationInfo.id).filter(LocationInfo.MapId == map_id).all()]


def check_scenario(db: Session, sc: VoiceScenario, steps: list[dict], map_id: Optional[int] = None) -> dict:
    """map_id: 저장 전 바꾼 운영 맵(없으면 지금 맵) 기준으로 검사한다."""
    target = map_id or sc.MapId
    return ed.check(steps, map_id=target, map_place_ids=_map_place_ids(db, target))


def _running_session(db: Session, sc: VoiceScenario) -> Optional[ServiceSession]:
    return (db.query(ServiceSession)
            .filter(ServiceSession.ScenarioId == sc.id, ServiceSession.EndedAt.is_(None)).first())


def _set_map(db: Session, sc: VoiceScenario, biz: int, map_id: int) -> None:
    m = db.query(RobotMapInfo).filter(RobotMapInfo.id == map_id, RobotMapInfo.BusinessId == biz).first()
    if not m:
        raise HTTPException(status_code=422, detail="이 로봇 사업장의 맵이 아닙니다")
    sc.FloorId, sc.MapId = m.FloorId, m.id
    if sc.StartLocationId:
        start = db.query(LocationInfo).filter(LocationInfo.id == sc.StartLocationId).first()
        if not start or start.MapId != sc.MapId:
            sc.StartLocationId = None      # 다른 맵의 시작 장소로 가면 엉뚱한 곳에 선다


def _save_steps(db: Session, sc: VoiceScenario, raw: list[dict], base_version: int,
                note: Optional[str], user: UserInfo, map_id: Optional[int] = None) -> dict:
    """(맵 변경) → 검사 → 버전 확인 → 단계 전체 교체 → 버전 기록. 한 트랜잭션."""
    biz = _scenario_business(db, sc)
    if base_version != (sc.Version or 0):
        raise HTTPException(status_code=409, detail=(
            f"다른 사람이 먼저 저장했습니다(지금 버전 {sc.Version or 0}). 다시 불러온 뒤 고쳐 주세요."))
    running = _running_session(db, sc)
    if running:
        # 운영자 입력은 진행 중 세션의 현재 단계를 DB 에서 다시 찾는다 — 도중에 단계가 바뀌면 엉뚱하게 넘어간다
        raise HTTPException(status_code=409, detail=(
            f"이 시나리오로 안내가 진행 중입니다(#{running.id}). 안내가 끝난 뒤 저장하세요."))
    if map_id is not None and map_id != sc.MapId:
        _set_map(db, sc, biz, map_id)
    steps = ed.normalize(raw)
    result = check_scenario(db, sc, steps)
    if result["errors"]:
        raise HTTPException(status_code=422, detail={
            "message": f"고칠 곳이 {len(result['errors'])}건 있어 저장하지 않았습니다.", **result})

    ed.ensure_baseline(db, sc, user.id)
    db.query(VoiceScenarioStep).filter(VoiceScenarioStep.ScenarioId == sc.id).delete(synchronize_session=False)
    db.flush()
    for i, s in enumerate(steps):
        db.add(VoiceScenarioStep(
            ScenarioId=sc.id, StepCode=s["stepCode"], StepLabel=s["stepLabel"], StepOrder=(i + 1) * 10,
            SayText=s["sayText"], WaitFor=s["waitFor"], ExpectedIntent=s["expectedIntent"],
            RetryCount=s["retryCount"], ResponseTimeoutSec=s["responseTimeoutSec"],
            ActionType=s["actionType"], SlotName=s["slotName"], TargetLocationId=s["targetLocationId"],
            NextStepCode=s["nextStepCode"], OnNoStepCode=s["onNoStepCode"],
            OnTimeoutStepCode=s["onTimeoutStepCode"], AutoAdvanceSec=s["autoAdvanceSec"],
            SpokenPlaceName=s["spokenPlaceName"], ProgressText=s["progressText"],
            VoiceExitKeywords=s["voiceExitKeywords"], VoiceExitStepCode=s["voiceExitStepCode"],
            IsActive=1 if s["isActive"] else 0))
    db.flush()
    version = ed.record_version(db, sc, ed.current_steps(db, sc), note, user.id)
    return {"version": version, "warnings": result["warnings"], "stepCount": len(steps)}


# ── 엔진 지원 목록 ────────────────────────────────────
@database.get("/voice-engine/capabilities")
def voice_engine_capabilities(current_user: UserInfo = Depends(require_permission("voice"))):
    return ed.capabilities()


# ── 새로 만들기 ───────────────────────────────────────
@database.post("/robots/{robot_id}/voice-scenarios")
def create_voice_scenario(
    robot_id: int,
    req: ScenarioCreateReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """빈 시나리오(인사 → 종료)를 만든다. 코드는 서버가 붙이고, '사용'은 꺼진 채로 시작한다."""
    robot = _get_robot_or_404(db, robot_id, current_user)
    if not robot.BusinessId:
        raise HTTPException(status_code=409, detail=NO_BUSINESS_DETAIL)
    name = req.displayName.strip()
    if not name:
        raise HTTPException(status_code=422, detail="시나리오 이름을 입력하세요")
    m = (db.query(RobotMapInfo)
         .filter(RobotMapInfo.id == req.mapId, RobotMapInfo.BusinessId == robot.BusinessId).first())
    if not m:
        raise HTTPException(status_code=422, detail="이 로봇 사업장의 맵이 아닙니다")

    codes = {c for (c,) in db.query(VoiceScenario.Code).filter(VoiceScenario.RobotId == robot.id).all()}
    n = len(codes) + 1
    while f"{AUTO_CODE_PREFIX}{n}" in codes:
        n += 1
    sort = db.query(sa_func.max(VoiceScenario.SortOrder)).filter(VoiceScenario.RobotId == robot.id).scalar() or 0
    sc = VoiceScenario(RobotId=robot.id, Code=f"{AUTO_CODE_PREFIX}{n}", DisplayName=name,
                       FloorId=m.FloorId, MapId=m.id, IsActive=0, SortOrder=sort + 1, Version=0)
    db.add(sc)
    db.flush()
    _save_steps(db, sc, [dict(s) for s in NEW_STEPS], 0, "새로 만듦", current_user)
    db.commit()
    write_audit(db, current_user.id, "voice_scenario_created", "voice_scenario", sc.id,
                detail=f"{sc.Code} name={name} map={m.id}", ip_address=get_client_ip(request))
    return {"id": sc.id, "code": sc.Code, "version": sc.Version}


# ── 검사 · 저장 ───────────────────────────────────────
@database.post("/voice-scenarios/{scenario_id}/validate")
def validate_voice_scenario(
    scenario_id: int,
    req: StepsReq,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """저장하지 않고 검사만 한다. 화면의 [검사] 버튼."""
    sc = _scenario_or_404(db, scenario_id, current_user)
    biz = _scenario_business(db, sc)
    if req.mapId and not db.query(RobotMapInfo.id).filter(RobotMapInfo.id == req.mapId,
                                                          RobotMapInfo.BusinessId == biz).first():
        raise HTTPException(status_code=422, detail="이 로봇 사업장의 맵이 아닙니다")
    steps = ed.normalize([s.model_dump() for s in req.steps])
    return {**check_scenario(db, sc, steps, req.mapId), "steps": steps}


@database.put("/voice-scenarios/{scenario_id}/steps")
def save_voice_scenario_steps(
    scenario_id: int,
    req: StepsSaveReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """단계 전체 저장. 다음 안내부터 적용된다(엔진이 안내를 시작할 때 번들을 새로 받는다)."""
    sc = _scenario_or_404(db, scenario_id, current_user)
    out = _save_steps(db, sc, [s.model_dump() for s in req.steps], req.baseVersion, req.note, current_user,
                      map_id=req.mapId)
    db.commit()
    write_audit(db, current_user.id, "voice_scenario_saved", "voice_scenario", sc.id,
                detail=(f"{sc.Code} v{out['version']} steps={out['stepCount']} map={sc.MapId} "
                        f"warnings={len(out['warnings'])}"),
                ip_address=get_client_ip(request))
    return out


# ── 버전 ──────────────────────────────────────────────
@database.get("/voice-scenarios/{scenario_id}/versions")
def list_voice_scenario_versions(
    scenario_id: int,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    sc = _scenario_or_404(db, scenario_id, current_user)
    rows = (db.query(VoiceScenarioVersion).filter(VoiceScenarioVersion.ScenarioId == sc.id)
            .order_by(VoiceScenarioVersion.Version.desc()).all())
    names = {u.id: u.UserName for u in db.query(UserInfo).filter(
        UserInfo.id.in_({r.CreatedBy for r in rows if r.CreatedBy} or {0})).all()}
    return {"current": sc.Version or 0, "items": [{
        "version": r.Version, "note": r.Note, "createdAt": r.CreatedAt,
        "createdBy": names.get(r.CreatedBy), "stepCount": len(json.loads(r.StepsJson)),
    } for r in rows]}


def _version_or_404(db: Session, sc: VoiceScenario, version: int) -> VoiceScenarioVersion:
    v = (db.query(VoiceScenarioVersion)
         .filter(VoiceScenarioVersion.ScenarioId == sc.id, VoiceScenarioVersion.Version == version).first())
    if not v:
        raise HTTPException(status_code=404, detail=f"버전 {version} 이(가) 없습니다")
    return v


@database.get("/voice-scenarios/{scenario_id}/versions/{version}")
def get_voice_scenario_version(
    scenario_id: int,
    version: int,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    sc = _scenario_or_404(db, scenario_id, current_user)
    v = _version_or_404(db, sc, version)
    return {"version": v.Version, "note": v.Note, "createdAt": v.CreatedAt, "steps": json.loads(v.StepsJson)}


@database.post("/voice-scenarios/{scenario_id}/versions/{version}/restore")
def restore_voice_scenario_version(
    scenario_id: int,
    version: int,
    req: RestoreReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """그 버전의 단계로 되돌린다. 되돌리기도 새 버전으로 남는다. 지금 맵 기준으로 다시 검사한다."""
    sc = _scenario_or_404(db, scenario_id, current_user)
    v = _version_or_404(db, sc, version)
    out = _save_steps(db, sc, json.loads(v.StepsJson), req.baseVersion, f"버전 {version} 으로 되돌림", current_user)
    db.commit()
    write_audit(db, current_user.id, "voice_scenario_restored", "voice_scenario", sc.id,
                detail=f"{sc.Code} v{version} → v{out['version']}", ip_address=get_client_ip(request))
    return out


# ── 삭제 ──────────────────────────────────────────────
@database.delete("/voice-scenarios/{scenario_id}")
def delete_voice_scenario(
    scenario_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_permission("voice")),
):
    """시나리오와 단계·버전 기록을 지운다. 상시 대기 작업이 쓰거나 안내가 진행 중이면 막는다."""
    sc = _scenario_or_404(db, scenario_id, current_user)
    from app.database.models import RobotInfo
    robot = db.query(RobotInfo).filter(RobotInfo.id == sc.RobotId).first()
    tasks = _standby_tasks_by_code(db, robot.RobotName).get(sc.Code, []) if robot else []
    if tasks:
        raise HTTPException(status_code=409, detail=(
            f"상시 대기 작업 {len(tasks)}건이 쓰고 있어 삭제할 수 없습니다 — "
            + ", ".join(t["name"] for t in tasks) + " 의 응대 방식을 먼저 바꾸세요."))
    running = _running_session(db, sc)
    if running:
        raise HTTPException(status_code=409, detail=f"이 시나리오로 안내가 진행 중입니다(#{running.id}).")
    code, name = sc.Code, sc.DisplayName
    db.query(VoiceScenarioVersion).filter(VoiceScenarioVersion.ScenarioId == sc.id).delete(synchronize_session=False)
    db.query(VoiceScenarioStep).filter(VoiceScenarioStep.ScenarioId == sc.id).delete(synchronize_session=False)
    db.delete(sc)
    db.commit()
    write_audit(db, current_user.id, "voice_scenario_deleted", "voice_scenario", scenario_id,
                detail=f"{code} name={name}", ip_address=get_client_ip(request))
    return {"status": "ok", "deletedId": scenario_id}
