"""안내 중 장애물 정지·재개 — 관제 쪽 상태 (2026-10-29, 10-30 시간표 변경).

NOS 감지 노드(robot_receiver/obstacle_guard.py)가 앞이 막히면 바로 이동을 취소한다(관제 폴링을 기다리면
멈추는 거리가 줄어든다 — 10-18 시뮬). 관제는 주행 감시(polling.nav_thread) 주기마다 감지 노드에 '안내 중' 신호를
보내 감지를 켜 두고, 멈췄다는 보고를 받으면 여기서 정지 상태를 잡는다. 시간은 멈춘 순간부터 센다.

    정지 중        도착·재전송·멈춤 판정을 쉰다(폴링이 그 주기를 건너뛴다)
    멈춤 알자마자 멈춤 멘트(OBSTACLE_ANNOUNCE_SEC 0) — 1.5초 기준은 감시 주기 1초와 겹쳐 2.4초 뒤에 나왔다(10-30 #505).
                   기준을 0보다 크게 두면 그 시간 안에 풀린 정지는 말없이 다시 출발한다
    30초           '조금 더 기다렸다가 출발할게요' 1번(관제 알림 없음 — 10-29 실기 정지는 11·23·46초에 풀림)
    풀림(2분 전)   (멘트했으면) '다시 출발하겠습니다' → RESUME_LEAD_SEC 뒤 같은 목표 재전송(멘트가 출발보다 먼저 들리게).
                   그 사이 다시 막히면 출발하지 않고 정지를 이어 간다.
                   OBSTACLE_ASK_AFTER_SEC 넘게 멈췄으면(긴 정지) 먼저 손님에게 '준비됐어요' 를 묻는다(시연 대본 19~21, 10-30):
                   Jetson 이 /robot/nav obstacle.ask 를 보고 듣고 /voice/session/{id}/obstacle-answer 로 답한다.
                   '네·준비됐어요·가요' → 출발. 무응답·다른 말 → 한 번 더 묻고, 그래도 아니면 직원 호출.
                   묻는 중 다시 막히면 말없이 정지로 돌아가 풀리면 다시 묻는다
    2분            직원 호출 = 관제 오류 알림·확인창(nav_hold 'guide'), 45초마다 대기 안내.
                   이때부터는 길이 비어도 스스로 출발하지 않고 관제 처리까지 그 자리에서 기다린다(자동 종료 없음)
                   — 직원이 오는 중에 로봇이 떠나면 헛걸음한다(10-30 사용자 결정).
                   '다시 출발' 은 감지 노드가 앞이 비었다고 할 때만 받는다(잘못 눌러도 막힌 쪽으로 가지 않게).
                   '안내 종료' 는 운영자 종료로 세션을 닫는다.
멘트는 /robot/nav 의 notice {seq, text} 로 싣고 음성 엔진이 seq 가 바뀌면 말한다(방향 안내 cue 와 같은 방식).
감지 노드는 관제 신호가 GUARD_TTL_SEC 끊기면 스스로 감지를 끈다 — 관제가 죽어도 로봇을 계속 세우지 않게.
전방 카메라(OBSTACLE_CAMERA_ENABLED)에도 같은 켜기 신호를 위치와 함께 보낸다(C3 — 카메라가 사람 위치를 NOS 로 보낸다).
감지는 안내 시나리오 세션의 이동에서만 켠다(10-29 결정). OBSTACLE_GUARD_MODE=record 면 신호에 record 를 실어
감지 노드가 판단·기록만 하게 하고, 관제도 정지 상태를 잡지 않는다(실기 확인용).
"""
from __future__ import annotations

import json
import socket
import threading
import time

from app.robot_io import config

HOLD = "hold"        # 정지 중 — 폴링은 이번 주기 판정을 건너뛴다
RESUME = "resume"    # 지금 같은 목표를 다시 보낸다
ABORT = "abort"      # 운영자가 안내를 끝냈다

GUARD_TTL_SEC = 3.0
EXCHANGE_TIMEOUT_SEC = 0.3

# 시연 대본(docs/본사_Demo_시연_수정대본_Ji_261019_개정안.md 19~21)과 같은 문장 — Jetson OBSTACLE_PHRASES 와 글자까지 같게
STOP_TEXT = "앞쪽에 장애물이 있어 잠시 멈추겠습니다. 그 자리에서 기다려 주세요."
RESUME_TEXT = "다시 출발하겠습니다."
WAIT_TEXT = "아직 앞이 막혀 있습니다. 조금 더 기다리겠습니다."
ASK_TEXT = "이제 다시 이동할 수 있습니다. 준비되시면 '준비됐어요'라고 말씀해 주세요."
STAFF_TEXT = "직원을 불렀습니다. 잠시만 기다려 주세요."
FIXED_PHRASES = (STOP_TEXT, RESUME_TEXT, WAIT_TEXT, ASK_TEXT, STAFF_TEXT)
ASK_TRIES = 2            # 같은 풀림에서 묻는 횟수 — 그래도 대답이 없으면 직원 호출(대본: 10초 뒤 한 번 더 → 운영자 알림)
ASK_TIMEOUT_SEC = 20.0   # Jetson 이 답을 안 보내면(끊김) 이 시간 뒤 무응답으로 본다 — 질문 약 5초 + 듣기 6초 + 여유
HOLD_REASON = "장애물 정지"
WAIT_NOTICE_SEC = 30.0   # 멈춘 뒤 이 시간에 '조금 더 기다릴게요' 를 한 번 한다
RETRY_BLOCKED_MSG = "앞이 아직 막혀 있어 출발할 수 없습니다. 앞이 비면 다시 고르세요."
# '다시 출발할게요' 뒤 재전송까지(초). 감시 주기 1초에 맞춰 실제로는 1~2초 뒤에 나가고, 로봇은 재전송 뒤 약 1.6초에
# 움직인다 — 멘트(약 1초, Jetson 은 10-30 #505 에서 0.06초 만에 받아 말함)가 끝난 뒤 출발한다
RESUME_LEAD_SEC = 1.0

_lock = threading.Lock()
_st: dict = {"holding": False, "since": 0.0, "episode": None, "done_episode": None,
             "announced": False, "waited": False, "staff": False, "staff_said": 0.0, "guard": None, "kind": None,
             "resume_at": None, "asking": None, "answer": None, "ask_seq": 0}
_notice: dict = {"seq": 0, "text": None}


def mode() -> str:
    """off | record | on"""
    m = getattr(config, "OBSTACLE_GUARD_MODE", None)
    if m in ("off", "record", "on"):
        return m
    return "on" if getattr(config, "OBSTACLE_GUARD_ENABLED", False) else "off"


def enabled() -> bool:
    return mode() != "off"


def get_notice() -> dict:
    """마지막 장애물 멘트 {seq, text}. seq 가 바뀌면 새 멘트다."""
    with _lock:
        return dict(_notice)


# 감지 노드가 정지마다 카메라·라이다로 가른 구분(C3) — 지금은 알림·기록에만 쓰고 동작은 같다
KIND_LABEL = {"person": "사람", "moving": "움직이는 물체", "object": "사람 아닌 물체", "unknown": "구분 못 함"}


def _kind() -> str | None:
    """이번 정지의 구분 — 감지 노드는 풀리는 순간 구분을 지우므로 정지 중 받은 값을 들고 있는다."""
    return _st.get("kind")


def front_state() -> str:
    """지금 앞: clear | blocked | unknown(감지 노드 답 없음·판단 전)."""
    with _lock:
        st = (_st.get("guard") or {}).get("state")
    return st if st in ("clear", "blocked") else "unknown"


def retry_blocked_reason() -> str | None:
    """운영자 '다시 출발' 을 받을 수 없으면 그 사유 — nav_hold.request 가 장애물 정지일 때 묻는다."""
    return None if front_state() == "clear" else RETRY_BLOCKED_MSG


def snapshot() -> dict:
    """화면·음성 엔진용 — 정지 중인가, 몇 초째인가, 직원을 불렀는가, 지금 앞, 무엇에 막혔나."""
    with _lock:
        held = time.time() - _st["since"] if _st["holding"] else 0.0
        ask = _st["asking"]
        out = {"holding": _st["holding"], "heldSec": round(held, 1), "staffCalled": _st["staff"],
               "kind": _kind() if _st["holding"] else None,
               # Jetson 은 notice seq 가 ask.noticeSeq 이상(질문을 말한 뒤)일 때 듣는다
               "ask": {"id": ask["id"], "noticeSeq": ask["notice"]} if ask and _st["holding"] else None}
    out["front"] = front_state() if out["holding"] else None
    return out


def reset() -> None:
    """주행이 끝났거나 새로 시작했다. 정지 상태를 지운다(멘트 번호는 이어 간다)."""
    with _lock:
        _st.update(holding=False, since=0.0, episode=None, announced=False, waited=False, staff=False, resume_at=None,
                   asking=None, answer=None)


def submit_answer(ask_id: int, answer: str) -> bool:
    """Jetson 이 들은 대답 — yes | no | none. 지금 묻는 질문의 답이 아니면 False."""
    with _lock:
        ask = _st["asking"]
        if not ask or ask["id"] != ask_id:
            return False
        _st["answer"] = (ask_id, answer)
    print(f"[OBSTACLE] 손님 대답 #{ask_id}: {answer}")
    return True


def _say(text: str) -> None:
    with _lock:
        _notice["seq"] += 1
        _notice["text"] = text
    print(f"[OBSTACLE] 멘트 #{_notice['seq']}: {text}")


def _exchange(req: dict) -> dict | None:
    """감지 노드와 한 번 주고받는다. 응답이 없으면 None(감지 노드 없음·통신 두절)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(EXCHANGE_TIMEOUT_SEC)
        sock.sendto(json.dumps(req).encode("utf-8"), (config.RECEIVER_IP, config.OBSTACLE_GUARD_PORT))
        data, _ = sock.recvfrom(4096)
        return json.loads(data.decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        sock.close()


def _notify_camera(req: dict, pos: tuple[float, float] | None) -> None:
    """카메라 서비스에 켜기 신호(응답 안 기다림). 실패해도 판정에 영향 없다."""
    if not getattr(config, "OBSTACLE_CAMERA_ENABLED", False):
        return
    from app.robot_io import runtime
    from app.user_cache import get_robot_id
    rid = get_robot_id()
    p = runtime.get_position(rid) if rid else None
    msg = {**req, "reply": False,
           "pose": [round(p["x"], 3), round(p["y"], 3), round(p["yaw"], 3)] if p and p.get("timestamp") else None}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(json.dumps(msg).encode("utf-8"), (config.OBSTACLE_CAMERA_HOST, config.OBSTACLE_CAMERA_PORT))
    except OSError:
        pass
    finally:
        sock.close()


def _stop_points() -> list[list[float]]:
    """남은 지점 가운데 로봇이 서는 곳(통과 지점 제외) — 감지 노드는 이 근처에서 보지 않는다(도착은 로봇 회피에 맡김)."""
    from app.navigation import send_move as sm
    i = max(0, sm.heading_wp_no() - 1)
    return [[round(w["x"], 2), round(w["y"], 2)] for w in sm.waypoints_list[i:] if not sm._passes(w)]


def _position() -> tuple[float, float] | None:
    from app.robot_io import runtime
    from app.user_cache import get_robot_id
    rid = get_robot_id()
    pos = runtime.get_position(rid) if rid else None
    return (float(pos["x"]), float(pos["y"])) if pos and pos.get("timestamp") else None


def _log(kind: str, msg: str, level: str = "schedule") -> None:
    from app.logs.service import log_event
    from app.user_cache import get_robot_business_id, get_robot_id, get_robot_name
    log_event(level, kind, msg, robot_id=get_robot_id(), robot_name=get_robot_name(),
              business_id=get_robot_business_id())


def _target_name() -> str:
    from app.navigation import send_move as sm
    return (sm.get_current_target() or {}).get("name") or "목적지"


def cycle(nav_active: bool, guide: bool) -> str:
    """주행 감시 한 주기. HOLD | RESUME | ABORT | ""(평소 판정). guide = 안내 시나리오 세션 이동 중."""
    if not (enabled() and nav_active and guide):
        if _st["holding"]:
            reset()
        return ""
    pos = _position()
    record = mode() == "record"
    req = {"action": "GUARD", "arm": True, "record": record, "suppress": False, "stops": _stop_points(),
           "ttl": GUARD_TTL_SEC}
    _notify_camera(req, pos)
    resp = _exchange(req)
    with _lock:
        _st["guard"] = resp
        if _st["holding"] and (resp or {}).get("kind"):
            _st["kind"] = resp["kind"]
    if record:
        return ""            # 기록만 — 감지 노드가 이동을 취소하지 않으니 관제도 평소대로 판정한다
    if not _st["holding"]:
        ep = (resp or {}).get("episode")
        if resp and resp.get("stopped") and ep != _st["done_episode"]:
            with _lock:
                _st.update(holding=True, since=time.time(), episode=ep, announced=False, waited=False, staff=False,
                           kind=resp.get("kind"))
            print(f"[OBSTACLE] 앞이 막혀 멈췄다 — {_target_name()} 가는 중 (거리 {resp.get('lastDist')}m)")
            _log("guide_obstacle_stop", f"안내 이동 — 앞이 막혀 멈췄습니다 ({_target_name()} 가는 중)")
            if config.OBSTACLE_ANNOUNCE_SEC <= 0:
                with _lock:
                    _st["announced"] = True
                _say(STOP_TEXT)      # 다음 주기(1초)를 기다리지 않는다
            return HOLD
        return ""
    return _holding_step(resp, pos)


def _holding_step(resp: dict | None, pos: tuple[float, float] | None) -> str:
    from app.navigation import nav_hold
    now = time.time()
    held = now - _st["since"]
    if _st["staff"]:
        # 직원 호출 뒤 — 관제 처리까지 그 자리에서 기다린다
        h = nav_hold.get_hold()
        if h and h.get("reason") == HOLD_REASON:
            if _st["resume_at"] is not None:
                return _lead_step(now, held, nav_hold, "운영자 다시 출발")
            req = nav_hold.take_request()
            if req == "retry":
                if front_state() != "clear":
                    _retry_blocked()
                    return HOLD
                _start_lead(now)
                return HOLD
            if req == "abort":
                return ABORT
        if now - _st["staff_said"] >= config.OBSTACLE_STAFF_REPEAT_SEC:
            with _lock:
                _st["staff_said"] = now
            _say(STAFF_TEXT)
        return HOLD
    if resp is None:
        return HOLD          # 감지 노드와 끊겼다 — 앞을 모르는 채 출발하지 않는다(두절 60초면 폴링이 안전 정지)
    if _st["resume_at"] is not None:
        return _lead_step(now, held, None, "막힘 풀림")
    if _st["asking"] is not None:
        out = _asking_step(now, held)
        if out is not None:
            return out
    elif resp.get("state") == "clear":
        if held < config.OBSTACLE_SETTLE_SEC:
            return HOLD
        if _st["announced"] and held >= config.OBSTACLE_ASK_AFTER_SEC:
            _start_ask(1)        # 긴 정지 — 손님 준비를 묻는다
            return HOLD
        if _st["announced"]:
            _start_lead(now)     # 멘트 먼저 — 재전송은 RESUME_LEAD_SEC 뒤
            return HOLD
        _end_hold(f"막힘 풀림 — {held:.0f}초 만에 다시 출발 (막은 것: {KIND_LABEL.get(_kind(), '구분 못 함')})")
        return RESUME
    if not _st["announced"] and held >= config.OBSTACLE_ANNOUNCE_SEC:
        with _lock:
            _st["announced"] = True
        _say(STOP_TEXT)
    if not _st["waited"] and held >= WAIT_NOTICE_SEC:
        with _lock:
            _st["waited"] = True
        _say(WAIT_TEXT)
    if held >= config.OBSTACLE_STAFF_SEC:
        _call_staff(now, pos, f"앞이 {int(held)}초째 막혀 있습니다")
    return HOLD


def _call_staff(now: float, pos: tuple[float, float] | None, why: str) -> None:
    """직원 호출 — 관제 오류 알림·확인창. 이 뒤로는 관제 처리까지 그 자리에서 기다린다."""
    from app.navigation import nav_hold
    from app.navigation.send_move import heading_wp_no
    with _lock:
        _st.update(staff=True, staff_said=now, asking=None, answer=None)
    nav_hold.open_hold("guide", heading_wp_no(), _target_name(), pos, HOLD_REASON)
    _log("guide_obstacle_staff",
         f"안내 이동 — {why}. 로봇은 그 자리에서 기다립니다. 현장 확인 후 "
         f"'다시 출발'(앞이 비었을 때만) 또는 '안내 종료'를 고르세요 "
         f"({_target_name()} 가는 중, 막은 것: {KIND_LABEL.get(_kind(), '구분 못 함')})", level="error")
    _say(STAFF_TEXT)


def _start_ask(tries: int) -> None:
    with _lock:
        _st["ask_seq"] += 1
        _st["answer"] = None
        _st["asking"] = {"id": _st["ask_seq"], "at": time.time(), "tries": tries, "notice": _notice["seq"] + 1}
    _say(ASK_TEXT)


def _asking_step(now: float, held: float) -> str | None:
    """손님 준비를 묻는 중. 반환 None 이면 평소 판정(멘트·직원 호출 시간)을 이어 간다."""
    ask = _st["asking"]
    front = front_state()
    if front == "blocked":
        with _lock:
            _st["asking"] = None     # 다시 막혔다 — 말없이 정지로(대본 21 비고). 풀리면 다시 묻는다
        print("[OBSTACLE] 묻는 중 다시 막힘 — 정지로 돌아감")
        return None
    with _lock:
        ans = _st["answer"][1] if _st["answer"] and _st["answer"][0] == ask["id"] else None
    if ans == "yes":
        if front != "clear":
            # 감지 노드가 막 다시 켜져 판단 전(unknown) — 막힌 게 아니다. 대답은 들고 있다가 비면 바로 출발한다
            # (11-02 세션 521: 이 상태를 '다시 막힘'으로 보고 대답을 버려 "아직 앞이 막혀" 를 말했다)
            return HOLD
        with _lock:
            _st["asking"] = None
        _start_lead(now)
        return HOLD
    if ans is None and now - ask["at"] < ASK_TIMEOUT_SEC:
        return HOLD
    if ask["tries"] < ASK_TRIES:
        _start_ask(ask["tries"] + 1)
        return HOLD
    _call_staff(now, _position(), f"막힘은 풀렸지만 손님이 '준비됐어요'에 대답하지 않습니다({int(held)}초 정지)")
    return HOLD


def _start_lead(now: float) -> None:
    """'다시 출발할게요' 를 하고 재전송을 RESUME_LEAD_SEC 미룬다."""
    with _lock:
        _st["resume_at"] = now
    _say(RESUME_TEXT)


def _lead_step(now: float, held: float, nav_hold, why: str) -> str:
    """멘트 뒤 기다리는 중 — 다시 막히면 출발을 거두고, 시간이 되면 재전송한다. nav_hold 가 있으면(직원 호출 뒤) 창을 닫는다."""
    front = front_state()
    if front == "blocked":
        with _lock:
            _st["resume_at"] = None
        print(f"[OBSTACLE] {why} — 출발 직전 다시 막혀 출발하지 않음")
        if nav_hold is not None:
            _retry_blocked()
        return HOLD
    if front != "clear":
        return HOLD          # 판단 전(unknown) — 출발은 미루되 거두지 않는다
    if now - _st["resume_at"] < RESUME_LEAD_SEC:
        return HOLD
    if nav_hold is not None:
        nav_hold.close_hold()
    _end_hold(f"{why} — {held:.0f}초 정지 뒤 다시 출발 (막은 것: {KIND_LABEL.get(_kind(), '구분 못 함')})")
    return RESUME


def _retry_blocked() -> None:
    # 운영자 '다시 출발' 을 받은 뒤 다시 막혔다 — 출발하지 않는다. 확인창은 그대로 열려 있다
    print("[OBSTACLE] 운영자 다시 출발 — 그사이 다시 막혀 출발하지 않음")
    _log("guide_obstacle_retry_blocked", f"안내 이동 — {RETRY_BLOCKED_MSG} ({_target_name()} 가는 중)")


def _end_hold(why: str) -> None:
    with _lock:
        ep = _st["episode"]
        _st.update(holding=False, since=0.0, episode=None, announced=False, waited=False, staff=False,
                   resume_at=None, asking=None, answer=None, done_episode=ep)
    print(f"[OBSTACLE] {why}")
    _log("guide_obstacle_resume", f"안내 이동 재개 — {why}")
