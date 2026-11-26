"""배터리 부족 충전 뒤 하던 일 이어 가기 — 운영자 확인(2026-10-22 결정).

배터리 부족으로 충전소로 갈 때 하던 일을 하나 적어 두고, 관제에 묻는다(battery_resume_ask):
  [충전 후 재개](기본, 무응답도 재개) / [오늘은 여기서 종료]
충전이 RESUME_PCT(80%) 가 되면:
  - 상시 대기(standby): 근무 시간이니 묻지 않고 대기 장소로 돌아가 근무를 이어 간다.
    로봇이 다른 일을 하는 중이면 끝날 때까지 기다린다. 운영 시간이 끝났으면 버린다.
  - 화면 경로 N회 반복(route): 충전만 했으면 남은 횟수를 이어 간다. 충전 중에 누가 도크에서 뺐다가
    다시 충전했으면 마음대로 출발하지 않고 다시 묻는다(battery_resume_confirm) — 응답 전엔 출발 안 함.
  - 예약 작업(task): 끊긴 회차는 이미 battery_low 로 끝났다. 다음 회차는 시작 조건(80%)이 지킨다.
    [오늘은 여기서 종료] 면 오늘 남은 회차를 시작하지 않는다(battery_low 로 남김).
결정은 POST /robot/battery-resume/{token}. 화면은 GET 으로 아직 그 요청이 살아 있는지 본다.

기록은 서버 메모리에만 둔다. 대신 확인 요청 로그에 이어 갈 정보를 적고 끝맺음도 로그로 남겨,
서버가 재시작되면 끝맺음 없는 요청을 찾아 관제에 알린다(report_lost_on_startup) — 로봇이 스스로 이어 가지는 않는다.
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import require_permission

router = APIRouter()

KIND_LABELS = {"standby": "근무", "route": "경로 반복", "task": "예약 작업"}
ASK_ACTION = "battery_resume_ask"
# 확인 요청의 끝맺음 — 이 중 하나가 뒤에 없으면 서버 재시작으로 사라진 요청이다
RESOLVED_ACTIONS = ("route_resume_after_charge", "standby_resume_after_charge", "battery_resume_ended",
                    "battery_resume_dropped", "battery_resume_done")
LOST_LOOKBACK_HOURS = 12

_lock = threading.Lock()
_pending: dict[int, dict] = {}       # robot_id → 이어 갈 일 하나
_ended_today: dict[int, date] = {}   # schedule_id → '오늘은 여기서 종료'를 고른 날
_seq = 0


def _next_token() -> int:
    global _seq
    _seq += 1
    return _seq


def describe(kind: str, label: str, data: dict) -> str:
    """사람이 읽는 '무엇을 이어 가는가' — 서버 재시작 뒤에도 운영자가 직접 다시 시킬 수 있게."""
    text = f"{KIND_LABELS[kind]} '{label}'"
    if kind == "route":
        left = "무한 반복" if data.get("infinite") else f"남은 {data.get('laps')}회"
        text += f"({left}, {data.get('done', 0) + 1}바퀴째부터)"
    return text


def _detail_extra(kind: str, label: str, data: dict) -> str:
    keys = {"route": ("laps", "done", "total", "infinite"), "task": ("schedule_id",)}.get(kind, ())
    return " ".join([f"{k}={data[k]}" for k in keys if k in data] + [f"label={label}"])


def remember(robot_id: int, kind: str, label: str, **data) -> int:
    """충전 후 이어 갈 일을 적고 관제에 묻는다. 로봇당 하나 — 새로 적으면 덮어쓴다. 반환: 요청 번호."""
    with _lock:
        token = _next_token()
        _pending[robot_id] = {"token": token, "kind": kind, "label": label, "docked": False,
                              "intervened": False, "confirm_asked": False, "confirmed": False, **data}
    _alert(ASK_ACTION, robot_id, token, kind,
           f"배터리 부족으로 충전소로 갑니다 — 충전 후 {describe(kind, label, data)}을(를) 이어 갈까요? "
           "(응답이 없으면 80%에 이어 갑니다)",
           extra=_detail_extra(kind, label, data))
    return token


def ended_today(schedule_id: int) -> bool:
    """운영자가 '오늘은 여기서 종료'를 고른 예약 작업인가 — 오늘 남은 회차를 시작하지 않는다."""
    with _lock:
        return _ended_today.get(schedule_id) == date.today()


def current(robot_id: int) -> dict | None:
    with _lock:
        p = _pending.get(robot_id)
        return {k: p[k] for k in ("token", "kind", "label", "confirm_asked")} if p else None


def _alert(action: str, robot_id: int, token: int, kind: str, msg: str, extra: str = "") -> None:
    from app.logs.service import log_event
    from app.user_cache import get_robot_name, get_robot_business_id
    print(f"🔋 [BATTERY-RESUME] {msg}")
    log_event("robot", action, msg, detail=f"resume={token} kind={kind}" + (f" {extra}" if extra else ""),
              robot_id=robot_id, robot_name=get_robot_name(), business_id=get_robot_business_id())


def _log(robot_id: int, action: str, msg: str) -> None:
    from app.logs.service import log_event
    from app.user_cache import get_robot_name, get_robot_business_id
    print(f"🔋 [BATTERY-RESUME] {msg}")
    log_event("robot", action, msg, robot_id=robot_id, robot_name=get_robot_name(),
              business_id=get_robot_business_id())


def tick(robot_id: int, bat_pct: int, is_charging: bool, resume_pct: int) -> None:
    """heartbeat 마다 — 도킹·개입을 기록하고, 이어 갈 때가 되면 이어 간다."""
    import app.navigation.send_move as nav_mod
    from app.database.routes.voice_scenario import scenario_busy
    from app.scheduler import loop
    from app.user_cache import get_robot_name

    docked_now = confirm = task_done = False
    action = None
    with _lock:
        p = _pending.get(robot_id)
        if not p:
            return
        if is_charging and not p["docked"]:
            p["docked"] = docked_now = True
        elif not is_charging and p["docked"]:
            # 충전하던 로봇이 도크에서 나갔다 — 사람이 다른 일을 시켰다. 경로 반복은 다시 묻는다.
            p["docked"] = False
            p["intervened"] = True
        kind, token, label = p["kind"], p["token"], p["label"]
        if kind == "task":
            if bat_pct >= resume_pct:
                _pending.pop(robot_id)        # 다음 회차는 시작 조건이 지킨다 — 물을 것이 없다
                task_done = True
        elif bat_pct < resume_pct:
            pass
        elif kind == "standby":
            action = "standby"
        elif kind == "route" and is_charging:
            if p["intervened"] and not p["confirmed"]:
                if not p["confirm_asked"]:
                    p["confirm_asked"] = confirm = True
            else:
                action = "route"

    if task_done:
        _log(robot_id, "battery_resume_done", f"충전 {bat_pct}% — 예약 작업 '{label}'은(는) 다음 회차부터 정상으로 돕니다")
        return
    if docked_now and kind == "standby":
        with loop._lock:
            going = bool(loop._standby) and loop._standby.get("away") == "charge_going"
        if going:
            loop._set_away("charge")
    if kind == "standby" and loop.get_standby_state() is None:
        if _drop(robot_id, token):            # 운영 시간이 끝나 근무가 닫혔다
            _log(robot_id, "battery_resume_dropped", f"근무 '{label}'이(가) 끝나 충전 후 복귀하지 않습니다")
        return
    if confirm:
        _alert("battery_resume_confirm", robot_id, token, kind,
               f"충전이 {bat_pct}%가 됐습니다. 충전 중에 로봇이 도크에서 나간 적이 있어 바로 출발하지 않습니다 — "
               f"{KIND_LABELS[kind]} '{label}'을(를) 이어 갈까요?")
        return
    if not action:
        return
    # 다른 일을 하는 중이면 끊지 않는다 — 다음 heartbeat 에 다시 본다
    if nav_mod.is_navigating or scenario_busy(get_robot_name()):
        return
    with _lock:
        p = _pending.get(robot_id)
        if not p or p["token"] != token:
            return
        _pending.pop(robot_id)
    threading.Thread(target=_resume, args=(robot_id, p, bat_pct), daemon=True,
                     name="battery-resume").start()


def _drop(robot_id: int, token: int) -> bool:
    with _lock:
        p = _pending.get(robot_id)
        if p and p["token"] == token:
            _pending.pop(robot_id)
            return True
        return False


def _resume(robot_id: int, p: dict, bat_pct: int) -> None:
    try:
        if p["kind"] == "standby":
            from app.scheduler import loop
            if not loop.resume_standby_after_charge():
                _log(robot_id, "battery_resume_dropped",
                     f"근무 '{p['label']}' — 충전 자리 비움 상태가 아니라 복귀하지 않습니다")
        else:
            _resume_route(robot_id, p, bat_pct)
    except Exception as e:  # noqa: BLE001
        print(f"[BATTERY-RESUME ERR] 이어 가기 실패: {e}")
        _log(robot_id, "battery_resume_dropped", f"충전 후 이어 가기 실패: {str(e)[:120]}")


def _resume_route(robot_id: int, r: dict, bat_pct: int) -> None:
    """경로 반복 이어 가기 — start_path_navigation 과 같은 시작 순서."""
    import app.navigation.send_move as nav_mod
    from app.robot_control.charge import release_charge_for_work
    from app.scheduler import loop

    if loop.get_active_schedule_id() is not None or nav_mod._initpose_blocked():
        _log(robot_id, "battery_resume_dropped",
             f"충전 후 {describe('route', r['label'], r)}을(를) 이어 가려 했지만 다른 작업 중이거나 "
             "위치 확인이 필요해 취소합니다")
        return
    release_charge_for_work()
    nav_mod.waypoints_list = [dict(w) for w in r["waypoints"]]
    nav_mod.current_wp_index = 0
    nav_mod.is_navigating = True
    nav_mod.clear_arrival_actions()
    nav_mod.nav_loop_infinite = r["infinite"]
    nav_mod.nav_loop_remaining = 0 if r["infinite"] else r["laps"] - 1
    nav_mod.nav_loop_total = r["total"]
    nav_mod.nav_loop_count = r["done"] + 1
    nav_mod.auto_return_to_charge = r["auto_charge"]
    nav_mod._signal_nav_reset(full=True)
    nav_mod._update_is_working(robot_id, True)
    left = "무한 반복" if r["infinite"] else f"남은 {r['laps']}회"
    _log(robot_id, "route_resume_after_charge", f"배터리 {bat_pct}% — 충전 완료, 경로 반복 이어 가기({left})")
    nav_mod.navigation_send_next()


def decide(robot_id: int, token: int, decision: str) -> str:
    """운영자 결정. 반환: 화면 안내 문구. 이미 처리된 요청이면 ValueError."""
    from app.scheduler import loop

    with _lock:
        p = _pending.get(robot_id)
        if not p or p["token"] != token:
            raise ValueError("이미 처리됐거나 지난 요청입니다")
        if decision == "resume":
            if p["confirm_asked"]:
                p["confirmed"] = True     # 다음 heartbeat 에 출발(충전 중·80% 이상일 때)
        else:
            _pending.pop(robot_id)
            if p["kind"] == "task" and p.get("schedule_id") is not None:
                _ended_today[p["schedule_id"]] = date.today()
    what = f"{KIND_LABELS[p['kind']]} '{p['label']}'"
    if decision == "resume":
        msg = "이어 갑니다" if p["confirm_asked"] else "충전이 끝나면 이어 갑니다"
        _log(robot_id, "battery_resume_accepted", f"운영자 결정: {what} — {msg}")
        return msg
    if p["kind"] == "standby":
        loop.end_standby_by_operator("배터리 부족 — 운영자가 오늘은 여기서 종료")
    _log(robot_id, "battery_resume_ended", f"운영자 결정: {what} — 오늘은 여기서 종료")
    return "오늘은 여기서 종료합니다"


def report_lost_on_startup() -> str | None:
    """서버 시작 때 — 최근 확인 요청 중 끝맺음 로그가 없는 것이 있으면(재시작으로 사라짐) 관제에 알린다.

    반환: 알린 내용(없으면 None). 로봇은 스스로 이어 가지 않는다 — 운영자가 보고 직접 다시 시킨다.
    """
    from app.database.database import SessionLocal
    from app.database.models import LogDataInfo
    from app.user_cache import get_robot_id, get_robot_name

    since = datetime.now() - timedelta(hours=LOST_LOOKBACK_HOURS)
    db = SessionLocal()
    try:
        q = db.query(LogDataInfo).filter(LogDataInfo.Action.in_((ASK_ACTION,) + RESOLVED_ACTIONS),
                                         LogDataInfo.CreatedAt >= since)
        name = get_robot_name()
        if name:
            q = q.filter(LogDataInfo.RobotName == name)
        rows = q.order_by(LogDataInfo.CreatedAt, LogDataInfo.id).all()
    finally:
        db.close()
    last_ask = max((i for i, r in enumerate(rows) if r.Action == ASK_ACTION), default=None)
    if last_ask is None or any(r.Action in RESOLVED_ACTIONS for r in rows[last_ask + 1:]):
        return None
    ask = rows[last_ask]
    what = ask.Message.split("충전 후 ", 1)[-1].split("을(를) 이어 갈까요", 1)[0]
    msg = (f"서버 재시작으로 충전 후 이어 가기가 취소됐습니다 — {what}. 필요하면 직접 다시 시작해 주세요 "
           f"(요청 {ask.CreatedAt:%m-%d %H:%M})")
    _log(get_robot_id(), "battery_resume_lost", msg)
    return msg


class DecideReq(BaseModel):
    decision: str


@router.get("/robot/battery-resume")
def get_battery_resume():
    """충전 후 이어 갈 일(확인 대기 중인 요청). 없으면 null."""
    from app.user_cache import get_robot_id
    return {"pending": current(get_robot_id())}


@router.post("/robot/battery-resume/{token}", dependencies=[Depends(require_permission("robot-list"))])
def post_battery_resume(token: int, req: DecideReq):
    from app.user_cache import get_robot_id
    if req.decision not in ("resume", "end"):
        raise HTTPException(status_code=400, detail="decision 은 resume 또는 end 입니다")
    try:
        msg = decide(get_robot_id(), token, req.decision)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "msg": msg}
