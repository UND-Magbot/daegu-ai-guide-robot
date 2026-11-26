"""배터리 최소 기준 기반 충전 복귀와 충전 후 이어 가기.

status_thread 가 매 heartbeat 마다 `check_battery_and_return(robot_id)` 를 호출한다.
정책(2026-10-22 — 기준 하나, 충전 후 규칙은 10-16 결정):
  - 로봇별 최소 배터리(robot_info.LimitBattery) 이하가 LOW_CONFIRM_COUNT 번 연속이면 충전소로 간다.
      · 안내 시나리오 중: 그 자리에 서서 사과·안내 데스크 멘트를 끝까지 한 뒤 안내를 닫고(aborted_battery) 충전소로.
      · 상시 대기 근무 중: 근무는 남기고 자리 비움('충전소로 이동 중' → 도킹 후 '충전 중').
        RESUME_PCT 가 되면 대기 장소로 돌아가 근무를 이어 간다.
      · 예약 작업(순찰·경로) 중: 회차를 battery_low 로 끝낸다. 단일 작업은 끝, 반복 작업은 다음 회차를
        시작 조건(executor.battery_gate — 배터리 부족 복귀 뒤엔 RESUME_PCT 전까지 보류)을 거쳐 그대로.
      · 화면 경로 N회 반복 중: RESUME_PCT 가 되면 남은 횟수를 이어 간다(끊긴 바퀴는 처음부터).
      · 대기 중: 바로 충전소로.
  - 충전 후 이어 가기·운영자 확인([충전 후 재개]/[오늘은 여기서 종료])은 battery_resume 모듈.
  - 엘리베이터 층 전환·자동문 통과 중이면 끝날 때까지 미룬다(중간에 끊으면 더 위험).
  - 충전소 가는 길은 노드망을 따른다. 노드망이 미완성인 맵이면 직행한다(charge.py).
  - 한 번 트리거 후 COOLDOWN_SEC 쿨다운: 사용자가 수동 정지해도 즉시 재트리거 안 함.
  - 회복 조건(쿨다운 해제): ① 충전 진입(is_charging=True), 또는
      ② 배터리가 기준보다 RECOVERY_HYSTERESIS %p 이상 높게 회복.
  - LimitBattery 가 0 이면 기능을 끈다. 값이 비어 있을 때(DB 직접 입력 등)만 30% 를 쓴다.
"""
from __future__ import annotations

import threading
import time

# 한 번 트리거 후 재시도까지의 최소 간격 (초). 사용자 수동 정지/복귀 취소 후
# 즉시 재트리거되는 것을 막는다. 충전 진입 시 자연스럽게 armed로 회복되므로
# 정상 흐름에선 이 쿨다운이 닿지 않는다.
COOLDOWN_SEC = 600

# 기준 위로 이 정도(%) 회복돼야 다시 armed 처리. 기준 근처에서 깜빡거리는
# 충전 시작/해제로 인한 재트리거 방지(히스테리시스).
RECOVERY_HYSTERESIS = 5

# 하던 일을 바로 끊으니 오독(통신 직후 0% 등) 한 번으로 끊기지 않게 연속 확인한다.
# runtime.BATTERY_LOW_CONFIRM_COUNT 와 같은 값.
LOW_CONFIRM_COUNT = 3

# 값이 비어 있을 때만 쓰는 기본 기준(%) — 로봇 등록 화면의 처음 값과 같다.
DEFAULT_LIMIT_PCT = 30

# 배터리 부족으로 충전하러 간 뒤 근무·작업을 다시 시작하는 배터리(%) — 10-16 결정
RESUME_PCT = 80

LOW_BATTERY_SPEECH = "배터리가 부족해 충전하러 갑니다."
# 안내 중 — 사과와 이후 도움받을 곳을 말한 뒤에 떠난다(10-22 사용자 결정 B안)
GUIDE_END_SPEECH = ("죄송합니다. 배터리가 부족해서 안내를 여기까지만 도와드릴게요. "
                    "가까운 안내 데스크에 문의해 주시면 도와드릴 거예요.")
# 멘트 길이 어림(한국어 TTS 초당 글자 수) — 음성 에이전트가 재생 끝을 알려 주는지 확인 전까지 쓴다
SPEECH_CHARS_PER_SEC = 7.0

_lock = threading.Lock()
_state: dict[int, dict] = {}  # robot_id → {"armed", "last_trigger_ts", "low_count", "deferred"}
_low_return: set[int] = set()  # 배터리 부족으로 충전하러 간 로봇 — RESUME_PCT 전까지 작업 시작 보류


def limit_of(entry: dict) -> int:
    """런타임 엔트리의 최소 배터리 기준(%). 0 이면 끔, 비어 있으면 DEFAULT_LIMIT_PCT."""
    v = entry.get("limit_battery")
    return DEFAULT_LIMIT_PCT if v is None else int(v)


def mark_low_return(robot_id: int) -> None:
    """배터리 부족으로 충전하러 간다 — RESUME_PCT 가 될 때까지 근무·작업 시작을 보류한다."""
    with _lock:
        _low_return.add(robot_id)


def low_return_pending(robot_id: int) -> bool:
    with _lock:
        return robot_id in _low_return


def check_battery_and_return(robot_id: int) -> None:
    """heartbeat 직후 호출. 조건 충족 시 충전 복귀 트리거, 충전이 끝났으면 하던 일을 이어 간다."""
    import app.robot_io.runtime as runtime

    # 런타임 스냅샷 — 락 보유 시간 최소화
    with runtime._lock:
        entry = runtime._runtime.get(robot_id)
        if not entry:
            return
        battery = dict(entry.get("battery") or {})
        robot_type = entry.get("robot_type", "")
        is_charging = entry.get("_is_charging", False)
        limit = limit_of(entry)
        basic = entry.get("basic_status") or {}
        sleep_val = basic.get("Sleep")

    # 로봇이 꺼져있으면(Sleep != 0) 트리거 의미 없음. 깨우는 책임 없음.
    if sleep_val != 0:
        return
    # LimitBattery <= 0 이면 기능 비활성
    if limit <= 0:
        return

    bat_pct = runtime.battery_percent(battery, robot_type)
    if bat_pct is None:
        return

    with _lock:
        if bat_pct >= RESUME_PCT:
            _low_return.discard(robot_id)
    from app.robot_control import battery_resume
    battery_resume.tick(robot_id, bat_pct, is_charging, RESUME_PCT)

    now = time.time()
    with _lock:
        state = _state.setdefault(robot_id, {"armed": True, "last_trigger_ts": 0.0})

        # 회복 조건: 충전 진입 또는 기준+히스테리시스 이상으로 회복 → armed 복원
        if is_charging or bat_pct >= limit + RECOVERY_HYSTERESIS:
            state["armed"] = True
        # 쿨다운 종료 → armed 복원
        if not state["armed"] and (now - state["last_trigger_ts"]) > COOLDOWN_SEC:
            state["armed"] = True
        # 위치를 몰라 못 떠났던 복귀 — 위치가 다시 잡히면 쿨다운을 기다리지 않고 바로 다시 시도한다
        if state.get("pose_wait") and not runtime.is_initpose_pending(robot_id):
            state.pop("pose_wait", None)
            state["armed"] = True

        if is_charging or bat_pct > limit:
            state["low_count"] = 0
            state.pop("deferred", None)
            return
        state["low_count"] = state.get("low_count", 0) + 1
        if state["low_count"] < LOW_CONFIRM_COUNT or not state["armed"]:
            return
        busy = _busy(robot_id)
        if busy:
            first = not state.get("deferred")
            state["deferred"] = True
        else:
            state["armed"] = False
            state["last_trigger_ts"] = now
            state.pop("deferred", None)

    if busy:
        if first:
            _log("battery_low_deferred",
                 f"배터리 {bat_pct}% (기준 {limit}%) — {busy} 중이라 끝난 뒤 충전소로 갑니다", robot_id)
        return
    mark_low_return(robot_id)
    _go_charge_now(robot_id, bat_pct, limit)


def _route_snapshot() -> dict | None:
    """화면 경로 N회(또는 무한) 반복 중이면 이어 갈 정보. 끊긴 바퀴는 처음부터 다시 돈다."""
    import app.navigation.send_move as nav_mod
    infinite = bool(nav_mod.nav_loop_infinite)
    if not infinite and nav_mod.nav_loop_total <= 1:
        return None
    return {
        # 노드망 경유점은 출발 위치마다 다시 펼친다 — 경로의 장소만 남긴다
        "waypoints": [dict(w) for w in nav_mod.waypoints_list if not w.get("node")],
        "infinite": infinite,
        "laps": nav_mod.nav_loop_remaining + 1,
        "total": nav_mod.nav_loop_total,
        "done": max(0, nav_mod.nav_loop_count - 1),
        "auto_charge": bool(nav_mod.auto_return_to_charge),
        "label": " → ".join(w.get("name", "?") for w in nav_mod.waypoints_list if not w.get("node")),
    }


def _busy(robot_id: int) -> str:
    """충전 복귀를 미뤄야 하는 동작 중이면 그 이름. 엘리베이터 층 전환·자동문 통과는 중간에 끊으면 더 위험하다."""
    try:
        from app.navigation.floor_transition import is_transitioning
        if is_transitioning(robot_id):
            return "층 전환"
    except Exception:
        pass
    try:
        from app.navigation.door_op import get_session
        if get_session(robot_id) is not None:
            return "자동문 통과"
    except Exception:
        pass
    return ""


def _log(action: str, msg: str, robot_id: int) -> None:
    from app.logs.service import log_event
    from app.user_cache import get_robot_name, get_robot_business_id
    print(f"🔋 [AUTO-RETURN] {msg}")
    log_event("robot", action, msg, robot_id=robot_id, robot_name=get_robot_name(),
              business_id=get_robot_business_id())


def wait_for_pose(robot_id: int, reason: str) -> None:
    """위치를 몰라 충전소로 못 떠났다(노드망 경로 켬, 10-23) — 알리고, 위치가 다시 잡히면 바로 다시 시도하게 둔다."""
    with _lock:
        state = _state.setdefault(robot_id, {"armed": False, "last_trigger_ts": time.time()})
        state["armed"] = False
        state["pose_wait"] = True
    _log("battery_low_no_pose",
         f"{reason} — 위치를 알 수 없어 충전소로 가지 못하고 멈췄습니다. 위치 재조정이 필요합니다"
         "(재조정되면 바로 충전소로 갑니다)", robot_id)


def _go_charge_now(robot_id: int, bat_pct: int, limit: int) -> None:
    """하던 일을 멈추고 충전소로 보낸다. 안내 멘트를 끝까지 기다리므로 heartbeat 스레드 밖에서 돈다."""
    threading.Thread(target=_leave_for_charge, args=(robot_id, bat_pct, limit),
                     daemon=True, name="battery-low").start()


def _say_and_wait(robot_id: int, text: str) -> None:
    """멘트를 끝까지 들려준다. 음성 에이전트가 재생 전에 응답해도 멘트 길이만큼은 기다린다."""
    from app.navigation.arrival_gate import speak_only
    t0 = time.time()
    speak_only(robot_id, text)
    left = len(text) / SPEECH_CHARS_PER_SEC + 1.0 - (time.time() - t0)
    if left > 0:
        time.sleep(left)


def _leave_for_charge(robot_id: int, bat_pct: int, limit: int) -> None:
    import app.navigation.send_move as nav_mod
    from app.database.routes.voice_scenario import abort_for_battery, scenario_busy
    from app.navigation.arrival_gate import speak_only
    from app.robot_control import battery_resume
    from app.robot_control.charge import _return_to_charge_internal
    from app.scheduler import loop
    from app.user_cache import get_robot_name

    reason = f"배터리 {bat_pct}% — 최소 기준({limit}%) 이하"
    name = get_robot_name()
    try:
        if scenario_busy(name):
            # 안내 중: 그 자리에 서서 방문객에게 끝까지 말한 뒤 세션을 닫는다.
            # 세션이 닫히면 종료 후처리(on_scenario_closed)가 근무를 자리 비움으로 두고 충전소로 보낸다.
            if nav_mod.is_navigating:
                nav_mod.stop_navigation_internal("배터리 부족 — 안내 중단", keep_schedule=True)
            _say_and_wait(robot_id, GUIDE_END_SPEECH)
            abort_for_battery(name, bat_pct)
            return

        was_moving = nav_mod.is_navigating
        route = _route_snapshot() if was_moving and loop.get_active_schedule_id() is None else None
        if was_moving:
            nav_mod.stop_navigation_internal("배터리 부족", keep_schedule=True)
        # 상시 대기는 자리 비움(80% 에 복귀), 예약 작업은 회차를 battery_low 로 끝낸다.
        # 무엇을 이어 갈지 적고 관제에 묻는다(battery_resume)
        if not loop.charge_away_for_battery():
            task = loop.end_task_for_battery(reason)
            if task:
                battery_resume.remember(robot_id, "task", task[1], schedule_id=task[0])
            elif route:
                battery_resume.remember(robot_id, "route", route.pop("label"), **route)
        result = _return_to_charge_internal(cancel_running=False, low_battery=True)
    except Exception as e:  # noqa: BLE001
        print(f"[AUTO-RETURN ERR] 충전 복귀 실패: {e}")
        return
    if result.get("reason") == "no_pose":
        wait_for_pose(robot_id, reason)
        return
    if was_moving:
        speak_only(robot_id, LOW_BATTERY_SPEECH)
    # 실패해도 쿨다운(last_trigger_ts)은 유지 — 충전소 미등록 등 영구적 실패에서
    # 매 heartbeat 마다 재시도해 로그가 폭주하는 것을 막는다. 10분 후 자연 재시도.
    _log("auto_return_triggered" if result.get("ok") else "battery_low_no_station",
         f"{reason} — " + ("하던 일을 멈추고 " if was_moving else "")
         + f"충전소 복귀: {result.get('msg')}", robot_id)
