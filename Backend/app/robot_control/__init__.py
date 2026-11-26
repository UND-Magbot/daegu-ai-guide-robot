"""
로봇 제어 엔드포인트 aggregator.
각 도메인 파일이 자체 APIRouter를 가지고 있으며, 여기서 하나로 묶는다.
main.py에서 `from app.robot_control import router` 로 가져가 include한다.

인증: 여기 묶는 경로는 **기본으로 로그인이 필요하다**(라우터 단위로 한 번 건다).
경로마다 Depends 를 붙이던 방식은 빠뜨리면 그대로 열린다 — 실제로 충전소 이동·
언도킹·작업 복귀가 로그인 없이 열려 있었다. 권한(robot-list 등)은 경로에서 더 건다.
로그인 잠금 밖에 두는 것은 아래 세 묶음뿐이다.
  - camera_public_router : 로봇 측 비전 스크립트의 검출 결과 수신(토큰 없음)
  - status_agent_router  : Jetson(X-Agent-Token) 이 읽는 /robot/nav — 자체 검사
  - relocalize_ws_router : WebSocket — 자체 검사(get_current_user_ws)
"""

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.robot_control.camera import router as camera_router, public_router as camera_public_router
from app.robot_control.status import router as status_router, agent_router as status_agent_router
from app.robot_control.charge import router as charge_router
from app.robot_control.return_to_work import router as return_to_work_router
from app.robot_control.battery_resume import router as battery_resume_router
from app.robot_control.error import router as error_router
from app.robot_control.pad import router as pad_router
from app.robot_control.mode import router as mode_router
from app.robot_control.relocalize import router as relocalize_router, ws_router as relocalize_ws_router
from app.robot_control.control_lock_routes import router as control_lock_router
from app.robot_control.arm import router as arm_router  # RM 6축 팔 (Phase 1, additive)

_LOGIN = [Depends(get_current_user)]

router = APIRouter()
router.include_router(camera_router, dependencies=_LOGIN)
router.include_router(status_router, dependencies=_LOGIN)
router.include_router(charge_router, dependencies=_LOGIN)
router.include_router(return_to_work_router, dependencies=_LOGIN)
router.include_router(battery_resume_router, dependencies=_LOGIN)
router.include_router(error_router, dependencies=_LOGIN)
router.include_router(pad_router, dependencies=_LOGIN)
router.include_router(mode_router, dependencies=_LOGIN)
router.include_router(relocalize_router, dependencies=_LOGIN)
router.include_router(control_lock_router, dependencies=_LOGIN)
router.include_router(arm_router, dependencies=_LOGIN)  # /robot/arm/* (RM 6축 팔)

router.include_router(camera_public_router)
router.include_router(status_agent_router)
router.include_router(relocalize_ws_router)

# start_charge 는 navigation/send_move.py 에서 직접 호출하므로 re-export
from app.robot_control.charge import start_charge  # noqa: F401

__all__ = ["router", "start_charge"]
