import os
import secrets

from fastapi import Depends, Header, HTTPException, Request, WebSocket, WebSocketException, status
from sqlalchemy.orm import Session

from app.database.database import get_db  # re-export (하위 호환)
from app.database.models import UserInfo, UserPermission, MenuInfo, RobotInfo, RobotMapInfo
from app.auth.jwt_handler import decode_token, decode_token_allow_expired


def _user_from_token(token: str | None, db: Session) -> UserInfo:
    """access_token 쿠키 값 → 사용자. HTTP·WebSocket 공용."""
    if not token:
        raise HTTPException(status_code=401, detail="인증이 필요합니다")

    # 정상 디코딩 (만료 체크 포함)
    payload = decode_token(token)
    if not payload:
        # 만료 vs 변조 구분
        payload_any = decode_token_allow_expired(token)
        if payload_any and payload_any.get("type") == "access":
            raise HTTPException(status_code=401, detail="토큰이 만료되었습니다")
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다")

    user_id = int(payload["sub"])
    user = db.query(UserInfo).filter(UserInfo.id == user_id, UserInfo.DeletedAt.is_(None)).first()

    if not user:
        raise HTTPException(status_code=401, detail="사용자를 찾을 수 없습니다")
    if not user.IsActive:
        raise HTTPException(status_code=401, detail="비활성화된 계정입니다")

    # access_token의 버전과 DB 버전 비교 → 권한 변경 등으로 무효화된 토큰 차단
    token_ver = payload.get("ver", 0)
    if token_ver != (user.TokenVersion or 0):
        raise HTTPException(status_code=401, detail="토큰이 만료되었습니다")
    return user


def get_current_user(request: Request, db: Session = Depends(get_db)) -> UserInfo:
    """JWT 쿠키에서 현재 사용자를 인증하여 반환. 권한 목록을 request scope에 캐싱."""
    user = _user_from_token(request.cookies.get("access_token"), db)

    # 권한 목록을 request scope에 1회 캐싱 (admin은 스킵)
    if user.Permission != 1 and not hasattr(request.state, "user_permissions"):
        rows = (
            db.query(MenuInfo.MenuKey)
            .join(UserPermission, UserPermission.MenuId == MenuInfo.id)
            .filter(UserPermission.UserId == user.id, MenuInfo.IsGroup == 0)
            .all()
        )
        request.state.user_permissions = frozenset(r.MenuKey for r in rows)

    return user


def get_current_user_ws(websocket: WebSocket, db: Session = Depends(get_db)) -> UserInfo:
    """WebSocket 용 인증. 브라우저는 핸드셰이크에 쿠키를 실어 보낸다.

    HTTP 용 get_current_user 는 Request 를 받아 WebSocket 경로에서 쓸 수 없다.
    """
    try:
        return _user_from_token(websocket.cookies.get("access_token"), db)
    except HTTPException as e:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason=str(e.detail))


def require_user_or_agent(
    request: Request,
    db: Session = Depends(get_db),
    x_agent_token: str | None = Header(default=None, alias="X-Agent-Token"),
) -> UserInfo | None:
    """로그인 사용자 또는 로봇 에이전트(Jetson, 헤더 X-Agent-Token = env AGENT_TOKEN).

    에이전트면 None 을 돌려준다(사람 계정이 아니다).
    """
    expected = (os.getenv("AGENT_TOKEN") or "").strip()
    if expected and x_agent_token and secrets.compare_digest(x_agent_token, expected):
        return None
    return get_current_user(request, db)


def require_admin(current_user: UserInfo = Depends(get_current_user)) -> UserInfo:
    """관리자 권한 필수."""
    if current_user.Permission != 1:
        raise HTTPException(status_code=403, detail="권한이 없습니다")
    return current_user


def require_permission(menu_key: str):
    """특정 메뉴 접근 권한 검사 (캐싱된 권한 사용). admin은 bypass."""
    def _checker(request: Request, current_user: UserInfo = Depends(get_current_user)) -> UserInfo:
        if current_user.Permission == 1:
            return current_user
        if menu_key not in request.state.user_permissions:
            raise HTTPException(status_code=403, detail="해당 메뉴에 대한 접근 권한이 없습니다")
        return current_user
    return _checker


def require_any_permission(*menu_keys: str):
    """여러 MenuKey 중 하나라도 권한이 있으면 통과 (캐싱된 권한 사용). admin은 bypass."""
    def _checker(request: Request, current_user: UserInfo = Depends(get_current_user)) -> UserInfo:
        if current_user.Permission == 1:
            return current_user
        if not request.state.user_permissions.intersection(menu_keys):
            raise HTTPException(status_code=403, detail="해당 메뉴에 대한 접근 권한이 없습니다")
        return current_user
    return _checker


# ── BusinessId 기반 필터링 헬퍼 ──

def get_business_robot_names(db: Session, business_id: int) -> list[str]:
    """사업장 소속 로봇 이름 목록."""
    rows = db.query(RobotInfo.RobotName).filter(
        RobotInfo.BusinessId == business_id,
        RobotInfo.DeletedAt.is_(None),
    ).all()
    return [r.RobotName for r in rows]


def get_business_robot_ids(db: Session, business_id: int) -> list[int]:
    """사업장 소속 로봇 ID 목록."""
    rows = db.query(RobotInfo.id).filter(
        RobotInfo.BusinessId == business_id,
        RobotInfo.DeletedAt.is_(None),
    ).all()
    return [r.id for r in rows]


def get_business_map_ids(db: Session, business_id: int) -> list[int]:
    """사업장 소속 맵 ID 목록."""
    rows = db.query(RobotMapInfo.id).filter(
        RobotMapInfo.BusinessId == business_id,
    ).all()
    return [r.id for r in rows]


def is_admin(user: UserInfo) -> bool:
    return user.Permission == 1


def require_manager(current_user: UserInfo = Depends(get_current_user)) -> UserInfo:
    """관리자(Permission 1 또는 2) 필수."""
    if current_user.Permission not in (1, 2):
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다")
    return current_user
