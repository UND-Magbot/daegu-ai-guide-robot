"""안내 사전 주행 — 안내를 시작하기 전에 시나리오 동선을 멘트 없이 한 바퀴 돈다.
속도는 기본 고속, 층 이동 시험 탭에서는 저속·보통도 고를 수 있다(2026-10-27).

동선(B안, 2026-10-22 사용자 결정): 켜진 이동 단계를 단계 순서대로 따라간다. 고객 목적지 단계는
운영 맵에 연결된 목적지 장소를 모두 돈다. 실제 안내는 고객마다 목적지가 하나라 '앞 장소 → 목적지 →
뒤 장소' 를 다니므로, 목적지마다 그 세 지점을 그대로 다시 밟는다.
    예) 진료대기장소 → 간호실데스크 → 원무과수납 → 진료대기장소 → 1F_엘리베이터입구 → 원무과수납
뒤쪽 목적지 단계(추가 목적지 안내)는 같은 목적지라 다시 넣지 않는다.

진행 감시는 1초 타이머로 한다(주행 자체는 send_move 흐름 그대로).
- 측위 상실: 주행을 세우고 최대 PRERUN_LOC_WAIT_SEC 기다린다. 상실 중 NAV 재전송이 겹치면 로봇이
  거부하고 basic_server 가 재시작된다(2026-09-29 실측) — 그래서 기다리는 동안 보내지 않는다.
  회복되면 그 구간부터 이어서 간다.
- 못 간 지점(재전송 한도 초과), 다른 이동 명령, 통신 두절 정지는 바로 멈춘다. 운영자가 다시 고른다.
- 끝나도 안내를 자동으로 시작하지 않는다 — 운영자가 확인 버튼으로 연다(화면).

상태는 이 관제가 주행시키는 로봇 한 대분만 메모리에 둔다. 관제를 재시작하면 사라진다.
"""

import math
import os
import threading
import time
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.database.models import LocationInfo, VoiceScenario, VoiceScenarioStep, VoiceSlotLocation, VoiceSlotValue
from app.logs.service import log_event

PRERUN_SPEED = "fast"           # 속도를 고르지 않으면(안내 시작 창의 고속 1바퀴) 이 값
PRERUN_SPEEDS = ("slow", "normal", "fast")
PRERUN_LOC_WAIT_SEC = float(os.getenv("PRERUN_LOC_WAIT_SEC", "30"))
TICK_SEC = 1.0
# 이 안에 있으면 그 장소는 이미 왔다고 본다. 가까운 목표로 NAV 를 보내면 로봇이 움직이지 않아
# '이동 미감지' 로 15초를 기다린다(polling NEAR_SKIP_DISTANCE 와 같은 값).
PRERUN_NEAR_M = 0.5

RUNNING = "running"
LOC_WAIT = "loc_wait"
FAILED = "failed"
STOPPED = "stopped"
DONE = "done"
ACTIVE = (RUNNING, LOC_WAIT)

ROLE_START = "start"
ROLE_STEP = "step"
ROLE_DEST = "destination"


# ── 동선 ─────────────────────────────────────────────
def _leg(place: LocationInfo, role: str, step: Optional[str], branch: bool = False) -> dict:
    return {"id": place.id, "name": place.LacationName, "role": role, "step": step,
            "x": place.LocationX, "y": place.LocationY, "branch": branch}


def _main_flow(db: Session, scenario_id: int) -> set[str]:
    """응답이 없을 때 실제로 가는 기본 흐름의 단계 코드.

    첫 단계부터 다음 단계로 간다. 사용자에게 묻는 단계는 응답 없음(OnTimeout)이 앞으로 가는 단계면 그쪽,
    아니면 다음 단계(NextStep)로 간다 — 같은 단계로 되묻거나 지난 단계로 돌아가는 것은 따라가지 않는다.
    이 흐름에 없는 이동 단계(편의점 '예' 쪽, 어디서도 이어지지 않는 단계 등)는 대답에 따라 가는 갈래다.
    """
    steps = (db.query(VoiceScenarioStep)
             .filter(VoiceScenarioStep.ScenarioId == scenario_id, VoiceScenarioStep.IsActive == 1)
             .order_by(VoiceScenarioStep.StepOrder, VoiceScenarioStep.id).all())
    by_code = {s.StepCode: s for s in steps}
    if not steps or not any(s.NextStepCode for s in steps):
        return set(by_code)              # 흐름 연결이 없는 시나리오는 갈래를 가릴 수 없다 — 모두 기본
    seen: list[str] = []
    code: Optional[str] = steps[0].StepCode
    while code and code in by_code and code not in seen:
        seen.append(code)
        s = by_code[code]
        nxt = s.NextStepCode
        if s.WaitFor == "user" and s.OnTimeoutStepCode and s.OnTimeoutStepCode not in seen \
                and s.OnTimeoutStepCode != code:
            nxt = s.OnTimeoutStepCode
        code = nxt
    return set(seen)


def select_legs(plan: dict, keep: list[int]) -> dict:
    """plan 의 구간 중 keep(0부터 번호)만 남긴다. 같은 장소가 연달아 오면 한 번으로 합친다."""
    legs = [lg for i, lg in enumerate(plan["legs"]) if i in set(keep)]
    deduped = [lg for i, lg in enumerate(legs) if i == 0 or lg["id"] != legs[i - 1]["id"]]
    return {**plan, "legs": deduped}


def _destinations(db: Session, robot_id: int, slot_name: str, map_id: int) -> list[LocationInfo]:
    """운영 맵에 연결된 목적지 장소 — 같은 장소에 걸린 목적지는 한 번만(사전 순서)."""
    rows = (db.query(LocationInfo)
            .join(VoiceSlotLocation, VoiceSlotLocation.LocationId == LocationInfo.id)
            .join(VoiceSlotValue, VoiceSlotValue.id == VoiceSlotLocation.SlotValueId)
            .filter(VoiceSlotValue.RobotId == robot_id, VoiceSlotValue.SlotName == slot_name,
                    VoiceSlotLocation.MapId == map_id)
            .order_by(VoiceSlotValue.id).all())
    return list({p.id: p for p in rows}.values())


def plan_route(db: Session, sc: VoiceScenario, start: Optional[LocationInfo]) -> dict:
    """사전 주행 동선. {"legs": [...], "warnings": [...], "destinations": n}.

    legs 는 차례로 멈춰 설 장소다(같은 장소가 연달아 오면 한 번). start 를 주면 그곳에서 시작해
    그곳으로 돌아온다 — 안내가 그 호출 위치에서 시작하므로.
    """
    steps = (db.query(VoiceScenarioStep)
             .filter(VoiceScenarioStep.ScenarioId == sc.id, VoiceScenarioStep.IsActive == 1,
                     VoiceScenarioStep.ActionType == "navigate")
             .order_by(VoiceScenarioStep.StepOrder, VoiceScenarioStep.id).all())
    places = {p.id: p for p in db.query(LocationInfo).filter(
        LocationInfo.id.in_([s.TargetLocationId for s in steps if s.TargetLocationId])).all()}

    main = _main_flow(db, sc.id)
    warnings: list[str] = []
    before: list[dict] = []          # 첫 목적지 단계 앞
    after: list[dict] = []           # 그 뒤
    dest_step: Optional[VoiceScenarioStep] = None
    for st in steps:
        if st.SlotName:
            if dest_step is None:
                dest_step = st
            continue                 # 뒤쪽 목적지 단계는 같은 목적지라 넣지 않는다
        place = places.get(st.TargetLocationId)
        if not place:
            warnings.append(f"'{st.StepCode}' 단계에 이동 장소가 없어 건너뜁니다.")
            continue
        if place.MapId != sc.MapId:
            warnings.append(f"'{st.StepCode}' 단계의 '{place.LacationName}' 은(는) 운영 맵 장소가 아니라 건너뜁니다.")
            continue
        (after if dest_step else before).append(_leg(place, ROLE_STEP, st.StepCode, st.StepCode not in main))

    dests = _destinations(db, sc.RobotId, dest_step.SlotName, sc.MapId) if dest_step else []
    if dest_step and not dests:
        warnings.append(f"'{dest_step.StepCode}' 단계: 운영 맵에 연결된 목적지가 없어 건너뜁니다.")

    legs: list[dict] = []
    if start:
        legs.append(_leg(start, ROLE_START, None))
    legs += before
    for i, d in enumerate(dests):
        if i > 0 and before:
            legs.append(before[-1])
        legs.append(_leg(d, ROLE_DEST, dest_step.StepCode, dest_step.StepCode not in main))
        if after:
            legs.append(after[0])
    legs += after[1:] if dests else after
    if start:
        legs.append(_leg(start, ROLE_START, None))

    deduped = [lg for i, lg in enumerate(legs) if i == 0 or lg["id"] != legs[i - 1]["id"]]
    return {"legs": [dict(lg) for lg in deduped], "warnings": warnings, "destinations": len(dests)}


# ── 실행 ─────────────────────────────────────────────
_lock = threading.RLock()
_state: Optional[dict] = None
_route: Optional[list] = None        # 지금 주행 중인 send_move.waypoints_list (같은 객체인지로 본다)
_timer = None


def _now() -> float:
    return time.time()


def is_active() -> bool:
    return bool(_state and _state["status"] in ACTIVE)


def snapshot() -> Optional[dict]:
    """화면용 상태. 구간마다 done/current/wait/error/pending."""
    with _lock:
        if not _state:
            return None
        st = dict(_state)
        cur, status = st["leg"], st["status"]
        legs = []
        for i, lg in enumerate(st["legs"]):
            if i < st["doneLegs"]:
                mark = "done"
            elif i == cur and status == RUNNING:
                mark = "current"
            elif i == cur and status == LOC_WAIT:
                mark = "wait"
            elif i == cur and status in (FAILED, STOPPED):
                mark = "error"
            else:
                mark = "pending"
            legs.append({**lg, "status": mark})
        end = st["endedAt"] or _now()
        wait_left = (max(0, round(PRERUN_LOC_WAIT_SEC - (_now() - st["locWaitSince"])))
                     if status == LOC_WAIT else None)
        return {
            "status": status,
            "scenarioCode": st["scenarioCode"],
            "scenarioName": st["scenarioName"],
            "speed": st["speed"],
            "startLocationId": st["startLocationId"],
            "legs": legs,
            "leg": cur,
            "doneLegs": st["doneLegs"],
            "total": len(st["legs"]),
            "reason": st["reason"],
            "locLost": st["locLost"],
            "locWaitLeft": wait_left,
            "elapsedSec": round(end - st["startedAt"]),
            "startedAt": st["startedAtIso"],
        }


def start(robot, sc: VoiceScenario, start_location_id: Optional[int], plan: dict,
          speed: str = PRERUN_SPEED) -> dict:
    """사전 주행을 시작한다. 검사(맵·세션·자세·주행 중 여부)는 부르는 쪽이 한다.

    speed 는 한 바퀴 내내 쓰고, 다시 돌릴 때(resume)도 그대로 쓴다.
    """
    global _state
    if speed not in PRERUN_SPEEDS:
        raise ValueError(f"알 수 없는 속도입니다: {speed}")
    with _lock:
        if is_active():
            raise RuntimeError("이미 사전 주행 중입니다.")
        _state = {
            "robotId": robot.id, "robotName": robot.RobotName, "businessId": robot.BusinessId,
            "scenarioCode": sc.Code, "scenarioName": sc.DisplayName,
            "startLocationId": start_location_id, "speed": speed,
            "legs": plan["legs"], "leg": 0, "doneLegs": 0,
            "status": RUNNING, "reason": None, "locLost": 0, "locWaitSince": 0.0,
            "startedAt": _now(), "startedAtIso": datetime.now().isoformat(timespec="seconds"),
            "endedAt": None,
        }
    log_event("schedule", "guide_prerun_start",
              f"{sc.DisplayName} 사전 주행 시작 — {len(plan['legs'])}곳, 속도 {speed}",
              detail=" → ".join(lg["name"] for lg in plan["legs"]),
              robot_id=robot.id, robot_name=robot.RobotName, business_id=robot.BusinessId)
    _launch(0)
    return snapshot()


def resume(from_failed: bool) -> dict:
    """멈춘 사전 주행을 실패 지점부터(from_failed) 또는 처음부터 다시 돈다."""
    with _lock:
        if not _state or _state["status"] not in (FAILED, STOPPED):
            raise RuntimeError("다시 돌릴 사전 주행이 없습니다.")
        k = _state["leg"] if from_failed else 0
        _state.update(status=RUNNING, reason=None, endedAt=None, doneLegs=k, leg=k)
        if not from_failed:
            _state.update(startedAt=_now(), locLost=0,
                          startedAtIso=datetime.now().isoformat(timespec="seconds"))
    _launch(k)
    return snapshot()


def stop(reason: str = "운영자가 사전 주행을 멈췄습니다.") -> Optional[dict]:
    from app.navigation import send_move as sm
    with _lock:
        if not is_active():
            return snapshot()
        mine = sm.waypoints_list is _route and sm.is_navigating
        _end(STOPPED, reason)
    if mine:
        sm.stop_navigation_internal("사전 주행 중지", keep_schedule=True)
    return snapshot()


def dismiss() -> None:
    """끝난 사전 주행 기록을 지운다(안내를 시작했거나 닫았다)."""
    global _state
    with _lock:
        if is_active():
            raise RuntimeError("사전 주행 중에는 닫을 수 없습니다. 먼저 멈추세요.")
        _state = None


def _end(status: str, reason: Optional[str]) -> None:
    _state.update(status=status, reason=reason, endedAt=_now())
    kind = {DONE: "guide_prerun_done", FAILED: "guide_prerun_failed", STOPPED: "guide_prerun_stopped"}[status]
    msg = f"{_state['scenarioName']} 사전 주행 " + {
        DONE: f"완료 — {len(_state['legs'])}곳 정상",
        FAILED: f"멈춤 — {reason}",
        STOPPED: "중지",
    }[status]
    log_event("schedule", kind, msg, robot_id=_state["robotId"], robot_name=_state["robotName"],
              business_id=_state["businessId"])


def _fail(reason: str) -> None:
    _end(FAILED, reason)


def _build_waypoints(db: Session, legs: list[dict], k: int, pos: Optional[tuple[float, float]],
                     speed: str = PRERUN_SPEED) -> tuple[Optional[list], str]:
    """구간 k 부터 끝까지의 웨이포인트. 구간마다 벽 우회를 장소 이동과 같은 방식으로 붙인다."""
    from app.navigation.via_route import plan_via_waypoints
    from app.robot_io.config import NODE_ROUTING_ENABLED
    places = {p.id: p for p in db.query(LocationInfo).filter(
        LocationInfo.id.in_([lg["id"] for lg in legs])).all()}
    if NODE_ROUTING_ENABLED:
        # 노드망이면 장소 이동·안내 이동처럼 보낼 때 노드를 펼친다 — 예전 자동 우회로 미리 거절하지 않는다
        from app.navigation.send_move import node_entry_check
        missing = next((lg for lg in legs[k:] if lg["id"] not in places), None)
        if missing:
            return None, f"'{missing['name']}' 장소가 지워졌습니다."
        refused = node_entry_check(db, [places[lg["id"]] for lg in legs[k:]])
        if refused:
            return None, refused
    wps: list[dict] = []
    prev = pos
    for j in range(k, len(legs)):
        place = places.get(legs[j]["id"])
        if not place:
            return None, f"'{legs[j]['name']}' 장소가 지워졌습니다."
        via, why = ([], "node") if NODE_ROUTING_ENABLED else plan_via_waypoints(db, place, prev)
        if via is None:
            return None, f"'{place.LacationName}'까지 맵상 안전 우회 경로를 찾지 못했습니다({why})."
        for wp in via + [{"x": place.LocationX, "y": place.LocationY, "yaw": place.Yaw or 0.0,
                          "name": place.LacationName, "map_id": place.MapId, "floor_id": place.FloorId}]:
            wp["speed"] = speed
            wp["prerun_leg"] = j
            wp["halt_if_unreached"] = True
            wp["multi_pass"] = True      # 다지점이 켜져 있으면 한 바퀴를 한 묶음으로(장소마다 리셋·보행 전환 없음)
            wps.append(wp)
        prev = (place.LocationX, place.LocationY)
    return wps, ""


def _launch(k: int) -> None:
    """구간 k 부터 주행을 건다. 실패하면 상태를 FAILED 로 둔다."""
    global _route
    import app.robot_io.runtime as runtime
    from app.database.database import SessionLocal
    from app.navigation import send_move as sm
    from app.robot_control.charge import release_charge_for_work

    with _lock:
        legs = _state["legs"]
        rid = _state["robotId"]
        speed = _state["speed"]
    pos = runtime.get_position(rid) or {}
    start_xy = ((pos["x"], pos["y"])
                if pos.get("timestamp") and not pos.get("initpose_pending") else None)
    while start_xy and k < len(legs) and math.dist(start_xy, (legs[k]["x"], legs[k]["y"])) < PRERUN_NEAR_M:
        k += 1                       # 이미 그 자리 — 지난 구간으로 친다
    with _lock:
        _state.update(leg=k, doneLegs=k)
        if k >= len(legs):
            _end(DONE, None)
            return
    db = SessionLocal()
    try:
        wps, why = _build_waypoints(db, legs, k, start_xy, speed)
    finally:
        db.close()
    with _lock:
        if not wps:
            _fail(why or "갈 곳이 없습니다.")
            return
        release_charge_for_work()
        _route = wps
        sm.begin_route(wps)
    print(f"[PRERUN] 구간 {k + 1}/{len(legs)} 부터 주행 — 웨이포인트 {len(wps)}개, 속도 {speed}")
    sm.navigation_send_next()
    _arm()


def _arm() -> None:
    global _timer
    t = threading.Timer(TICK_SEC, _tick)
    t.daemon = True
    _timer = t
    t.start()


def _leg_of(wp_no: int) -> Optional[int]:
    if _route and 0 < wp_no <= len(_route):
        return _route[wp_no - 1].get("prerun_leg")
    return None


def _tick() -> None:
    """1초마다 — 진행 구간 갱신, 측위 상실 대기, 실패·완료 판정."""
    import app.robot_io.runtime as runtime
    from app.navigation import send_move as sm

    stop_nav = None
    relaunch = None
    with _lock:
        if not _state or _state["status"] not in ACTIVE:
            return
        rid = _state["robotId"]
        pending = runtime.is_initpose_pending(rid)
        reason = runtime.get_initpose_reason(rid) if pending else None

        if _state["status"] == LOC_WAIT:
            if not pending:
                print(f"[PRERUN] 측위 회복 — 구간 {_state['leg'] + 1} 부터 이어서 주행")
                _state["status"] = RUNNING
                relaunch = _state["leg"]
            elif _now() - _state["locWaitSince"] >= PRERUN_LOC_WAIT_SEC:
                _fail(f"측위 상실이 {PRERUN_LOC_WAIT_SEC:.0f}초 안에 회복되지 않았습니다. "
                      "위치 재조정 후 다시 주행하세요.")
        else:
            if sm.waypoints_list is not _route:
                _fail("다른 이동 명령이 들어와 사전 주행을 멈췄습니다.")
            else:
                leg = _leg_of(sm.heading_wp_no())
                if leg is not None:
                    _state["leg"] = leg
                    _state["doneLegs"] = leg
                unreached = next((wp for wp in _route if wp.get("unreached")), None)
                if pending and reason == runtime.LOCALIZATION_LOST_REASON:
                    print(f"[PRERUN] 측위 상실 — 주행을 세우고 최대 {PRERUN_LOC_WAIT_SEC:.0f}s 대기")
                    _state.update(status=LOC_WAIT, locWaitSince=_now(), locLost=_state["locLost"] + 1)
                    stop_nav = "사전 주행 측위 상실 대기"
                elif pending:
                    _fail(sm._initpose_block_msg())
                    stop_nav = "사전 주행 위치 미확정"
                elif unreached is not None:
                    _state["leg"] = unreached["prerun_leg"]
                    _state["doneLegs"] = unreached["prerun_leg"]
                    _fail(f"'{unreached.get('name')}' — {unreached.get('unreached_reason') or '도착하지 못했습니다'}")
                    stop_nav = "사전 주행 실패"
                elif not sm.is_navigating:
                    if sm.current_wp_index >= len(_route):
                        _state.update(doneLegs=len(_state["legs"]), leg=len(_state["legs"]))
                        _end(DONE, None)
                    else:
                        _fail(sm.last_nav_error_message() or "주행이 중단되었습니다(정지 명령 또는 통신 두절).")
        active = _state["status"] in ACTIVE

    if stop_nav and sm.waypoints_list is _route and sm.is_navigating:
        sm.stop_navigation_internal(stop_nav, keep_schedule=True)
    if relaunch is not None:
        _launch(relaunch)
        return
    if active:
        _arm()
