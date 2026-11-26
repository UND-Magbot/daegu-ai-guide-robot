"""웨이크워드 게이트 — "나를 부른 말"만 통과시킨다.

**왜 필요한가.** 신호처리로는 "손님이 로봇에게 하는 말"과 "손님이 동행에게 하는 말"을
가를 수 없다. 둘 다 크고 둘 다 정면이다. 빔포밍은 *방향*, AEC 는 *자기 소리*,
노이즈 억제는 *정상 소음*을 다룰 뿐 **"누구에게 하는 말인가"** 는 모른다.

선행 구현(voice_assistant/wakeword.py)의 실측 기록을 그대로 따른다.
2026-08-04 사무실 15분 측정에서 `고객:` 채택 54건이 전부 옆 대화였고, 로봇이 매번
응답하느라 자기 목소리 폐기가 162건이었다. 그 사이 진짜 발화가 '로봇 발화 중' 창에
걸려 같이 버려졌다 — 옆 대화가 파이프라인을 점유한 것이다.

여기서 구현한 것은 **텍스트 레벨 게이트**다. STT 결과에 호출어가 있어야 통과시킨다.
음향 레벨(openWakeWord)은 한국어 호출어 모델 학습이 선행이라 아직 없다. 둘은
배타적이지 않으므로, 나중에 음향 게이트가 들어와도 이 텍스트 게이트는 오탐 보정으로 남는다.
"""

from __future__ import annotations

import re
import time

from robot_voice import config


def _norm(s: str) -> str:
    """비교용 정규화 — 공백·문장부호를 지워 "큐리야," "큐리야!" 를 같게 본다."""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s)


_WORDS = tuple(_norm(w) for w in config.WAKE_WORDS if _norm(w))


class WakeGate:
    """호출어를 들으면 일정 시간 '청취 창'을 연다.

    한 문장에 호출어와 용건이 같이 오는 경우("큐리야 원무과로 안내해줘")와
    나눠 오는 경우("큐리야" → "원무과로 안내해줘")를 모두 받는다.
    """

    def __init__(self) -> None:
        self._open_until = 0.0

    @property
    def enabled(self) -> bool:
        return config.WAKE_ENABLED and bool(_WORDS)

    def is_open(self) -> bool:
        return time.time() < self._open_until

    def close(self) -> None:
        """용건을 처리했으면 창을 닫는다 — 다음 말까지 계속 열어두지 않는다."""
        self._open_until = 0.0

    def extend(self) -> None:
        self._open_until = time.time() + config.WAKE_WINDOW_SEC

    def open_for_response(self, seconds: float) -> None:
        """로봇이 방금 질문했다 — 그 응답을 받는 동안만 호출어를 면제한다.

        시간 기반 창(말할 때마다 연장)과 다르다. 로봇이 묻지 않은 시점에 들린 말은
        옆 사람 대화일 가능성이 높으므로 호출어를 요구한다.
        """
        self._open_until = time.time() + max(1.0, seconds)

    def check(self, text: str) -> tuple[bool, str, bool]:
        """발화를 게이트에 통과시킨다.

        Returns:
            (통과 여부, 호출어를 걷어낸 용건, 방금 불렸는지)

        - 게이트가 꺼져 있으면 항상 통과한다(기존 동작 유지).
        - 창이 열려 있으면 통과하고 창을 연장한다.
        - 호출어가 있으면 통과시키고 창을 연다.
        """
        if not self.enabled:
            return True, text, False

        norm = _norm(text)
        hit = next((w for w in _WORDS if w in norm), None)
        if hit:
            self.extend()
            return True, _strip_wake(text, hit), True

        if self.is_open():
            # 연장하지 않는다. 이 창은 '로봇이 던진 질문 하나'에 대한 응답용이다.
            return True, text, False

        return False, text, False


def is_wake_only(text: str) -> bool:
    """호출어만 부르고 용건이 없는 말인가("큐리야", "큐리야?")."""
    norm = _norm(text)
    hit = next((w for w in _WORDS if w in norm), None)
    return bool(hit) and not _norm(_strip_wake(text, hit))


def _strip_wake(text: str, hit_norm: str) -> str:
    """호출어를 문장에서 걷어낸다 — 용건 판정에 섞이면 목적지 매칭이 흐려진다."""
    for w in config.WAKE_WORDS:
        if _norm(w) == hit_norm:
            text = re.sub(re.escape(w), " ", text)
    return re.sub(r"\s+", " ", text).strip(" ,.!?~")
