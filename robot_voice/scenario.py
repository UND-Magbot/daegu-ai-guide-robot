"""시나리오 단계 엔진 — WaitFor 기반 상태 기계.

한 단계는 [발화] -> [무엇을 기다림] -> [다음 단계] 로 이뤄진다.

    none      발화 후 바로 다음 단계
    user      사용자 음성 응답 대기 (ResponseTimeoutSec, RetryCount 만큼 재질문)
    operator  관제 입력 대기 — 대기번호·창구. **이 단계만 네트워크가 필수다**
    arrival   이동 도착 대기

관제가 끊기면 `operator` 단계에서 **그 자리에 멈춰 기다린다.** 대기번호는 추측할 수 없는
값이라 건너뛰지 않는다. 다만 고객이 고장으로 오인하지 않도록 주기적으로 안내를 재생한다.

설계: 02_Backend/음성-시나리오-단계엔진-설계.md
"""

import inspect
import logging
import re
import threading
import time
from typing import Callable, Optional

import requests

from robot_voice import ai, config, follow
from robot_voice.backend import BackendClient, BackendError

log = logging.getLogger("robot_voice.scenario")

WAIT_NONE = "none"
WAIT_USER = "user"
WAIT_OPERATOR = "operator"
WAIT_ARRIVAL = "arrival"

# 호출 위치 이동 — 관제가 안내 시작 때 호출 위치를 정하면 세션이 이 단계로 열린다.
# 시나리오 단계 표에는 없고, 세션의 startLocationId 로 이동한 뒤 시나리오 첫 단계로 넘어간다.
CALL_MOVE_STEP = "call_move"

# 운영자 입력을 기다리는 동안 이만큼마다 한 번씩 안내한다.
OPERATOR_NOTICE_SEC = 45.0
OPERATOR_NOTICE = "잠시만 기다려 주세요."

# 운영자 일시 정지 — 다음 이동 단계 앞에서 한 번 말하고 재개까지 기다린다(반복 안내 없음).
# 재개하면 따로 말하지 않고 다음 단계 멘트("○○로 안내하겠습니다")로 잇는다.
HOLD_NOTICE = "잠시 안내를 멈추겠습니다. 잠시만 기다려 주세요."
HOLD_STATES = ("requested", "holding")

# 배터리 위급 중단(D10) — 관제가 세션에 중단 요청(abortRequest=battery)을 실으면 다음 단계 앞에서
# (운영자 입력·일시 정지 대기 중이면 다음 폴링에서) 한 번 말하고 끝낸다. 관제가 이 종료를 받아
# 충전소로 보낸다. 3분 안에 안 닫으면 관제가 멘트 없이 닫는다(BATTERY_ABORT_CLOSE_SEC).
ABORT_BATTERY = "battery"
BATTERY_NOTICE = "죄송합니다. 배터리가 부족해 끝까지 안내하지 못합니다. 직원에게 문의해 주세요."
BATTERY_ABORTED = ("aborted_battery", "timeout")

# 이동 전 확인에서 출발할 수 없을 때 방문자에게 하는 말 — 사유(측위 상실 등)는 운영자 화면에 남긴다
MOVE_UNAVAILABLE = "지금은 안내를 시작할 수 없어요. 가까운 직원에게 문의해 주세요."
# 엔진이 멘트 외에 직접 말하는 고정 문장 — 시작할 때 미리 합성해 둔다(main._prewarm)
# 재질문 — 못 들었을 때와 들었지만 못 알아들었을 때를 나눈다.
#   못 들음: 질문 자체를 못 들었을 수 있다 — 알린 뒤 질문을 다시 한다(두 멘트로 나눠 각각 캐시를 쓴다)
#   못 알아들음: 질문은 들었다 — 같은 질문을 되풀이하지 않고 다시 말해 달라고 한다
# 짧게, 할 일을 앞에 둔다. 사과("죄송해요")는 되풀이되면 계속 사과하는 것처럼 들려 넣지 않는다.
# 질문형("말씀해 주시겠어요?")은 "네" 로만 답할 수 있어 요청형으로 쓴다.
REASK_NOT_HEARD = "잘 못 들었어요."
REASK_NOT_UNDERSTOOD = "다시 한 번 말씀해 주세요."
ASK_YES_NO = "'네' 또는 '아니요'로 말씀해 주세요."
# 질문이 대답 말을 따옴표로 정해 두면('준비됐어요'라고 말씀해 주세요) 판정 실패 때 그 말로 되묻는다 —
# "네/아니요로 말씀해 주세요" 는 '준비됐어요'를 기다리는 단계에서 엉뚱하다(11-03 memo, 세션 550 ready_check).
ANSWER_QUOTE_RE = re.compile(r"'([^'\n]{2,15})'\s*(?:이)?라고")
READY_REASK = "잘 못 들었어요. '준비됐어요'라고 말씀해 주세요."
ASK_OTHER_PLACE = "다른 곳을 말씀해 주세요."
ASK_FORM_DONE = "작성이 끝나시면 '작성 완료'라고 말씀해 주세요."
# 신청서 작성 대기 반응(10-30 사용자 요청: 못 알아들었을 때·시간이 지났을 때 반응이 없었다).
# 도착 멘트는 되풀이하지 않는다(10-08 실기: 도착 멘트 세 번). 못 알아들음·완료가 아닌 말은 한 단계에서 FORM_REASK_MAX 번까지.
FORM_REASK = "잘 못 들었어요. 작성이 끝나시면 '작성 완료'라고 말씀해 주세요."
FORM_WAIT_NOTICE = "천천히 작성하세요. 다 쓰시면 '작성 완료'라고 말씀해 주세요."
FORM_REASK_MAX = 2
# 경유지 방향 안내 — 관제(navigation/turn_cues.py)가 고르는 문구. 미리 합성해 둔다.
# 관제와 글자 하나까지 같아야 미리 합성한 음성을 쓴다(다르면 그 자리에서 합성해 1~2초 늦다).
TURN_CUE_TURNS = ("잠시 후 왼쪽으로 방향을 바꾸겠습니다.", "잠시 후 오른쪽으로 방향을 바꾸겠습니다.",
                  "잠시 후 뒤로 방향을 바꾸겠습니다.")
ARRIVE_SOON = "곧 도착합니다."


def arrive_soon_phrase(name: Optional[str]) -> str:
    """도착 접근 안내 — 단계의 부르는 이름이 있으면 "곧 원무과에 도착합니다."(관제 turn_cues.arrive_soon_phrase)."""
    name = (name or "").strip()
    return f"곧 {name}에 도착합니다." if name else ARRIVE_SOON


def turn_cue_phrases(names=()) -> tuple:
    """관제가 낼 수 있는 방향·도착 안내 전부. names: 시나리오 단계의 부르는 이름들."""
    soons = [ARRIVE_SOON] + [arrive_soon_phrase(n) for n in sorted({(n or "").strip() for n in names} - {""})]
    return tuple(TURN_CUE_TURNS) + tuple(f"{t} {s}" for s in soons for t in TURN_CUE_TURNS) + tuple(soons)


TURN_CUE_PHRASES = turn_cue_phrases()
# 출발 제자리 회전 직전 안내 — 관제 이동 전 확인(move/check)의 departTurn 과 글자까지 같다(turn_cues.DEPART_TURN_PHRASES)
DEPART_TURN_PHRASES = ("왼쪽으로 방향을 바꾸겠습니다.", "오른쪽으로 방향을 바꾸겠습니다.", "뒤로 방향을 바꾸겠습니다.")
# 안내 중 장애물 정지 멘트 — 관제(navigation/obstacle_hold.py)가 /robot/nav notice 로 싣는 문장. 글자까지 같아야 미리 합성한 음성을 쓴다.
# 시연 대본(19~21)과 같은 합쇼체. OBSTACLE_ASK 를 말한 뒤에는 대답을 듣고 관제에 보낸다(_obstacle_ask_tick).
OBSTACLE_ASK = "이제 다시 이동할 수 있습니다. 준비되시면 '준비됐어요'라고 말씀해 주세요."
OBSTACLE_PHRASES = ("앞쪽에 장애물이 있어 잠시 멈추겠습니다. 그 자리에서 기다려 주세요.", "다시 출발하겠습니다.",
                    "아직 앞이 막혀 있습니다. 조금 더 기다리겠습니다.", OBSTACLE_ASK,
                    "직원을 불렀습니다. 잠시만 기다려 주세요.")
OBSTACLE_LISTEN_SEC = 6.0    # 질문 뒤 대답을 기다리는 시간 — 동행 확인(follow.LISTEN_SEC)과 같다
# 방향·도착·진행 안내를 말하는 동안 장애물 멘트가 왔는지 보는 간격 — 오면 안내를 끊고 장애물 멘트를 바로 말한다(10-30 B).
# 멈춘 뒤 '곧 도착합니다' 를 다 말하고서야 장애물 멘트가 나와 멈춘 뒤 3.8초에 들렸다(10-30 16:00 관공서 로비).
NOTICE_PEEK_SEC = 0.3
FIXED_PHRASES = (OPERATOR_NOTICE, HOLD_NOTICE, BATTERY_NOTICE, ASK_YES_NO, READY_REASK, REASK_NOT_HEARD, REASK_NOT_UNDERSTOOD, ASK_OTHER_PLACE, ASK_FORM_DONE, FORM_REASK, FORM_WAIT_NOTICE, MOVE_UNAVAILABLE,
                 "다시 말씀해 주시겠어요?", "어디로 안내해 드릴까요?", "이동이 중단됐어요.",
                 *TURN_CUE_PHRASES, *DEPART_TURN_PHRASES, *OBSTACLE_PHRASES, *follow.FIXED_PHRASES)

YES_WORDS = ("네", "예", "응", "맞아", "맞아요", "그래", "좋아", "부탁", "해줘", "맞습니다")
NO_WORDS = ("아니", "아니요", "아뇨", "안돼", "싫어", "다른", "아닙니다", "없어", "없습니다", "없음")
# '없' 이 들어가도 긍정인 말 — NO 보다 먼저 본다("창구로 안내할까요?" "문제없어요")
# 다시 말해 달라는 말 — 예/아니요로 보지 않는다. '해줘'(YES_WORDS) 때문에 "다시 말해줘" 가 '준비됐어요' 단계를
# 예로 넘겼다(11-03 세션 553 ready_check). 질문을 그대로 다시 들려준다.
REPEAT_RE = re.compile(r"다시(?:한번|한번만)?(?:말|얘기|이야기|들려|알려)|뭐라고|뭐라구|못들었|안들려|잘안들|한번더말")
YES_DESPITE_NO = ("문제없", "상관없")
CANCEL_WORDS = ("취소", "그만", "멈춰", "정지", "스톱")
# 예/아니요 대답 보강(2026-10-08 실측 · 시뮬레이션 문장 32개 오판 9 → 0)
# STT 가 "네" 를 이렇게 받아쓴다 — **그 말 하나일 때만** 예로 본다("내일 올게요" 는 아니다)
YES_EXACT = ("뇌", "내", "넵", "녜", "넹", "네네", "예예", "넵넵")
# 확인 질문에 흔한 긍정 대답
YES_PHRASES = ("안내해", "해주세요", "해줘요", "가주세요", "좋아요", "그래요")
# 부정 — 위 긍정 표현보다 먼저 본다("안 해주셔도 돼요" 의 '해주' 를 예로 잡지 않게).
# "괜찮아요" 는 뜻이 갈려 넣지 않는다(판정 안 함 → 재질문).
NO_PHRASES = ("안해", "안가", "안갈", "안할", "못가", "필요없", "됐어요", "됐습니다")
# 이 말로 시작했는데 아니요 낱말이 섞이면 판정하지 않고 다시 묻는다. 시끄러울 때 "네 안내해 주세요" 를
# "네, 안 돼 해주세요" 로 받아써 아니요로 뒤집은 일이 있었다(병원 시험 64건 중 2건) — 못 알아듣는
# 것보다 반대로 알아듣는 것이 위험하다.
YES_LEADS = ("네", "예", "넵", "맞아")
# "준비됐어요" — 출발 준비 확인의 대답. '됐어요'(NO_PHRASES)가 들어 있어 아니요보다 먼저 본다.
# 준비와 됐 사이에는 조사·'다'만 허용한다 — "준비 안 됐어요"·"준비가 덜 됐어요" 는 걸리지 않는다.
# STT 가 '됐' 을 '됬'·'되었'·'됏' 으로 받아쓰는 것도 받는다.
READY_RE = re.compile(r"준비(?:가|는|이|다|가다|는다)?(?:됐|됬|됏|되었|돼|되써|돼써|완료|끝)")
# 출발 재촉 — **그 말 하나일 때만** 예로 본다("집에 가요" 는 판정 안 함)
GO_EXACT = ("가요", "가자", "가죠", "갈게요", "가볼게요", "출발", "출발해요", "출발하세요", "출발해주세요")

# 목적지 질문에서 '다른' 은 거절이 아니다 — "다른 곳도 안내해 주세요"
DEST_NO_WORDS = tuple(w for w in NO_WORDS if w != "다른")
# 목적지 질문의 '아니요' 분기(출구 안내 등)로 보낼 말 — "출구요", "집에 갈래요"
EXIT_WORDS = ("출구", "나갈", "나가", "집에")
# "관공서 말고 그냥 나갈게요" — 이 말 앞에 나온 목적지는 고르지 않는다
NEGATION_MARK = "말고"
# 취소어("그만할게요")가 들리면 작별 멘트를 하고 끝낸다 — 말없이 멈추면 고장으로 안다
FAREWELL_STEP = "finish"
ASK_WHERE = "어디로 안내해 드릴까요?"

# 질문이 들리지 않으면 대답을 받을 수 없다 — 운영자 알림과 함께 안내를 끝낸다
# 이동 요청 응답이 늦어 끊겼을 때 관제가 출발시켰는지 지켜보는 시간
MOVE_START_CONFIRM_SEC = 5.0
TTS_FAILED_RESULT = ("aborted_tts", "voice")
TTS_FAILED_REASON = "안내 음성이 나오지 않아 질문을 전달하지 못했습니다. 로봇 스피커·TTS 를 확인하세요."

# 운영자가 관제에서 안내를 종료했다. 관제가 이미 세션을 닫았으므로 종료를 다시 보고하지 않는다.
OPERATOR_CLOSED = ("aborted_operator", "operator")
# 관공서 6.5 — "큐리야, 작성 완료". 호출어는 WakeGate 가 떼어내고 본문만 온다.
FORM_DONE_WORDS = ("완료", "다했", "다썼", "작성했", "끝났", "끝냈")

# 목적지를 묻는 두 가지 방식
#   PURPOSE      진료 목적·민원 종류 — 목적지 사전(진료과·민원)에서만 고른다
#   DESTINATION  추가 목적지 — 사전 + 운영 맵의 모든 장소(화장실·로비 …)에서 고른다
DEST_INTENTS = ("PURPOSE", "DESTINATION")
# 층 이동 목적지를 알아보는 장소 이름 — 관제 도착 시드(status.py)도 엘리베이터 장소를 이름으로 찾는다
FLOOR_LINK_WORD = "엘리베이터"
# LLM 판정을 믿는 최소 확신도. 이보다 낮으면 되묻는다 — 엉뚱한 곳으로 데려가는 것보다 낫다.
PICK_MIN_CONFIDENCE = 0.5
# 목적지 질문에서 '가겠다는 말만 함'(네·있어요·부탁해요)으로 볼 대답의 최대 길이(문장부호·공백 뺀 글자 수).
# 이보다 길면 증상·사유를 말한 것으로 보고 LLM 판정으로 보낸다.
ASK_MAX_LEN = 5
# 이름만 말한 대답에서 이 말이 이름 밖에 있으면 LLM 으로 문장 뜻을 본다("정형외과 말고요", "안과 아니에요").
NEGATION_WORDS = ("아니", "말고", "싫", "안가", "없", "빼고")


def _norm(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text or "")


# 음성 출구를 막는 부정 — 목적지 부정어에 "안 갈래요"·"안 돌아가요"·"괜찮아요" 를 더한다(띄어쓰기 뺀 형태)
VOICE_EXIT_NEGATIONS = NEGATION_WORDS + ("안갈", "안돌아", "괜찮")


def voice_exit_match(heard: Optional[str], words) -> Optional[str]:
    """운영자 대기 중 방문자 말이 음성 출구 말 중 하나를 담고 있으면 그 말. 띄어쓰기·문장부호는 무시한다.

    부정("대기 자리 안 갈래요", "돌아가지 말고")이 있으면 넘어가지 않는다 — 운영자 입력을 계속 기다린다.
    """
    said = _norm(heard)
    if not said:
        return None
    for w in words or ():
        key = _norm(w)
        if key and key in said:
            rest = said.replace(key, " ")
            if any(n in rest for n in VOICE_EXIT_NEGATIONS):
                return None
            return w
    return None


def slots_for_map(slots: list[dict], map_id: Optional[int], floor_id: Optional[int] = None) -> list[dict]:
    """시나리오 운영 맵에서 쓸 목적지. 각 목적지의 locationId 를 그 맵의 장소로 바꿔 돌려준다.

    병원(1F)과 관공서(3F)의 목적지 사전은 한 로봇에 같이 들어 있다. 운영 맵에 장소가
    없는 목적지는 뺀다 — 연결이 빠진 목적지가 모든 층에서 후보가 되면 병원에서
    "신청서" 가 잡히고, 다른 맵 좌표로 가면 엉뚱한 곳에 선다.
    맵별 연결(locations)이 없는 예전 번들은 층으로만 가른다.
    """
    out = []
    for sl in slots:
        locs = sl.get("locations")
        if locs is None:
            if floor_id is None or sl.get("floorId") in (None, floor_id):
                out.append(sl)
            continue
        if map_id is None:
            out.append(sl)
            continue
        here = next((lo for lo in locs if lo.get("mapId") == map_id), None)
        if here:
            out.append({**sl, "locationId": here["locationId"], "x": here.get("x"), "y": here.get("y"),
                        "yaw": here.get("yaw"), "floorId": here.get("floorId"), "mapId": map_id})
    return out


def josa(word: str, with_batchim: str, without: str) -> str:
    """받침에 따라 조사를 고른다. josa("정형외과", "은", "는") -> "정형외과는"."""
    last = (word or "")[-1:]
    if "가" <= last <= "힣":
        return word + (with_batchim if (ord(last) - 0xAC00) % 28 else without)
    return word + with_batchim


def josa_ro(word: str) -> str:
    """'로/으로' 를 붙인다. ㄹ 받침은 '로' 다 — 진료실로, 주차장으로, 관공서로."""
    if not word:
        return ""
    last = word[-1]
    if "가" <= last <= "힣":
        jong = (ord(last) - 0xAC00) % 28
        return word + ("로" if jong in (0, 8) else "으로")
    return word + "로"


def yes_no_reask(say_text) -> str:
    """예/아니요 판정 실패 때 되묻는 말. 질문이 대답을 따옴표로 정해 뒀으면('준비됐어요'라고) 그 말로, 아니면 네/아니요로."""
    m = ANSWER_QUOTE_RE.search(say_text or "")
    if not m or m.group(1).strip() in ("네", "예", "아니요", "아니오"):
        return ASK_YES_NO
    word = m.group(1).strip()
    return READY_REASK if word == "준비됐어요" else f"잘 못 들었어요. '{word}'라고 말씀해 주세요."


def classify_yes_no(text: str) -> Optional[str]:
    """YES/NO 를 키워드로 먼저 가른다 — LLM 왕복 없이 즉답하고 오프라인에서도 된다."""
    t = re.sub(r"[^0-9A-Za-z가-힣]", "", text)
    if any(w in t for w in CANCEL_WORDS):
        return "CANCEL"
    if REPEAT_RE.search(t) and not READY_RE.search(t):
        return None                      # 다시 말해 달라는 말 — 예로 보지 않는다
    if any(w in t for w in YES_DESPITE_NO) or READY_RE.search(t):
        return "YES"
    if any(w in t for w in (*NO_WORDS, *NO_PHRASES)):
        return None if t.startswith(YES_LEADS) else "NO"
    if any(w in t for w in YES_WORDS):
        return "YES"
    if t in YES_EXACT or t in GO_EXACT or any(w in t for w in YES_PHRASES):
        return "YES"
    return None


def _accepts_kw(fn: Callable, name: str) -> bool:
    """fn 이 키워드 인자 name 을 받는가(**kwargs 포함). 알 수 없으면 False."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is p.VAR_KEYWORD for p in params)


# {증상} — 진료 목적 대답에서 방문자가 말한 아픈 곳("허리가 아파서 왔어요" → 허리). 시연 대본 05(2026-10-19)
# "허리가 불편하시군요. 정형외과로 안내해 드릴까요?". 목적지 사전 동의어는 부위가 아닌 말(진료실·침침·다래끼)이
# 섞여 있어 그대로 쓰지 않고, 이 목록의 부위가 대답에 조사와 함께 나올 때만 쓴다. 한 글자 부위는 뒤에 조사가
# 있어야 한다("목이" 는 되고 "목적" 은 안 됨). 치아는 '이' 가 조사와 같아 '이빨·치아' 로만 본다.
BODY_PARTS = ("허리", "무릎", "어깨", "목", "손목", "발목", "팔", "팔꿈치", "다리", "허벅지", "종아리", "엉덩이",
              "골반", "등", "발", "발바닥", "손", "손가락", "발가락", "관절", "뼈", "척추", "꼬리뼈",
              "눈", "귀", "코", "머리", "배", "가슴", "피부", "치아", "이빨", "잇몸", "얼굴", "턱")
_BODY_AFTER = r"(?=이|가|은|는|도|을|를|쪽|부분|랑|하고|만|이랑|\s|[,.?!]|$)"


def _body_parts_in(heard: str) -> list[str]:
    """대답에 나온 몸 부위(나온 순서, 겹치면 긴 것 — "손목" 안의 "목" 은 뺀다)."""
    spans = []
    for w in BODY_PARTS:
        for m in re.finditer(re.escape(w) + _BODY_AFTER, heard or ""):
            spans.append((m.start(), m.start() + len(w), w))
    spans = [s for s in spans if not any(o != s and o[0] <= s[0] and s[1] <= o[1] for o in spans)]
    return list(dict.fromkeys(w for _, _, w in sorted(spans)))


def symptom_word(heard: str, dest: dict, cands: list[dict]) -> Optional[str]:
    """{증상} 에 넣을 부위. 하나만 말했으면 그것, 여럿이면 고른 목적지 동의어에 있는 하나, 아니면 None.

    다른 목적지의 동의어인 부위 하나만 나왔으면(부정 "눈은 아니고 …" 등) 쓰지 않는다 — 엉뚱한 부위를 되풀이하면 안 된다.
    """
    found = _body_parts_in(heard)
    if not found:
        return None
    own = {_norm(w) for w in (dest.get("name"), *dest.get("synonyms", [])) if w}
    if len(found) == 1:
        others = {_norm(w) for c in cands if c is not dest and c.get("name") != dest.get("name")
                  for w in (c.get("name"), *c.get("synonyms", [])) if w}
        w = found[0]
        return None if (_norm(w) in others and _norm(w) not in own) else w
    hit = [w for w in found if _norm(w) in own]
    return hit[0] if len(hit) == 1 else None


def _distance_phrase(meters) -> str:
    """{거리} — "약 9미터". 모르면 빈칸."""
    if meters is None:
        return ""
    return f"약 {max(1, int(round(float(meters))))}미터"


class _EarlyMove:
    """이동 단계의 출발 멘트와 이동 요청을 겹친다.

    멘트를 다 말한 뒤 이동을 요청하면 보행 전환(약 3초) 동안 로봇이 제자리에 서 있다. 재생 직전에
    음성 길이를 받아(schedule) 끝나기 MOVE_LEAD_SEC 전에 다른 스레드에서 요청하고, 멘트가 끝나면
    finish 로 결과를 받는다. 길이를 못 받았으면(멘트 없음·합성 실패) finish 가 그때 요청한다 — 예전 동작.
    요청은 어느 쪽에서든 한 번만 나간다.
    """

    def __init__(self, runner: "ScenarioRunner", step: dict) -> None:
        self._runner = runner
        self._step = step
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._started = False
        self._cancelled = False
        self._done = threading.Event()
        self._failure: Optional[str] = None

    def schedule(self, speech_sec: float) -> None:
        """재생 직전에 불린다(발화 함수의 on_start)."""
        if config.MOVE_LEAD_SEC <= 0:
            return
        # 내비 보행을 유지해 서 있으면 준비 대기·보행 전환 없이 바로 출발한다(약 1초 빠르다) — 같은 시간을
        # 앞당기면 멘트 도중에 출발한다(2026-10-11 실기: 간호실 → 경유지_02 에서 제자리 회전이 멘트 중에 시작)
        fast = bool(self._runner._move_info.get("fastStart"))
        lead = config.MOVE_LEAD_FAST_SEC if fast else config.MOVE_LEAD_SEC
        self._arm(speech_sec, lead, " (보행 유지 — 바로 출발)" if fast else "")

    def schedule_turn(self, speech_sec: float) -> None:
        """출발 회전 안내의 on_start — 안내가 끝날 즈음 돌기 시작하게 MOVE_LEAD_SEC 앞서 요청한다.
        이 문장은 회전 자체를 알리는 말이라 끝나기 직전에 돌기 시작해도 된다(보행 유지면 요청→출발 약 1초)."""
        if config.MOVE_LEAD_SEC <= 0:
            return
        self._arm(speech_sec, config.MOVE_LEAD_SEC, " (출발 회전 안내)")

    def _arm(self, speech_sec: float, lead: float, note: str) -> None:
        with self._lock:
            if self._timer or self._started or self._cancelled:
                return      # 재생을 다시 시도하며 또 불려도 한 번만 예약한다
            delay = max(0.0, speech_sec - lead)
            self._timer = threading.Timer(delay, self._fire)
            self._timer.daemon = True
            self._timer.start()
        log.info("[%s] 출발 멘트 %.1f초 — %.1f초 뒤 이동 요청%s", self._step.get("stepCode"), speech_sec, delay, note)

    def _claim(self) -> bool:
        with self._lock:
            if self._started or self._cancelled:
                return False
            self._started = True
            return True

    def _send(self) -> None:
        try:
            self._failure = self._runner._send_move(self._step)
        except Exception:  # noqa: BLE001 — 타이머 스레드에서 죽으면 finish 가 영원히 기다린다
            log.exception("[%s] 이동 요청 중 오류", self._step.get("stepCode"))
            self._failure = "이동 명령을 보내지 못했어요."
        finally:
            self._done.set()

    def _fire(self) -> None:
        # 멘트 도중 안내가 종료됐으면 보내지 않는다 — 실행부가 종료를 보고 cancel 한다
        if self._runner.closed_by_operator() or not self._claim():
            return
        self._send()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._timer:
                self._timer.cancel()

    def finish(self) -> Optional[str]:
        """멘트가 끝난 뒤 부른다. 이동 요청이 성공이면 None, 실패면 방문자에게 할 말."""
        if self._timer:
            self._timer.cancel()      # 아직 안 울렸으면(멘트가 예상보다 일찍 끝남) 지금 보낸다
        if self._claim():
            self._send()
        self._done.wait()
        return self._failure


class ScenarioRunner:
    """시나리오 한 편을 끝까지 돌린다.

    Args:
        backend: 관제 클라이언트
        speak: 발화 함수 (문자열 -> None)
        listen: 발화 수신 함수 (timeout -> 텍스트 or None)
    """

    def __init__(self, backend: BackendClient,
                 speak: Callable[[str], None],
                 listen: Callable[[float], Optional[str]]) -> None:
        self.backend = backend
        self._speak_fn = speak
        self.listen = listen
        # 듣기 함수(main._listen_for_step)와 주고받는 값 — 못 알아들은 말소리에서 멈추고 되묻기
        self.stop_on_garbled = False
        self.last_garbled = False
        self._form_reasks = 0           # 신청서 작성 대기에서 짧게 되물은 횟수(_wait_user 가 단계마다 0 으로)
        # 지금 하는 발화가 질문인가 — 실패 보고에 싣는다(질문이 안 들리면 안내가 진행되지 않는다)
        self._speaking_critical = False
        # 세션을 닫을 때 남길 사유(이동 전 확인 실패·발화 실패). 없으면 관제가 정한다.
        self._close_reason: Optional[str] = None
        # 이번 이동 단계의 이동 전 확인 결과 {ok, reason, distanceM, fastStart} — {거리}·출발 시점에 쓴다
        self._move_info: dict = {}
        # {증상} — 진료 목적 대답에서 고른 아픈 곳(symptom_word). 목적지를 새로 받을 때마다 바뀐다
        self._symptom: Optional[str] = None
        # 마지막으로 말한(또는 출발 전 기준으로 잡은) 방향 안내 번호 — /robot/nav 의 cue.seq
        self._cue_seq: Optional[int] = None
        self._notice_seq: Optional[int] = None
        self._ask_done: Optional[int] = None    # 대답을 보낸 장애물 질문 id
        self._peek_at = 0.0                       # 안내 중 장애물 멘트를 마지막으로 확인한 시각
        self._peeked: Optional[dict] = None       # 안내를 끊게 한 /robot/nav 상태
        self._cut_cue: Optional[str] = None       # 장애물로 끊긴 안내 — 다시 걷기 시작하면 한 번 다시 말한다
        self.session: Optional[dict] = None
        self.steps: dict[str, dict] = {}
        self.first_step: Optional[str] = None
        self.slots: list[dict] = []
        self.places: list[dict] = []
        # 이동 중 동행 확인 계획(번들 followCheck). None 이면 묻지 않는다
        self.follow_plan: Optional[dict] = None
        # 경유지 방향 안내를 말할지(번들 turnCues, 시나리오별). 예전 관제는 값이 없어 켠 것으로 본다
        self.turn_cues = True
        self._nav_place_id: Optional[int] = None
        self._stop = False
        # 고객이 '그만' 해서 작별 단계로 갔다 — 종료 결과를 completed 가 아닌 aborted_user 로 남긴다
        self._cancelled = False
        # 운영자 종료 감지. 단계 도중(대답·도착·재생 대기)에도 끊으려고 감시 스레드가 세운다.
        # 감시 스레드는 신호만 세운다 — 재생·녹음은 그 일을 하는 스레드가 신호를 보고 스스로
        # 멈춘다. 감시 스레드에서 sd.stop() 을 불렀다가 sd.wait() 와 교착된 적이 있다.
        self._closed = threading.Event()

    # ── 발화 ──────────────────────────────────────────
    @property
    def speak(self) -> Callable[[str], bool]:
        """발화 함수. 실행부(main)가 종료 감지를 붙인 함수로 바꿔 끼우므로 대입해도 실패 감지가 남게 감싼다."""
        return self._speak_checked

    @speak.setter
    def speak(self, fn: Callable[[str], Optional[bool]]) -> None:
        self._speak_fn = fn

    def _speak_checked(self, text: str, on_start: Optional[Callable[[float], None]] = None,
                       interrupt: Optional[Callable[[], bool]] = None) -> bool:
        """말한다. 못 말했으면 관제에 알리고 False. 발화 함수가 결과를 안 돌려주면(None) 말한 것으로 본다.

        on_start: 재생 직전에 음성 길이(초)를 받는다. 발화 함수가 이를 받지 않으면 넘기지 않는다
        (그러면 이동 단계는 예전처럼 멘트가 끝난 뒤 출발한다).
        """
        kw = {}
        if on_start and _accepts_kw(self._speak_fn, "on_start"):
            kw["on_start"] = on_start
        if interrupt and _accepts_kw(self._speak_fn, "interrupt"):
            kw["interrupt"] = interrupt      # 방향 안내만 — 장애물 멘트가 오면 끊는다(_speak_cue)
        ok = self._speak_fn(text, **kw) is not False
        if not ok:
            log.error("발화 실패%s: %s", "(질문)" if self._speaking_critical else "", text)
            if self.session:
                self.backend.report_tts_failed(self.session["id"], text, self._speaking_critical)
        return ok

    def _say_step(self, text: str, critical: bool,
                  on_start: Optional[Callable[[float], None]] = None) -> bool:
        self._speaking_critical = critical
        try:
            return self._speak_checked(text, on_start=on_start)
        finally:
            self._speaking_critical = False

    # ── 준비 ──────────────────────────────────────────
    def load(self, scenario_code: str) -> bool:
        # 운영자가 방금 멘트·장소를 고치고 안내를 시작했을 수 있다 — 새로 받는다.
        # 관제가 끊겨 있으면 bundle() 이 캐시로 떨어진다.
        try:
            self.backend.bundle(force=True)
        except BackendError as e:
            log.warning("번들 갱신 실패: %s", e)
        sc = self.backend.scenario(scenario_code)
        if not sc:
            log.error("시나리오 '%s' 를 번들에서 찾지 못했다", scenario_code)
            return False
        self.apply(sc)
        if not self.steps:
            log.error("시나리오 '%s' 에 단계가 없다", scenario_code)
            return False
        log.info("시나리오 로드: %s (%d단계, 목적지 %d개, 맵 장소 %d개)",
                 sc.get("displayName"), len(self.steps), len(self.slots), len(self.places))
        return True

    def stop(self) -> None:
        self._stop = True

    def closed_by_operator(self) -> bool:
        """관제에서 이 세션이 닫혔는가. listen·wait_arrival 의 중단 조건으로 넘긴다."""
        return self._closed.is_set()

    def _watch_session(self, done: threading.Event) -> None:
        """세션이 관제에서 닫혔는지 주기적으로 본다.

        단계 사이에서만 확인하면 대답 대기(재질문 포함 수십 초~분)나 도착 대기(최대 300초)
        동안 종료 버튼이 먹지 않고, 그 사이 열린 다음 세션이 뒤에 쌓인다.
        폴링이 실패하면(관제 두절) 판단을 미룬다 — 오프라인에서도 안내는 계속 돈다.
        """
        # requests.Session 을 메인 흐름과 나눠 쓰지 않도록 감시 전용 클라이언트를 둔다
        watcher = BackendClient() if isinstance(self.backend, BackendClient) else self.backend
        sid = self.session["id"]
        while not done.wait(config.SESSION_POLL_SEC):
            try:
                cur = watcher.current_session()
            except BackendError:
                continue
            if cur and cur.get("id") == sid:
                continue
            log.info("세션 #%s 이 관제에서 종료됐다 — 진행 중인 단계를 끊는다", sid)
            self._closed.set()
            return

    def apply(self, sc: dict) -> None:
        """번들의 시나리오 한 편을 붙인다(단계·운영 맵 목적지·장소). 미리 합성(main._prewarm)도 이것을 쓴다."""
        self.steps = {st["stepCode"]: st for st in sc.get("steps", [])}
        # 번들 단계는 StepOrder 순이다 — 호출 위치 이동 뒤에 이어 갈 첫 단계
        self.first_step = next(iter(self.steps), None)
        self.slots = slots_for_map(self.backend.slots(), sc.get("mapId"), sc.get("floorId"))
        # 운영 맵의 장소 — 추가 목적지(DESTINATION)에서 사전에 없는 곳(화장실 등)도 고르게 한다
        self.places = sc.get("places") or []
        self.follow_plan = sc.get("followCheck")
        self.turn_cues = sc.get("turnCues", True) is not False

    def filled_texts(self, ticket: str, counter: str) -> list[str]:
        """치환자가 든 멘트를 목적지(사전 목적지·운영 맵 장소)마다 채운 문장 — 미리 합성용.

        대기번호·창구는 고정값으로 채운다. {거리} 는 갈 때마다 달라 그 문장을 뺀 꼴만 나온다.
        """
        dests = [(sl.get("value"), sl.get("locationId")) for sl in self.slots]
        dests += [(p.get("name"), p.get("id")) for p in self.places]
        saved, saved_sym, out = self.session, self._symptom, []
        syms = {sl.get("value"): [w for w in sl.get("synonyms", []) if w in BODY_PARTS] for sl in self.slots}
        try:
            for st in self.steps.values():
                text = st.get("sayText") or ""
                if "{" not in text:
                    continue
                for name, loc in dict.fromkeys(d for d in dests if d[0]):
                    self.session = {"destinationName": name, "destinationId": loc,
                                    "ticketNo": ticket, "counterNo": counter}
                    # {증상} 은 그 목적지 동의어에 든 부위마다(허리·뼈 → 정형외과) 채운다 — 없으면 그 문장을 뺀 꼴
                    for sym in [None] + [w for w in syms.get(name, []) if "{증상}" in text]:
                        self._symptom = sym
                        out.append(self._fill(text))
        finally:
            self.session, self._symptom = saved, saved_sym
        return out

    # ── 치환 ──────────────────────────────────────────
    def _fill(self, text: Optional[str]) -> str:
        """치환자를 세션 값으로 채운다.

        {목적지}   고객이 말해 확정한 목적지 이름("3층 관공서")
        {도착지}   실제로 도착하는 장소 이름("엘리베이터 입구") — 목적지와 같으면 목적지 이름
        {도착안내} 목적지 사전의 도착 문구("엘리베이터를 타고 3층으로 이동해 주세요."), 없으면 빈칸
        {대기번호} {창구}
        {증상}     진료 목적 대답에서 말한 아픈 곳("허리") — 모르면 그 문장을 뺀다. {증상}이/가 는 받침에 맞춘다
        {거리}     지금 위치에서 이동할 장소까지 경유지를 따라간 거리("약 9미터") — 이동 단계만.
                   모르면 그 문장을 뺀다
        {목적지}로 / {도착지}로 는 받침에 맞춰 '로/으로' 를 고른다.
        """
        if not text:
            return ""
        s = self.session or {}
        dest = s.get("destinationName") or ""
        table = {
            "목적지": dest,
            "도착지": self._arrival_name(dest, s.get("destinationId")),
            "도착안내": self._arrival_text(dest),
            "대기번호": s.get("ticketNo") or "",
            "창구": s.get("counterNo") or "",
            "거리": _distance_phrase(self._move_info.get("distanceM")),
            "증상": self._symptom or "",
        }
        if not table["증상"]:
            text = re.sub(r"[^.?!]*\{증상\}[^.?!]*[.?!]?", "", text)
        text = re.sub(r"\{증상\}(?:이|가)", lambda m: josa(table["증상"], "이", "가"), text)
        if not table["거리"]:
            # 거리를 모르면(근처·위치 미확인·관제 응답 없음) 그 문장을 통째로 뺀다
            text = re.sub(r"[^.?!]*\{거리\}[^.?!]*[.?!]?", "", text)
        text = re.sub(r"\{(목적지|도착지)\}(?:으로|로)",
                      lambda m: josa_ro(table[m.group(1)]), text)
        for k, v in table.items():
            text = text.replace("{" + k + "}", v)
        return re.sub(r"\s+", " ", text).strip()

    def _arrival_name(self, dest: str, place_id: Optional[int]) -> str:
        """도착 장소 이름. 목적지 사전에 도착지 음성명이 있으면 그것,
        목적지 이름과 같은 곳(진료실 ↔ 진료실_)이면 목적지 이름으로 부른다."""
        slot = next((sl for sl in self.slots if sl.get("value") == dest), None)
        spoken = ((slot or {}).get("spokenName") or "").strip()
        if spoken:
            return spoken
        place = next((p.get("name") for p in self.places if p.get("id") == place_id), None)
        if not place or _norm(place) == _norm(dest):
            return dest
        return place

    def _arrival_text(self, dest: str) -> str:
        slot = next((sl for sl in self.slots if sl.get("value") == dest), None)
        return ((slot or {}).get("arrivalText") or "").strip()

    # ── 목적지 판정 (Jetson 이 한다) ──────────────────
    def dest_slots(self, expected: str) -> list[dict]:
        """이 질문에서 고를 수 있는 사전 목적지. STT 힌트와 판정 후보가 같은 목록을 쓴다.

        진료·민원 목적(PURPOSE)에서는 층 이동 목적지(엘리베이터로 연결된 '3층관공서' 등)를 뺀다.
        힌트에 들어 있으면 "네"·짧은 말을 그 낱말로 받아쓰는 환각이 진료 목적 질문에서 생긴다
        (2026-10-13 11:46 "2층관공서" → "3층관공서 안내가 맞으신가요?"). 추가 목적지(DESTINATION)는 그대로다.
        """
        if expected != "PURPOSE":
            return self.slots
        lift = {p.get("id") for p in self.places if FLOOR_LINK_WORD in (p.get("name") or "")}
        kept = [sl for sl in self.slots if sl.get("locationId") not in lift]
        return kept or self.slots      # 전부 층 이동이면 예전처럼 모두 쓴다

    def _candidates(self, expected: str) -> list[dict]:
        """판정 후보 [{name, info, locationId, source}]. 순서가 LLM 의 index 다."""
        out = [{"name": sl["value"], "info": ", ".join(sl.get("synonyms", [])),
                "synonyms": sl.get("synonyms", []), "locationId": sl.get("locationId"),
                "source": "사전"} for sl in self.dest_slots(expected)]
        if expected == "DESTINATION":
            linked = {c["locationId"] for c in out if c["locationId"]}
            names = {_norm(c["name"]) for c in out}
            for p in self.places:
                # 사전 목적지가 이미 가리키는 장소·같은 이름은 한 번만 — 후보가 겹치면 LLM 이 흔들린다
                if p.get("id") in linked or _norm(p.get("name")) in names:
                    continue
                out.append({"name": p["name"], "info": "", "synonyms": [],
                            "locationId": p["id"], "source": "장소"})
        return out

    @staticmethod
    def _direct_match(heard: str, cands: list[dict]) -> Optional[dict]:
        """이름·동의어가 대답에 그대로 들어 있는 후보. 긴 표현을 우선한다."""
        norm = _norm(heard)
        best = None
        for c in cands:
            for w in (c["name"], *c["synonyms"]):
                nw = _norm(w)
                if nw and nw in norm and (best is None or len(nw) > best[1]):
                    best = (c, len(nw), w)
        if not best:
            return None
        log.info("  목적지 판정[%s]: '%s' (일치 표현 '%s')", best[0]["source"], best[0]["name"], best[2])
        return best[0]

    def match_destination(self, heard: str, expected: str = "PURPOSE",
                          question: str = "") -> Optional[dict]:
        """대답에서 목적지를 찾는다.

        목적지 질문(진료·민원 목적 PURPOSE · 추가 목적지 DESTINATION)은 **문장 전체의 뜻으로**
        고른다 — LLM 먼저, LLM 을 못 쓰거나 못 고르면 이름·동의어 직매칭. 단, 목적지 이름(동의어) 하나만
        말했으면 LLM 없이 바로 쓴다(_only_name, config.DEST_DIRECT_FAST). 직매칭은 낱말 하나만
        보므로 "눈은 괜찮고 허리가 아파요" 처럼 후보 낱말이 둘 섞이거나 "무릎이 쑤셔요"·"손 씻을 데
        있어요?" 처럼 사전에 없는 말을 못 가른다. 대신 LLM 왕복(약 1초)이 대답마다 붙는다.

        Returns:
            {"name", "locationId", "source"} 또는 None
        """
        cands = self._candidates(expected)
        if not cands:
            log.warning("  목적지 판정[생략]: 후보가 없다 (관제 음성 관리 › 목적지 사전·운영 맵 장소)")
            return None
        direct = self._direct_match(heard, cands)
        if direct and config.DEST_DIRECT_FAST and self._only_name(heard, cands, direct):
            log.info("  목적지 판정[LLM 생략]: 이름만 말함 — '%s'", direct["name"])
            return direct

        if not config.DEST_LLM_ENABLED:
            # 크레딧이 없거나 오프라인 운영일 때 — 직매칭만. 무의미한 429 재시도 3회(약 2초)와
            # 오류 로그가 사라지고, 직매칭이 실패하면 호출부가 재질문 → onTimeoutStepCode 로 간다.
            if not direct:
                log.info("  목적지 판정[LLM 생략]: DEST_LLM_ENABLED=0 — 사전 직매칭만 사용")
            return direct

        picked = self._llm_pick(heard, cands, question)
        if picked:
            return picked
        if direct:
            log.info("  목적지 판정: LLM 이 고르지 못해 직매칭 '%s' 를 쓴다", direct["name"])
        return direct

    @staticmethod
    def _only_name(heard: str, cands: list[dict], direct: dict) -> bool:
        """대답이 사실상 그 목적지 이름(또는 동의어) 하나뿐인가 — LLM 없이 믿어도 되는가.

        가장 긴 일치 표현을 뺀 나머지에 다른 목적지 낱말이 없고, 짧고(DEST_DIRECT_FAST_REST),
        부정하는 말이 없어야 한다. "허리가 아파서 왔어요"(나머지 7자)는 통과, "눈은 괜찮고 허리가 아파요"
        (안과 낱말 '눈'이 남음)·"정형외과 말고 안과"(부정·후보 둘)는 LLM 으로 보낸다.
        """
        norm = _norm(heard)
        word = max((_norm(w) for w in (direct["name"], *direct["synonyms"]) if _norm(w) and _norm(w) in norm),
                   key=len, default="")
        if not word:
            return False
        rest = norm.replace(word, "", 1)
        if len(rest) > config.DEST_DIRECT_FAST_REST or any(n in rest for n in NEGATION_WORDS):
            return False
        # 같은 목적지의 다른 동의어("연금 신청서 쓰러 왔어요")는 괜찮다 — 다른 목적지 낱말이 남을 때만 LLM 으로
        return not any(_norm(w) and _norm(w) in rest
                       for c in cands if c is not direct for w in (c["name"], *c["synonyms"]))

    def _llm_pick(self, heard: str, cands: list[dict], question: str) -> Optional[dict]:
        """LLM 으로 후보 중 하나를 고른다. 못 고르거나(확신 미달·없음) 호출이 실패하면 None."""
        try:
            t0 = time.time()
            d = ai.pick_destination(heard, cands, question)
            ms = int((time.time() - t0) * 1000)
        except Exception as e:
            log.warning("  목적지 판정[LLM 실패]: %s", e)
            return None

        idx, conf = d["index"], d["confidence"]
        if idx is not None and conf >= PICK_MIN_CONFIDENCE:
            log.info("  목적지 판정[LLM %dms]: '%s' (확신 %.2f, %s) — 후보 %d개",
                     ms, cands[idx]["name"], conf, d["reason"], len(cands))
            return cands[idx]
        log.info("  목적지 판정[LLM %dms]: 없음 (%s%s) — 후보: %s", ms,
                 f"'{cands[idx]['name']}' 확신 {conf:.2f} 미달, " if idx is not None else "",
                 d["reason"], ", ".join(c["name"] for c in cands))
        return None

    def _accept_destination(self, heard: str, dest: dict, intent: str) -> None:
        self._symptom = symptom_word(heard, dest, self._candidates(intent)) if intent == "PURPOSE" else None
        if self._symptom:
            log.info("  증상: '%s'", self._symptom)
        self._patch(destinationId=dest.get("locationId"), destinationName=dest["name"],
                    matchedSlot=dest["name"], matchedIntent=intent)
        self.backend.log_dialog(heard, dest["name"], acted=True)

    def _destination_answer(self, step: dict, heard: str,
                            question: str) -> tuple[str, Optional[dict]]:
        """목적지 대답 하나를 처리한다. 반환: (결과, 목적지)

        결과: ok | unlinked | none
        """
        dest = self.match_destination(heard, step.get("expectedIntent") or "PURPOSE", question)
        if not dest:
            return "none", None
        if dest.get("locationId") is None:
            # 사전에는 있지만 맵 장소가 연결되지 않았다. 이동 단계에서 "목적지를
            # 확인하지 못했어요" 로 뭉개지 말고 여기서 사실대로 말한다.
            self.speak(f"{josa(dest['name'], '은', '는')} 아직 안내할 수 없습니다.")
            self.backend.log_dialog(heard, dest["name"], acted=False)
            return "unlinked", dest
        self._accept_destination(heard, dest, step.get("expectedIntent") or "PURPOSE")
        return "ok", dest

    # ── 단계 실행 ─────────────────────────────────────
    def run(self, scenario_code: str, floor_id: Optional[int] = None) -> str:
        """시나리오를 끝까지 돌리고 종료 사유를 돌려준다."""
        if not self.load(scenario_code):
            return "load_failed"

        try:
            self.session = self.backend.start_session(scenario_code, floor_id)
        except BackendError as e:
            log.error("세션 시작 실패: %s", e)
            return "session_failed"

        self._symptom = None
        log.info("세션 #%s 시작 (step=%s)", self.session["id"], self.session["currentStepCode"])
        return self._drive()

    def resume(self, session: dict) -> str:
        """관제가 이미 연 세션을 이어받아 끝까지 돌린다(사양 1M — 운영자 수동 안내시작)."""
        self.session = session
        self._symptom = None
        code = session.get("scenarioCode") or config.SCENARIO_CODE
        if not self.load(code):
            self._close("load_failed", "timeout")
            return "load_failed"
        log.info("세션 #%s 이어받음 — '%s' (step=%s)",
                 session["id"], code, session.get("currentStepCode"))
        return self._drive()

    def _close(self, result: str, closed_by: str) -> None:
        try:
            self.backend.close_session(self.session["id"], result, closed_by, self._close_reason)
        except BackendError as e:
            log.error("세션 종료 보고 실패: %s", e)
        log.info("세션 #%s 종료 — %s (%s)", self.session["id"], result, closed_by)

    def _drive(self) -> str:
        """현재 단계부터 끝까지 진행하고 세션을 닫는다."""
        done = threading.Event()
        threading.Thread(target=self._watch_session, args=(done,), daemon=True).start()
        try:
            return self._drive_steps()
        finally:
            done.set()

    def _drive_steps(self) -> str:
        code = self.session.get("currentStepCode")
        result, closed_by = "completed", "voice"
        self._cancelled = False

        while code and not self._stop:
            if self.closed_by_operator() or not self._still_open():
                return self._operator_closed()
            step = self._call_move_step() if code == CALL_MOVE_STEP else self.steps.get(code)
            if not step:
                log.error("단계 '%s' 가 시나리오에 없다", code)
                result, closed_by = "aborted_bad_step", "timeout"
                break
            # 멘트만 하는 단계(도착 안내 등)는 마저 하고, 이동·질문·운영자 대기 앞에서 끝낸다
            if self._battery_requested() and step.get("waitFor", WAIT_NONE) != WAIT_NONE:
                return self._abort_battery(code)
            if self._hold_pending(step) and not self._wait_hold(code):
                if self._stop:
                    continue
                if self._battery_requested():
                    return self._abort_battery(code)
                return self._operator_closed()

            nxt, err = self._run_step(step)
            if self.closed_by_operator():
                return self._operator_closed()
            if err == BATTERY_ABORTED:
                return self._abort_battery(code)
            if err:
                result, closed_by = err
                break
            code = nxt
            if code:
                self._patch(currentStepCode=code)

        if self._stop:
            result, closed_by = "aborted_operator", "operator"
        elif self._cancelled and result == "completed":
            result = "aborted_user"

        self._close(result, closed_by)
        return result

    def _operator_closed(self) -> str:
        """관제가 이미 닫은 세션 — 종료를 다시 보고하지 않고 다음 세션을 기다리러 돌아간다."""
        log.info("세션 #%s 이 관제에서 종료됐다 — 진행을 멈춘다", self.session["id"])
        return OPERATOR_CLOSED[0]

    def _call_move_step(self) -> Optional[dict]:
        """호출 위치로 가는 가상 단계. 도착하면 시나리오 첫 단계(인사)로 넘어간다.

        멘트는 없다 — 고객은 로봇 곁이 아니라 호출한 자리에 있다.
        """
        place_id = (self.session or {}).get("startLocationId")
        if place_id is None:
            # 호출 위치 없이 이 단계로 열렸다면 이동할 곳이 없다 — 제자리에서 시작한다
            log.warning("[%s] 호출 위치가 없다 — 제자리에서 시작한다", CALL_MOVE_STEP)
            return {"stepCode": CALL_MOVE_STEP, "waitFor": WAIT_NONE, "nextStepCode": self.first_step}
        return {
            "stepCode": CALL_MOVE_STEP,
            "sayText": None,
            "waitFor": WAIT_ARRIVAL,
            "actionType": "navigate",
            "targetLocationId": place_id,
            "nextStepCode": self.first_step,
        }

    def _still_open(self) -> bool:
        """운영자가 세션을 닫았는가. 관제가 끊겼으면 열려 있다고 본다(오프라인에도 계속 돈다).

        열려 있으면 받은 세션으로 갈아 둔다 — 배터리 중단 요청을 단계마다 보려고.
        """
        try:
            cur = self.backend.current_session()
        except BackendError:
            return True
        if not cur or cur.get("id") != self.session["id"]:
            return False
        self.session = cur
        return True

    def _battery_requested(self) -> bool:
        return (self.session or {}).get("abortRequest") == ABORT_BATTERY

    def _abort_battery(self, code: Optional[str]) -> str:
        """배터리 위급 — 방문객에게 알리고 끝낸다. 충전소 이동은 관제가 종료를 받아 한다."""
        log.warning("[%s] 관제가 배터리 위급으로 안내 중단을 요청했다 — 알리고 끝낸다", code)
        self.speak(BATTERY_NOTICE)
        self._close(*BATTERY_ABORTED)
        return BATTERY_ABORTED[0]

    def _hold_pending(self, step: dict) -> bool:
        """이 이동 단계 앞에서 멈춰야 하나 — 운영자가 일시 정지를 눌렀다.

        이동 단계 앞에서만 본다: 도착 안내·질문·운영자 입력 단계는 마저 진행하고, 다음 목적지로
        떠나기 전에 선다. 호출 위치 이동은 안내 전이라 제외한다. 관제가 끊기면 멈추지 않는다.
        """
        if step.get("actionType") != "navigate" or step["stepCode"] == CALL_MOVE_STEP:
            return False
        try:
            cur = self.backend.current_session()
        except BackendError:
            return False
        if not cur or cur.get("id") != self.session["id"]:
            return False   # 닫혔다 — 단계 실행 쪽에서 알아챈다
        self.session = cur
        return cur.get("holdState") in HOLD_STATES

    def _wait_hold(self, code: str) -> bool:
        """일시 정지 — 재개되면 True, 세션이 닫히거나 엔진이 멈추면 False."""
        sid = self.session["id"]
        resumed_engine = self.session.get("holdState") == "holding"   # 재시작으로 이어받음 — 다시 말하지 않는다
        try:
            self.session = self.backend.hold(sid, "holding")
        except BackendError as e:
            log.warning("  일시 정지 보고 실패(그대로 기다림): %s", e)
        if not self.session.get("holdState"):
            log.info("[%s] 일시 정지 예약이 취소됐다 — 그대로 잇는다", code)
            return True
        if not resumed_engine:
            self.speak(HOLD_NOTICE)
        log.info("[%s] 운영자 일시 정지 — 관제에서 재개할 때까지 출발하지 않는다", code)

        while not self._stop:
            if self._closed.wait(config.SESSION_POLL_SEC):
                return False
            try:
                cur = self.backend.current_session()
            except BackendError as e:
                log.warning("  세션 폴링 실패(일시 정지 유지): %s", e)
                continue
            if not cur or cur.get("id") != sid:
                return False
            self.session = cur
            if self._battery_requested():
                return False      # 호출한 쪽이 배터리 중단으로 끝낸다
            if not cur.get("holdState"):
                log.info("[%s] 운영자 재개 — 다음 단계로 잇는다", code)
                return True
        return False

    def _patch(self, **fields) -> None:
        if not self.session:
            return
        try:
            self.session = self.backend.patch_session(self.session["id"], **fields)
        except BackendError as e:
            log.warning("세션 갱신 실패(계속 진행): %s", e)

    def _run_step(self, step: dict) -> tuple[Optional[str], Optional[tuple]]:
        """한 단계를 실행한다.

        Returns:
            (다음 단계 코드, 오류) — 오류는 (result, closedBy) 또는 None
        """
        code = step["stepCode"]
        wait = step.get("waitFor", WAIT_NONE)
        log.info("[%s] wait=%s", code, wait)

        # 이동 단계는 "안내하겠습니다" 전에 출발할 수 있는지부터 본다 — 멘트를 듣고 따라나선
        # 방문자를 제자리에 세워 두지 않는다(측위 상실·위치 미초기화·맵상 길 없음은 미리 안다)
        self._move_info = {}
        if step.get("actionType") == "navigate":
            blocked = self._check_move(step)
            if blocked:
                return None, blocked
            self._cue_baseline()

        say = self._fill(step.get("sayText"))
        # 이동 단계는 멘트가 끝날 때 로봇이 출발하도록 끝나기 MOVE_LEAD_SEC 전에 이동을 요청한다
        early = _EarlyMove(self, step) if step.get("actionType") == "navigate" else None
        # 출발하자마자 제자리에서 돈다면(관제 이동 전 확인 departTurn) 출발 멘트 뒤 회전 직전에 방향을 알린다 —
        # 이동 요청은 그 안내에 맞춘다(11-02: 말없이 돌아 손잡이를 잡은 방문자가 놀람). 멘트 없는 이동(마중)은 알리지 않는다
        turn_cue = self._move_info.get("departTurn") if early and say else None
        # 대답을 기다리는 단계의 멘트는 질문이다 — 안 들리면 대답을 받을 수 없다
        if say and not self._say_step(say, critical=wait == WAIT_USER,
                                      on_start=early.schedule if early and not turn_cue else None) and wait == WAIT_USER:
            self._close_reason = TTS_FAILED_REASON
            return None, TTS_FAILED_RESULT
        if turn_cue and not self.closed_by_operator():
            log.info("[%s] 출발 제자리 회전 안내 — %s", code, turn_cue)
            self._speak_checked(turn_cue, on_start=early.schedule_turn)

        # 멘트 도중 종료됐으면 이동 명령을 보내지 않는다 — 안내할 사람이 없는데 출발하면 안 된다.
        # 이미 보냈으면 관제가 세션 종료와 함께 주행을 멈춘다.
        if self.closed_by_operator():
            if early:
                early.cancel()
            return None, OPERATOR_CLOSED

        # ── 액션 ──
        if early:
            failure = early.finish()
            if failure:
                self.speak(failure)
                return None, ("nav_failed", "timeout")

        # ── 대기 ──
        if wait == WAIT_NONE:
            return step.get("nextStepCode"), None

        if wait == WAIT_ARRIVAL and self._follows(step):
            return self._wait_arrival_following(step)

        if wait == WAIT_ARRIVAL:
            arrived = self.backend.wait_arrival(should_stop=self.closed_by_operator,
                                                on_tick=self._nav_tick(step))
            if self.closed_by_operator():
                return None, OPERATOR_CLOSED
            if not arrived:
                self.speak("이동이 중단됐어요.")
                return None, ("nav_failed", "timeout")
            return step.get("nextStepCode"), None

        if wait == WAIT_OPERATOR:
            return self._wait_operator(step)

        if wait == WAIT_USER:
            return self._wait_user(step)

        return step.get("nextStepCode"), None

    # ── 경유지 방향 안내 ──────────────────────────────
    def _cue_baseline(self) -> None:
        """출발 전 방향 안내 번호를 잡아 둔다 — 지난 이동의 안내를 다시 말하지 않게."""
        try:
            st = self.backend.robot_state() or {}
            self._cue_seq = (st.get("cue") or {}).get("seq")
            self._notice_seq = (st.get("notice") or {}).get("seq")
        except Exception:  # noqa: BLE001 — 방향 안내는 보조 기능이다. 실패해도 안내를 멈추지 않는다
            self._cue_seq = self._notice_seq = None

    def _turn_cue_tick(self, step: dict) -> Optional[Callable[[dict], None]]:
        """도착을 기다리는 동안 /robot/nav 를 볼 때마다 불릴 함수. 경유지를 지나면 꺾는 방향을 말한다.

        출발 전에 길 전체를 설명하면 보이지 않는 방문객은 알 수 없고, 로봇을 마주 보고 있으면 좌우가
        뒤집힌다 — 걷는 중에 꺾는 순간 말하면 로봇 뒤를 따르는 사람에게 같은 방향이다.
        호출 위치로 가는 이동(따라오는 사람 없음)과 방향 안내를 끈 시나리오(관공서 기본)에서는 말하지 않는다.
        """
        if (not config.TURN_CUES_ENABLED or not self.turn_cues
                or step.get("stepCode") == CALL_MOVE_STEP):
            return None

        def tick(st: dict) -> None:
            cue = st.get("cue") or {}
            seq = cue.get("seq")
            if seq is None:
                return
            if self._cue_seq is None:
                self._cue_seq = seq          # 기준을 못 잡았으면 처음 본 값을 기준으로
                return
            if seq <= self._cue_seq:
                self._resay_cut_cue(st)
                return
            self._cue_seq = seq
            self._cut_cue = None             # 새 안내가 나왔다 — 끊긴 옛 안내는 버린다
            if cue.get("text") and not self.closed_by_operator():
                log.info("  경유지 통과 — %s", cue["text"])
                self._speak_cue(cue["text"])
        return tick

    def _speak_cue(self, text: str) -> None:
        """방향·도착·진행 안내 — 말하는 중 장애물 멘트가 오면 끊고(다시 말하지 않는다) 장애물 멘트를 바로 말한다."""
        self._peeked = None
        self._speak_checked(text, interrupt=self._notice_arrived)
        if self._peeked is not None:
            st, self._peeked = self._peeked, None
            self._cut_cue = text
            log.info("  안내를 끊고 장애물 멘트로")
            self._notice_tick(st)

    def _resay_cut_cue(self, st: dict) -> None:
        """끊긴 안내가 있고 장애물이 풀려 다시 걷고 있으면 한 번 다시 말한다(도착했으면 버린다)."""
        if not self._cut_cue:
            return
        try:
            st = self.backend.robot_state()  # 이 확인 안에서 출발 멘트를 말하느라 몇 초 지났을 수 있다 — 지금 상태로
        except BackendError:
            return
        if st.get("arrived") or not st.get("is_navigating"):
            self._cut_cue = None
            return
        if (st.get("obstacle") or {}).get("holding"):
            return                           # 아직 서 있다 — 출발 멘트('다시 출발하겠습니다') 뒤에
        text, self._cut_cue = self._cut_cue, None
        if not self.closed_by_operator():
            log.info("  끊겼던 안내 다시 — %s", text)
            self._speak_cue(text)

    def _notice_arrived(self) -> bool:
        """새 장애물 멘트가 관제에 올라왔는가(NOTICE_PEEK_SEC 마다만 묻는다). 올라왔으면 그 상태를 남긴다."""
        now = time.monotonic()
        if now - self._peek_at < NOTICE_PEEK_SEC:
            return False
        self._peek_at = now
        try:
            st = self.backend.robot_state()
        except BackendError:
            return False
        seq = (st.get("notice") or {}).get("seq")
        if seq is not None and self._notice_seq is not None and seq > self._notice_seq:
            self._peeked = st
            return True
        return False

    def _notice_tick(self, st: dict) -> None:
        """장애물 정지 멘트 — 관제가 notice 의 seq 를 올리면 그 글을 말한다(방향 안내와 달리 늘 켜져 있다)."""
        notice = st.get("notice") or {}
        seq = notice.get("seq")
        if seq is None:
            return
        if self._notice_seq is None:
            self._notice_seq = seq           # 기준을 못 잡았으면 처음 본 값을 기준으로
            return
        if seq <= self._notice_seq:
            return
        self._notice_seq = seq
        if notice.get("text") and not self.closed_by_operator():
            log.info("  장애물 안내 — %s", notice["text"])
            self.speak(notice["text"])

    def _obstacle_ask_tick(self, st: dict) -> None:
        """장애물 긴 정지 뒤 '준비됐어요' 질문 — 질문을 말한 뒤(notice seq 가 따라잡은 뒤) 듣고 yes|no|none 을 보낸다."""
        ask = (st.get("obstacle") or {}).get("ask")
        if not ask or ask.get("id") == getattr(self, "_ask_done", None) or not self.session:
            return
        if self._notice_seq is None or self._notice_seq < (ask.get("noticeSeq") or 0):
            return                           # 질문을 아직 말하지 않았다 — 다음 확인에서
        self._ask_done = ask["id"]
        if self.closed_by_operator():
            return
        heard = self.listen(OBSTACLE_LISTEN_SEC)
        verdict = classify_yes_no(heard) if heard else None
        answer = "yes" if verdict == "YES" else ("no" if heard else "none")
        log.info("  장애물 질문 #%s 대답: %r → %s", ask["id"], heard, answer)
        try:
            self.backend.obstacle_answer(self.session["id"], ask["id"], answer)
        except BackendError as e:
            log.warning("장애물 대답 전달 실패(관제가 시간 뒤 다시 묻는다): %s", e)

    def _nav_tick(self, step: dict) -> Callable[[dict], None]:
        """도착을 기다리는 동안 /robot/nav 를 볼 때마다 — 장애물 멘트, 그다음 경유지 방향 안내."""
        cue_tick = self._turn_cue_tick(step)

        def tick(st: dict) -> None:
            self._notice_tick(st)
            self._obstacle_ask_tick(st)
            if cue_tick:
                cue_tick(st)
        return tick

    def _follows(self, step: dict) -> bool:
        """이 이동에서 동행을 확인하는가. 호출 위치로 가는 이동은 따라올 사람이 없다."""
        plan = self.follow_plan or {}
        return (bool(plan.get("enabled")) and step.get("actionType") == "navigate"
                and step["stepCode"] != CALL_MOVE_STEP)

    def _wait_arrival_following(self, step: dict) -> tuple[Optional[str], Optional[tuple]]:
        guide = follow.FollowGuide(self.backend, self.speak, self.listen, self.session["id"],
                                   self.follow_plan, self.closed_by_operator,
                                   on_tick=self._nav_tick(step))
        res = guide.run(self._nav_place_id)
        if res == follow.ARRIVED:
            return step.get("nextStepCode"), None
        if res == follow.CLOSED:
            return None, OPERATOR_CLOSED
        if res == follow.CANCEL:
            return None, ("aborted_user", "voice")
        self.speak("이동이 중단됐어요.")
        return None, ("nav_failed", "timeout")

    def _nav_target(self, step: dict) -> Optional[int]:
        place_id = step.get("targetLocationId")
        if place_id is None and step.get("slotName"):
            # 고객 목적지 — 고객이 말해 확정한 곳. 관제는 이동 명령으로 이 값을 바꾸지 않는다
            # (이동 장소는 세션 navTarget 에 따로 남는다).
            place_id = (self.session or {}).get("destinationId")
        return place_id

    def _check_move(self, step: dict) -> Optional[tuple]:
        """출발할 수 없으면 방문자에게 알리고 종료 결과를 준다. 확인할 수 없으면(관제 두절) 그대로 간다."""
        place_id = self._nav_target(step)
        if place_id is None:
            return None             # 목적지 없음은 _send_move 가 사실대로 말한다
        try:
            res = self.backend.move_check(self.session["id"], place_id)
        except BackendError as e:
            log.warning("[%s] 이동 전 확인 실패(그대로 진행): %s", step["stepCode"], e)
            return None
        if res.get("ok", True):
            self._move_info = res
            return None
        reason = res.get("reason") or "이동을 시작할 수 없습니다."
        log.warning("[%s] 출발 불가 — %s", step["stepCode"], reason)
        self._close_reason = reason
        self.speak(MOVE_UNAVAILABLE)
        return "nav_failed", "voice"

    def _move_started_anyway(self) -> bool:
        """이동 요청 응답이 늦어 끊겼을 때 — 관제가 실제로는 출발시켰는지 잠시 지켜본다.

        끊긴 순간 관제는 아직 NAV 를 보내는 중일 수 있다. 출발 전에 실패로 닫으면 관제의 이동 처리가
        닫힌 세션을 보고 출발하지 않으므로(session_move), 여기서 못 보면 출발하지 않은 것이다.
        """
        deadline = time.time() + MOVE_START_CONFIRM_SEC
        while time.time() < deadline:
            try:
                st = self.backend.robot_state()
            except BackendError:
                st = {}
            if st.get("navigating") or st.get("is_navigating"):
                return True
            time.sleep(1.0)
        return False

    def _send_move(self, step: dict) -> Optional[str]:
        """목적지를 정해 이동 명령을 보낸다. 성공이면 None, 실패면 방문자에게 할 말.

        멘트 재생 중 다른 스레드에서도 부르므로 여기서는 말하지 않는다 — 소리가 겹친다.
        """
        place_id = self._nav_target(step)

        if place_id is None:
            log.error("[%s] 이동할 목적지가 없다", step["stepCode"])
            return "목적지를 확인하지 못했어요."

        try:
            self.backend.move(self.session["id"], place_id)
            self._nav_place_id = place_id
            return None
        except BackendError as e:
            # 기립 대기 등으로 응답이 늦어 끊겼어도 관제는 출발시켰을 수 있다 — 실패로 단정하면
            # 세션은 닫히고 로봇 혼자 간다
            if isinstance(e.__cause__, requests.Timeout) and self._move_started_anyway():
                log.warning("이동 요청 응답 지연 — 로봇은 출발했으므로 이어 간다: %s", e)
                self._nav_place_id = place_id
                return None
            log.error("이동 명령 실패: %s", e)
            return "이동 명령을 보내지 못했어요."

    def _listen_once(self, step: dict, timeout: float) -> Optional[str]:
        """대기 한 번. 말은 했는데 못 알아들었으면 남은 시간 안에서 바로 되묻는다.

        예전에는 대기 시간이 다 가야 되물어서, 길게 기다리는 단계(lobby_ask 120초)는 실패가
        이어져도 "잘 못 들었어요" 가 나오지 않았다(2026-10-11 세션 308: 95초 동안 21회).
        되묻기는 REASK_GARBLED_MAX 번까지이고 재시도 횟수(RetryCount)를 쓰지 않는다 —
        잘못 들은 것 때문에 '대답 없음' 경로(onTimeout)로 넘어가면 안 된다.
        신청서 작성 단계는 짧게 되묻는다(FORM_REASK) — 질문(도착 멘트)은 되풀이하지 않고, 단계 전체에서
        FORM_REASK_MAX 번까지(_wait_user 가 센다).
        """
        deadline = time.monotonic() + timeout
        form = step.get("expectedIntent") == "FORM_COMPLETE"
        asks = 0
        while True:
            can_ask = (self._form_reasks < FORM_REASK_MAX) if form else (asks < config.REASK_GARBLED_MAX)
            self.stop_on_garbled = can_ask
            self.last_garbled = False
            try:
                heard = self.listen(max(0.0, deadline - time.monotonic()))
            finally:
                self.stop_on_garbled = False
            left = deadline - time.monotonic()
            if heard or not self.last_garbled or self.closed_by_operator() or left < 1.0:
                return heard
            if form:
                self._form_reasks += 1
                log.info("  신청서 작성 중 말소리를 알아듣지 못함 — 짧게 되묻는다(%d/%d)", self._form_reasks, FORM_REASK_MAX)
                self.speak(FORM_REASK)
                deadline = max(deadline, time.monotonic() + min(timeout, 10.0))
                continue
            asks += 1
            log.info("  말소리는 있었지만 알아듣지 못함 — 되묻는다(%d/%d, 남은 %.0f초)",
                     asks, config.REASK_GARBLED_MAX, left)
            question = self._fill(step.get("sayText"))
            self.speak(REASK_NOT_HEARD)
            if question:
                self.speak(question)
            # 되묻느라 쓴 시간만큼 대기를 늘려 준다 — 대답할 시간이 줄지 않게
            deadline = max(deadline, time.monotonic() + min(timeout, 10.0))

    def _wait_user(self, step: dict) -> tuple[Optional[str], Optional[tuple]]:
        """사용자 응답 대기. RetryCount 만큼 재질문한다."""
        expected = step.get("expectedIntent")
        timeout = float(step.get("responseTimeoutSec", 10))
        retries = int(step.get("retryCount", 2))
        self._form_reasks = 0

        for attempt in range(retries + 1):
            heard = self._listen_once(step, timeout)
            if self.closed_by_operator():
                return None, OPERATOR_CLOSED

            if not heard:
                if attempt < retries:
                    # 신청서를 쓰는 동안은 조용한 게 정상이다 — 도착 멘트를 되풀이하면
                    # "신청서가 있는 위치에 도착했습니다" 를 세 번 듣게 된다(2026-10-08 실기)
                    if expected == "FORM_COMPLETE":
                        # 쓰는 중일 수 있다 — 재촉하지 않는 말로 아직 기다린다는 것만 알린다
                        self.speak(FORM_WAIT_NOTICE)
                    else:
                        question = self._fill(step.get("sayText"))
                        if question:
                            self.speak(REASK_NOT_HEARD)
                            self.speak(question)
                        else:
                            self.speak(REASK_NOT_UNDERSTOOD)
                    continue
                nxt = step.get("onTimeoutStepCode")
                if nxt:
                    return nxt, None
                return None, ("aborted_no_response", "timeout")

            log.info("  방문자: %s", heard)
            self._patch(heard=heard)

            if expected == "YES_NO":
                verdict = classify_yes_no(heard)
                if verdict == "CANCEL":
                    return self._farewell()
                if verdict == "YES":
                    # "네, 화장실이요" — 예 뒤에 목적지까지 말했으면 다시 묻지 않는다
                    if len(_norm(heard)) > 4:
                        jump = self._carry_destination(step, heard)
                        if jump:
                            return jump, None
                    return step.get("nextStepCode"), None
                if verdict == "NO":
                    nxt = step.get("onNoStepCode") or step.get("nextStepCode")
                    return nxt, None
                # "추가로 필요한 곳 있으세요?" 에 "화장실이요" 처럼 목적지를 바로 말하면
                # 되묻지 않고 받는다 — 다음 단계가 목적지를 묻는 단계일 때만.
                jump = self._carry_destination(step, heard)
                if jump:
                    return jump, None
                # 판정 실패 → 재질문(다시 말해 달라면 질문 그대로, 아니면 정해 둔 대답으로)
                if attempt < retries:
                    question = self._fill(step.get("sayText"))
                    if question and REPEAT_RE.search(re.sub(r"[^0-9A-Za-z가-힣]", "", heard)):
                        self.speak(question)
                    else:
                        self.speak(yes_no_reask(step.get("sayText")))
                    continue
                return None, ("no_match", "timeout")

            if expected in DEST_INTENTS:
                verdict = self._dest_verdict(step, heard)
                if verdict == "CANCEL":
                    return self._farewell()
                if verdict == "NO":
                    return step["onNoStepCode"], None
                # 목적지 판정은 '말고' 뒤의 말로만 한다
                live = heard.split(NEGATION_MARK)[-1]
                ask = (ASK_WHERE if expected == "DESTINATION"
                       else self._fill(step.get("sayText")) or ASK_WHERE)
                if verdict == "ASK" and attempt < retries:
                    # "네", "다른 곳도요" — 가고 싶다는 말만 했다. 긴 질문 대신 어디인지만 묻는다
                    self.speak(ask)
                    continue
                result, _dest = self._destination_answer(step, live, self._fill(step.get("sayText")))
                if result == "ok":
                    return step.get("nextStepCode"), None
                if attempt < retries:
                    # 들었지만 목적지를 못 찾았다 — 질문을 되풀이하지 않고 다시 말해 달라고 한다
                    self.speak(ASK_OTHER_PLACE if result == "unlinked" else REASK_NOT_UNDERSTOOD)
                    continue
                if result == "none":
                    self.backend.log_dialog(heard, None, acted=False)
                nxt = step.get("onTimeoutStepCode")
                return (nxt, None) if nxt else (None, ("unknown_dest", "voice"))

            if expected == "FORM_COMPLETE":
                t = re.sub(r"[^0-9A-Za-z가-힣]", "", heard)
                if any(w in t for w in FORM_DONE_WORDS):
                    return step.get("nextStepCode"), None
                if any(w in t for w in CANCEL_WORDS):
                    return self._farewell()
                # 작성 중 혼잣말·옆 사람 말·잘못 받아쓴 말 — 짧게 알려 준다(단계 전체 FORM_REASK_MAX 번까지)
                if attempt < retries:
                    if self._form_reasks < FORM_REASK_MAX:
                        self._form_reasks += 1
                        self.speak(FORM_REASK)
                    continue
                nxt = step.get("onTimeoutStepCode")
                return (nxt, None) if nxt else (None, ("aborted_no_response", "timeout"))

            # 기대 의도가 없으면 들은 것만 기록하고 진행
            return step.get("nextStepCode"), None

        return None, ("no_match", "timeout")

    def _dest_verdict(self, step: dict, heard: str) -> Optional[str]:
        """목적지 질문의 대답을 먼저 가른다. 목적지를 말했으면 '아니요' 가 섞여도 목적지다.

        CANCEL 취소어 · DEST 이름·동의어 그대로 말함 · NO 거절·출구어('아니요' 분기가 있을 때만)
        · ASK 가겠다는 말만 함("네", "다른 곳도요") · None 그 밖(LLM 판정으로)
        """
        if any(w in _norm(heard) for w in CANCEL_WORDS):
            return "CANCEL"
        live = heard.split(NEGATION_MARK)[-1]
        if self._direct_match(live, self._candidates(step.get("expectedIntent") or "PURPOSE")):
            return "DEST"
        t = _norm(live)
        if step.get("onNoStepCode") and any(w in t for w in (*DEST_NO_WORDS, *EXIT_WORDS)):
            return "NO"
        if config.DEST_LLM_ENABLED and len(t) > ASK_MAX_LEN:
            # 긴 대답은 LLM 에 맡긴다 — "무릎에 통증이 있어요" 의 '있'·"손 씻을 데 있어요?" 의 '있' 을
            # '가겠다는 말만 함' 으로 보고 되물으면 이유·장소를 말한 사람에게 같은 질문을 반복한다
            return None
        if any(w in t for w in YES_WORDS) or "다른" in t or "있" in t:
            return "ASK"
        return None

    def _farewell(self) -> tuple[Optional[str], Optional[tuple]]:
        """취소어를 들었다. 작별 단계가 있으면 인사하고 끝내고, 없으면 바로 끝낸다."""
        if FAREWELL_STEP in self.steps:
            self._cancelled = True
            return FAREWELL_STEP, None
        return None, ("aborted_user", "voice")

    def _carry_destination(self, step: dict, heard: str) -> Optional[str]:
        """예/아니요 질문에 목적지로 답했을 때, 목적지 단계를 건너뛰고 그 다음 단계 코드를 준다."""
        nxt = self.steps.get(step.get("nextStepCode") or "")
        if not nxt or nxt.get("expectedIntent") not in DEST_INTENTS:
            return None
        dest = self.match_destination(heard, nxt["expectedIntent"], self._fill(step.get("sayText")))
        if not dest or dest.get("locationId") is None:
            return None
        self._accept_destination(heard, dest, nxt["expectedIntent"])
        log.info("  예/아니요 대신 목적지를 답했다 — '%s' 로 바로 진행", dest["name"])
        return nxt.get("nextStepCode")

    def _voice_exit(self, step: dict) -> tuple[Optional[str], list[str]]:
        """운영자 단계의 음성 출구(번들 voiceExitKeywords·voiceExitStepCode). 없거나 모르는 단계면 (None, [])."""
        to = step.get("voiceExitStepCode")
        words = [w for w in (step.get("voiceExitKeywords") or []) if isinstance(w, str) and w.strip()]
        if not to or not words:
            return None, []
        if to not in self.steps:
            # 모르는 단계로 가면 aborted_bad_step 으로 끝난다 — 차라리 듣지 않고 운영자를 기다린다
            log.warning("  음성 출구 단계 '%s' 가 시나리오에 없어 듣지 않는다", to)
            return None, []
        return to, words

    def _listen_voice_exit(self, waiting: str, exit_to: str, words: list[str]) -> Optional[str]:
        """운영자 대기 중 한 번 듣는다. 음성 출구 말이면 관제에 조건부로 단계를 넘기고 갈 단계를 돌려준다.

        관제는 세션이 아직 이 단계일 때만 바꾼다(fromStepCode) — 운영자가 먼저 [재개]를 눌렀으면
        운영자가 정한 단계를 돌려받아 그쪽으로 간다. 관제가 응답하지 않으면 방문자 말대로 간다.
        """
        heard = self.listen(config.SESSION_POLL_SEC)
        if not heard or self.closed_by_operator():
            return None
        hit = voice_exit_match(heard, words)
        log.info("  운영자 대기 중 방문자: %s → %s", heard, f"음성 출구('{hit}')" if hit else "음성 출구 아님")
        if not hit:
            return None
        try:
            cur = self.backend.patch_session(self.session["id"], currentStepCode=exit_to,
                                             fromStepCode=waiting, result="running")
        except BackendError as e:
            log.warning("  음성 출구 전달 실패 — 방문자 말대로 진행: %s", e)
            return exit_to
        if cur:
            self.session = cur
            if cur.get("result") not in ("running", "paused"):
                return None              # 그사이 닫혔다 — 다음 폴링이 종료로 처리한다
            got = cur.get("currentStepCode")
            if got and got != exit_to:
                log.info("  운영자가 먼저 넘겼다 — 음성 출구 대신 %s", got)
                return got
        return exit_to

    def _wait_operator(self, step: dict) -> tuple[Optional[str], Optional[tuple]]:
        """관제 입력 대기. 끊기면 그 자리에서 기다린다.

        운영자 입력(대기번호·창구·재개)은 관제가 **단계를 넘기는 것** 으로 온다. 입력값이
        있는지로 판정하면 두 번째 운영자 단계(창구)에서 이미 들어 있는 대기번호 때문에 바로
        통과해 버린다.
        """
        waiting = step["stepCode"]
        self._patch(result="paused")
        log.info("  운영자 입력 대기(%s) — 관제에서 입력할 때까지 진행하지 않는다",
                 step.get("expectedIntent") or "RESUME")
        exit_to, exit_words = self._voice_exit(step)
        if exit_to:
            log.info("  음성 출구 듣는 중 — %s → %s", " / ".join(exit_words), exit_to)

        last_notice = time.time()
        while not self._stop:
            if exit_to:
                # 폴링 간격 동안 듣는다 — 들은 말이 음성 출구면 운영자 입력 없이 그 단계로 간다
                nxt = self._listen_voice_exit(waiting, exit_to, exit_words)
                if self.closed_by_operator():
                    return None, OPERATOR_CLOSED
                if nxt:
                    return nxt, None
            # 감시 스레드가 종료를 먼저 알아채면 폴링 간격을 기다리지 않고 빠진다
            elif self._closed.wait(config.SESSION_POLL_SEC):
                return None, OPERATOR_CLOSED

            try:
                cur = self.backend.current_session()
            except BackendError as e:
                # 관제가 끊겼다. 대기번호는 추측할 수 없으니 건너뛰지 않고 계속 기다린다.
                log.warning("  세션 폴링 실패(대기 유지): %s", e)
            else:
                if not cur or cur.get("id") != self.session["id"]:
                    # 운영자가 종료했다(또는 입력으로 마지막 단계를 넘겨 완료됐다)
                    return None, ("aborted_operator", "operator")
                self.session = cur
                if self._battery_requested():
                    return None, BATTERY_ABORTED
                if cur.get("currentStepCode") != waiting:
                    log.info("  운영자 입력 확인: ticket=%s counter=%s -> %s",
                             cur.get("ticketNo"), cur.get("counterNo"), cur.get("currentStepCode"))
                    # 로봇이 이 단계에 닿기 전에 운영자가 먼저 입력했으면 위의 'paused' 가
                    # 입력 뒤에 들어가 있다 — 관제 화면에 '입력 대기' 로 남지 않게 되돌린다.
                    if cur.get("result") == "paused":
                        self._patch(result="running")
                    return cur.get("currentStepCode"), None

            if time.time() - last_notice >= OPERATOR_NOTICE_SEC:
                self.speak(OPERATOR_NOTICE)
                last_notice = time.time()

        return None, ("aborted_operator", "operator")
