"""안내 시나리오 편집기 — 엔진 지원 목록 · 저장 전 검사 · 단계 직렬화.

설계: docs/계획보고/2026-10-19/계획보고_2026-10-19_다단계안내시나리오_편집화면_설계.md 7·8장.
편집기는 단계 전체를 한 번에 보낸다. 여기서 엔진(Jetson robot_voice/scenario.py)이 지키는 규칙대로
검사하고, 오류가 있으면 저장하지 않는다. 경고만 있으면 저장한다.

엔진 규칙 요약
    - 첫 단계 = 순서가 가장 앞인 켜진 단계. NextStepCode 가 비면 안내가 끝난다.
    - 대답(user): 알아들으면 다음, '아니요'면 OnNo(없으면 다음), 끝내 무응답이면 OnTimeout(없으면 종료).
    - 엔진이 모르는 단계 코드로 가면 aborted_bad_step 으로 끝난다 → 없는·꺼진 단계로 가는 연결은 오류.
"""

import re
from collections import deque
from types import SimpleNamespace
from typing import Iterable, Optional

from app.database import voice_segment_test as seg
from app.database.voice_scenario_defaults import (
    OPERATOR_INPUTS,
    SLOT_DEST,
    USER_INTENTS,
    WAIT_ARRIVAL,
    WAIT_NONE,
    WAIT_OPERATOR,
    WAIT_USER,
)

# ── 엔진 지원 목록 (엔진에 새 종류를 넣으면 Jetson 배포 → 여기 추가 순서) ──
WAITS = (WAIT_NONE, WAIT_USER, WAIT_OPERATOR, WAIT_ARRIVAL)
ACTIONS = ("none", "navigate")
SLOTS = (SLOT_DEST,)
PLACEHOLDERS = ("{목적지}", "{도착지}", "{도착안내}", "{대기번호}", "{창구}")
RESERVED_CODES = ("call_move",)        # 안내 시작 때 호출 위치로 가는 가상 단계
FINISH_CODE = "finish"                 # "그만" 이라고 하면 엔진이 와서 인사하고 끝내는 단계

MAX_STEPS = 200
MAX_SAY_TEXT = 400
MAX_LABEL = 40
MAX_SPOKEN_NAME = 30          # 도착 접근 안내의 부르는 이름(VoiceScenarioStep.SpokenPlaceName)
MAX_PROGRESS_TEXT = 100       # 이동 중 진행 멘트(VoiceScenarioStep.ProgressText)
MAX_VOICE_EXIT_KEYWORDS = 200  # 운영자 단계 음성 출구의 말 목록(VoiceScenarioStep.VoiceExitKeywords)
VOICE_EXIT_MIN_LEN = 2        # 한 글자 말은 아무 말에나 걸린다
TIMEOUT_RANGE = (3, 600)
RETRY_RANGE = (0, 5)
AUTO_ADVANCE_RANGE = (0, 1800)
AUTO_ADVANCE_OFF = -1
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
AUTO_CODE_PREFIX = "s_"

_INPUT_LABEL = {seg.INPUT_DESTINATION: "고객 목적지", seg.INPUT_TICKET: "대기번호", seg.INPUT_COUNTER: "창구 번호"}


def capabilities() -> dict:
    """화면이 고를 수 있는 값. 엔진이 모르는 값은 화면에서 고를 수 없게 한다."""
    return {
        "waits": list(WAITS),
        "userIntents": list(USER_INTENTS),
        "operatorInputs": list(OPERATOR_INPUTS),
        "actions": list(ACTIONS),
        "slots": list(SLOTS),
        "placeholders": list(PLACEHOLDERS),
        "reservedCodes": list(RESERVED_CODES),
        "finishCode": FINISH_CODE,
        "limits": {
            "maxSteps": MAX_STEPS, "maxSayText": MAX_SAY_TEXT, "maxLabel": MAX_LABEL,
            "maxSpokenName": MAX_SPOKEN_NAME, "maxVoiceExitKeywords": MAX_VOICE_EXIT_KEYWORDS,
            "timeout": list(TIMEOUT_RANGE), "retry": list(RETRY_RANGE),
            "autoAdvance": list(AUTO_ADVANCE_RANGE), "autoAdvanceOff": AUTO_ADVANCE_OFF,
        },
    }


# ── 직렬화: DB 행 ↔ 편집기 단계(dict) ─────────────────
STEP_KEYS = ("stepCode", "stepLabel", "sayText", "waitFor", "expectedIntent", "retryCount",
             "responseTimeoutSec", "actionType", "slotName", "targetLocationId", "nextStepCode",
             "onNoStepCode", "onTimeoutStepCode", "autoAdvanceSec", "spokenPlaceName", "progressText",
             "voiceExitKeywords", "voiceExitStepCode", "isActive")


def step_to_dict(st) -> dict:
    """DB 단계 → 편집기 형식(버전 기록에도 이 형식으로 남긴다)."""
    return {
        "stepCode": st.StepCode, "stepLabel": getattr(st, "StepLabel", None), "sayText": st.SayText,
        "waitFor": st.WaitFor, "expectedIntent": st.ExpectedIntent, "retryCount": st.RetryCount,
        "responseTimeoutSec": st.ResponseTimeoutSec, "actionType": st.ActionType,
        "slotName": st.SlotName, "targetLocationId": st.TargetLocationId,
        "nextStepCode": st.NextStepCode, "onNoStepCode": st.OnNoStepCode,
        "onTimeoutStepCode": st.OnTimeoutStepCode, "autoAdvanceSec": st.AutoAdvanceSec,
        "spokenPlaceName": getattr(st, "SpokenPlaceName", None),
        "progressText": getattr(st, "ProgressText", None),
        "voiceExitKeywords": getattr(st, "VoiceExitKeywords", None),
        "voiceExitStepCode": getattr(st, "VoiceExitStepCode", None),
        "isActive": bool(st.IsActive),
    }


def normalize(steps: list[dict]) -> list[dict]:
    """대기 종류에 맞지 않는 칸을 비우고, 코드가 없는 새 단계에 코드를 붙인다.

    기다림을 바꾸면 이전 종류에만 쓰던 칸(장소·인식 종류·갈래·자동 진행)이 남지 않게 한다 —
    남겨 두면 엔진이 엉뚱한 값으로 움직인다(예: 말만 단계에 장소가 남아 이동).
    """
    used = {s.get("stepCode") for s in steps if s.get("stepCode")}
    n = 1
    out = []
    for raw in steps:
        s = {k: raw.get(k) for k in STEP_KEYS}
        s["stepLabel"] = (s["stepLabel"] or "").strip() or None
        s["spokenPlaceName"] = (s["spokenPlaceName"] or "").strip() or None
        s["progressText"] = (s["progressText"] or "").strip() or None
        s["voiceExitKeywords"] = ", ".join(voice_exit_words(s["voiceExitKeywords"])) or None
        s["voiceExitStepCode"] = (s["voiceExitStepCode"] or "").strip() or None
        s["sayText"] = s["sayText"] if s["sayText"] is None else s["sayText"].strip()
        s["isActive"] = True if s["isActive"] is None else bool(s["isActive"])
        s["retryCount"] = 2 if s["retryCount"] is None else s["retryCount"]
        s["responseTimeoutSec"] = 10 if s["responseTimeoutSec"] is None else s["responseTimeoutSec"]
        if not s["stepCode"]:
            while f"{AUTO_CODE_PREFIX}{n:03d}" in used:
                n += 1
            s["stepCode"] = f"{AUTO_CODE_PREFIX}{n:03d}"
            used.add(s["stepCode"])
        wait = s["waitFor"]
        s["actionType"] = "navigate" if wait == WAIT_ARRIVAL else "none"
        if wait != WAIT_ARRIVAL:
            s["targetLocationId"] = s["slotName"] = s["spokenPlaceName"] = s["progressText"] = None
        elif s["slotName"]:
            s["targetLocationId"] = None      # 고객 목적지로 가면 고정 장소는 쓰지 않는다
        if wait != WAIT_USER:
            s["onNoStepCode"] = s["onTimeoutStepCode"] = None
            if wait != WAIT_OPERATOR:
                s["expectedIntent"] = None
        # 대답 단계의 '아니요' 갈래는 예/아니요가 아니어도 남긴다 — 목적지 단계에서 "없어요"·"그만"이면
        # 엔진이 이 갈래로 간다(robot_voice/scenario.py _classify_dest 의 NO)
        if wait != WAIT_OPERATOR:
            s["autoAdvanceSec"] = None
            s["voiceExitKeywords"] = s["voiceExitStepCode"] = None
        out.append(s)
    return out


# ── 검사 ──────────────────────────────────────────────
def _issue(code: Optional[str], field: Optional[str], message: str, short: Optional[str] = None) -> dict:
    """short 가 있으면 화면 단계 목록 줄에는 그것만 보이고, 전체 문장은 검사 결과 목록에 보인다."""
    out = {"stepCode": code, "field": field, "message": message}
    if short:
        out["short"] = short
    return out


def _ns(s: dict) -> SimpleNamespace:
    """voice_segment_test 가 쓰는 DB 행 모양으로."""
    return SimpleNamespace(
        StepCode=s["stepCode"], SayText=s.get("sayText"), WaitFor=s.get("waitFor"),
        ExpectedIntent=s.get("expectedIntent"), ActionType=s.get("actionType"), SlotName=s.get("slotName"),
        NextStepCode=s.get("nextStepCode"), OnNoStepCode=s.get("onNoStepCode"),
        OnTimeoutStepCode=s.get("onTimeoutStepCode"), VoiceExitStepCode=s.get("voiceExitStepCode"))


def _name(s: dict) -> str:
    return s.get("stepLabel") or s["stepCode"]


def _reach(by_code: dict, start: str) -> set[str]:
    seen, q = {start}, deque([start])
    while q:
        for nxt in seg._edges(by_code[q.popleft()]):
            if nxt in by_code and nxt not in seen:
                seen.add(nxt)
                q.append(nxt)
    return seen


def _missing_inputs(by_code: dict, start: str) -> dict[str, set[str]]:
    """단계별로, 시작에서 그 단계까지 오는 어떤 길에서든 아직 정해지지 않은 채 쓰이는 값."""
    out: dict[str, set[str]] = {}
    seen: set[tuple[str, frozenset]] = set()
    q = deque([(start, frozenset())])
    while q:
        code, have = q.popleft()
        st = by_code.get(code)
        if st is None or (code, have) in seen:
            continue
        seen.add((code, have))
        miss = seg._uses(st) - have
        if miss:
            out.setdefault(code, set()).update(miss)
        have = have | seg._sets(st)
        for nxt in seg._edges(st):
            q.append((nxt, have))
    return out


def _can_end(st) -> bool:
    """여기서 안내가 끝날 수 있는가 — 다음이 없거나, 대답 단계가 끝내 무응답이면 종료로 가거나."""
    return not st.NextStepCode or (st.WaitFor == WAIT_USER and not st.OnTimeoutStepCode)


def check(steps: list[dict], *, map_id: Optional[int], map_place_ids: Iterable[int]) -> dict:
    """편집기 단계 전체를 검사한다. normalize() 를 거친 목록을 받는다.

    map_place_ids: 시나리오 운영 맵의 장소 id — 다른 맵 장소는 좌표계가 달라 엉뚱한 곳에 선다.
    """
    errors: list[dict] = []
    warnings: list[dict] = []
    places = set(map_place_ids)

    if len(steps) > MAX_STEPS:
        errors.append(_issue(None, None, f"단계는 {MAX_STEPS}개까지입니다"))
    codes = [s["stepCode"] for s in steps]
    for c in sorted({c for c in codes if codes.count(c) > 1}):
        errors.append(_issue(c, "stepCode", f"단계 코드 '{c}' 가 겹칩니다"))
    for s in steps:
        c = s["stepCode"]
        if c in RESERVED_CODES:
            errors.append(_issue(c, "stepCode", f"'{c}' 는 쓸 수 없는 단계 코드입니다"))
        elif not CODE_RE.match(c):
            errors.append(_issue(c, "stepCode", "단계 코드는 영문 소문자로 시작하고 영문 소문자·숫자·_ 40자 이내입니다"))

    active = [s for s in steps if s["isActive"]]
    if not active:
        errors.append(_issue(None, None, "켜진 단계가 하나도 없습니다"))
        return {"errors": errors, "warnings": warnings}
    active_codes = {s["stepCode"] for s in active}
    all_codes = set(codes)

    for s in active:
        c, wait, intent = s["stepCode"], s["waitFor"], s["expectedIntent"]
        if s["stepLabel"] and len(s["stepLabel"]) > MAX_LABEL:
            errors.append(_issue(c, "stepLabel", f"표시 이름은 {MAX_LABEL}자까지입니다"))
        if s.get("spokenPlaceName") and len(s["spokenPlaceName"]) > MAX_SPOKEN_NAME:
            errors.append(_issue(c, "spokenPlaceName", f"부르는 이름은 {MAX_SPOKEN_NAME}자까지입니다"))
        if s.get("progressText") and len(s["progressText"]) > MAX_PROGRESS_TEXT:
            errors.append(_issue(c, "progressText", f"진행 멘트는 {MAX_PROGRESS_TEXT}자까지입니다"))
        say = s["sayText"] or ""
        if len(say) > MAX_SAY_TEXT:
            errors.append(_issue(c, "sayText", f"멘트는 {MAX_SAY_TEXT}자까지입니다"))
        if not say and wait != WAIT_ARRIVAL:      # 말 없이 이동하는 단계(도착 장소)는 정상
            warnings.append(_issue(c, "sayText", f"'{_name(s)}' 멘트가 비어 있습니다"))
        if wait not in WAITS:
            errors.append(_issue(c, "waitFor", f"엔진이 지원하지 않는 기다림 종류입니다: {wait}"))
            continue
        if wait == WAIT_USER:
            if intent not in USER_INTENTS:
                errors.append(_issue(c, "expectedIntent", f"'{_name(s)}' 의 인식 종류를 고르세요"))
            lo, hi = TIMEOUT_RANGE
            if not lo <= s["responseTimeoutSec"] <= hi:
                errors.append(_issue(c, "responseTimeoutSec", f"응답 대기는 {lo}~{hi}초입니다"))
            lo, hi = RETRY_RANGE
            if not lo <= s["retryCount"] <= hi:
                errors.append(_issue(c, "retryCount", f"재질문은 {lo}~{hi}회입니다"))
            if intent == "YES_NO" and not s["onNoStepCode"]:
                warnings.append(_issue(c, "onNoStepCode", f"'{_name(s)}' 에서 '아니요'라고 해도 다음 단계로 갑니다"))
        if wait == WAIT_OPERATOR:
            if intent not in OPERATOR_INPUTS:
                errors.append(_issue(c, "expectedIntent", f"'{_name(s)}' 의 입력 종류를 고르세요"))
            auto = s["autoAdvanceSec"]
            lo, hi = AUTO_ADVANCE_RANGE
            if auto is not None and auto != AUTO_ADVANCE_OFF and not lo <= auto <= hi:
                errors.append(_issue(c, "autoAdvanceSec", f"자동 진행은 {lo}~{hi}초 또는 끔입니다"))
            _check_voice_exit(s, errors, warnings)
        if wait == WAIT_ARRIVAL:
            if s["slotName"]:
                if s["slotName"] not in SLOTS:
                    errors.append(_issue(c, "slotName", f"엔진이 지원하지 않는 이동 대상입니다: {s['slotName']}"))
            elif not s["targetLocationId"]:
                errors.append(_issue(c, "targetLocationId", f"'{_name(s)}' 의 이동 장소를 고르세요"))
            elif not map_id:
                errors.append(_issue(c, "targetLocationId", "운영 맵을 먼저 지정하세요"))
            elif s["targetLocationId"] not in places:
                errors.append(_issue(c, "targetLocationId", f"'{_name(s)}' 의 장소가 운영 맵의 장소가 아닙니다"))
        for field in ("nextStepCode", "onNoStepCode", "onTimeoutStepCode", "voiceExitStepCode"):
            ref = s[field]
            if not ref:
                continue
            if ref not in all_codes:
                errors.append(_issue(c, field, f"'{_name(s)}' 이(가) 없는 단계로 이어집니다: {ref}"))
            elif ref not in active_codes:
                errors.append(_issue(c, field, f"'{_name(s)}' 이(가) 꺼진 단계로 이어집니다: {ref}"))

    by_code = {s["stepCode"]: _ns(s) for s in active}
    first = active[0]["stepCode"]
    reach = _reach(by_code, first)
    by_dict = {s["stepCode"]: s for s in active}
    for s in active:
        if s["stepCode"] not in reach:
            warnings.append(_unreached(s, steps))

    for code, miss in sorted(_missing_inputs(by_code, first).items()):
        st, s = by_code[code], by_dict[code]
        if seg.INPUT_DESTINATION in miss and st.ActionType == "navigate" and st.SlotName:
            errors.append(_issue(code, "slotName",
                                 f"'{_name(s)}' 앞에 목적지를 묻는 단계가 없는 길이 있습니다(고객 목적지로 이동 불가)"))
        say_miss = {m for m in miss if any(ph in (st.SayText or "") for ph, need in seg._PLACEHOLDERS.items() if need == m)}
        if say_miss:
            names = "·".join(_INPUT_LABEL[m] for m in (seg.INPUT_DESTINATION, seg.INPUT_TICKET, seg.INPUT_COUNTER)
                             if m in say_miss)
            warnings.append(_issue(code, "sayText", f"'{_name(s)}' 멘트의 {names} 이(가) 아직 정해지지 않아 빈칸으로 말할 수 있습니다"))

    ends = {c for c, st in by_code.items() if _can_end(st)}
    stuck = sorted(c for c in reach if not (_reach(by_code, c) & ends))
    if stuck:
        warnings.append(_issue(stuck[0], None, "끝나지 않는 고리가 있습니다(운영자가 끝낼 때까지 반복): "
                               + ", ".join(_name(by_dict[c]) for c in stuck)))
    if FINISH_CODE not in active_codes:
        warnings.append(_issue(None, None, "'종료(finish)' 단계가 없어 방문자가 '그만'이라고 하면 인사 없이 끝납니다"))
    return {"errors": errors, "warnings": warnings}


def voice_exit_words(raw: Optional[str]) -> list[str]:
    """음성 출구 말 목록 — 쉼표(，、 포함)·줄바꿈으로 나누고 앞뒤 공백·빈 칸·중복을 뺀다(순서 유지)."""
    out: list[str] = []
    for w in re.split(r"[,，、\n]", raw or ""):
        w = " ".join(w.split())
        if w and w not in out:
            out.append(w)
    return out


def _check_voice_exit(s: dict, errors: list[dict], warnings: list[dict]) -> None:
    """운영자 단계 음성 출구 — 말과 넘어갈 단계가 함께 있어야 엔진이 듣는다."""
    c, words, to = s["stepCode"], voice_exit_words(s.get("voiceExitKeywords")), s.get("voiceExitStepCode")
    if s.get("voiceExitKeywords") and len(s["voiceExitKeywords"]) > MAX_VOICE_EXIT_KEYWORDS:
        errors.append(_issue(c, "voiceExitKeywords", f"음성 출구 말은 모두 {MAX_VOICE_EXIT_KEYWORDS}자까지입니다"))
    short = [w for w in words if len(w.replace(" ", "")) < VOICE_EXIT_MIN_LEN]
    if short:
        errors.append(_issue(c, "voiceExitKeywords",
                             f"음성 출구 말은 {VOICE_EXIT_MIN_LEN}글자 이상이어야 합니다: {', '.join(short)}"))
    if words and not to:
        errors.append(_issue(c, "voiceExitStepCode", f"'{_name(s)}' 음성 출구로 넘어갈 단계를 고르세요"))
    elif to and not words:
        errors.append(_issue(c, "voiceExitKeywords", f"'{_name(s)}' 음성 출구에서 알아들을 말을 적으세요"))
    elif to and to == c:
        errors.append(_issue(c, "voiceExitStepCode", "음성 출구는 같은 단계로 이어질 수 없습니다"))


UNREACHED_SHORT = "실제 안내에서 실행 안 됨"


def _unreached(s: dict, steps: list[dict]) -> dict:
    """시작 단계에서 연결을 따라가도 닿지 않는 켜진 단계 — 왜 그런지와 고칠 곳을 함께 알린다.

    앞 단계가 켜져 있는데 닿지 않으면 그 앞 단계가 원인이라 할 일은 그쪽 경고에만 적는다.
    """
    c = s["stepCode"]
    came = [p for p in steps if p["stepCode"] != c and c in seg._edges(_ns(p))]
    live = [p for p in came if p["isActive"]]
    if live:
        why = f"앞 단계 '{_name(live[0])}' 이(가) 실행되지 않기 때문입니다."
    elif came:
        why = (f"이 단계로 넘어오는 단계 '{_name(came[0])}' 이(가) 꺼져 있습니다. "
               "그 단계를 켜거나, 다른 단계에서 연결하세요.")
    else:
        why = "이 단계로 넘어오는 단계가 없습니다. 다른 단계의 다음 단계로 연결하거나, 쓰지 않으면 끄세요."
    return _issue(c, None, f"'{_name(s)}' 은(는) 실제 안내에서 실행되지 않습니다 — {why}", short=UNREACHED_SHORT)


# ── 버전 기록 ─────────────────────────────────────────
MAX_VERSIONS = 50


def current_steps(db, sc) -> list[dict]:
    from app.database.models import VoiceScenarioStep
    rows = (db.query(VoiceScenarioStep).filter(VoiceScenarioStep.ScenarioId == sc.id)
            .order_by(VoiceScenarioStep.StepOrder, VoiceScenarioStep.id).all())
    return [step_to_dict(st) for st in rows]


def record_version(db, sc, steps: list[dict], note: Optional[str], user_id: Optional[int]) -> int:
    """지금 단계 전체를 새 버전으로 남기고 시나리오 버전을 올린다. 오래된 기록은 50개만 둔다."""
    import json
    from app.database.models import VoiceScenarioVersion as V
    sc.Version = (sc.Version or 0) + 1
    db.add(V(ScenarioId=sc.id, Version=sc.Version, StepsJson=json.dumps(steps, ensure_ascii=False),
             Note=note, CreatedBy=user_id))
    db.flush()
    old = (db.query(V.id).filter(V.ScenarioId == sc.id).order_by(V.Version.desc())
           .offset(MAX_VERSIONS).all())
    if old:
        db.query(V).filter(V.id.in_([r.id for r in old])).delete(synchronize_session=False)
    return sc.Version


def ensure_baseline(db, sc, user_id: Optional[int]) -> None:
    """편집기로 처음 고치기 전 상태를 버전 1로 남긴다 — 되돌릴 곳이 있게."""
    from app.database.models import VoiceScenarioVersion as V
    if (sc.Version or 0) == 0 and not db.query(V.id).filter(V.ScenarioId == sc.id).first():
        steps = current_steps(db, sc)
        if steps:
            record_version(db, sc, steps, "편집기로 처음 고치기 전 상태", user_id)
