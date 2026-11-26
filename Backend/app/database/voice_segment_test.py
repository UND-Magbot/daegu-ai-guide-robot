"""안내 시나리오 구간 테스트 — 시작 단계부터 종료 단계까지만 돌려 보기 위한 검사.

세션을 시작 단계로 열면 Jetson 엔진이 평소처럼 이어받아 돈다. 관제는
    - 종료 단계를 지나는 순간(엔진의 단계 갱신) 세션을 닫고,
    - 종료 단계가 운영자 입력 단계면 그 단계에 닿았을 때(paused) 닫는다.
그 전에 여기서 두 가지를 막는다.
    1) 시작 단계에서 종료 단계로 갈 수 없는 조합 — 종료 단계를 끝내 만나지 않아 시나리오 끝까지 돈다
    2) 앞 단계에서 정해졌어야 할 값이 빈 채로 시작하는 조합 — "번 접수창구로" 처럼 빈 멘트가 나오고,
       고객 목적지 이동은 '목적지를 확인하지 못했어요' 로 끝난다
"""

from collections import deque
from typing import Iterable, Protocol

INPUT_DESTINATION = "destination"
INPUT_TICKET = "ticket"
INPUT_COUNTER = "counter"

# 멘트 치환자 → 필요한 값. {도착지}·{도착안내} 도 고객 목적지에서 나온다.
_PLACEHOLDERS = {
    "{목적지}": INPUT_DESTINATION, "{도착지}": INPUT_DESTINATION, "{도착안내}": INPUT_DESTINATION,
    "{대기번호}": INPUT_TICKET, "{창구}": INPUT_COUNTER,
}
_DEST_INTENTS = ("PURPOSE", "DESTINATION")


class StepLike(Protocol):
    StepCode: str
    SayText: str | None
    WaitFor: str
    ExpectedIntent: str | None
    ActionType: str
    SlotName: str | None
    NextStepCode: str | None
    OnNoStepCode: str | None
    OnTimeoutStepCode: str | None


def _edges(st: StepLike) -> list[str]:
    # 음성 출구(운영자 단계에서 방문자 말로 넘어가는 갈래)도 실제로 가는 길이다
    exit_code = getattr(st, "VoiceExitStepCode", None)
    return [c for c in (st.NextStepCode, st.OnNoStepCode, st.OnTimeoutStepCode, exit_code) if c]


def _uses(st: StepLike) -> set[str]:
    say = st.SayText or ""
    out = {need for ph, need in _PLACEHOLDERS.items() if ph in say}
    if st.ActionType == "navigate" and st.SlotName:
        out.add(INPUT_DESTINATION)
    return out


def _sets(st: StepLike) -> set[str]:
    if st.WaitFor == "user" and st.ExpectedIntent in _DEST_INTENTS:
        return {INPUT_DESTINATION}
    if st.WaitFor == "operator" and st.ExpectedIntent == "TICKET":
        return {INPUT_TICKET}
    if st.WaitFor == "operator" and st.ExpectedIntent == "COUNTER":
        return {INPUT_COUNTER}
    return set()


def reachable(steps: Iterable[StepLike], start: str, end: str) -> bool:
    """시작 단계에서 다음·아니요·무응답 분기를 따라 종료 단계에 닿을 수 있는가."""
    by_code = {st.StepCode: st for st in steps}
    if start not in by_code or end not in by_code:
        return False
    seen, q = {start}, deque([start])
    while q:
        code = q.popleft()
        if code == end:
            return True
        for nxt in _edges(by_code[code]):
            if nxt in by_code and nxt not in seen:
                seen.add(nxt)
                q.append(nxt)
    return False


def required_inputs(steps: Iterable[StepLike], start: str, end: str) -> list[str]:
    """시작~종료 사이 어느 경로에서든, 정해지기 전에 쓰이는 값. 미리 넣어 줘야 한다."""
    by_code = {st.StepCode: st for st in steps}
    need: set[str] = set()
    seen: set[tuple[str, frozenset]] = set()
    q = deque([(start, frozenset())])
    while q:
        code, have = q.popleft()
        st = by_code.get(code)
        if st is None or (code, have) in seen:
            continue
        seen.add((code, have))
        need |= _uses(st) - have
        if code == end:
            continue
        have = have | _sets(st)
        for nxt in _edges(st):
            q.append((nxt, have))
    order = (INPUT_DESTINATION, INPUT_TICKET, INPUT_COUNTER)
    return [k for k in order if k in need]
