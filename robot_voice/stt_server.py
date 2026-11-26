"""로컬 STT HTTP 서버 — OpenAI 크레딧 없이 전사한다.

**왜 필요한가.** 2026-09-30 OpenAI 크레딧이 소진되어(`429 credit_balance_exhausted`)
STT 가 멈췄다. 예비 모델(`whisper-1`)도 같은 계정이라 폴백이 무의미했다. 그런데 Jetson 에는
구 시스템이 구축해 둔 faster-whisper small(CUDA · float16)이 그대로 남아 있고, 같은 오디오를
**정확히** 전사한다 — 실측(2026-09-30, 1m 정면):

    [0.0-2.0] ' 눈이 아파서 왔어'  no_speech=0.069  logp=-0.516
    [2.0-4.0] ' 눈이 아파서 왔어'  no_speech=0.069  logp=-0.516

**구 시스템의 `/listen`(:8090)을 쓰지 않는 이유.** 그 경로는 의도 판정까지 돌려 6.4초가
걸리고, 구 시스템의 프롬프트·필터 설정에 얽혀 있다(같은 wav 가 `"눈이 안가져 왔어"` 로
새는 것을 실측). 여기서는 **전사만** 하고 프롬프트를 호출부가 정한다.

**어휘 힌트는 호출부가 `prompt` 필드로 넘긴다.** 짧은 단어는 힌트 없이 맞추기 어렵다 —
실측(2026-09-30): 힌트 없이 `"안과"` → `"안좋아"` 가 나와 사전 직매칭이 실패하고
LLM(크레딧 필요)까지 갔다. 힌트의 부작용인 무음 구간 에코 환각은 호출부의
`ai.is_hint_echo()` 가 잡는다.

**모델은 기동 시 한 번 로드한다.** 요청마다 로드하면 20~30초가 붙는다.

실행 (구 시스템 venv 로 — 그쪽에 CUDA 로 소스 빌드한 CTranslate2 가 있다.
PyPI 휠은 CPU 전용이라 새 venv 에 설치하면 GPU 를 못 쓴다):

    /home/und02/project/voice/voice_assistant/.venv/bin/python stt_server.py

    POST /stt   (multipart: file=wav, prompt=선택, max_tokens=선택)  ->  {"text": "...", "ms": 830}
    GET  /health                                    ->  {"status":"ok","model":"small",...}
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
import wave

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stt_server")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


HOST = _env("STT_SERVER_HOST", "127.0.0.1")
PORT = int(_env("STT_SERVER_PORT", "8091"))
MODEL = _env("STT_SERVER_MODEL", "small")
DEVICE = _env("STT_SERVER_DEVICE", "cuda")
COMPUTE = _env("STT_SERVER_COMPUTE", "float16")
LANG = _env("STT_SERVER_LANG", "ko")
BEAM = int(_env("STT_SERVER_BEAM", "5"))
# 세그먼트 품질 임계. 이 값을 벗어난 세그먼트는 버린다(아래 /stt 참고).
#   실측(2026-09-30) 정상 발화: no_speech 0.069 · logp -0.516
#   환각 사례: 신호가 약한 3.4초 녹음에서 "고마워요, 고맙습니다"
# 환각이 계속 통과하면 NO_SPEECH_MAX 를 낮추고(예 0.4) LOGPROB_MIN 을 올린다(예 -0.8).
NO_SPEECH_MAX = float(_env("STT_SERVER_NO_SPEECH_MAX", "0.6"))
LOGPROB_MIN = float(_env("STT_SERVER_LOGPROB_MIN", "-1.0"))
# 약한 신호를 올려서 넣는다. Whisper 의 특징 추출은 절대 레벨에 영향을 받는다 —
# 실측(2026-09-30) 발화 최대가 0.05 수준까지 떨어진 상태에서 환각이 쏟아졌다.
# 잡음만 있는 구간을 증폭하면 환각이 늘 수 있으므로 **이득에 상한을 둔다**.
# 품질 필터(no_speech / avg_logprob)가 그 부작용을 받아낸다.
NORM_TARGET = float(_env("STT_SERVER_NORM_TARGET", "0.30"))   # 목표 피크 (0 이면 비활성)
NORM_MAX_GAIN = float(_env("STT_SERVER_NORM_MAX_GAIN", "8.0"))

app = FastAPI(title="local-stt")
_model: WhisperModel | None = None
_lock = threading.Lock()   # WhisperModel 은 스레드 세이프가 아니다


def model() -> WhisperModel:
    global _model
    if _model is None:
        t0 = time.time()
        log.info("모델 로드: %s / %s / %s", MODEL, DEVICE, COMPUTE)
        _model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE)
        log.info("모델 로드 완료 %.1f초", time.time() - t0)
    return _model


def _wav_to_mono_f32(raw: bytes) -> tuple[np.ndarray, int]:
    """wav 바이트 -> float32 mono. 2채널이면 **ch0 만** 쓴다.

    XVF3800 은 좌우에 서로 다른 신호를 실을 수 있어(mux) 평균을 내면 성격이 다른 신호가
    섞인다. 운영에서 쓰는 채널이 ch0 이므로 여기서도 ch0 을 고른다.
    """
    with wave.open(io.BytesIO(raw), "rb") as w:
        if w.getsampwidth() != 2:
            raise HTTPException(status_code=422, detail="16bit PCM wav 만 받는다")
        rate = w.getframerate()
        ch = w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        pcm = pcm.reshape(-1, ch)[:, 0]
    return pcm.astype(np.float32) / 32768.0, rate


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": MODEL, "device": DEVICE, "compute": COMPUTE,
            "loaded": _model is not None, "lang": LANG, "beam": BEAM,
            "norm_target": NORM_TARGET, "no_speech_max": NO_SPEECH_MAX,
            "logprob_min": LOGPROB_MIN}


@app.post("/stt")
async def stt(file: UploadFile = File(...), prompt: str = Form(""), max_tokens: int = Form(0)) -> dict:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="빈 오디오입니다")

    audio, rate = _wav_to_mono_f32(raw)
    if rate != 16000:
        log.warning("입력이 %dHz 다 — faster-whisper 가 내부에서 16k 로 맞춘다", rate)

    gain = 1.0
    if NORM_TARGET > 0.0:
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        if 0.0 < peak < NORM_TARGET:
            gain = min(NORM_TARGET / peak, NORM_MAX_GAIN)
            audio = audio * gain

    # 생성 길이 상한(0 이면 제한 없음). 짧은 대답(예/아니요)용이다. 말이 없는 녹음에 힌트를 주면
    # 모델이 힌트 말을 끝까지(수백 토큰) 되풀이하다 품질 필터에 버려져, 결과는 빈 문자열인데
    # 전사에 9초가 걸렸다(2026-10-08 실측 2건). 24 토큰으로 묶으면 같은 녹음이 1.9초 안에 끝나고
    # 22개 녹음의 판정 결과는 그대로였다.
    extra = {"max_new_tokens": max_tokens} if max_tokens > 0 else {}

    t0 = time.time()
    with _lock:
        segs, _info = model().transcribe(
            audio, language=LANG, beam_size=BEAM,
            # 어휘 힌트. 짧은 단어는 힌트 없이 맞추기 어렵다 — 실측(2026-09-30):
            #   힌트 없음  "안과" → "안좋아"  → 사전 직매칭 실패 → LLM 호출(429)
            # 부작용(무음 구간에서 힌트를 그대로 받아쓰는 환각)은 호출부의
            # ai.is_hint_echo() 가 잡는다. 빈 문자열이면 힌트 없이 돈다.
            initial_prompt=prompt or None,
            # 앞 세그먼트를 컨텍스트로 물지 않는다. 기본(True)이면 한 번 지어낸 문장이
            # 다음 세그먼트로 **연쇄**된다 — 실측에서 같은 환각이 두 번 반복 출력됐다.
            condition_on_previous_text=False,
            # 실패 시 temperature 를 올려 재시도하는 기본 동작이 환각을 만든다.
            # 0.0 으로 고정하면 결정적으로 돌고, 못 알아들으면 빈 결과가 나온다.
            temperature=0.0,
            no_speech_threshold=NO_SPEECH_MAX,
            log_prob_threshold=LOGPROB_MIN,
            # Silero VAD 는 Jetson aarch64 에서 onnxruntime 이 CPU 판별에 실패해
            # C++ abort 로 프로세스를 죽인다(구 시스템 실측). 절대 켜지 않는다.
            vad_filter=False,
            **extra,
        )
        # 세그먼트 품질을 **여기서 직접** 걸러낸다. faster-whisper 의
        # no_speech_threshold / log_prob_threshold 는 temperature fallback 과 얽혀
        # 세그먼트를 확실히 버리지 않는다.
        #
        # 왜 필요한가: 신호가 약하면 Whisper 가 **지어낸다.** 실측(2026-09-30) — 발화
        # 최대 음량이 소음의 1.8배뿐인 3.4초 녹음에서 "고마워요, 고맙습니다" 가 나왔고,
        # 힌트("큐리야, 정형외과, 안과")를 전혀 따라가지 않았다. 즉 음성이 없었다는 뜻이다.
        # 그 환각이 목적지 판정까지 흘러가 LLM 호출(유료)까지 유발했다.
        # → 품질 미달이면 **빈 문자열**을 돌려 호출부가 (STT 결과 없음) 으로 처리하게 한다.
        kept, dropped = [], []
        for sg in segs:
            nsp = float(getattr(sg, "no_speech_prob", 0.0) or 0.0)
            lp = float(getattr(sg, "avg_logprob", 0.0) or 0.0)
            txt = (sg.text or "").strip()
            if not txt:
                continue
            if nsp > NO_SPEECH_MAX or lp < LOGPROB_MIN:
                dropped.append((txt, nsp, lp))
                continue
            kept.append((txt, nsp, lp))
    ms = int((time.time() - t0) * 1000)
    text = " ".join(t for t, _, _ in kept).strip()
    if dropped:
        for txt, nsp, lp in dropped:
            log.info("버림(품질) 결과=%r  no_speech=%.3f logp=%.3f", txt[:60], nsp, lp)
    # 결과를 앞에 둔다. 힌트(미리 건넨 단어 목록)는 매 줄 같아서 앞에 있으면 결과로 오인했다.
    log.info("전사 %dms  결과=%r  (이득 %.1fx, 힌트: %s)", ms, text[:80], gain, prompt[:60] or "없음")
    # 버린 구간의 글·점수도 돌려준다 — 짧은 예/아니요는 실제 대답인데 no_speech 가 기준을 조금
    # 넘어 버려지는 일이 있어(2026-10-08 실측 0.67), 호출부가 예/아니요 단계에서만 살려 쓴다.
    dropped_segments = [{"text": t[:60], "no_speech": round(n, 3), "logprob": round(l, 3)}
                        for t, n, l in dropped[:5]]
    # 채택한 구간의 점수 — 힌트 낱말 하나만 나온 결과가 진짜 대답인지 호출부가 가를 때 쓴다
    segments = [{"text": t[:60], "no_speech": round(n, 3), "logprob": round(l, 3)} for t, n, l in kept[:5]]
    return {"text": text, "ms": ms, "dropped": len(dropped), "dropped_segments": dropped_segments,
            "segments": segments}


if __name__ == "__main__":
    model()            # 첫 요청이 20~30초 걸리지 않게 미리 로드한다
    uvicorn.run(app, host=HOST, port=PORT, workers=1, log_level="warning")
