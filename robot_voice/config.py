"""robot_voice 설정. 모든 값은 환경변수로 주입한다(systemd 유닛의 Environment=)."""

import os


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


# ── 관제 백엔드 ────────────────────────────────────────
# Jetson 은 사무실 Wi-Fi 로 관제 PC 에 닿는다.
BACKEND_BASE = _env("BACKEND_BASE", "http://192.168.1.58:8010")
# 관제 .env 의 AGENT_TOKEN 과 같은 값. agent_bridge 가 쓰는 토큰을 공유한다.
AGENT_TOKEN = _env("AGENT_TOKEN") or _env("VA_AGENT_TOKEN")
# 이 Jetson 이 담당하는 로봇. 번들·세션 조회에 쓴다.
ROBOT_ID = _env_int("ROBOT_ID", 0)
# 시나리오 코드 — 세션 시작 시 사용. hospital | public_office
SCENARIO_CODE = _env("SCENARIO_CODE", "hospital")
# 번들 갱신 주기(초). 끊기면 마지막 캐시로 계속 동작한다.
BUNDLE_REFRESH_SEC = _env_float("BUNDLE_REFRESH_SEC", 300.0)
# 세션 상태 폴링 주기(초) — 운영자 입력·종료 감지
SESSION_POLL_SEC = _env_float("SESSION_POLL_SEC", 3.0)
# 번들 로컬 캐시 경로. 서비스는 로그인 사용자로 돌고 /opt/robot/voice 는 root 소유라
# 거기엔 못 쓴다 — systemd StateDirectory(/var/lib/robot-voice, 서비스 사용자 소유)를 쓴다.
# 수동 실행(STATE_DIRECTORY 없음)은 홈 아래에 둔다.
_STATE_DIR = os.environ.get("STATE_DIRECTORY") or os.path.expanduser("~/.cache/robot-voice")
BUNDLE_CACHE = _env("BUNDLE_CACHE") or os.path.join(_STATE_DIR, "bundle_cache.json")
# 시험용 자동 대답 — 이 파일이 있으면 마이크 대신 단계에 맞는 대답을 넣는다(auto_answer.py).
# 서비스 사용자(und02) 폴더라 sudo·재시작 없이 만들고 지우면 다음 듣기부터 적용된다.
AUTO_ANSWER_FILE = _env("AUTO_ANSWER_FILE") or os.path.join(_STATE_DIR, "auto_answer.json")
BACKEND_TIMEOUT = _env_float("BACKEND_TIMEOUT", 8.0)
# 이동 요청만 따로 길게 — 관제가 앉은 로봇을 일으킨 뒤(STAND_WAIT_SEC 15초 + 안정화 1초) NAV 를 보내고
# 응답한다. BACKEND_TIMEOUT 으로 끊으면 엔진은 실패로 알고 로봇은 출발한다.
MOVE_TIMEOUT = _env_float("MOVE_TIMEOUT", 25.0)
# 이동 단계의 출발 멘트("○○로 안내하겠습니다")가 끝나기 이만큼 전에 이동을 요청한다.
# 요청에서 로봇이 움직이기까지 실제로는 1.43~1.69초(평균 1.57)다 — 2026-10-08 로봇 본체 로그
# (plannerStatus 1001) 기준. 처음 잡았던 2.9초(10-07 관제 로그 16건 중앙)는 상태 폴링·출력 지연이
# 섞여 부풀려진 값이라, 3.0 이면 멘트가 끝나기 약 1.4초 전에 출발했다.
# 1.0 이면 멘트가 끝나고 0.4~0.7초 뒤 출발한다 — 말 도중에 출발하지 않는다.
# 0 이면 예전처럼 멘트가 끝난 뒤 요청한다.
MOVE_LEAD_SEC = _env_float("MOVE_LEAD_SEC", 1.0)
# 내비 보행을 유지해 서 있을 때(관제 move/check 의 fastStart)의 같은 값. 이때는 관제가 준비 대기(1초)와
# 보행 전환 없이 바로 보내 MOVE_LEAD_SEC 를 그대로 쓰면 멘트 도중에 출발한다 — 2026-10-11 실기에서
# 간호실 → 경유지_02 첫 동작(제자리 회전)이 "수납을 위해 원무과로 안내하겠습니다" 도중에 시작됐다.
# 0 이면 멘트가 끝나는 순간 요청해 끝나고 약 0.3~0.5초 뒤 움직인다.
MOVE_LEAD_FAST_SEC = _env_float("MOVE_LEAD_FAST_SEC", 0.0)
# 경유지를 지나는 순간 꺾는 방향을 말한다("왼쪽으로 방향을 바꾸겠습니다" — 관제 navigation/turn_cues.py). 0 이면 끈다.
TURN_CUES_ENABLED = _env("TURN_CUES_ENABLED", "1") == "1"
# 합성한 멘트 음성 캐시 — 번들 캐시와 같은 곳(서비스 사용자 소유) 아래에 둔다
TTS_CACHE_DIR = _env("TTS_CACHE_DIR") or os.path.join(os.path.dirname(BUNDLE_CACHE), "tts")

# ── OpenAI ────────────────────────────────────────────
# 프로젝트 .env 는 OPEN_AI_SECRET_KEY 이름을 쓰므로 둘 다 받는다.
OPENAI_API_KEY = _env("OPENAI_API_KEY") or _env("OPEN_AI_SECRET_KEY")
# SDK 기본 연결 타임아웃은 5초다. Jetson Wi-Fi(-68dBm)가 순간 흔들리면 5초 안에 연결을
# 못 잡아 "Request timed out" → 600초 로컬 목소리로 빠졌다(2026-10-07, 7일간 5건).
# 재시도까지 합치면 연결 실패 시 최대 OPENAI_CONNECT_TIMEOUT × (OPENAI_MAX_RETRIES+1) 만큼 말이 늦어진다.
OPENAI_CONNECT_TIMEOUT = _env_float("OPENAI_CONNECT_TIMEOUT", 10.0)
# 연결 뒤 응답 대기. 실측 TTS 1.4~10초라 여유를 둔다(SDK 기본 600초는 너무 길다).
OPENAI_READ_TIMEOUT = _env_float("OPENAI_READ_TIMEOUT", 60.0)
OPENAI_MAX_RETRIES = int(_env_float("OPENAI_MAX_RETRIES", 2))
# gpt-4o-mini-transcribe: whisper-1 보다 빠르고, 소음 구간에서 문장을 덜 지어낸다.
# 계정에서 못 쓰면 ai.transcribe 가 STT_FALLBACK_MODEL 로 한 번 더 시도한다.
STT_MODEL = _env("STT_MODEL", "gpt-4o-mini-transcribe")
STT_FALLBACK_MODEL = _env("STT_FALLBACK_MODEL", "whisper-1")
# Whisper 프롬프트에 목적지 이름을 넣을지. **기본 off.**
# 넣으면 무음·저SNR 구간에서 모델이 그 이름을 그대로 출력하고, 직후의 사전 직매칭이
# 걸려 **말하지 않은 목적지로 확인 질문까지 진행된다**(2026-09-29 실측).
# 오인식 보정 효과는 측정된 바 없어 A/B 용 스위치로만 남긴다. → ai._stt_prompt
STT_HINT_PLACES = _env("STT_HINT_PLACES", "0") == "1"
# 로컬 STT 서버 주소. **비어 있으면 OpenAI 를 쓴다(기본값).**
# 값이 있으면 ai.transcribe 가 그 서버로 전사한다 — OpenAI 크레딧을 쓰지 않는다.
#   예: http://127.0.0.1:8091/stt   (robot_voice/stt_server.py · systemd/stt-server.service)
# 2026-09-30 크레딧 소진(429 credit_balance_exhausted)으로 STT 가 멈춰 만든 우회로다.
# 크레딧을 채우면 이 값을 비우기만 하면 원래 경로로 돌아온다.
STT_LOCAL_URL = _env("STT_LOCAL_URL", "")
STT_LOCAL_TIMEOUT = _env_float("STT_LOCAL_TIMEOUT", 20.0)
# 로컬 STT 에 어휘 힌트(호출어 + 목적지)를 줄지. **기본 on.**
# 짧은 단어는 힌트 없이 맞추기 어렵다 — 실측: "안과" → "안좋아".
# 에코 환각은 ai.is_hint_echo() 가 잡으므로 OpenAI 경로(STT_HINT_PLACES, 기본 off)와
# 다르게 기본으로 켠다.
STT_LOCAL_HINT = _env("STT_LOCAL_HINT", "1") == "1"
# 목적지 **LLM 판정**을 쓸지. 끄면 사전·장소 이름 직매칭만으로 판정한다.
# 크레딧이 없을 때(429) 끄면: 무의미한 재시도 3회(약 2초)와 오류 로그가 사라지고,
# 직매칭 실패는 그냥 재질문으로 처리된다. 의미 판정("눈이 아파서 왔어"→안과)은 못 한다.
#   1 = 사용(기본)   0 = 생략
DEST_LLM_ENABLED = _env("DEST_LLM_ENABLED", "1") == "1"
# 목적지 이름(또는 동의어) 하나만 또렷이 말했으면 LLM 판정(1.3~2.2초, 2026-10-28 로그)을 건너뛴다.
# 다른 후보 낱말이 섞였거나, 이름을 뺀 나머지가 DEST_DIRECT_FAST_REST 글자보다 길거나, 부정하는 말이 있으면
# 지금처럼 LLM 으로 문장 전체를 본다. 1 = 사용(기본)   0 = 예전처럼 늘 LLM
DEST_DIRECT_FAST = _env("DEST_DIRECT_FAST", "1") == "1"
DEST_DIRECT_FAST_REST = _env_int("DEST_DIRECT_FAST_REST", 8)
# OpenAI TTS 가 실패하면 여기로 폴백한다. 구 시스템의 voice-agent(:8090)가 로컬
# MeloTTS-Korean 을 물고 있고 wav 바이트를 그대로 돌려준다.
# 2026-09-30 크레딧 소진 실측: 숫자가 든 멘트("12번 순서가 되었습니다")는 그때 설계상
#   캐시하지 않아 매번 합성이 필요했고 429 로 **무음**이 됐다(TTS 실패 9건).
#   2026-10-08 부터는 숫자 문장도 캐시한다(ai.synthesize_cached).
#   고정 멘트는 캐시에서 나와 들렸기 때문에 "어떤 건 들리고 어떤 건 안 들린다" 로 보였다.
# 빈 값이면 폴백하지 않고 예외를 그대로 올린다(기존 동작).
TTS_LOCAL_URL = _env("TTS_LOCAL_URL", "http://127.0.0.1:8090/tts/preview")
# OpenAI 가 한 번 실패하면 이만큼은 다시 시도하지 않고 바로 로컬로 간다.
# 매번 재시도하면 문장마다 2.5초(재시도 2회)가 붙는다. 이 시간이 지나면 다시
# OpenAI 를 시도하므로 **크레딧을 채우면 저절로 원래 경로로 돌아온다.**
TTS_FALLBACK_COOLDOWN = _env_float("TTS_FALLBACK_COOLDOWN", 600.0)
TTS_LOCAL_TIMEOUT = _env_float("TTS_LOCAL_TIMEOUT", 20.0)
TTS_MODEL = _env("TTS_MODEL", "gpt-4o-mini-tts")
# 2026-10-08 alloy → nova. 같은 멘트 2문장으로 alloy·coral·sage·nova·tts-1-hd 를 비교해 골랐다
# (docs/TTS비교샘플_2026-10-08). 바꾸면 캐시 키가 달라져 멘트가 새 목소리로 다시 합성된다.
TTS_VOICE = _env("TTS_VOICE", "nova")
# 말투 지시(gpt-4o-mini-tts 전용). 비우면 문장마다 모델이 억양을 새로 정해 캐시된 멘트끼리
# 톤이 달라진다 — 시나리오마다 "다른 사람이 말하는 것 같다" 로 들렸다(2026-10-02).
# 모든 멘트에 같은 지시를 줘서 한 사람의 말투로 고정한다. 바꾸면 캐시 키가 달라져 새로 합성된다.
TTS_INSTRUCTIONS = _env(
    "TTS_INSTRUCTIONS",
    "병원 안내 데스크 직원처럼 차분하고 친절하게 말한다. "
    "또렷한 발음으로 일정한 속도와 높낮이를 유지하고, 감정을 과장하지 않는다.",
)
# 로컬 폴백 음성을 캐시에 남기지 않는 대신 쓸지 여부. 목소리가 다르므로 OpenAI 가 실패한
# 순간에만 쓰고 저장하지 않는다(쓰면 크레딧이 돌아와도 그 문장만 다른 목소리로 남는다).
#   1 = 실패 시 로컬로라도 말한다(기본)   0 = 말하지 않는다(목소리 일관성 우선)
TTS_LOCAL_FALLBACK = _env("TTS_LOCAL_FALLBACK", "1") == "1"
CHAT_MODEL = _env("CHAT_MODEL", "gpt-4o-mini")

# ── 오디오 ────────────────────────────────────────────
def _device(name: str):
    """오디오 장치 지정을 해석한다.

    숫자면 sounddevice 인덱스, 문자열이면 장치 이름 일부로 매칭한다.
    USB 를 다시 꽂으면 인덱스가 바뀔 수 있어, 이름 지정이 더 안정적이다.
    비우면 시스템 기본 장치를 쓴다.
    """
    v = _env(name)
    if not v:
        return None
    return int(v) if v.lstrip("-").isdigit() else v


# 예: INPUT_DEVICE=11 (인덱스) 또는 INPUT_DEVICE=reSpeaker (이름)
# 비우면 아래 힌트로 자동 탐색한다. 목록 확인: python -m robot_voice.audio --list
INPUT_DEVICE = _device("INPUT_DEVICE")
OUTPUT_DEVICE = _device("OUTPUT_DEVICE")

# 자동 탐색용 이름 힌트. 카드 번호는 USB 장치가 하나만 늘어도 밀리므로
# (실측: ReSpeaker 재연결 한 번에 card 3 -> 4) 이름으로 잡는 편이 안전하다.
INPUT_HINTS = [h for h in _env("INPUT_HINTS", "respeaker,xvf3800,array,xmos,seeed").split(",") if h.strip()]
OUTPUT_HINTS = [h for h in _env("OUTPUT_HINTS", "uacdemo,usb audio,usb-audio").split(",") if h.strip()]

# 스피커 하드웨어 음량 — 시작할 때 amixer 로 맞춘다(11-02 메모: 90% → 100%).
# 부팅 때 복원되는 ALSA 저장값(60%)과 실제 값(90%)이 달라 누가 바꿨는지 모르는 값에 기대지 않는다.
# 카드는 이름으로 잡는다(번호는 USB 재연결로 밀린다). SPEAKER_VOLUME_PCT=off 면 건드리지 않는다.
SPEAKER_MIXER_CARD = _env("SPEAKER_MIXER_CARD", "UACDemoV10")
SPEAKER_MIXER_CONTROL = _env("SPEAKER_MIXER_CONTROL", "PCM")
_vol = _env("SPEAKER_VOLUME_PCT", "100").lower()
SPEAKER_VOLUME_PCT = None if _vol in ("off", "none", "-1") else max(0, min(100, _env_int("SPEAKER_VOLUME_PCT", 100)))

# XVF3800 캡처 2채널: ch0=Conference(듣기 좋은) / ch1=ASR(음성인식용).
# 1채널로 열면 ALSA 가 둘을 평균내 섞어 어느 쪽 장점도 남지 않는다.
INPUT_CHANNELS = _env_int("INPUT_CHANNELS", 2)
INPUT_PICK_CHANNEL = _env_int("INPUT_PICK_CHANNEL", 1)
SAMPLE_RATE = _env_int("SAMPLE_RATE", 16000)   # Whisper 권장

# 발화 감지(VAD) — 에너지 기반. 현장 소음에 맞춰 조정한다.
VAD_THRESHOLD = _env_float("VAD_THRESHOLD", 0.015)   # RMS 임계값의 하한
# 문턱은 주변 소음의 이 배수 이상으로 자동으로 올라간다(모터·팬 소음 대응 — audio.Endpointer).
# XVF3800 ASR 채널(ch1)은 AGC 가 소음까지 키워 말소리와의 차이가 작다. 실측(2026-09-23, 296):
# 정지 상태 소음 p50 0.026 / 최대 0.035. 2.5배(0.064)에선 말소리를 못 잡아 1.8배(0.046)로 둔다.
# ⚠ 1.5 이하로 내리지 말 것. 종료 문턱이 `문턱 × 0.8 = 소음 × ratio × 0.8` 이라
#   ratio 1.3 이면 소음의 1.04배가 되어 소음이 조금만 튀어도 silent_run 이 리셋되고
#   **말이 끝나도 끝났다고 보지 못해 VAD_MAX_SEC 까지 끌려간다.**
#   실측 시뮬레이션(발화+팬 8초, HPF 150Hz 적용):
#     ratio 1.3 / 1.5 → 종료 못함 (최대길이)   ·   1.8 → 6.5초 정상 종료   ·   2.2 → 1.9초
VAD_NOISE_RATIO = _env_float("VAD_NOISE_RATIO", 1.8)
# 이만큼 조용하면 발화 끝. 짧을수록 반응이 빠르고, 너무 짧으면 말 중간 쉼에서 끊긴다.
VAD_SILENCE_SEC = _env_float("VAD_SILENCE_SEC", 0.8)
# 예/아니요·"준비됐어요" 같은 짧은 대답 단계(expectedIntent=YES_NO)만 쓰는 값 — 말 끝 판정을 이만큼 앞당긴다.
# 이동 중 동행 확인("잘 따라오고 계신가요?")은 걷는 소음 때문에 VAD_SILENCE_SEC 그대로 둔다.
# 짧은 대답은 중간에 쉬는 일이 드물다(2026-10-28 실제 녹음 재생 시뮬). 0 이면 VAD_SILENCE_SEC 를 쓴다.
# 2026-11-03: 0.5 → 0.4초. 예/아니요 녹음 168건 재생에서 실제 말 잘림 0(걸린 1건은 받아쓰기 빈 잡음), 0.12초 빨라짐.
YES_NO_SILENCE_SEC = _env_float("YES_NO_SILENCE_SEC", 0.4)
# 운영자가 넣는 대기번호·창구 번호 고정값(2026-10-28 결정) — 관제 화면 입력칸 기본값과 같게 둔다.
# 이 값으로 채운 멘트("고객님의 대기번호는 10번입니다.")를 목적지 멘트와 함께 시작할 때 미리 합성한다.
FIXED_TICKET_NO = _env("FIXED_TICKET_NO", "10")
FIXED_COUNTER_NO = _env("FIXED_COUNTER_NO", "1")
VAD_MAX_SEC = _env_float("VAD_MAX_SEC", 15.0)         # 최대 녹음 길이
VAD_MIN_SEC = _env_float("VAD_MIN_SEC", 0.4)          # 이보다 짧으면 잡음으로 버림
LISTEN_TIMEOUT_SEC = _env_float("LISTEN_TIMEOUT_SEC", 20.0)  # 발화 시작 대기

# --- 저역 소음 대책 (팬·모터) — 둘 다 0 이면 기존 동작 그대로 -------------
# 회전 기계 소음은 저역에 몰려 있다. 실측(2026-09-30, 296 로봇 마이크 무음 5초):
#   <300Hz 75.3% · 300-3400Hz 24.4% · >3400Hz 0.2% · 스펙트럼 피크 197Hz · CV 0.306
# 이 주파수 미만을 **판정용 레벨 계산에서만** 빼면 소음 바닥이 절반으로 내려가고
# 문턱(소음 × VAD_NOISE_RATIO)도 함께 내려가, 같은 발화가 문턱을 넉넉히 넘는다.
# 저장해서 STT 로 보내는 오디오에는 걸지 않는다 — 원본 그대로 보낸다.
#
# 150Hz 는 실제 녹음(8초, 발화 2회 + 팬)으로 스윕해 고른 값이다. ratio 만 보면 더 높은
# 컷오프가 유리해 보이지만 음성 에너지도 같이 깎여 실질이 나빠진다:
#   컷오프   bg        문턱넘은프레임  maxRun
#   없음     0.04047   145/266        0.51s
#   150Hz    0.01928   131/266        0.45s   ← bg 절반, 발화는 유지
#   300Hz    0.00963    67/266        0.42s
#   700Hz    0.00781    22/266        0.21s   ← ratio 최고(9.4)인데 발화가 반토막
VAD_HPF_HZ = _env_float("VAD_HPF_HZ", 150.0)
# 발화 시작 판정에 **에너지 변동** 조건을 더한다. 회전 기계 소음은 평탄하고(실측 CV 0.306)
# 사람 말은 음절 강약으로 들쭉날쭉하다(0.7 이상, 선행 구현 기록). 문턱을 넘어도 최근 창의
# 변동계수가 이 값 미만이면 말로 보지 않는다 — 팬 소음이 VAD_MAX_SEC 까지 녹음을 끌고 가
# 그 안에 섞인 실제 발화까지 함께 버려지는 것을 막는다(2026-09-30 실측: 15초 덩어리 2건).
# **기본 비활성(0).** HPF 150Hz 만으로 팬-only 구간의 오탐이 0 이 됐으므로(시뮬레이션:
# 팬만 녹음 5초 → 시작 0회) 켤 필요가 없었다. 참고 실측값은 아래와 같다 —
# 소음이 더 심해져 HPF 로 부족할 때 0.45 정도로 켜면 된다.
#   발화 시작 순간의 CV 0.92   ·   팬만 있는 구간에서는 시작 자체가 안 걸림
# ⚠ 대화가 이어져 발화 **중간**에 재시작되는 경우 창이 전부 큰 소리라 CV 가 낮아져
#   거절될 수 있다. 켜기 전에 그 경우를 재볼 것.
VAD_MIN_CV = _env_float("VAD_MIN_CV", 0.0)
VAD_CV_WINDOW = _env_int("VAD_CV_WINDOW", 16)   # CV 를 재는 최근 블록 수 (16 = 480ms)

# --- 발화 앞부분 보존 ------------------------------------------------------
# 말 시작을 감지한 **이전** 구간을 이만큼 앞에 붙여 STT 로 보낸다.
# 문턱을 넘기까지 시간이 걸리는 음절(약한 첫 자음)이 그 사이 지나가 버린다.
# 실측(2026-09-30): "정형외과 안내해줘"(약 2.0초)가 **1.4초만 녹음**되고 STT 가
#   "안내해줘" 만 냈다 — 앞 0.6초 이상이 잘렸다. 300ms 로는 부족하다.
# 길게 잡으면 발화 전 잡음이 앞에 붙지만, 품질 필터가 받아낸다.
VAD_PREROLL_SEC = _env_float("VAD_PREROLL_SEC", 0.7)
# 문턱을 이만큼 **연속으로** 넘어야 말 시작으로 본다. 짧으면 딸깍·쿵 한 번에 녹음이
# 열리고, 길면 시작이 늦어 앞부분이 잘리거나 **짧은 대답을 아예 놓친다.**
# 실측(2026-09-30): 0.09(3블록)에서 확인 질문의 "네" 를 세 번 연속 놓쳤다 —
#   말 시작 감지 못함 (최대 음량 0.0184, 문턱 0.0169, **연속 1/3**)
#   "네" 는 0.2초짜리 단음절이라 문턱을 간신히 넘는 레벨에서 3블록이 이어지지 않는다.
#   그 때문에 안내가 마지막 확인 단계에서 aborted_no_response 로 끊겼다.
VAD_ONSET_SEC = _env_float("VAD_ONSET_SEC", 0.06)

# --- 예/아니요 대답 ----------------------------------------------------------
# 예/아니요를 묻는 단계(확인 질문·순서 안내·동행 확인)는 듣는 방식을 따로 둔다.
# 2026-10-08 실측(예/아니요 듣기 23회, 성공 13): 목적지 이름 힌트가 "네" 를 "뇌"·"뱅" 으로
# 끌고, 말이 없는 구간에서 "기초연금 입구, 기초연금 입구, …" 를 지어냈다. 그리고 소음 때문에
# 말 끝을 못 찾아 "네" 한 마디에 녹음이 15초까지 갔다(22개 중 9개).
# 같은 녹음을 이 설정으로 다시 전사하면 13 → 17(판정 단어 보강까지 18), 대답 후 반응
# 평균 11.0초 → 5.7초 — docs/분석보고_2026-10-08_예아니요_인식률_원인·개선방안.md
# 힌트: 목적지 이름 대신 이 말을 STT 에 준다. 빈 값이면 힌트 없이 전사한다.
STT_YES_NO_HINT = _env("STT_YES_NO_HINT", "네, 아니요")
# 신청서 작성 대기(FORM_COMPLETE)의 힌트 — 기본은 힌트 없음("").
# 목적지 이름을 주면 "작성 완료"를 "기초연금"으로 받아썼고(2026-10-30 관공서 form_write 5번 중 3번),
# "작성 완료"를 주면 다른 말·잡음도 "작성 완료"로 받아써 통과했다(같은 날 16:51 "잘 못 들었어요" → "작성 완료").
# 그날 녹음 13개: 힌트 없음이면 실제 작성 완료 5개 모두 맞고 다른 말 8개는 하나도 통과하지 않았다.
STT_FORM_DONE_HINT = _env("STT_FORM_DONE_HINT", "")
# 힌트 없이 전사한 결과가 품질 미달로 버려졌을 때만 이 힌트로 한 번 더 전사한다(빈 값이면 하지 않음).
# 작은 목소리의 "작성 완료"가 '속상하네요'·'저 스탠바디요' 로 받아써져 버려졌다(2026-11-02 세션 520, 4번 연속).
# 녹음 320개 재전사: 실제 작성 완료 12/18 → 17/18, 다른 단계 녹음 285개 오통과 0(하한 -0.6).
# 힌트를 처음부터 주면 같은 녹음에서 오통과 11개("준비됐어요" → "작성 완료")라 첫 전사는 힌트 없이 둔다.
STT_FORM_DONE_RETRY_HINT = _env("STT_FORM_DONE_RETRY_HINT", "작성 완료")
# 재전사 결과의 구간 확신도(logprob) 하한 — 이보다 낮은 구간이 있으면 버린다(-0.7 부터 오통과 1).
FORM_DONE_RETRY_MIN_LOGPROB = _env_float("FORM_DONE_RETRY_MIN_LOGPROB", -0.6)
# 녹음 최대 길이(초, 말 시작 감지 뒤). 15초 녹음 9개 모두 대답이 앞 4초 안에 있었다.
# 배경이 문턱을 늘 넘는 곳에서는 소음으로 녹음이 시작돼 상한에서 끊긴다. 예전에는 그때마다
# STT 동안(약 2초) 마이크가 닫혀 듣는 시간의 32% 를 놓쳤다(세션 277, 임시로 8초로 늘렸었다).
# 지금은 STT 중에도 다음 녹음을 이어 가므로(main.listen) 짧게 둔다. 실시간 시뮬레이션
# (대답 시점 38곳): 4초 38/38 · 반응 평균 3.6초, 8초 38/38 · 6.0초.
# 2026-11-03 메모(준비됐어요 반응): 예/아니요 단계 실제 녹음 168건 재생 — 3초 상한 도달 1건(받아쓰기 빈 잡음)·말 잘림 0,
# 2초는 상한 도달 3건이라 3초로 줄였다(_자료/2026-11-03_준비됐어요_녹음단축/run_ready_cut.py).
YES_NO_MAX_SEC = _env_float("YES_NO_MAX_SEC", 3.0)
# 로컬 STT 생성 길이 상한(토큰). 말이 없는 녹음에서 힌트를 되풀이하느라 전사가 9초 걸리던 것을
# 막는다 — 같은 녹음 22개를 24 토큰으로 묶어도 판정 결과는 같고, 최대 9.1초 → 1.9초.
# 0 이면 제한하지 않는다. 이 값을 모르는 예전 stt_server 는 무시한다.
YES_NO_MAX_TOKENS = _env_int("YES_NO_MAX_TOKENS", 24)
# 품질 필터(no_speech > 0.6)가 버린 구간이라도 **반복 없는 한 마디 예/아니요**이고 no_speech 가
# 이 값 이하면 대답으로 받는다(예/아니요 단계만). 2026-10-08 세션 279: 유일한 실제 대답 "네" 가
# no_speech 0.67 로 버려져 무응답 종료됐다. 같은 날 예/아니요 녹음 56개 시뮬레이션에서 실제 대답
# 18 → 19/19, 소음 오인 0/28(소음은 "네, 네, 네…" 반복형으로만 나와 걸리지 않음).
# 0 이면 끈다.
# --- 목적지 대답 ------------------------------------------------------------
# 목적지를 묻는 단계(PURPOSE·DESTINATION)의 녹음 상한과 전사 생성 길이. 소음으로 말 끝을 못 찾으면
# 녹음이 15초까지 가고, 긴 잡음 구간에서 힌트 단어를 되풀이하느라 전사가 8초 넘게 걸렸다.
# 병원 시나리오 시험(대답 120건, 실제 로봇 소음): 정확도 108 → 108, 녹음+전사 15.8초 → 8.7초.
# docs/분석보고_2026-10-08_예아니요_인식률_원인·개선방안.md §11
DEST_MAX_SEC = _env_float("DEST_MAX_SEC", 8.0)
DEST_MAX_TOKENS = _env_int("DEST_MAX_TOKENS", 48)

YES_NO_RESCUE_MAX_NO_SPEECH = _env_float("YES_NO_RESCUE_MAX_NO_SPEECH", 0.75)
YES_NO_RESCUE_MIN_LOGPROB = _env_float("YES_NO_RESCUE_MIN_LOGPROB", -1.3)

# --- 듣기 시작 신호음 -------------------------------------------------------
# 로봇이 질문을 마치고 마이크를 열 때 짧은 "띵" 을 낸다. 언제 말해야 하는지 알려주지
# 않으면 질문 도중(마이크가 닫혀 있다)이나 듣기를 시작하기 전에 대답해 말이 잘리거나
# 아예 안 잡힌다. 2026-09-28~10-07 시나리오 세션 118건 중 목적 질문에서 들은 말 없이
# 끝난 것이 29건이었다(무응답 종료 11 + 운영자 종료 18).
# 질문 한 번에 한 번만 울린다 — 로봇이 말한 뒤 첫 듣기에서만(main.listen).
#   1 = 사용(기본)   0 = 끔
LISTEN_CUE_ENABLED = _env("LISTEN_CUE_ENABLED", "1") == "1"
LISTEN_CUE_HZ = _env_float("LISTEN_CUE_HZ", 880.0)
LISTEN_CUE_SEC = _env_float("LISTEN_CUE_SEC", 0.15)
# 0~1. TTS 멘트보다 작게 — 알림이지 말이 아니다.
LISTEN_CUE_VOLUME = _env_float("LISTEN_CUE_VOLUME", 0.25)
# 신호음이 끝나고 마이크를 열기까지 쉬는 시간. 스피커와 마이크 사이에 에코 제거가 없어
# 바로 열면 신호음 울림을 말 시작으로 잡거나 소음 바닥으로 배울 수 있다.
LISTEN_CUE_GAP_SEC = _env_float("LISTEN_CUE_GAP_SEC", 0.1)

# --- 말은 했는데 못 알아들었을 때 바로 되묻기 -----------------------------------
# 녹음에서 음성 대역(300~3400Hz) 소리가 이만큼(초) 이상인데 STT 가 비거나 버려졌으면
# "말은 했는데 못 알아들은 것" 으로 보고, 대기 시간이 남아 있어도 듣기를 멈추고 되묻는다.
# 대기가 긴 단계(lobby_ask 120초)는 예전에는 대기 시간이 다 갈 때까지 아무 말이 없었다
# (2026-10-11 세션 308: 95초 동안 실패 21회, "잘 못 들었어요" 0회).
# 실측(같은 날 녹음 39개): 몸체 저음만 든 녹음 0~0.06초 · 뭉개진 실제 대답 0.45초 ·
# 가장 짧은 성공 대답("아니요") 0.21초. 0 이면 끈다.
REASK_VOICED_SEC = _env_float("REASK_VOICED_SEC", 0.15)
# 한 번의 대기에서 위 이유로 되묻는 최대 횟수. 넘으면 남은 시간은 되묻지 않고 듣기만 한다.
REASK_GARBLED_MAX = _env_int("REASK_GARBLED_MAX", 2)

# --- 스피커 출력 -------------------------------------------------------------
# 1 이면 출력 스트림을 한 번 열어 계속 열어 두고 멘트를 이어 흘린다(audio._Player). 이 USB 스피커는
# 스트림을 닫은 직후 다시 열면 앞부분을 버려, 이어지는 멘트("잘 못 들었어요" + 질문 등)의 앞이
# 잘렸다 — 2026-10-11 실측: 간격 0초면 뒤 소리의 1~4% 만 나왔고 0.6초는 지나야 정상.
# 0 이면 예전처럼 멘트마다 sd.play 로 열고 닫는다(문제가 생기면 되돌리는 스위치).
PLAY_KEEP_STREAM = _env("PLAY_KEEP_STREAM", "1") == "1"

# --- 스피커 깨우기 ------------------------------------------------------------
# 멘트 앞에 이만큼(초) 무음을 붙여 재생한다. USB 스피커(Jieli UACDemo)는 재생할 때마다 스트림을
# 새로 연다 — 오래 쉰 앰프가 깨는 동안 첫 음절이 잘리면 켠다. TTS 파일은 말이 0~40ms 에 바로
# 시작해 앞 여유가 없다. 2026-10-11 실측(2~3분 쉰 뒤 1kHz 신호음을 마이크로 재기): 재생 지연
# 170ms 로 쉰 뒤·바로 다시가 같았고, 첫 30ms 뒤로는 소리가 온전했다 — 그 조건에서는 잘림이
# 재현되지 않아 기본은 끈다(0). 켜면 멘트마다 그만큼 늦어진다.
PLAY_LEAD_SEC = _env_float("PLAY_LEAD_SEC", 0.0)

# --- 진단 기록 --------------------------------------------------------------
# 듣기 한 번마다 결과(감지 실패·품질로 버림·환각·채택)와 레벨을 turns.jsonl 에 남기고,
# 녹음이 있으면 wav 도 저장한다. 세션이 무응답으로 끝났을 때 "말을 안 했다 / 했는데
# 못 잡았다 / 잡았는데 STT 가 버렸다" 를 가르려면 녹음과 레벨이 필요하다 — DB 의
# service_session.Heard 는 마지막 한 마디만 남아 구별할 수 없었다.
# ⚠ 방문자 음성(진료 목적 등 민감할 수 있는 말)이 Jetson 에 남는다. 외부로 보내지 않고
#   보관 기한·용량을 넘으면 오래된 것부터 지운다. 필요 없으면 VOICE_DIAG=0.
DIAG_ENABLED = _env("VOICE_DIAG", "1") == "1"
# wav 저장 여부. 0 이면 결과·레벨만 남긴다.
DIAG_SAVE_WAV = _env("VOICE_DIAG_WAV", "1") == "1"
DIAG_DIR = _env("VOICE_DIAG_DIR") or os.path.join(os.path.dirname(BUNDLE_CACHE), "diag")
DIAG_KEEP_DAYS = _env_int("VOICE_DIAG_KEEP_DAYS", 7)
# wav 총량 상한(MB). 16kHz 모노라 1초에 32KB — 500MB 면 약 4시간 분량이다.
DIAG_MAX_MB = _env_float("VOICE_DIAG_MAX_MB", 500.0)

# ── 웨이크워드 ────────────────────────────────────────
# 호출어 없이 상시 반응하면 옆 사람 대화까지 받아 파이프라인을 점유한다.
# (선행 구현 실측: 15분간 채택 54건이 전부 옆 대화)
WAKE_ENABLED = _env("WAKE_ENABLED", "1") == "1"

# 변형을 넉넉히 둔다 — STT 가 "큐리야"를 "쿠리야/규리야"로 받아쓰는 것을 흡수한다.
# 단, Whisper 프롬프트에는 대표형 하나만 넣는다(아래 STT_PROMPT 주석 참고).
WAKE_WORDS = [
    w.strip()
    for w in _env("WAKE_WORDS", "큐리야,큐리,쿠리야,규리야,큐리아,키리야,로봇아,안내로봇,안내 로봇").split(",")
    if w.strip()
]

# 호출 후 이 시간 동안은 호출어 없이 대화를 이어간다. 말할 때마다 연장된다.
WAKE_WINDOW_SEC = _env_float("WAKE_WINDOW_SEC", 12.0)

# 호출만 하고 용건이 없을 때의 응답 ("큐리야" -> "네, 말씀하세요.")
WAKE_ACK = _env("WAKE_ACK", "네, 말씀하세요.")

# ── 단순 동작 응대("큐리야, 앉아 / 일어서") — motion_cmd.py ─────────
# 관제 상시 대기 작업의 응대 방식이 '단순 동작'(responseMode=motion)일 때만 쓴다. 스위치 파일은 없다.
# 말 시작 뒤 최대 녹음 길이 — "큐리야 앉아" 는 2초 안팎이다
MOTION_MAX_SEC = _env_float("MOTION_MAX_SEC", 4.0)
# "큐리야" 만 부르고 멈췄을 때 "네, 말씀하세요" 뒤 명령을 기다리는 시간
MOTION_FOLLOW_SEC = _env_float("MOTION_FOLLOW_SEC", 6.0)
# 명령을 보낸 뒤 로봇 자세가 그 자세가 될 때까지는 다음 명령을 "동작 중" 으로 거절한다(계속 듣기는 한다).
# 이만큼 지나도 자세가 안 바뀌면 명령을 안 먹은 것으로 보고 다시 받는다.
MOTION_ACTION_TIMEOUT_SEC = _env_float("MOTION_ACTION_TIMEOUT_SEC", 10.0)
# 로봇 안 receiver(NOS). 관제 .env 의 RECEIVER_IP/RECEIVER_PORT 와 같은 값.
RECEIVER_IP = _env("RECEIVER_IP", "10.21.31.106")
RECEIVER_PORT = _env_int("RECEIVER_PORT", 40000)
RECEIVER_TIMEOUT_SEC = _env_float("RECEIVER_TIMEOUT_SEC", 1.5)

# ── 상시대기 음성 시작 — main._standby_listen_once ─────────
# 관제에 '마이크' 호출 방식 상시대기 근무가 있고 대기 장소에 도착해 있으면, 세션이 없는 동안
# "큐리야 …" 를 듣고 관제(/DB/agent/standby-call)에 올린다. 근무가 없는 시간(mode=free)에는 듣지 않는다.
STANDBY_LISTEN_ENABLED = _env("STANDBY_LISTEN_ENABLED", "1") == "1"
# 한 번에 듣는 시간. 끝날 때마다 관제 세션을 확인하므로 화면 [안내 시작] 이 이만큼까지 늦을 수 있다.
STANDBY_LISTEN_SEC = _env_float("STANDBY_LISTEN_SEC", 5.0)
# 단발 응대 근무에서 "큐리야" 만 불렀을 때 묻는 말과 대답을 기다리는 시간
STANDBY_ASK = _env("STANDBY_ASK", "네, 어디로 안내해 드릴까요?")
STANDBY_ASK_WAIT_SEC = _env_float("STANDBY_ASK_WAIT_SEC", 8.0)
# 관제가 호출을 거절했을 때(다른 맵·응대 중 등) 손님에게 하는 말
STANDBY_REJECT = _env("STANDBY_REJECT", "지금은 안내를 시작할 수 없어요. 직원에게 문의해 주세요.")
# 대기 장소 안내 멘트 — 손님은 호출어를 모르니 듣기가 켜질 때(근무 시작·응대 뒤 복귀) 말하고 간격마다 되풀이한다.
# 응대 방식별(시나리오/단발). 비우면 그 방식은 말하지 않는다.
STANDBY_INVITE_SCENARIO = _env("STANDBY_INVITE_SCENARIO", "도움이 필요하시면 '큐리야'라고 불러 주세요.")
STANDBY_INVITE_SINGLE = _env("STANDBY_INVITE_SINGLE",
                             "길 안내가 필요하시면 '큐리야'라고 부르고 가실 곳을 말씀해 주세요.")
# 단순 동작 근무(앉아/일어서만)의 안내 멘트
STANDBY_INVITE_MOTION = _env("STANDBY_INVITE_MOTION",
                             "'큐리야, 앉아' 또는 '큐리야, 일어서'라고 말씀해 보세요.")
# 되풀이 간격(초). 0 이면 듣기가 켜질 때 한 번만 말한다.
STANDBY_INVITE_SEC = _env_float("STANDBY_INVITE_SEC", 120.0)

# ── 시나리오 ──────────────────────────────────────────
ARRIVAL_POLL_SEC = _env_float("ARRIVAL_POLL_SEC", 1.0)
ARRIVAL_TIMEOUT_SEC = _env_float("ARRIVAL_TIMEOUT_SEC", 300.0)
MAP_ID = _env_int("MAP_ID", 0) or None   # POI 를 특정 맵으로 제한할 때
SESSION_IDLE_SEC = _env_float("SESSION_IDLE_SEC", 180.0)  # 무응답 시 세션 종료


def validate() -> list[str]:
    """필수 설정 누락을 모아서 돌려준다."""
    missing = []
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY (또는 OPEN_AI_SECRET_KEY)")
    if not AGENT_TOKEN:
        missing.append("AGENT_TOKEN")
    if not ROBOT_ID:
        missing.append("ROBOT_ID")
    if not BACKEND_BASE:
        missing.append("BACKEND_BASE")
    return missing
