"""자동문 통과 게이트 — 문 간선 앞 노드에 멈춘 뒤, 문 너머 노드를 보내기 전에 통과 FSM 을 돌린다.

노드 경로(node_route)는 문 간선 양끝 노드에 door_id 를 붙이고, send_move 는 그 노드에서 묶음을 끊어
로봇을 문 앞에 세운다. navigation_send_next 가 '직전 점 = 문 앞 노드, 다음 점 = 같은 문의 반대편 노드'를
보면 여기 run_door_gate 를 부른다. 결과가 "pass" 일 때만 문 너머 노드를 보낸다.

이번 범위(문 앞 대기): APPROACH → REQUEST → OPEN_WAIT → PASS(통과 명령 보냄)까지.
통과 중 감시(닫힘 감지·후진·문틀 이탈 확인)는 카메라 판정기(DetectType=vision 의 판독)와 후진 명령이 생긴 뒤에 붙인다.
- 개폐 호출: auto_sensor(센서식 자동문)·manual(상시 개방)만 지원. 다른 방식은 문 앞에서 멈추고 주행을 끝낸다.
- 열림 판독: open_reader 가 등록돼 있으면 그 값을 쓴다. 없으면 None(판독 없음) —
  DetectType=none 은 3초 뒤 가정 진입, vision 은 가정하지 않고 시간 초과 시 조작자 확인.
- 측위 상실·위치 확인 필요 중에는 열려 있어도 들어가지 않는다(1F 유리 자동문 10-27 실기 사례).
- 조작자 개입(열림 재요청·수동 확인·강제 진행·중단)은 대시보드 오버레이 → door_op API 로 들어온다.
"""
from __future__ import annotations

import math
import time
from typing import Callable, Optional

from app.logs.service import log_event
from app.navigation.door_fsm import DoorFSM, DoorFsmConfig, Intent, Phase
from app.navigation import door_op

TICK_SEC = 1.0
MAX_WAIT_SEC = 600            # 조작자 응답을 기다리는 최대 시간 — 넘으면 주행을 끝낸다(문 앞에 무한정 서 있지 않게)
ENTRY_RADIUS_M = 1.0          # 문 앞 노드에서 이 안이어야 '대기점 도달'
SUPPORTED_CONTROL = {"auto_sensor", "manual"}

# 열림 판독기(카메라 판정기가 생기면 등록). (door_id, robot_id) → True 열림 / False 닫힘 / None 판독 불가
open_reader: Optional[Callable[[int, int], Optional[bool]]] = None


def crossing_door(prev_wp: Optional[dict], wp: Optional[dict]) -> Optional[int]:
    """직전 점과 다음 점이 같은 문의 양쪽 노드면 그 door_id. 아니면 None."""
    if not prev_wp or not wp:
        return None
    a, b = prev_wp.get("door_id"), wp.get("door_id")
    return a if a is not None and a == b else None


def _ids():
    from app.user_cache import get_robot_name, get_robot_business_id
    return {"robot_name": get_robot_name(), "business_id": get_robot_business_id()}


class _GatePorts:
    """문 앞 대기 단계의 외부 작용. 로봇을 움직이는 일은 하지 않는다(통과 명령은 send_move 가 보낸다)."""

    def __init__(self, robot_id: int, door_id: int, door_name: str, entry: dict):
        self.robot_id, self.door_id, self.door_name, self.entry = robot_id, door_id, door_name, entry

    def at_entry(self) -> bool:
        from app.robot_io import runtime
        pos = runtime.get_position(self.robot_id) or {}
        if "x" not in pos:
            return False
        return math.hypot(pos["x"] - self.entry["x"], pos["y"] - self.entry["y"]) <= ENTRY_RADIUS_M

    def door_open(self) -> Optional[bool]:
        if open_reader is None:
            return None
        try:
            return open_reader(self.door_id, self.robot_id)
        except Exception as e:
            print(f"[DOOR] 열림 판독 실패: {e}")
            return None

    def pass_blocker(self) -> Optional[str]:
        from app.robot_io import runtime
        if runtime.is_initpose_pending(self.robot_id):
            return "로봇 위치 확인 필요 — 회복을 기다립니다"
        loc = runtime.get_localization_state(self.robot_id)
        if loc not in (None, 0):
            return "측위 상실 — 회복을 기다립니다"
        return None

    # 이번 범위에서는 PASS 에 들어가면 게이트가 끝나므로 아래 판독은 쓰지 않는다
    def passed(self) -> bool:
        return True

    def clear_of_door(self) -> bool:
        return True

    def request_open(self) -> None:
        pass                        # auto_sensor(접근하면 열림)·manual(상시 개방)은 호출할 것이 없다

    def back_off(self) -> None:
        print("[DOOR] 후진 폴백 없음 — 정지 유지")

    def hold_safe(self, reason: str) -> None:
        print(f"[DOOR] 안전 정지 유지: {reason}")

    def operator_prompt(self, info: dict) -> None:
        log_event("error", "door_pass_fallback",
                  f"자동문 '{self.door_name}' 통과 대기 — {info.get('reason')}. 대시보드에서 확인해 주세요",
                  robot_id=self.robot_id, **_ids())

    def resume(self) -> None:
        pass


def _load_door(door_id: int):
    from app.database.database import SessionLocal
    from app.database.models import DoorInfo, LocationInfo
    db = SessionLocal()
    try:
        d = db.query(DoorInfo).filter(DoorInfo.id == door_id).first()
        if not d:
            return None, {}
        places = {}
        for lid in (d.NearLocationId, d.FarLocationId):
            if lid:
                p = db.query(LocationInfo).filter(LocationInfo.id == lid).first()
                if p:
                    places[lid] = (float(p.LocationX), float(p.LocationY))
        db.expunge(d)
        return d, places
    finally:
        db.close()


def _is_reverse(door, places: dict, entry: dict) -> bool:
    """문 앞 노드가 Far 쪽에 더 가까우면 Far→Near(역방향) 통과."""
    near, far = places.get(door.NearLocationId), places.get(door.FarLocationId)
    if not near or not far:
        return False
    here = (entry["x"], entry["y"])
    return math.dist(here, far) < math.dist(here, near)


def finish_crossing(robot_id: Optional[int]) -> None:
    """문 너머 노드에 도착했거나 주행이 끝났다 — 남은 통과 표시를 거둔다."""
    if robot_id is not None and door_op.get_session(robot_id) is not None:
        door_op.end_session(robot_id)


def run_door_gate(door_id: int, entry: dict, robot_id: int, is_navigating: Callable[[], bool]) -> str:
    """문 앞에서 통과 가능해질 때까지 기다린다. "pass" 면 문 너머 노드를 보내고, "abort" 면 주행을 끝낸다."""
    ids = _ids()
    door, places = _load_door(door_id)
    if door is None:
        log_event("error", "door_pass_abort", f"자동문 #{door_id} 정보가 없어 문을 지나지 않고 멈춥니다",
                  robot_id=robot_id, **ids)
        return "abort"
    name = door.Name or f"#{door_id}"
    if (door.ControlType or "") not in SUPPORTED_CONTROL:
        log_event("error", "door_pass_abort",
                  f"자동문 '{name}' 개폐 방식({door.ControlType or '미지정'})은 아직 자동 통과를 지원하지 않아 멈춥니다",
                  robot_id=robot_id, **ids)
        return "abort"

    cfg = DoorFsmConfig(
        door_id=door_id, door_name=name, reverse=_is_reverse(door, places, entry),
        detect_vision=(door.DetectType or "none") == "vision", tick_sec=TICK_SEC,
        open_timeout_sec=door.OpenTimeoutSec or 20, pass_timeout_sec=door.PassTimeoutSec or 30,
    )
    fsm = DoorFSM(_GatePorts(robot_id, door_id, name, entry), cfg)
    door_op.start_session(robot_id, fsm)
    log_event("schedule", "door_pass_start",
              f"자동문 '{name}' 앞 도착 — 열림 확인 대기({'카메라' if cfg.detect_vision else '판독 없음'})",
              robot_id=robot_id, **ids)

    waited = 0.0
    while True:
        if not is_navigating():
            door_op.end_session(robot_id)
            return "abort"
        if door_op.get_session(robot_id) is not fsm:
            # 조작자 개입 API 가 끝냈다(강제 진행 DONE / 중단 ABORTED) — 끝난 상태로 판정
            phase = fsm.phase
        else:
            phase = fsm.tick()
        if phase in (Phase.PASS, Phase.DONE):
            how = "강제 진행" if phase == Phase.DONE else ("가정 진입" if fsm.assumed_open else "열림 확인")
            log_event("schedule", "door_pass_go", f"자동문 '{name}' 통과 시작({how})", robot_id=robot_id, **ids)
            if phase == Phase.DONE:
                door_op.end_session(robot_id)
            return "pass"
        if phase == Phase.ABORTED:
            door_op.end_session(robot_id)
            log_event("schedule", "door_pass_abort", f"자동문 '{name}' 통과 중단(조작자)", robot_id=robot_id, **ids)
            return "abort"
        if waited >= MAX_WAIT_SEC:
            fsm.submit(Intent.ABORT)
            fsm.tick()
            door_op.end_session(robot_id)
            log_event("error", "door_pass_abort",
                      f"자동문 '{name}' 앞에서 {MAX_WAIT_SEC // 60}분 동안 통과하지 못해 주행을 끝냅니다",
                      robot_id=robot_id, **ids)
            return "abort"
        time.sleep(TICK_SEC)
        waited += TICK_SEC
