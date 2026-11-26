"""자동 대답 — 시험용. 상태 폴더에 auto_answer.json 이 있으면 마이크 대신 단계에 맞는 대답을 돌려준다.

안내 반복(관제 안내 시작 창 '무한')과 함께 써서 사람 없이 시나리오를 계속 돌린다.
    켜기: echo {} > /var/lib/robot-voice/auto_answer.json     끄기: 파일 삭제
듣기마다 파일을 보므로 재시작이 필요 없다 — 다음 듣기부터 적용된다.

파일 내용(모두 선택):
    {"answers": {"<시나리오>.<단계 코드>": "<대답>", "<단계 코드>": "<대답>"}, "delaySec": 1.5}
    예: {"answers": {"hospital.purpose_ask": "안과"}}  — 시나리오를 붙인 쪽이 먼저

단계별 기본 대답:
    예/아니요 질문        "네" — 한 세션에서 같은 단계에 두 번째로 오면 "아니요"
                          ("추가로 안내가 필요한 곳?" 이 끝없이 이어지지 않게)
    이동 중 동행·장애물 확인  "네"
    신청서 작성 대기       "작성 완료"
    목적지 질문           첫 번째 목적지(맵 장소가 연결된 것, 층 이동 목적지 제외) — 매번 같다
                          (단, DEFAULT_ANSWERS 에 적힌 단계는 그 말 — 병원 수납 뒤는 "3층 관공서요")
    운영자 대기(음성 출구)  음성 출구 말 첫 번째
    그 밖                 None → 평소처럼 마이크로 듣는다
"""
import json
import logging
import os
import time
from typing import Callable, Optional

from robot_voice import config

log = logging.getLogger(__name__)

TAG = "[자동] "                 # 관제 세션 대답 기록(Heard) 앞에 붙여 사람 대답과 구분한다
YES = "네"
NO = "아니요"
FORM_DONE = "작성 완료"
DEFAULT_DELAY_SEC = 1.5         # 질문 멘트가 끝난 뒤 대답하기까지 — 사람이 말하는 틈과 비슷하게
FLOOR_LINK_WORD = "엘리베이터"   # 층 이동 목적지는 운영자 손이 필요해 자동으로 고르지 않는다
_POLL_SEC = 0.1
# 단계별 기본 대답 — 파일의 answers 가 같은 열쇠면 파일이 이긴다
DEFAULT_ANSWERS = {
    # 1층 병원 수납 뒤 "더 안내가 필요한 곳" — 진료실 재방문 대신 3층 관공서(1층 엘리베이터 입구)로(11-05 요청)
    "hospital.exit_ask": "3층 관공서요",
    # 3층 관공서 끝 "더 안내가 필요한 곳 있으세요?" — 추가 목적지 없이 바로 끝낸다(11-05 요청)
    "public_office.finish_ask": "아니요",
}
_warned = False                 # 깨진 파일 경고는 고쳐질 때까지 한 번만


def settings() -> Optional[dict]:
    """켜져 있으면 설정(빈 dict 가능), 꺼져 있으면 None. 파일이 깨져 있어도 켜진 것으로 보고 기본값을 쓴다."""
    global _warned
    path = config.AUTO_ANSWER_FILE
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        if not _warned:
            log.warning("자동 대답 파일을 읽지 못해 기본값으로 대답한다: %s", e)
            _warned = True
        return {}
    _warned = False
    return data if isinstance(data, dict) else {}


def dest_choices(slots: list[dict]) -> list[str]:
    """자동으로 말할 수 있는 목적지 이름 — 맵 장소가 연결돼 있고 층 이동이 아닌 것, 번들 순서대로."""
    return [sl["value"] for sl in slots
            if sl.get("value") and sl.get("locationId") is not None and FLOOR_LINK_WORD not in sl["value"]]


def wait(delay: float, timeout: float, stopped: Callable[[], bool]) -> bool:
    """대답하기 전 잠깐 기다린다. 그 사이 안내가 끝나면 True."""
    end = time.monotonic() + max(0.0, min(delay, timeout))
    while time.monotonic() < end:
        if stopped():
            return True
        time.sleep(_POLL_SEC)
    return stopped()


class AutoAnswer:
    """세션마다 단계 방문 횟수를 세어 대답을 고른다."""

    def __init__(self) -> None:
        self._sid = None
        self._last_code: Optional[str] = None
        self._visits: dict[str, int] = {}
        self.last_given: Optional[str] = None

    def _visit(self, session: dict) -> int:
        """지금 단계가 이 세션에서 몇 번째 방문인가. 같은 방문 안의 되묻기는 세지 않는다."""
        sid, code = session.get("id"), session.get("currentStepCode") or ""
        if sid != self._sid:
            self._sid, self._last_code, self._visits = sid, None, {}
        if code != self._last_code:
            self._visits[code] = self._visits.get(code, 0) + 1
            self._last_code = code
        return self._visits.get(code, 0)

    def answer(self, step: dict, session: dict, dests: list[str], conf: dict) -> Optional[str]:
        """이 단계에 넣을 대답. None 이면 자동으로 대답하지 않는다(마이크로 듣는다)."""
        visits = self._visit(session)
        code = step.get("stepCode") or session.get("currentStepCode") or ""
        given = {**DEFAULT_ANSWERS, **(conf.get("answers") if isinstance(conf.get("answers"), dict) else {})}
        # "시나리오.단계" 가 먼저 — 단계 코드(purpose_ask 등)는 병원·관공서에 똑같이 있다
        own = given.get(f"{session.get('scenarioCode') or ''}.{code}") or given.get(code)
        if isinstance(own, str) and own.strip():
            return own.strip()
        if step.get("waitFor") == "operator":
            words = [w for w in (step.get("voiceExitKeywords") or []) if isinstance(w, str) and w.strip()]
            return words[0].strip() if words else None
        intent = step.get("expectedIntent")
        if intent == "FORM_COMPLETE":
            return FORM_DONE
        if intent == "YES_NO":
            return NO if visits > 1 else YES
        if step.get("waitFor") == "arrival":
            return YES
        if intent in ("PURPOSE", "DESTINATION"):
            return dests[0] if dests else None
        return None

    def delay(self, conf: dict) -> float:
        try:
            return max(0.0, float(conf.get("delaySec", DEFAULT_DELAY_SEC)))
        except (TypeError, ValueError):
            return DEFAULT_DELAY_SEC

    def tag_heard(self, patch: Callable[..., None]) -> Callable[..., None]:
        """세션 갱신을 감싸, 방금 자동으로 넣은 대답이 기록될 때 앞에 [자동] 을 붙인다."""
        def wrapped(**fields) -> None:
            if self.last_given is not None and fields.get("heard") == self.last_given:
                fields["heard"] = TAG + fields["heard"]
                self.last_given = None
            return patch(**fields)
        return wrapped
