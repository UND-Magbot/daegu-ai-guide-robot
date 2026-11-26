"""이동 멈춤(막힘·도착 못 함) 상태 — 운영자 선택을 기다리는 동안의 단일 출처 (10-22 P2).

주행 감시(polling)가 '60초 제자리' 를 잡으면 여기에 멈춤을 연다. 예전에는 다시 보내기 3번 뒤 도착으로
치고 다음 지점으로 넘어가, 못 간 지점의 도착 안내를 막힌 자리에서 하고 회차를 '성공' 으로 남겼다.

종류(kind)에 따라 운영자가 고를 수 있는 것과 응답 없을 때(NAV_HOLD_TIMEOUT_SEC) 처리가 다르다.
    guide      안내 이동      다시 출발 · 안내 종료              자동 처리 없음
    work       예약 작업      다시 시도 · 건너뛰고 계속 · 작업 중단  → 회차 중단 + 충전소
    manual     직접 이동      다시 시도 · 건너뛰고 계속 · 이동 취소  → 이동 취소
    standby    대기 장소 이동  다시 시도 · 근무 중단              → 근무 '자리 비움(복귀하지 못함)'
    charge     충전소 복귀     다시 시도                         없음(알림 유지)
로봇이 스스로 다시 움직이면(막힘이 풀림) polling 이 멈춤을 닫고 원래 주행을 잇는다.
"""
from __future__ import annotations

import os
import threading
import time

NAV_HOLD_TIMEOUT_SEC = float(os.getenv("NAV_HOLD_TIMEOUT_SEC", "300"))

ACTIONS = {
    "guide": ("retry", "abort"),
    "work": ("retry", "skip", "abort"),
    "manual": ("retry", "skip", "abort"),
    "standby": ("retry", "abort"),
    "charge": ("retry",),
}
TIMEOUT_KINDS = ("work", "manual", "standby")

_lock = threading.Lock()
_hold: dict | None = None
# polling 이 가져갈 운영자 요청 — 주행 상태는 polling 스레드에서만 바꾼다
_pending: str | None = None


def move_kind() -> str:
    """지금 주행의 종류. 안내 → 충전 → 대기 장소 → 예약 작업 → 직접 이동 순으로 본다."""
    try:
        from app.scheduler import standby_session as sess_mod
        sess = sess_mod.get_session()
        if sess and sess.get("phase") == "guiding":
            return "guide"
        from app.database.routes.voice_scenario import scenario_busy
        from app.user_cache import get_robot_name
        if scenario_busy(get_robot_name()):
            return "guide"
    except Exception:  # noqa: BLE001
        pass
    import app.navigation.send_move as nav
    if getattr(nav, "charge_on_arrival", False) or getattr(nav, "sit_on_arrival", False):
        return "charge"
    from app.scheduler.loop import get_active_schedule_id, get_standby_state
    if get_standby_state() is not None:
        return "standby"
    if get_active_schedule_id() is not None:
        return "work"
    return "manual"


def open_hold(kind: str, wp_no: int, wp_name: str, pos: tuple | None, reason: str) -> bool:
    """멈춤을 연다. 이미 열려 있으면 False."""
    global _hold, _pending
    with _lock:
        if _hold is not None:
            return False
        _hold = {"kind": kind, "wpNo": wp_no, "wpName": wp_name, "since": time.time(),
                 "pos": list(pos) if pos else None, "reason": reason}
        _pending = None
    return True


def close_hold() -> dict | None:
    global _hold, _pending
    with _lock:
        h, _hold, _pending = _hold, None, None
    return h


def get_hold() -> dict | None:
    with _lock:
        return dict(_hold) if _hold else None


def snapshot() -> dict | None:
    """화면용 — 남은 자동 처리 시간 포함."""
    h = get_hold()
    if not h:
        return None
    held = time.time() - h["since"]
    auto_in = max(0, int(NAV_HOLD_TIMEOUT_SEC - held)) if h["kind"] in TIMEOUT_KINDS else None
    from app.robot_io.polling import nav_comm_ok
    out = {**h, "heldSec": int(held), "autoActionInSec": auto_in, "actions": list(ACTIONS[h["kind"]]),
           "online": nav_comm_ok()}
    if h.get("reason") == _obstacle_reason():
        from app.navigation import obstacle_hold
        # 장애물 정지 — 지금 앞 상태(화면이 '다시 출발' 을 막는다), 멈춘 시간은 멈춘 순간부터
        out["front"] = obstacle_hold.front_state()
        out["heldSec"] = int(obstacle_hold.snapshot()["heldSec"]) or out["heldSec"]
    return out


def _obstacle_reason() -> str:
    from app.navigation.obstacle_hold import HOLD_REASON
    return HOLD_REASON


def request(action: str) -> dict:
    """운영자 선택 — 검사만 하고 polling 이 다음 주기에 처리한다."""
    global _pending
    with _lock:
        if _hold is None:
            return {"ok": False, "message": "멈춘 이동이 없습니다."}
        if action not in ACTIONS[_hold["kind"]]:
            return {"ok": False, "message": "이 이동에서 고를 수 없는 처리입니다."}
        if action == "retry" and _hold.get("reason") == _obstacle_reason():
            from app.navigation import obstacle_hold
            why = obstacle_hold.retry_blocked_reason()
            if why:
                return {"ok": False, "message": why}
        from app.robot_io.polling import nav_comm_ok
        if not nav_comm_ok():
            # 받아 두면 복구 뒤 늦게 실행되거나, 안전 정지로 멈춤이 닫히며 조용히 버려진다
            return {"ok": False, "message": "로봇과 통신이 끊겨 지금은 처리할 수 없습니다. 복구되면 다시 고르세요."}
        _pending = action
        kind = _hold["kind"]
    return {"ok": True, "kind": kind, "action": action}


def take_request() -> str | None:
    global _pending
    with _lock:
        a, _pending = _pending, None
    return a


def timed_out() -> bool:
    h = get_hold()
    return bool(h and h["kind"] in TIMEOUT_KINDS and time.time() - h["since"] >= NAV_HOLD_TIMEOUT_SEC)
