"""안내 이동 중 동행 확인 — "잘 따라오고 계신가요?"

이동하면서 묻는다(멈추지 않는다). 대답에 따라:

    네                 계속 이동
    무응답             한 번 더 묻고, 그래도 없으면 '아니요' 와 같다
    아니요             주행을 멈추고 기다린다 → "네" 를 들으면 같은 목적지로 다시 출발
    기다려도 안 옴      FollowMaxWaitSec 뒤 운영자 호출(관제 알림) → 방문자 "네" 또는 관제 '재개' 로 출발
    그만/취소          안내 종료

묻는 간격은 관제가 구간(목적지)별 과거 이동 시간으로 정해 번들에 넣어 준다
(Backend app/database/voice_follow.py). 도착 직전과 짧은 구간에서는 묻지 않는다.
"""

import logging
import re
import time
from typing import Callable, Optional

from robot_voice.backend import (
    NAV_ARRIVED, NAV_FAILED, NAV_MOVING, BackendError, nav_phase, obstacle_holding,
)

log = logging.getLogger("robot_voice.follow")

FOLLOW_ASK = "잘 따라오고 계시면 '네'라고 말씀해 주세요."
FOLLOW_ASK_AGAIN = "잘 따라오고 계신가요?"
FOLLOW_WAIT = "여기서 잠시 기다릴게요. 오시면 '네'라고 말씀해 주세요."
FOLLOW_LOST = "직원을 불렀어요. 잠시만 기다려 주세요."
FOLLOW_RESUME = "다시 안내할게요. 천천히 따라와 주세요."
FIXED_PHRASES = (FOLLOW_ASK, FOLLOW_ASK_AGAIN, FOLLOW_WAIT, FOLLOW_LOST, FOLLOW_RESUME)

LISTEN_SEC = 6.0             # 묻고 나서 대답을 기다리는 시간(이동 중)
HOLD_LISTEN_SEC = 6.0        # 멈춰 기다리는 동안 한 번에 듣는 시간 — 그 사이마다 관제 재개를 확인한다
NEAR_ARRIVAL_SEC = 8.0       # 평균 도착 시각까지 이만큼 남았으면 묻지 않는다
LOST_NOTICE_SEC = 45.0       # 운영자 호출 뒤 이만큼마다 "기다려 달라" 고 다시 말한다
RESUME_SETTLE_SEC = 1.5      # 정지 직후 바로 NAV 를 보내면 보행 전환과 겹쳐 제자리에 선다(0xa328)
POLL_SEC = 1.0

# 결과
ARRIVED = "arrived"
FAILED = "failed"
CLOSED = "closed"            # 관제에서 세션을 닫았다
CANCEL = "cancel"            # 방문자가 그만두겠다고 했다
RESUMED = "resumed"

# 이 질문에 한정한 낱말 — 시나리오 공용 classify_yes_no 보다 넓게 받는다
_CANCEL = ("취소", "그만", "멈춰", "정지", "스톱")
_NO = ("아니", "아뇨", "못", "잠깐", "잠시", "기다려", "천천히", "늦", "힘들")
_YES = ("네", "예", "응", "맞아", "그래", "괜찮", "따라가", "따라오", "가고있", "잘가", "있어요", "갈게")


def classify_follow(text: Optional[str]) -> Optional[str]:
    """YES | NO | CANCEL | None. '못 따라가요' 처럼 섞이면 NO 가 이긴다."""
    t = re.sub(r"[^0-9A-Za-z가-힣]", "", text or "")
    if not t:
        return None
    if any(w in t for w in _CANCEL):
        return "CANCEL"
    if any(w in t for w in _NO):
        return "NO"
    if any(w in t for w in _YES):
        return "YES"
    return None


class FollowGuide:
    """안내 이동 한 번을 도착까지 지켜보며 동행을 확인한다.

    Args:
        backend: 관제 클라이언트 (robot_state · follow · move · current_session)
        speak / listen: 시나리오와 같은 발화·청취 함수
        session_id: 세션 id
        plan: 번들 scenarios[].followCheck
        closed: 관제 종료 여부 (ScenarioRunner.closed_by_operator)
        clock / sleep: 시험에서 가상 시계를 넣는다
        on_tick: 이동 상태(/robot/nav)를 볼 때마다 불린다 — 경유지 방향 안내(ScenarioRunner._turn_cue_tick)
    """

    def __init__(self, backend, speak: Callable[[str], None],
                 listen: Callable[[float], Optional[str]], session_id: int, plan: dict,
                 closed: Callable[[], bool],
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 on_tick: Optional[Callable[[dict], None]] = None) -> None:
        self.backend = backend
        self.speak = speak
        self.listen = listen
        self.session_id = session_id
        self.plan = plan or {}
        self.closed = closed
        self.clock = clock
        self.sleep = sleep
        self.on_tick = on_tick
        self._legs = {leg["locationId"]: leg for leg in self.plan.get("legs") or []}

    def leg(self, place_id: Optional[int]) -> tuple[int, Optional[float]]:
        """(묻는 간격, 평균 이동 시간). 간격 0 이면 이 구간은 묻지 않는다."""
        leg = self._legs.get(place_id)
        if leg:
            return int(leg.get("intervalSec") or 0), leg.get("avgSec")
        return int(self.plan.get("intervalSec") or 0), None

    # ── 도착 대기 ─────────────────────────────────────
    def run(self, place_id: Optional[int], timeout_sec: float = 300.0) -> str:
        """도착까지 기다리며 간격마다 묻는다. 반환: ARRIVED | FAILED | CLOSED | CANCEL"""
        interval, avg = self.leg(place_id)
        log.info("동행 확인: 목적지 %s — 간격 %ss, 평균 %ss", place_id, interval or "끔", avg or "-")

        now = self.clock()
        deadline = now + timeout_sec
        moved = 0.0                  # 이번 구간에서 실제로 움직인 누적 시간(기다린 시간 제외)
        seg_start = now              # 이번 주행(출발·재출발) 시작
        last_ask = now
        started = False

        while True:
            if self.closed():
                return CLOSED
            now = self.clock()
            if now > deadline:
                return FAILED
            try:
                st = self.backend.robot_state()
            except BackendError:
                self.sleep(POLL_SEC)
                continue

            phase = nav_phase(st, started)
            if phase == NAV_ARRIVED:
                return ARRIVED
            if phase == NAV_FAILED:
                return FAILED
            if phase == NAV_MOVING:
                started = True
            if self.on_tick:
                self.on_tick(st)

            if obstacle_holding(st):
                # 장애물 앞에서 멈춰 있다 — 관제가 멘트·재출발을 맡는다. 묻지 않고, 기다린 시간은 도착 대기·간격에서 뺀다
                deadline += POLL_SEC
                last_ask += POLL_SEC
                seg_start += POLL_SEC
                self.sleep(POLL_SEC)
                continue

            elapsed = moved + (now - seg_start)
            if started and self._due(interval, avg, now - last_ask, elapsed):
                verdict = self._ask()
                last_ask = self.clock()
                if verdict in (CLOSED, CANCEL):
                    return verdict
                if verdict != "YES":
                    hold_from = self.clock()
                    moved += hold_from - seg_start
                    res = self._hold(place_id)
                    if res != RESUMED:
                        return res
                    deadline += self.clock() - hold_from     # 기다린 시간은 도착 대기에서 뺀다
                    seg_start = last_ask = self.clock()
                    started = False
                    continue

            self.sleep(POLL_SEC)

    @staticmethod
    def _due(interval: int, avg: Optional[float], since_ask: float, elapsed: float) -> bool:
        if not interval or since_ask < interval:
            return False
        # 곧 도착한다 — 지금 물으면 대답 도중에 도착 멘트가 겹친다
        if avg and elapsed >= avg - NEAR_ARRIVAL_SEC:
            return False
        return True

    # ── 묻기 ──────────────────────────────────────────
    def _ask(self) -> str:
        """YES | NO | NONE | CANCEL | CLOSED"""
        for attempt in range(2):
            self.speak(FOLLOW_ASK if attempt == 0 else FOLLOW_ASK_AGAIN)
            if self.closed():
                return CLOSED
            heard = self.listen(LISTEN_SEC)
            if self.closed():
                return CLOSED
            verdict = classify_follow(heard)
            log.info("  동행 확인 대답: %r → %s", heard, verdict or "없음")
            if verdict == "CANCEL":
                return CANCEL
            if verdict in ("YES", "NO"):
                return verdict
        return "NONE"

    # ── 멈춰 기다리기 ─────────────────────────────────
    def _hold(self, place_id: Optional[int]) -> str:
        """RESUMED | CLOSED | CANCEL | FAILED"""
        try:
            self.backend.follow(self.session_id, "waiting")
        except BackendError as e:
            # 멈추지 못했다 — 로봇은 계속 간다. 혼자 기다리면 사람과 더 멀어지므로 이동을 이어 간다.
            log.warning("주행 정지 요청 실패 — 이동을 계속한다: %s", e)
            return RESUMED
        self.speak(FOLLOW_WAIT)

        max_wait = float(self.plan.get("maxWaitSec") or 60)
        start = self.clock()
        lost_at: Optional[float] = None
        last_notice = start

        while True:
            if self.closed():
                return CLOSED
            if self._operator_resumed():
                log.info("  관제에서 재개 — 다시 출발한다")
                return self._resume(place_id)

            now = self.clock()
            if lost_at is None and now - start >= max_wait:
                self._report_lost()
                lost_at = last_notice = self.clock()
            elif lost_at is not None and now - last_notice >= LOST_NOTICE_SEC:
                self.speak(FOLLOW_LOST)
                last_notice = self.clock()

            heard = self.listen(HOLD_LISTEN_SEC)
            if self.closed():
                return CLOSED
            verdict = classify_follow(heard)
            if verdict == "CANCEL":
                return CANCEL
            if verdict == "YES":
                log.info("  방문자가 왔다 — 다시 출발한다")
                return self._resume(place_id)

    def _operator_resumed(self) -> bool:
        try:
            cur = self.backend.current_session()
        except BackendError:
            return False                    # 관제가 끊겼다 — 방문자 대답으로만 재개한다
        return bool(cur) and cur.get("id") == self.session_id and cur.get("followState") == "resume"

    def _report_lost(self) -> None:
        log.warning("  %ss 기다려도 오지 않는다 — 운영자 호출", self.plan.get("maxWaitSec") or 60)
        try:
            self.backend.follow(self.session_id, "lost")
        except BackendError as e:
            log.error("운영자 호출 실패(계속 기다린다): %s", e)
        self.speak(FOLLOW_LOST)

    def _resume(self, place_id: Optional[int]) -> str:
        self.speak(FOLLOW_RESUME)
        try:
            self.backend.follow(self.session_id, "ok")
        except BackendError as e:
            log.warning("동행 상태 해제 실패(출발은 한다): %s", e)
        self.sleep(RESUME_SETTLE_SEC)
        if place_id is None:
            return FAILED
        try:
            self.backend.move(self.session_id, place_id)
        except BackendError as e:
            log.error("재출발 실패: %s", e)
            return FAILED
        return RESUMED
