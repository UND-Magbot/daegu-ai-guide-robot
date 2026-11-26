"""OpenAI 기반 STT / TTS / 목적지 판정."""

import hashlib
import json
import logging
import os
import re
import time
from typing import Optional

import requests

from openai import OpenAI, Timeout

from robot_voice import config

log = logging.getLogger("robot_voice.ai")

_client: Optional[OpenAI] = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            timeout=Timeout(config.OPENAI_READ_TIMEOUT, connect=config.OPENAI_CONNECT_TIMEOUT),
            max_retries=config.OPENAI_MAX_RETRIES,
        )
    return _client


# ── STT ───────────────────────────────────────────────
def _stt_prompt(places: list[dict] | None) -> str:
    """Whisper 에 줄 어휘 힌트.

    호출어는 Whisper 가 학습한 적 없는 고유명사라 힌트가 필요하다.
    선행 구현의 실측 A/B(2026-08-07, 동일 오디오 3건):
        프롬프트에 없을 때 -> 퓨리야 / 퓨리약 / 퓨리야   0/3
        대표형을 넣었을 때 -> 큐리야 / 큐리야 / 큐리야   3/3

    주의 두 가지:
    - 변형(쿠리야·규리야…)까지 다 넣지 말 것. 힌트가 흐려진다. 대표형 하나만 넣고
      변형 흡수는 wakeword.py 의 사전이 맡는다.
    - 문장으로 주지 말 것. 잡음 구간에서 그 문장을 통째로 지어낸다. 단어 나열이면
      어휘 편향은 유지하면서 '받아쓸 문장'으로는 덜 쓰인다.
    - **목적지 이름은 넣지 않는다**(config.STT_HINT_PLACES, 기본 off). 단어 나열이어도
      새어나온다 — 2026-09-29 실측: 길이가 다른 세 녹음(1.1s·8.5s·15.0s)이 전부
      "큐리야, 정형외과, 안과" 였다. **오디오가 다른데 결과가 같으면 오디오에서 나온
      결과가 아니다.** 그 이름이 직후의 사전 직매칭에 걸려 말하지 않은 목적지로
      확인 질문까지 갔다. 목적지 어휘는 이미 뒤 단계(사전 직매칭 · LLM 후보 목록)가
      갖고 있으므로 STT 에 또 주는 것은 중복이고, 그 중복이 환각을 명령으로 바꾼다.
    """
    words: list[str] = []
    if config.WAKE_ENABLED and config.WAKE_WORDS:
        words.append(config.WAKE_WORDS[0])
    if config.STT_HINT_PLACES and places:
        words += [p["name"] for p in places if p.get("name")]
    return ", ".join(words)


def _norm_words(s: str) -> list[str]:
    """비교용 정규화 — 문장부호를 떼고 어절 목록으로."""
    return [w for w in re.split(r"[\s,·/]+", re.sub(r"[.!?~]", "", s or "")) if w]


def is_hint_echo(text: str, places: list[dict] | None) -> bool:
    """STT 결과가 어휘 힌트를 그대로 되풀이한 것인지.

    STT_HINT_PLACES 를 켜고 A/B 할 때의 안전장치다. 기본(off)에서는 힌트에 목적지가
    없으므로 거의 걸리지 않는다.

    판별 근거는 **방문자는 진료과를 하나만 말한다** 는 것이다. 목적지가 둘 이상
    한꺼번에 나오고 그 밖의 말이 없으면 사람의 대답이 아니다.

    is_hallucination 의 음절 heuristic 으로는 못 잡는다 — "큐리야, 정형외과, 안과" 는
    9음절이라 초당 음절 조건(10.0)에도, syllables > 12 조건에도 미달한다.

    목적지 이름은 **어절 묶음**으로 맞춘다. 어절 하나씩 비교하면 "엘리베이터 입구" 처럼
    띄어쓰기가 있는 이름이 끼는 순간 판정이 통째로 빠진다 — 2026-10-07 실측: 힌트
    그대로인 "정형외과, 안과, 엘리베이터 입구, 3층관공서" 가 통과해 말하지 않은
    '엘리베이터 입구' 로 확인 질문까지 갔다.
    """
    names = [p["name"] for p in (places or []) if p.get("name")]
    if len(names) < 2:
        return False      # 목적지가 1개면 정상 답변과 구별할 방법이 없다
    wake = config.WAKE_WORDS[0] if (config.WAKE_ENABLED and config.WAKE_WORDS) else None
    body = [w for w in _norm_words(text) if w != wake]
    if not body:
        return False
    # 긴 이름부터 맞춰야 "입구" 같은 짧은 이름이 "엘리베이터 입구" 의 일부를 먼저 먹지 않는다
    seqs = sorted({tuple(_norm_words(n)): n for n in names}.items(), key=lambda kv: -len(kv[0]))
    hits: set[str] = set()
    i = 0
    while i < len(body):
        for seq, name in seqs:
            if seq and tuple(body[i:i + len(seq)]) == seq:
                hits.add(name)
                i += len(seq)
                break
        else:
            return False  # 목적지 이름이 아닌 어절이 있다 — 사람의 말일 수 있다
    return len(hits) >= 2


def _stt_hint_words(places: list[dict] | None) -> str:
    """로컬 STT 에 줄 어휘 힌트 — 호출어 대표형 + 목적지 이름.

    OpenAI 경로의 _stt_prompt() 와 달리 목적지를 **기본으로 포함**한다. 그쪽은 유료라
    환각 한 번의 비용이 크고 최대길이 폐기까지 걸려 있어 보수적으로 껐지만, 로컬은
    무료이고 에코 환각은 is_hint_echo() 가 걸러낸다. 정확도 쪽 이득이 크다.
    """
    words: list[str] = []
    if config.WAKE_ENABLED and config.WAKE_WORDS:
        words.append(config.WAKE_WORDS[0])
    if places:
        words += [p["name"] for p in places if p.get("name")]
    return ", ".join(words)


def _transcribe_local(wav: bytes, places: list[dict] | None = None,
                      prompt: str | None = None, max_tokens: int = 0) -> dict:
    """로컬 STT 서버로 전사한다 — OpenAI 크레딧을 쓰지 않는다.

    2026-09-30 크레딧 소진으로 STT 가 멈췄을 때 만든 우회로다. Jetson 에 구 시스템의
    faster-whisper small(CUDA·float16)이 남아 있고, 같은 오디오를 정확히 전사한다
    (실측 1m 정면: "눈이 아파서 왔어" no_speech 0.069 · logp -0.516).

    어휘 힌트는 **보낸다**(config.STT_LOCAL_HINT, 기본 on). 짧은 단어는 힌트 없이는
    맞추기 어렵다 — 실측(2026-09-30): 힌트 없이 "안과" → "안좋아" 가 나와 사전 직매칭이
    실패하고 LLM(크레딧 필요)까지 갔다. 힌트의 부작용인 **무음 구간 에코 환각**은
    is_hint_echo() 가 잡는다(목적지 2개 이상 + 힌트 밖 어절 없음 → 버림).
    """
    data = {}
    if max_tokens > 0:
        data["max_tokens"] = str(max_tokens)
    if prompt is not None:
        if prompt:
            data["prompt"] = prompt
    elif config.STT_LOCAL_HINT:
        hint = _stt_hint_words(places)
        if hint:
            data["prompt"] = hint
    r = requests.post(config.STT_LOCAL_URL,
                      files={"file": ("utterance.wav", wav, "audio/wav")},
                      data=data,
                      timeout=config.STT_LOCAL_TIMEOUT)
    r.raise_for_status()
    return r.json()


def transcribe(wav: bytes, places: list[dict] | None = None) -> str:
    """발화 WAV 를 한국어 텍스트로. places 를 주면 어휘 힌트로 쓴다."""
    return transcribe_ex(wav, places)[0]


def transcribe_ex(wav: bytes, places: list[dict] | None = None,
                  prompt: str | None = None, max_tokens: int = 0) -> tuple[str, dict]:
    """transcribe 와 같고, 진단용 정보를 함께 준다.

    prompt 를 주면 places 로 만드는 어휘 힌트 대신 그 말을 힌트로 쓴다("" 면 힌트 없음).
    예/아니요 단계는 목적지 이름이 해롭다 — config.STT_YES_NO_HINT.
    max_tokens 는 로컬 STT 의 생성 길이 상한이다(0 이면 제한 없음, OpenAI 경로는 쓰지 않음).

    Returns:
        (텍스트, {"engine": "local" | 모델명, "ms": 왕복 ms,
                  "server_ms": 로컬 서버 전사 ms, "dropped": 품질로 버린 세그먼트 수})
        로컬 서버만 server_ms·dropped 를 준다. 텍스트가 비었는데 dropped > 0 이면
        "말은 들어왔지만 품질 필터가 버렸다" 는 뜻이다.
    """
    t0 = time.monotonic()
    # 로컬 서버가 지정돼 있으면 그쪽으로. 비어 있으면 아래 OpenAI 경로(기본).
    if config.STT_LOCAL_URL:
        body = _transcribe_local(wav, places, prompt, max_tokens)
        return (body.get("text") or "").strip(), {
            "engine": "local", "ms": int((time.monotonic() - t0) * 1000),
            "server_ms": body.get("ms"), "dropped": body.get("dropped"),
            "dropped_segments": body.get("dropped_segments") or [],
            "segments": body.get("segments") or []}
    text = _transcribe_openai(wav, places, prompt)
    return text, {"engine": _stt_model, "ms": int((time.monotonic() - t0) * 1000)}


def _transcribe_openai(wav: bytes, places: list[dict] | None, prompt: str | None = None) -> str:
    """OpenAI 전사. 기본 모델을 계정에서 못 쓰면(권한·미지원) 예비 모델로 한 번 더 시도한다 —
    모델 이름 하나 때문에 음성 전체가 멈추면 안 된다.
    """
    kwargs = {}
    if prompt is None:
        prompt = _stt_prompt(places)
    if prompt:
        kwargs["prompt"] = prompt

    global _stt_model
    try:
        resp = client().audio.transcriptions.create(
            model=_stt_model, file=("utterance.wav", wav, "audio/wav"), language="ko", **kwargs)
    except Exception as e:
        fallback = config.STT_FALLBACK_MODEL
        if not fallback or fallback == _stt_model:
            raise
        log.warning("STT 모델 %s 실패 → %s 로 전환: %s", _stt_model, fallback, e)
        _stt_model = fallback
        resp = client().audio.transcriptions.create(
            model=_stt_model, file=("utterance.wav", wav, "audio/wav"), language="ko", **kwargs)
    return (resp.text or "").strip()


_stt_model = config.STT_MODEL

# Whisper 계열이 무음·소음 구간에서 지어내는 단골 문장. 학습 데이터(유튜브 자막)의 흔적이다.
# 낱말이 아니라 구절로 둔다 — "좋아요"(예 대답)·"영상의학과"(진료과) 같은 진짜 대답을 버리면 안 된다.
_HALLUCINATIONS = ("영상은 여기까지", "다음 영상", "시청해 주셔서", "시청해주셔서", "구독과 좋아요",
                   "구독 부탁", "자막 제공", "MBC 뉴스", "KBS 뉴스")
# 같은 구절이 이만큼 이상 되풀이되면 지어낸 것이다. 2026-10-08 실측: 힌트 단어를 이어 붙인
# "기초연금 입구, 기초연금 입구, …" 가 초당 음절·힌트 되풀이 판정을 모두 통과했다(7건).
# 과거 운영 발화 171개에 적용하면 걸리는 것은 "헬스, 헬스, …" 같은 환각 3개뿐이었다.
_REPEAT_MIN = 3
# 한국어 발화는 초당 7~8음절 안팎이다. 넉넉히 잡아도 이보다 빽빽하면 녹음보다 긴 글을 지어낸 것이다.
_MAX_CHARS_PER_SEC = 10.0


# 품질 필터가 버린 구간에서 살려 쓸 수 있는 짧은 대답(띄어쓰기·문장부호를 뺀 형태)
_SHORT_ANSWERS = ("네", "예", "아니요", "아니오", "아뇨", "아니에요", "맞아요", "네네", "네맞아요")


def rescue_short_answer(dropped_segments: list[dict], max_no_speech: float,
                        min_logprob: float) -> str | None:
    """버려진 구간 중 살릴 짧은 예/아니요. 없으면 None.

    조건: 반복 없는 한 마디(같은 어절이 두 번 이상이면 안 됨 — 소음에서 지어낸 "네, 네, 네…")
    이고 no_speech ≤ max_no_speech, avg_logprob ≥ min_logprob. max_no_speech 가 0 이면 끈다.
    """
    if max_no_speech <= 0:
        return None
    for seg in dropped_segments or []:
        text = (seg.get("text") or "").strip()
        words = [w for w in (re.sub(r"[^가-힣]", "", x) for x in re.split(r"[,.\s]+", text)) if w]
        if not words or len(set(words)) != len(words) or "".join(words) not in _SHORT_ANSWERS:
            continue
        if seg.get("no_speech", 1.0) <= max_no_speech and seg.get("logprob", -9.0) >= min_logprob:
            return text
    return None


# 방문자가 하지 않는 말 — 로봇 멘트가 섞여 나오면 소음·힌트에서 지어낸 것이다.
# 2026-10-08 세션 280: "3층관공서에 도착했습니다." 를 목적지 대답으로 받아 엉뚱한 곳으로 진행했다.
_ROBOT_PHRASES = ("도착했습니다", "안내하겠습니다", "안내해드릴", "말씀해주세요", "맞으신가요",
                  "드리겠습니다", "방문하셨나요", "기다려주세요")
# "안과 말고 정형외과요" — 목적지를 둘 말해도 이런 말이 있으면 사람의 대답이다
_NEGATIONS = ("말고", "아니고", "아니라")


def is_dest_noise(text: str, places: list[dict] | None) -> str | None:
    """목적지 대답으로 보기 어려운 STT 결과면 그 이유. 목적지 단계에서만 쓴다.

    - 로봇 멘트 같은 말("~에 도착했습니다")
    - 목적지 이름이 2개 이상인데 "말고·아니고" 가 없다 — 방문자는 목적지를 하나 말한다.
      is_hint_echo 는 목적지 이름만 나열된 경우만 잡아 "정형외과, 엘리베이터 입구, 3층관공서 제가"
      처럼 한 마디가 붙으면 통과했다(병원 시험).
    """
    flat = re.sub(r"\s", "", text or "")
    if any(p in flat for p in _ROBOT_PHRASES):
        return "로봇 멘트"
    names = {p["name"] for p in (places or []) if p.get("name") and re.sub(r"\s", "", p["name"]) in flat}
    if len(names) >= 2 and not any(m in flat for m in _NEGATIONS):
        return "목적지 여러 개"
    return None


def is_hallucination(text: str, audio_sec: float) -> str | None:
    """STT 결과가 소음에서 지어낸 문장으로 보이면 그 이유. 정상이면 None.

    실측(2026-09-23): 모터 소음만 15초 녹음된 구간에서 "오늘 영상은 여기까지입니다. 다음 영상에서
    만나요!", 러시아어·한자가 섞인 긴 문장이 나왔다. 이런 걸 목적지 판정에 넘기면 엉뚱하게 진행한다.
    """
    t = text.strip()
    if not t:
        return "빈 결과"
    foreign = sum(1 for ch in t if ch.isalpha() and not ("가" <= ch <= "힣" or ch.isascii()))
    if foreign >= 2:
        return f"한글·영문 외 문자 {foreign}자"
    syllables = sum(1 for ch in t if "가" <= ch <= "힣")
    if audio_sec > 0 and syllables / audio_sec > _MAX_CHARS_PER_SEC and syllables > 12:
        return f"녹음 {audio_sec:.1f}초에 {syllables}음절"
    if any(w in t for w in _HALLUCINATIONS):
        return "소음 구간 단골 문장"
    phrases = [p for p in (re.sub(r"[^0-9A-Za-z가-힣]", "", x) for x in re.split(r"[,.?!·\n]+", t))
               if len(p) >= 2]
    if any(phrases.count(p) >= _REPEAT_MIN for p in set(phrases)):
        return "같은 구절 반복"
    # 띄어쓰기로만 되풀이되는 경우 — "3층관공서 3층관공서 3층관공서 …"(병원 시험)
    words = [w for w in (re.sub(r"[^0-9A-Za-z가-힣]", "", x) for x in t.split()) if len(w) >= 2]
    if any(words.count(w) >= _REPEAT_MIN for w in set(words)):
        return "같은 구절 반복"
    return None


# ── TTS ───────────────────────────────────────────────
# OpenAI TTS 가 실패한 시각 + 쿨다운. 그 전까지는 바로 로컬로 간다.
_tts_openai_down_until = 0.0


def _synthesize_local(text: str) -> bytes:
    """로컬 TTS(voice-agent :8090 · MeloTTS-Korean)로 합성한다 — 크레딧을 쓰지 않는다.

    그쪽 /tts/preview 는 wav 바이트를 그대로 돌려주고, 보내는 문장에
    ko_text.normalize_for_tts() 를 적용한다 — "12번" → "십이번" 처럼 숫자를 한글로
    바꿔주므로 숫자 멘트에 특히 유리하다.
    """
    r = requests.post(config.TTS_LOCAL_URL, json={"text": text},
                      timeout=config.TTS_LOCAL_TIMEOUT)
    r.raise_for_status()
    return r.content


def _synthesize_openai(text: str) -> bytes:
    # instructions 는 extra_body 로 넘긴다 — 인자로 받는 건 openai 1.68+ 라 Jetson 의
    # 구버전 SDK 에서 TypeError 가 나고, 그러면 매번 로컬 목소리로 빠진다.
    extra = {"instructions": config.TTS_INSTRUCTIONS} if config.TTS_INSTRUCTIONS else None
    resp = client().audio.speech.create(
        model=config.TTS_MODEL,
        voice=config.TTS_VOICE,
        input=text,
        response_format="wav",
        extra_body=extra,
    )
    return resp.read()


def _synthesize_with_engine(text: str) -> tuple[bytes, str]:
    """텍스트를 WAV 로. 반환: (wav, 실제로 합성한 엔진 "openai" | "local")

    OpenAI 가 기준 목소리다. 실패하면 config.TTS_LOCAL_FALLBACK 일 때만 로컬 TTS 로
    말한다 — 목소리가 다르므로 호출부(synthesize_cached)는 로컬 결과를 캐시하지 않는다.
    한 번 실패하면 TTS_FALLBACK_COOLDOWN 동안은 **재시도하지 않고 바로 로컬**로 간다 —
    매번 재시도하면 문장마다 2.5초가 붙는다. 쿨다운이 지나면 다시 OpenAI 를 시도하므로
    크레딧을 채우면 저절로 원래 경로로 돌아온다.
    """
    global _tts_openai_down_until
    fallback = config.TTS_LOCAL_FALLBACK and bool(config.TTS_LOCAL_URL)
    now = time.monotonic()
    if fallback and now < _tts_openai_down_until:
        return _synthesize_local(text), "local"
    try:
        return _synthesize_openai(text), "openai"
    except Exception as e:  # noqa: BLE001 — 폴백이 있으면 종류를 가리지 않는다
        if not fallback:
            raise
        _tts_openai_down_until = now + config.TTS_FALLBACK_COOLDOWN
        log.warning("OpenAI TTS 실패 → 로컬 TTS 로 전환(%.0f초간, 캐시 안 함): %s",
                    config.TTS_FALLBACK_COOLDOWN, str(e)[:120])
        return _synthesize_local(text), "local"


def synthesize(text: str) -> bytes:
    """텍스트를 WAV 로. sounddevice 로 바로 재생하려고 wav 를 받는다."""
    return _synthesize_with_engine(text)[0]


def _tts_path(text: str) -> str:
    # 엔진·말투까지 키에 넣는다. 예전 키(모델|목소리|문장)로 쌓인 캐시에는 크레딧 소진 중
    # 로컬로 만든 음성이 OpenAI 이름으로 섞여 있어, 키를 바꿔 전부 새로 합성되게 한다.
    raw = f"openai|{config.TTS_MODEL}|{config.TTS_VOICE}|{config.TTS_INSTRUCTIONS}|{text}"
    key = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return os.path.join(config.TTS_CACHE_DIR, f"{key}.wav")


def synthesize_cached(text: str) -> bytes:
    """같은 문장은 한 번만 합성한다.

    단계 멘트는 매번 같은데 질문마다 0.6~0.9초씩 합성을 기다렸다(실측). 캐시하면 바로 말한다.
    숫자가 든 문장(대기번호·창구)도 캐시한다. 예전에는 경우의 수가 끝없다고 빼 두었는데, 그래서
    그 문장만 매번 실시간 합성이 필요했고 OpenAI 가 순간 끊기면 로컬 목소리로 나갔다. 지금은
    관제가 번호를 자동으로 넣어(대기번호 10 · 창구 1, voice_scenario.AUTO_*) 같은 문장이 반복된다.
    번호가 바뀌어도 문장 하나당 wav 하나(수십~수백 KB)라 쌓여도 부담이 작다.
    로컬 폴백 결과는 캐시하지 않는다(목소리가 달라 그 문장만 영영 다른 소리로 남는다).
    """
    path = _tts_path(text)
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        pass
    wav, engine = _synthesize_with_engine(text)
    if engine == "openai":
        try:
            os.makedirs(config.TTS_CACHE_DIR, exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "wb") as f:
                f.write(wav)
            os.replace(tmp, path)          # 쓰다 죽어도 반쪽 파일이 캐시로 남지 않게
        except OSError as e:
            log.warning("TTS 캐시 저장 실패: %s", e)
    return wav


def prewarm_tts(texts: list[str]) -> int:
    """미리 합성해 둔다. 반환: 새로 합성한 개수. 실패는 넘긴다(말할 때 다시 시도된다)."""
    made = 0
    for t in dict.fromkeys(x.strip() for x in texts if x and x.strip()):
        if os.path.exists(_tts_path(t)):
            continue
        if time.monotonic() < _tts_openai_down_until:
            # OpenAI 가 막혀 있으면 로컬로 합성해도 캐시하지 않으니 헛일이다 — 말할 때 다시 시도된다
            log.warning("OpenAI TTS 사용 불가 — 미리 합성 중단")
            break
        try:
            synthesize_cached(t)
            made += 1
        except Exception as e:
            log.warning("TTS 미리 합성 실패(%s): %s", t[:20], e)
    return made


# ── 목적지 판정 전용 ────────────────────────────────────
PICK_PROMPT = """당신은 병원·관공서 안내 로봇의 목적지 판정기입니다.
로봇이 방금 한 질문과 방문자의 대답을 보고, 방문자가 가려는 곳을 후보 목록에서 고릅니다.

규칙:
1. 반드시 후보 중 하나의 index 를 고르거나, 해당하는 후보가 없으면 null 을 줍니다.
2. 이름이 정확히 같지 않아도 의미가 통하면 고릅니다.
   예) "허리가 아파요" -> 정형외과, "눈이 침침해요" -> 안과, "전입신고 하러 왔어요" -> 전입신고,
       "화장실 어디예요" -> 화장실
3. 음성 인식 오류로 글자가 조금 틀렸을 수 있습니다. 발음이 비슷한 후보를 고려합니다.
   예) "관공석" -> 관공서, "안가" -> 안과
4. 목적지와 무관한 말(인사, 감사, 잡음 같은 문장)이면 null 입니다. 억지로 고르지 않습니다.
5. confidence 는 0~1 입니다. 애매하면 낮게 줍니다.

반드시 JSON 으로만 답하세요:
{"index": 숫자 또는 null, "confidence": 0~1, "reason": "한 줄 근거"}"""


def pick_destination(heard: str, candidates: list[dict], question: str = "") -> dict:
    """대답에서 목적지를 고른다 — 대화하지 않고 후보 중 하나를 고르거나 없다고만 한다.

    Args:
        heard: STT 결과
        candidates: [{"name": ..., "info": 동의어·설명}] — 순서가 index 다
        question: 로봇이 방금 한 질문(맥락)

    Returns:
        {"index": int | None, "confidence": float, "reason": str}
    """
    catalog = "\n".join(
        f"{i}. {c['name']}" + (f" ({c['info']})" if c.get("info") else "")
        for i, c in enumerate(candidates)
    )
    user = f"후보:\n{catalog}\n\n로봇 질문: {question or '-'}\n방문자 대답: {heard}"
    resp = client().chat.completions.create(
        model=config.CHAT_MODEL,
        messages=[
            {"role": "system", "content": PICK_PROMPT},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("목적지 판정 응답이 JSON 이 아니다: %s", raw[:200])
        return {"index": None, "confidence": 0.0, "reason": "JSON 아님"}

    idx = out.get("index")
    if not isinstance(idx, int) or not 0 <= idx < len(candidates):
        if idx is not None:
            log.warning("목적지 판정이 목록 밖 index 를 골랐다: %s", raw[:200])
        idx = None
    try:
        conf = float(out.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return {"index": idx, "confidence": conf, "reason": str(out.get("reason") or "")[:120]}
