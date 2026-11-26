"""응대 세션 FSM (PRD-Schedule-Execution §3.4).

호출 감지 → 응답 발화 → 안내 이동 → 도착 발화 → 대기장소 복귀 → 세션 종료.

두 가지 원칙이 이 모듈의 모양을 정한다.
- **세션은 대기장소 복귀 완료 시점에 끝난다.** 도착만으로 끝내면 "안내는 했는데
  로봇이 복도에 서 있는" 상태가 집계에서 성공으로 잡힌다.
- **시간 상한은 안전망이지 이상 감지가 아니다.** 상한 도달은 중단이 아니라 판정이며,
  진전이 있으면 연장한다(§3.7 b-2). 진짜 이상은 무진전으로 잡는다.
"""

import os
import threading
from datetime import datetime, timedelta

from app.database.database import SessionLocal
from app.database.models import (
    ScheduleInfo, ServiceSession, LocationInfo, RobotInfo, VoiceSlotValue,
)
from app.database.voice_slot_links import location_on_map
from app.logs.service import log_event
from app.user_cache import get_robot_id, get_robot_name, get_robot_business_id

SLOT_NAME = "목적지"
# 작업 없이 응대했을 때 돌아갈 자리. 목적지 사전에서 이 이름을 찾는다 —
# 스케줄이 없으니 '대기 장소' 설정도 없고, 그렇다고 충전소로 보내면 다음 호출에
# 충전을 끊고 나오느라 응답이 느려진다.
FREE_RETURN_SLOT = "대기구역"

# 세션 결과
RESULT_RUNNING = "running"
RESULT_COMPLETED = "completed"
RESULT_COMPLETED_DELAYED = "completed_delayed"   # 10-22 이전 기록 — 연장 후 정상 완료(성공으로 집계)
RESULT_NO_MATCH = "no_match"
RESULT_UNKNOWN_DEST = "unknown_dest"
RESULT_NAV_FAILED = "nav_failed"
RESULT_ABORTED_NO_PROGRESS = "aborted_no_progress"   # 10-22 이전 기록 — 시간 상한·진전 없음
RESULT_ABORTED_OPERATOR = "aborted_operator"
RESULT_ABORTED_OFFLINE = "aborted_offline"

SUCCESS_RESULTS = {RESULT_COMPLETED, RESULT_COMPLETED_DELAYED}

# 도착 후 "추가 요청 없으시면 '없어'" 를 묻고 기다리는 시간.
# 사람이 그냥 가버려도 로봇이 현장에 방치되면 안 되므로 상한을 둔다.
#
# 60초는 짧았다 — 접수·진료실에서는 번호표를 뽑거나 안내를 듣느라 그 안에 답을 못 한다.
# 로봇이 먼저 돌아서면 사람은 다시 불러야 하고, 그게 반복되면 아무도 안 부른다.
# 현장에서 조정할 수 있게 env 로 뺀다(재빌드 없이 바꾸려면 이 값을 쓴다).
DISMISS_WAIT_SEC = int(os.getenv("DISMISS_WAIT_SEC") or 180)

_lock = threading.Lock()
# 진행 중 세션. 한 로봇이 동시에 두 사람을 안내할 수 없으므로 1건이면 충분하다.
_session: dict | None = None


def get_session() -> dict | None:
    with _lock:
        return dict(_session) if _session else None


def is_active() -> bool:
    return _session is not None


# ─── 목적지 판정 ───

def _norm(s: str) -> str:
    return "".join((s or "").split()).lower()


def match_destination(db, robot_id: int, heard: str) -> dict | None:
    """들은 말에서 목적지를 찾는다.

    부분 일치를 쓰되 **가장 긴 표현을 우선**한다 — "영상의학과"와 "의학과"가
    둘 다 등록돼 있으면 긴 쪽이 사용자가 말한 것이다. 애매하면 매칭하지 않는다:
    엉뚱한 곳으로 데려가는 것보다 "다시 말씀해 주세요"가 낫다.
    """
    target = _norm(heard)
    if not target:
        return None

    rows = (
        db.query(VoiceSlotValue)
        .filter(VoiceSlotValue.RobotId == robot_id, VoiceSlotValue.SlotName == SLOT_NAME)
        .all()
    )
    best = None
    for s in rows:
        candidates = [s.Value] + [x.strip() for x in (s.Synonyms or "").split(",") if x.strip()]
        for cand in candidates:
            c = _norm(cand)
            if not c or c not in target:
                continue
            # 신뢰도: 들은 말에서 목적지 표현이 차지하는 비율 (0.5~1.0)
            score = min(1.0, 0.5 + len(c) / max(len(target), 1) * 0.5)
            if best is None or len(c) > best["_len"]:
                best = {"slot": s, "matched": cand, "confidence": round(score, 2), "_len": len(c)}
    return best


def _allowed_destination(sched: ScheduleInfo | None, slot_id: int) -> bool:
    """응대 목적지 화이트리스트. 비어 있으면 전체 허용.

    작업이 없으면(sched=None) 제한할 근거가 없다 — 화이트리스트는 스케줄의 설정이다.
    """
    if sched is None:
        return True
    raw = (getattr(sched, "StandbyDestinations", None) or "").strip()
    if not raw:
        return True
    allowed = {x.strip() for x in raw.split(",") if x.strip()}
    return str(slot_id) in allowed


# ─── 발화 · 주행 ───

def _speak(text: str) -> None:
    try:
        from app.navigation.arrival_gate import speak_only
        speak_only(get_robot_id(), text)
    except Exception as e:  # noqa: BLE001
        print(f"[standby] 발화 실패(무시): {str(e)[:80]}")


def _nav_to(place: LocationInfo) -> bool:
    """지정 장소로 주행 시작. 충전 복귀는 붙이지 않는다(대기장소로 돌아와야 하므로)."""
    try:
        from app.navigation.send_move import _signal_nav_reset, navigation_send_next
        import app.navigation.send_move as nav_mod
        from app.navigation.waypoints import build_waypoints_from_places
        from app.robot_control.charge import release_charge_for_work

        waypoints = build_waypoints_from_places([place], [0], [None])
        release_charge_for_work()   # 충전 중이면 먼저 해제(도크 이탈)

        nav_mod.waypoints_list = waypoints
        nav_mod.current_wp_index = 0
        nav_mod.is_navigating = True
        nav_mod.clear_arrival_actions()
        nav_mod.auto_return_to_charge = False
        _signal_nav_reset(full=True)
        navigation_send_next()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[standby] 주행 시작 실패: {e}")
        return False


# ─── 세션 시작 ───

def start_scenario_call(db, sched: ScheduleInfo) -> dict:
    """작업의 안내 시나리오로 응대를 연다. 근무 연결·이동 정지는 open_session 이 한다."""
    from fastapi import HTTPException
    from app.database.models import RobotInfo
    from app.database.routes import voice_scenario as vs

    robot = (db.query(RobotInfo)
             .filter(RobotInfo.RobotName == sched.RobotName, RobotInfo.DeletedAt.is_(None)).first())
    if not robot:
        raise HTTPException(status_code=404, detail="로봇을 찾을 수 없습니다.")
    # 안내 장소는 시나리오 맵 좌표다 — 다른 맵에 서 있으면 첫 이동부터 엉뚱한 곳으로 간다
    # (음성관리 '안내 시작'과 같은 판정, 순환 import 라 늦게 가져온다)
    from app.database.models import VoiceScenario
    from app.database.routes.voice_scenario_admin import map_mismatch
    sc = (db.query(VoiceScenario)
          .filter(VoiceScenario.RobotId == robot.id, VoiceScenario.Code == sched.StandbyScenarioCode,
                  VoiceScenario.IsActive == 1).first())
    if sc and (why := map_mismatch(db, robot, sc)):
        raise HTTPException(status_code=409, detail=why)
    s = vs.open_session(db, robot, sched.StandbyScenarioCode)
    return {"ok": True, "sessionId": s.id, "mode": "scenario"}


def resolve_free_return_place(db, robot_id: int | None) -> LocationInfo | None:
    """작업 없는 응대의 복귀 지점. 목적지 사전의 '대기구역' 을 쓴다."""
    if robot_id is None:
        return None
    slot = (
        db.query(VoiceSlotValue)
        .filter(VoiceSlotValue.RobotId == robot_id,
                VoiceSlotValue.SlotName == SLOT_NAME,
                VoiceSlotValue.Value == FREE_RETURN_SLOT)
        .first()
    )
    robot = db.query(RobotInfo).filter(RobotInfo.id == robot_id).first()
    if not slot or not robot:
        return None
    return location_on_map(db, slot.id, robot.CurrentMapId)


def start_session(schedule_id: int | None, run_id: int | None, heard: str | None = None,
                  destination_id: int | None = None, retry_count: int = 0) -> dict:
    """호출을 받아 세션을 연다. 반환: {ok, result, message, sessionId, destination}

    실패(미인식·목적지 없음)해도 **세션은 기록된다** — 안내를 못 했다는 사실이
    어디에도 안 남는 것이 지금의 문제다.

    `schedule_id` 가 None 이면 **작업 없이 받은 호출**이다. 상시대기 근무가 없어도
    사람이 부르면 안내해야 한다 — 운영 시간표는 로봇을 쉬게 하려고 있는 것이지
    눈앞의 사람을 무시하라고 있는 게 아니다. 이때는 목적지 화이트리스트가 없고
    복귀 지점은 목적지 사전의 '대기구역' 이다.
    """
    global _session

    with _lock:
        if _session is not None:
            return {"ok": False, "result": "busy", "message": "이미 응대 중입니다."}

    # 다단계 안내(단계 엔진)가 이 로봇을 쓰고 있으면 단발 응대를 열지 않는다.
    # 호출 경로(버튼·음성·관제)가 여럿이라 입구 한 곳에서 막는다.
    from app.database.routes.voice_scenario import scenario_busy
    if scenario_busy(get_robot_name()):
        return {"ok": False, "result": "busy", "message": "다단계 안내가 진행 중입니다."}

    db = SessionLocal()
    try:
        sched = None
        if schedule_id is not None:
            sched = db.query(ScheduleInfo).filter(ScheduleInfo.id == schedule_id).first()
            if not sched:
                return {"ok": False, "result": "error", "message": "작업을 찾을 수 없습니다."}

        if sched is not None:
            robot = (
                db.query(RobotInfo)
                .filter(RobotInfo.RobotName == sched.RobotName, RobotInfo.DeletedAt.is_(None))
                .first()
            )
            robot_name = sched.RobotName
        else:
            # 작업이 없으면 기준이 될 스케줄도 없다 — 이 관제가 물고 있는 로봇을 쓴다
            robot = db.query(RobotInfo).filter(RobotInfo.id == get_robot_id()).first()
            robot_name = get_robot_name()

        now = datetime.now()
        base = dict(ScheduleId=schedule_id, RunId=run_id, RobotName=robot_name,
                    StartedAt=now, Heard=heard, RetryCount=retry_count)

        # 1) 목적지 판정
        slot = None
        matched_text = None
        confidence = None
        if destination_id is not None:
            slot = (
                db.query(VoiceSlotValue)
                .filter(VoiceSlotValue.id == destination_id,
                        VoiceSlotValue.RobotId == (robot.id if robot else -1))
                .first()
            )
            matched_text = slot.Value if slot else None
            confidence = 1.0            # 버튼으로 고른 것은 인식 오류가 없다
        elif heard and robot:
            hit = match_destination(db, robot.id, heard)
            if hit:
                slot = hit["slot"]
                matched_text = hit["matched"]
                confidence = hit["confidence"]

        if not slot:
            # 못 알아들음 — 로봇은 움직이지 않는다. 이미 대기장소에 있다(S1).
            _speak("죄송합니다. 다시 말씀해 주세요." if heard else "다시 눌러 주세요.")
            sess = ServiceSession(**base, Result=RESULT_NO_MATCH, EndedAt=now,
                                  Confidence=confidence,
                                  Reason="목적지를 인식하지 못함")
            db.add(sess); db.commit()
            return {"ok": False, "result": RESULT_NO_MATCH, "sessionId": sess.id,
                    "message": "목적지를 인식하지 못했습니다."}

        # 2) 로봇이 있는 맵에 좌표가 연결되어 있는가 (S2) — 다른 맵 좌표로 가면 엉뚱한 곳에 선다
        place = location_on_map(db, slot.id, robot.CurrentMapId) if robot else None
        if not place:
            _speak(f"{slot.Value}은 아직 안내할 수 없습니다.")
            sess = ServiceSession(**base, Result=RESULT_UNKNOWN_DEST, EndedAt=now,
                                  MatchedIntent="navigate", MatchedSlot=matched_text,
                                  Confidence=confidence, DestinationName=slot.Value,
                                  Reason="목적지 사전에 이 맵의 장소가 연결되지 않음")
            db.add(sess); db.commit()
            log_event("schedule", "standby_dest_unknown",
                      f"안내 불가 목적지 요청 — \"{slot.Value}\"",
                      detail=f"작업={sched.WorkName}, 요청 내용={heard or '(버튼)'}",
                      robot_id=get_robot_id(), robot_name=get_robot_name(),
                      business_id=get_robot_business_id())
            return {"ok": False, "result": RESULT_UNKNOWN_DEST, "sessionId": sess.id,
                    "message": f"'{slot.Value}'에 장소가 연결되어 있지 않습니다."}

        # 3) 화이트리스트 (S3) — 운영자가 범위를 좁힌 의도를 존중한다
        if not _allowed_destination(sched, slot.id):
            _speak(f"{slot.Value}은 안내 대상이 아닙니다.")
            sess = ServiceSession(**base, Result=RESULT_UNKNOWN_DEST, EndedAt=now,
                                  MatchedIntent="navigate", MatchedSlot=matched_text,
                                  Confidence=confidence, DestinationId=place.id,
                                  DestinationName=slot.Value,
                                  Reason="응대 허용 목적지가 아님")
            db.add(sess); db.commit()
            return {"ok": False, "result": RESULT_UNKNOWN_DEST, "sessionId": sess.id,
                    "message": f"'{slot.Value}'은 이 작업의 응대 목적지가 아닙니다."}

        # 4) 안내 시작
        sess = ServiceSession(**base, Result=RESULT_RUNNING,
                              MatchedIntent="navigate", MatchedSlot=matched_text,
                              Confidence=confidence, DestinationId=place.id,
                              DestinationName=slot.Value)
        db.add(sess); db.commit(); db.refresh(sess)

        _speak(f"{slot.Value}로 안내하겠습니다. 따라와 주세요.")
        if not _nav_to(place):
            sess.Result = RESULT_NAV_FAILED
            sess.EndedAt = datetime.now()
            sess.Reason = "안내 이동을 시작하지 못함"
            db.commit()
            return {"ok": False, "result": RESULT_NAV_FAILED, "sessionId": sess.id,
                    "message": "안내 이동을 시작하지 못했습니다."}

        work_name = sched.WorkName if sched else "작업 없음(음성 호출)"
        with _lock:
            _session = {
                "id": sess.id,
                "schedule_id": schedule_id,
                "work_name": work_name,
                "phase": "guiding",
                "heard": heard,
                "destination_id": place.id,
                "destination_name": slot.Value,
                "confidence": confidence,
                "started_at": now,
                "elapsed_sec": 0,
                "retry_count": retry_count,
            }
        log_event("schedule", "standby_session_start",
                  f"응대 시작: {slot.Value} 안내",
                  detail=f"작업={work_name}, 요청 내용={heard or '(버튼 호출)'}",
                  robot_id=get_robot_id(), robot_name=get_robot_name(),
                  business_id=get_robot_business_id())
        return {"ok": True, "result": RESULT_RUNNING, "sessionId": sess.id,
                "destination": slot.Value, "message": f"{slot.Value}로 안내를 시작합니다."}

    except Exception as e:  # noqa: BLE001
        print(f"[standby] 세션 시작 실패: {e}")
        db.rollback()
        return {"ok": False, "result": "error", "message": f"응대 시작 실패: {str(e)[:120]}"}
    finally:
        db.close()


# ─── 단계 전이 ───

def on_guide_arrived(return_place: LocationInfo | None) -> bool:
    """목적지 도착 → 도착 발화 → **해산 확인 대기**. 반환: 세션을 계속 잡고 있으면 True.

    예전에는 도착하자마자 돌아섰다. 그런데 안내는 도착으로 끝나지 않는다 — 사람이
    번호표를 뽑거나 뭘 더 물어볼 수 있는데 로봇은 이미 등을 돌린 뒤였다.
    이제 한 번 묻고, 답을 듣거나 시간이 지나야 돌아선다.
    """
    global _session
    with _lock:
        if not _session or _session["phase"] != "guiding":
            return False
        _session["phase"] = "awaiting_dismiss"
        _session["return_place"] = return_place
        _session["dismiss_until"] = datetime.now() + timedelta(seconds=DISMISS_WAIT_SEC)
        dest = _session["destination_name"]

    _speak(f"{dest}에 도착했습니다. 이용해 주세요. "
           f"추가 요청이 없으시면 '없어'라고 말씀해주세요.")
    return True


def on_guide_unreached(reason: str) -> bool:
    """목적지에 못 갔는데 주행이 끝났다(출발 못 함 등으로 관제가 건너뜀) — 도착 멘트 대신 알리고 끝낸다."""
    with _lock:
        if not _session or _session["phase"] != "guiding":
            return False
    _speak("죄송합니다. 지금은 이동할 수 없어 안내를 마치겠습니다. 직원에게 문의해 주세요.")
    _finish(RESULT_NAV_FAILED, reason)
    return True


def dismiss_status() -> dict:
    """로봇이 "지금 해산 응답을 들어야 하는가"를 판단할 정보. 도착 확인 게이트와 같은 계약."""
    with _lock:
        if not _session or _session["phase"] != "awaiting_dismiss":
            return {"active": False}
        until = _session["dismiss_until"]
        return {
            "active": True,
            "destName": _session["destination_name"],
            "remainingSec": max(0, int((until - datetime.now()).total_seconds())),
        }


def confirm_dismiss(by: str = "voice") -> bool:
    """해산 확인 → 복귀 발화 후 대기장소로 출발. 반환: 복귀를 시작했으면 True."""
    with _lock:
        if not _session or _session["phase"] != "awaiting_dismiss":
            return False
    return _begin_return(f"해산 확인({by})")


def _ro(name: str) -> str:
    """'로/으로' 고르기. 받침 없음·ㄹ 받침·한글 아님 → '로', 그 밖의 받침 → '으로'."""
    last = name[-1:] if name else ""
    if not ("가" <= last <= "힣"):
        return "로"
    jong = (ord(last) - 0xAC00) % 28
    return "로" if jong in (0, 8) else "으로"


def _begin_return(reason: str) -> bool:
    """대기장소 복귀 시작. 해산 확인과 무응답 만료가 같은 길을 쓴다."""
    global _session
    with _lock:
        if not _session or _session["phase"] != "awaiting_dismiss":
            return False
        _session["phase"] = "returning"
        return_place = _session.get("return_place")
        dest = _session["destination_name"]

    # 장소 칸 이름은 오타인 LacationName 이 정본이다(LocationName 으로 읽으면 늘 None → 늘 "대기구역").
    place_name = (getattr(return_place, "LacationName", None) or "").strip() or "대기구역"
    _speak(f"{place_name}{_ro(place_name)} 이동하겠습니다.")
    log_event("schedule", "standby_dismissed",
              f"안내 종료 — {dest} → {place_name}", detail=reason,
              robot_id=get_robot_id(), robot_name=get_robot_name(),
              business_id=get_robot_business_id())

    if not return_place:
        _finish(RESULT_NAV_FAILED, "대기 장소를 찾을 수 없어 복귀하지 못함")
        return False
    if not _nav_to(return_place):
        _finish(RESULT_NAV_FAILED, "대기 장소 복귀를 시작하지 못함")
        return False
    return True


def on_return_arrived() -> bool:
    """대기장소 복귀 완료 → 세션 종료. 여기까지 와야 성공이다."""
    with _lock:
        if not _session or _session["phase"] != "returning":
            return False
    _finish(RESULT_COMPLETED, None, returned=True)
    return True


def abort(result: str, reason: str) -> None:
    """정지·오프라인 등 외부 요인으로 세션을 끊는다."""
    if _session:
        _finish(result, reason)


def _finish(result: str, reason: str | None, returned: bool = False) -> None:
    global _session
    with _lock:
        state = dict(_session) if _session else None
        _session = None
    if not state:
        return

    now = datetime.now()
    db = SessionLocal()
    try:
        sess = db.query(ServiceSession).filter(ServiceSession.id == state["id"]).first()
        if sess:
            sess.Result = result
            sess.Reason = reason[:200] if reason else None
            sess.EndedAt = now
            if returned:
                sess.ReturnedAt = now
            db.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[standby] 세션 종료 기록 실패: {e}")
        db.rollback()
    finally:
        db.close()

    print(f"[standby] 응대 종료: {state['destination_name']} → {result}"
          + (f" ({reason})" if reason else ""))
    log_event("schedule", "standby_session_end",
              f"응대 종료: {state['destination_name']} — {result}",
              detail=reason,
              robot_id=get_robot_id(), robot_name=get_robot_name(),
              business_id=get_robot_business_id())


def close_orphan_sessions(reason: str = "서버 재시작") -> int:
    """프로세스가 죽어 'running' 으로 남은 응대 세션을 마감한다.

    회차(`runs.close_orphan_runs`)·녹화에는 같은 정리가 이미 있는데 응대 세션만
    빠져 있었다. 남으면 이력에 '진행 중'으로 영구히 떠 응대 건수를 부풀린다.
    """
    db = SessionLocal()
    try:
        orphans = db.query(ServiceSession).filter(ServiceSession.Result == RESULT_RUNNING).all()
        if not orphans:
            return 0
        now = datetime.now()
        for sess in orphans:
            sess.Result = RESULT_ABORTED_OFFLINE
            sess.Reason = reason[:200]
            sess.EndedAt = sess.EndedAt or now
        db.commit()
        print(f"[standby] 고아 응대 세션 {len(orphans)}건 → {RESULT_ABORTED_OFFLINE} ({reason})")
        return len(orphans)
    except Exception as e:  # noqa: BLE001
        print(f"[standby ERR] 고아 세션 정리 실패: {e}")
        db.rollback()
        return 0
    finally:
        db.close()


# ─── 시계 ───

def tick(now: datetime, interval_sec: int) -> None:
    """경과 시간을 세고 해산 확인 무응답을 끝낸다.

    응대 시간 상한·연장·진전 감시는 두지 않는다(10-22) — 응대 시간은 그때그때 다르고,
    멈춰 선 응대는 운영자가 화면에서 보고 종료한다. 이동 실패·통신 두절은 따로 끝난다.
    """
    with _lock:
        if not _session:
            return
        # 해산 확인 무응답 — 사람이 그냥 가버린 경우다. 말없이 서 있지 않는다.
        if _session["phase"] == "awaiting_dismiss" and now >= _session["dismiss_until"]:
            expired = True
        else:
            expired = False
    if expired:
        _begin_return("무응답 만료")
        return

    with _lock:
        if _session:
            _session["elapsed_sec"] += interval_sec
