"""단순 동작 응대 — "큐리야, 앉아" / "큐리야, 일어서" (전시 시연용).

관제 상시 대기 작업의 응대 방식을 '단순 동작'으로 등록했을 때만 쓴다(standby-context 의
responseMode=motion). 그 근무 중 대기 장소에서만 듣고, 근무가 끝나면 저절로 멈춘다.

**관제를 거치지 않는다.** 관제도 앉기·일어서기는 로봇 안 receiver(NOS)에 UDP JSON 한 줄
(`{"action": "SIT"}`)을 보내는 것이 전부라(Backend app/robot_io/sender.py), Jetson 이
같은 메시지를 보낸다. 대신 관제가 하던 안전 확인을 여기서 receiver 에 직접 묻는다
— 주행 중(NAV_STATUS ≠ 0)·충전 중·상태를 모를 때는 보내지 않는다. 응대 세션 기록은 남기지 않는다.

판정은 낱말로만 한다(LLM 없음). 앉아·일어서 두 가지 외의 동작은 열지 않는다.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time

from robot_voice import config

log = logging.getLogger("robot_voice.motion")

SIT = "SIT"
STAND = "STAND"

# receiver BasicStatus.MotionState — 관제 voice_scenario.STAND_STATES 와 같다
STAND_STATES = (1, 17)
SIT_STATES = (4,)
# receiver ChargeStatus.state — 1=도크로 이동, 2=충전 중, 5=도크·전류 없음(runtime.is_docked 와 같다)
DOCKED_STATES = (1, 2, 5)
# receiver 는 내비 상태를 1초마다 갱신한다(nav poll) — 이보다 오래되면 걷는 중인지 모른다
NAV_STALE_SEC = 5.0

SAY_OK = {SIT: "네, 앉을게요.", STAND: "네, 일어날게요."}
SAY_ALREADY = {SIT: "이미 앉아 있어요.", STAND: "이미 서 있어요."}
SAY_BUSY = {SIT: "지금 앉는 중이에요. 끝나면 다시 말씀해 주세요.",
            STAND: "지금 일어나는 중이에요. 끝나면 다시 말씀해 주세요."}
SAY_HELP = "'앉아' 또는 '일어서'라고 말씀해 주세요."
SAY_MOVING = "지금은 이동 중이라 할 수 없어요."
SAY_DOCKED = "지금은 충전 중이라 할 수 없어요."
SAY_UNKNOWN = "로봇 상태를 확인할 수 없어 지금은 할 수 없어요."
FIXED_PHRASES = (*SAY_OK.values(), *SAY_ALREADY.values(), *SAY_BUSY.values(), SAY_HELP, SAY_MOVING, SAY_DOCKED,
                 SAY_UNKNOWN)

# 어간으로 본다 — "앉아/앉아줘/앉으세요/앉자", "일어서/일어나/일어설래".
_SIT_WORDS = ("앉아", "앉자", "앉어", "앉으", "앉을")
_STAND_WORDS = ("일어서", "일어나", "일어설", "일어날", "기립")
# "앉지 마", "일어나지 마" — 부정이면 아무것도 하지 않는다
_NEGATIONS = ("지마", "지말", "말아", "하지마")


def _norm(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text or "")


def classify(text: str | None) -> str | None:
    """SIT | STAND | None. 둘 다 들었거나 부정이면 None — 애매하면 움직이지 않는다."""
    t = _norm(text or "")
    if not t or any(n in t for n in _NEGATIONS):
        return None
    sit = any(w in t for w in _SIT_WORDS)
    stand = any(w in t for w in _STAND_WORDS)
    if sit == stand:
        return None
    return SIT if sit else STAND


class ReceiverLink:
    """receiver(NOS) UDP 창구. 관제 sender.py·polling.py 와 같은 메시지를 쓴다."""

    def __init__(self, host: str | None = None, port: int | None = None,
                 timeout: float | None = None) -> None:
        self.addr = (host or config.RECEIVER_IP, port or config.RECEIVER_PORT)
        self.timeout = timeout if timeout is not None else config.RECEIVER_TIMEOUT_SEC

    def ask(self, action: str) -> dict | None:
        """응답이 오는 조회(STATUS·NAV_STATUS). 응답이 없거나 깨졌으면 None."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(self.timeout)
            sock.sendto(json.dumps({"action": action}).encode("utf-8"), self.addr)
            data, _ = sock.recvfrom(16384)
            return json.loads(data.decode("utf-8"))
        except (OSError, ValueError) as e:
            log.warning("receiver %s 조회 실패(%s:%d): %s", action, *self.addr, e)
            return None
        finally:
            sock.close()

    def send(self, action: str) -> None:
        """응답 없는 명령(SIT·STAND) — fire-and-forget."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(json.dumps({"action": action}).encode("utf-8"), self.addr)
        finally:
            sock.close()


def _reached(action: str, posture) -> bool:
    return posture in (SIT_STATES if action == SIT else STAND_STATES)


def decide(action: str, link: ReceiverLink, pending: tuple[str, float] | None = None,
           now: float | None = None) -> tuple[bool, str]:
    """보내도 되는지 receiver 에 묻는다. 반환: (보낼지, 말할 문장).

    Args:
        pending: 마지막으로 보낸 (명령, 보낸 시각). 로봇 자세가 아직 그 명령의 자세가 아니면
            동작 중으로 보고 거절한다 — 정해진 시간 동안 귀를 닫는 대신 실제 상태로 판단한다.
            MOTION_ACTION_TIMEOUT_SEC 가 지나도 자세가 안 바뀌었으면(명령을 안 먹었으면) 다시 받는다.
    """
    status = link.ask("STATUS")
    nav = link.ask("NAV_STATUS")
    if status is None or nav is None:
        return False, SAY_UNKNOWN
    basic = status.get("BasicStatus") or {}
    if basic.get("receiver_stale"):
        # receiver 가 로봇 본체 상태를 15초 넘게 못 받았다 — 자세·주행을 믿을 수 없다
        return False, SAY_UNKNOWN
    nav_age = nav.get("age")
    if nav.get("status") is None or nav_age is None or nav_age > NAV_STALE_SEC:
        # 내비 상태를 1초마다 받는데 오래 끊겼다 — 걷는 중인지 모른다
        return False, SAY_UNKNOWN
    if nav["status"] != 0:
        return False, SAY_MOVING
    charge = (status.get("ChargeStatus") or {}).get("state")
    if charge in DOCKED_STATES:
        return False, SAY_DOCKED
    posture = basic.get("MotionState")
    if pending is not None:
        last, sent_at = pending
        elapsed = (now if now is not None else time.time()) - sent_at
        if not _reached(last, posture) and elapsed < config.MOTION_ACTION_TIMEOUT_SEC:
            return False, SAY_BUSY[last]
    if (action == SIT and posture in SIT_STATES) or (action == STAND and posture in STAND_STATES):
        return False, SAY_ALREADY[action]
    return True, SAY_OK[action]
