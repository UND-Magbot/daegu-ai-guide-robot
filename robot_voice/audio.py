"""마이크 녹음(에너지 기반 VAD)과 스피커 재생.

Whisper API 는 파일 단위라 발화 구간을 잘라 보내야 한다. 웨이크워드 없이,
말이 시작되면 녹음하고 일정 시간 조용해지면 끊는 방식이다.

장비 구성(2026-09-23 실측):
    마이크  ReSpeaker XVF3800 4-Mic Array (Seeed, 2886:001a)  2채널
    스피커  UACDemoV1.0 (Jieli)                               별도 USB 장치

**장치는 번호가 아니라 이름으로 잡는다.** USB 장치가 하나만 늘어도 카드 번호가
밀린다 — 실제로 ReSpeaker 재연결 한 번에 card 3 -> 4 로 밀렸다. 못 찾으면 기본
장치로 조용히 떨어지지 않고 예외를 낸다. 마이크가 안 잡힌 걸 모른 채 "아무 소리도
안 나는" 상태가 제일 나쁘다.
"""

import io
import logging
import sys
import threading
import time
import wave
from collections import deque
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from robot_voice import config

log = logging.getLogger("robot_voice.audio")


def set_speaker_volume(card: str, control: str, pct: int) -> str:
    """스피커 하드웨어 음량을 맞추고 결과 한 줄을 돌려준다. 실패해도 예외를 내지 않는다(안내는 계속한다)."""
    import subprocess
    try:
        out = subprocess.run(["amixer", "-c", card, "sset", control, f"{pct}%"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        return f"실패({e.__class__.__name__}: {e})"
    if out.returncode != 0:
        return f"실패({(out.stderr or out.stdout).strip()[:120]})"
    vals = [ln.strip() for ln in out.stdout.splitlines() if "Playback" in ln and "[" in ln]
    return vals[-1] if vals else "설정함"


def list_devices() -> None:
    """오디오 장치 목록 출력. USB 마이크/스피커 확인용."""
    print(sd.query_devices())


def _find_device(kind: str, hints: list[str]):
    """이름 힌트로 장치를 찾는다.

    Args:
        kind: "input" | "output"
        hints: 장치 이름에 포함될 문자열들(대소문자 무시)

    Returns:
        sounddevice 장치 인덱스.

    Raises:
        RuntimeError: 후보를 못 찾은 경우. 기본 장치로 떨어지지 않는다.
    """
    want_input = kind == "input"
    candidates = []

    for idx, dev in enumerate(sd.query_devices()):
        ch = dev["max_input_channels"] if want_input else dev["max_output_channels"]
        if ch <= 0:
            continue
        name = (dev["name"] or "").lower()
        if any(h.strip().lower() in name for h in hints if h.strip()):
            return idx
        candidates.append(f"  [{idx}] {dev['name']} ({ch}ch)")

    raise RuntimeError(
        f"{kind} 장치를 찾지 못했다. 힌트={hints}\n"
        f"사용 가능한 {kind} 장치:\n" + ("\n".join(candidates) or "  (없음)") +
        "\n장치가 점유 중일 수 있다: sudo fuser -v /dev/snd/pcm*"
    )


def resolve_device(kind: str):
    """설정값이 있으면 그대로, 없으면 이름 힌트로 탐색한다."""
    explicit = config.INPUT_DEVICE if kind == "input" else config.OUTPUT_DEVICE
    if explicit is not None:
        return explicit
    hints = config.INPUT_HINTS if kind == "input" else config.OUTPUT_HINTS
    return _find_device(kind, hints)


def _rms(block: np.ndarray) -> float:
    """블록의 음량(RMS). 무음이면 0 에 가깝다."""
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))


def _device_rate(device, kind: str, fallback: int) -> int:
    """장치가 기본으로 쓰는 샘플레이트. 조회 실패 시 fallback.

    hw: 장치는 ALSA plug 변환을 거치지 않아 지원하지 않는 레이트를 거부한다
    (PaErrorCode -9997). 그래서 장치 기준에 맞춰야 한다.
    """
    try:
        info = sd.query_devices(device, kind)
        rate = int(info["default_samplerate"])
        return rate if rate > 0 else fallback
    except Exception:
        return fallback


def _device_channels(device, kind: str, want: int) -> int:
    """장치가 지원하는 채널 수로 조정한다(want 를 넘지 않게)."""
    try:
        info = sd.query_devices(device, kind)
        key = "max_input_channels" if kind == "input" else "max_output_channels"
        return max(1, min(want, int(info[key])))
    except Exception:
        return 1


def _resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """선형 보간 리샘플링. 음성 안내 품질에는 충분하다."""
    if src_rate == dst_rate or pcm.size == 0:
        return pcm
    n_dst = int(round(pcm.shape[0] * dst_rate / src_rate))
    if n_dst <= 0:
        return pcm
    src_idx = np.linspace(0, pcm.shape[0] - 1, n_dst)
    return np.interp(src_idx, np.arange(pcm.shape[0]), pcm).astype("float32")


class _RumbleFilter:
    """저역 제거 — 이동평균 저역통과를 빼서 고역만 남긴다. **판정용 레벨에만** 쓴다.

    per-sample IIR 루프를 피하려고 boxcar 저역통과를 빼는 방식을 쓴다(numpy 벡터 연산,
    scipy 불필요). 감쇠 특성은 버터워스보다 느슨하지만 **에너지 측정**에는 충분하다.
    16kHz·300Hz 설정에서 n=27 이고, 197Hz(실측 팬 피크)가 약 15dB 줄어든다.

    블록 경계에서 끊기지 않게 직전 n-1 샘플을 물고 있는다.
    """

    def __init__(self, cutoff_hz: float, rate: int) -> None:
        # 길이 n 의 boxcar 는 rate/n 에 첫 널을 갖고, 그걸 뺀 고역통과의 -3dB 점은
        # 대략 0.25*rate/n 에 온다. 그래서 n ≈ rate/(4*cutoff) 로 잡는다.
        # **홀수**로 맞춘다 — 지연이 정수 샘플((n-1)/2)이어야 정렬이 정확하다.
        n = max(3, int(round(rate / (4.0 * cutoff_hz))))
        self.n = n if n % 2 == 1 else n + 1
        self.h = (self.n - 1) // 2          # boxcar 群지연 = 정렬 보정량
        self.k = np.full(self.n, 1.0 / self.n, dtype=np.float32)
        self._tail = np.zeros(self.n - 1, dtype=np.float32)

    def __call__(self, block: np.ndarray) -> np.ndarray:
        x = np.concatenate((self._tail, block.astype(np.float32, copy=False)))
        self._tail = x[-(self.n - 1):].copy()
        low = np.convolve(x, self.k, mode="valid")       # len == len(block)
        # ⚠ 정렬이 핵심이다. low[j] 의 중심은 x 의 j+h 이므로, 직접 경로도 같은
        #   지점에서 가져와야 상쇄가 일어난다. 이 정렬을 빼먹으면 저역이 거의
        #   줄지 않는다(검증 실측: 197Hz 가 -15dB 대신 -1dB).
        #   레벨(에너지) 측정에만 쓰므로 h 샘플 지연은 무해하다.
        direct = x[self.h:self.h + block.shape[0]]
        return direct - low.astype(np.float32)


BLOCK_SEC = 0.03                 # 30ms 단위로 판정
# 아래 두 값은 config 로 조절한다(VAD_ONSET_SEC · VAD_PREROLL_SEC).
ONSET_BLOCKS = max(1, int(round(config.VAD_ONSET_SEC / BLOCK_SEC)))
# 말 시작 직전 구간도 붙인다 — 첫 음절이 잘리면 STT 가 틀린다.
# 실측(2026-09-30): 300ms 로는 부족해 "정형외과 안내해줘" 의 앞 0.6초가 날아갔다.
PREROLL_BLOCKS = max(1, int(round(config.VAD_PREROLL_SEC / BLOCK_SEC)))
NOISE_WARMUP_BLOCKS = 4          # 처음 120ms 만 소음 바닥 측정에 쓴다
# ⚠ 8(240ms)이었을 때 **즉답을 삼켰다.** 로봇 멘트가 끝나고 바로 대답하면 그 발화가
#   warmup 안에 들어가 loud_run 이 전혀 오르지 않는다. 실측(2026-09-30):
#     말 시작 감지 못함 (최대 음량 0.2824, 소음 0.0260, 문턱 0.0468)
#   → 문턱의 6배가 들어왔는데 감지 실패. peak 에는 기록되지만 비교를 안 하기 때문이다.


class Endpointer:
    """에너지 기반 발화 구간 판정 — 주변 소음에 맞춰 문턱을 스스로 올린다.

    고정 문턱(VAD_THRESHOLD)만 쓰면 로봇 모터·팬 소음이 문턱을 넘어 **말이 끝나도 끝났다고
    보지 못한다.** 실측(2026-09-23): 대답마다 VAD_MAX_SEC(15초)까지 녹음돼 응답이 20초 걸렸고,
    그 15초의 소음을 Whisper 가 "오늘 영상은 여기까지입니다" 같은 문장으로 지어냈다.

    그래서 말하지 않는 동안 소음 바닥을 계속 추적하고, 문턱을 소음의 VAD_NOISE_RATIO 배 이상으로
    둔다. 말하는 동안에는 소음 추정을 멈춘다(자기 목소리를 소음으로 배우지 않게).
    """

    def __init__(self, wait_blocks: int, silence_blocks: int, max_blocks: int,
                 noise_init: Optional[float] = None) -> None:
        self.wait_blocks = wait_blocks
        self.silence_blocks = silence_blocks
        self.max_blocks = max_blocks
        # 바로 앞 녹음에서 이어받은 소음 바닥. 있으면 warmup 으로 다시 재지 않는다 — 녹음을 쉬지
        # 않고 이어 붙일 때(main.listen) 스트림을 다시 연 순간에 대답이 들어오면 그 대답을 소음으로
        # 배워 문턱이 올라가 대답을 통째로 놓쳤다(2026-10-08 시뮬레이션).
        self._carried = noise_init is not None and noise_init > 0
        self.noise: Optional[float] = noise_init if self._carried else None
        self.speaking = False
        self.waited = 0
        self.loud_run = 0
        self.silent_run = 0
        self.spoken = 0
        self.peak = 0.0                  # 기다리는 동안 들어온 가장 큰 소리 — 못 잡았을 때 진단용
        # 최근 블록 레벨 — 발화 시작 판정의 변동(CV) 조건에 쓴다(config.VAD_MIN_CV).
        self.recent: deque = deque(maxlen=max(4, config.VAD_CV_WINDOW))
        self.cv = 0.0                    # 마지막으로 계산한 변동계수 (진단용)
        self.cv_rejects = 0              # CV 미달로 시작을 거절한 횟수 (진단용)
        self.max_loud_run = 0            # 문턱을 연속으로 넘은 최장 블록 수 (진단용)
        self._warm: list[float] = []     # warmup 구간 레벨 — 소음 초기값을 여기서 고른다

    def threshold(self) -> float:
        noise = self.noise or 0.0
        return max(config.VAD_THRESHOLD, noise * config.VAD_NOISE_RATIO)

    def _cv(self) -> float:
        """최근 창의 변동계수(표준편차/평균). 회전 기계는 낮고 사람 말은 높다."""
        if len(self.recent) < 4:
            return 0.0
        arr = np.fromiter(self.recent, dtype=np.float64, count=len(self.recent))
        mean = float(arr.mean())
        return float(arr.std() / mean) if mean > 0.0 else 0.0

    def _learn(self, level: float) -> None:
        if self.noise is None:
            # 첫 블록을 그대로 쓰면 **그 순간 사람이 말하고 있을 때 소음이 크게 잡혀**
            # 문턱이 덩달아 올라간다. warmup 구간의 **최솟값**을 쓴다.
            self._warm.append(level)
            if len(self._warm) < NOISE_WARMUP_BLOCKS:
                return
            self.noise = min(self._warm)
            return
        elif level < self.threshold():
            # 느리게 따라간다 — 한두 블록의 튐으로 문턱이 흔들리지 않게
            self.noise = 0.9 * self.noise + 0.1 * level

    def feed(self, level: float) -> str:
        """블록 하나의 음량을 넣는다. 반환: wait | start | speak | end | timeout"""
        self.recent.append(level)
        if not self.speaking:
            self.waited += 1
            self.peak = max(self.peak, level)
            warm = not self._carried and self.waited <= NOISE_WARMUP_BLOCKS
            if not warm and level >= self.threshold():
                self.loud_run += 1
                self.max_loud_run = max(self.max_loud_run, self.loud_run)
                if self.loud_run >= ONSET_BLOCKS:
                    self.cv = self._cv()
                    if config.VAD_MIN_CV > 0.0 and self.cv < config.VAD_MIN_CV:
                        # 문턱은 넘었지만 에너지가 평탄하다 — 팬·모터 소음이다.
                        # 녹음을 열지 않는다. 열면 침묵이 안 생겨 VAD_MAX_SEC 까지
                        # 끌려가고, 그 안에 섞인 실제 발화까지 함께 버려진다.
                        self.cv_rejects += 1
                        self.loud_run = 0
                        return "wait"
                    self.speaking = True
                    self.spoken = self.loud_run
                    return "start"
                return "wait"
            self.loud_run = 0
            self._learn(level)
            if self.waited >= self.wait_blocks:
                return "timeout"
            return "wait"

        self.spoken += 1
        # 끝 판정은 시작보다 조금 낮은 문턱으로 한다(히스테리시스) — 말끝이 작아지며 끊기지 않게
        self.silent_run = self.silent_run + 1 if level < self.threshold() * 0.8 else 0
        if self.silent_run >= self.silence_blocks or self.spoken >= self.max_blocks:
            return "end"
        return "speak"


def _levels(ep: Endpointer) -> dict:
    """진단용 레벨 요약. 감지 실패면 peak 가 threshold 바로 아래인지, max_loud_run 이
    onset 에 못 미쳤는지로 문턱·빔 문제를 가른다."""
    return {"peak": round(ep.peak, 4), "noise": round(ep.noise or 0.0, 4),
            "threshold": round(ep.threshold(), 4), "max_loud_run": ep.max_loud_run,
            "onset_blocks": ONSET_BLOCKS, "cv": round(ep.cv, 2)}


def record_utterance(wait_sec: Optional[float] = None,
                     should_stop: Optional[Callable[[], bool]] = None,
                     stats: Optional[dict] = None,
                     max_sec: Optional[float] = None,
                     noise_init: Optional[float] = None,
                     silence_sec: Optional[float] = None) -> Optional[bytes]:
    """한 번의 발화를 녹음해 WAV 바이트로 돌려준다.

    Args:
        wait_sec: 발화 시작을 기다리는 시간. 비우면 LISTEN_TIMEOUT_SEC.
            시작만 제한한다 — 말이 시작되면 끝날 때까지(최대 VAD_MAX_SEC) 녹음한다.
        should_stop: 참이 되면 녹음을 버리고 바로 돌아온다(운영자가 안내를 종료한 경우).
        stats: 주면 판정 결과·레벨을 채운다(진단 기록용 — diag). outcome 은
            ok | no_onset | too_short | stopped
        max_sec: 말 시작 뒤 최대 녹음 길이. 비우면 VAD_MAX_SEC. 예/아니요처럼 짧은 대답은
            소음으로 말 끝을 못 찾아도 여기서 끊는다.
        noise_init: 바로 앞 녹음의 소음 바닥(stats["noise"]). 이어 들을 때 넘기면 처음
            120ms 로 소음을 다시 재지 않는다.
        silence_sec: 이만큼 조용하면 말이 끝난 것으로 본다. 비우면 VAD_SILENCE_SEC.

    Returns:
        WAV 바이트. 발화가 없었거나 너무 짧으면 None.
    """
    st = stats if stats is not None else {}
    device = resolve_device("input")

    # 장치가 지원하는 레이트로 열어야 한다. Whisper 가 선호하는 16kHz 는
    # 저장 직전에 맞춘다.
    rate = _device_rate(device, "input", config.SAMPLE_RATE)

    # XVF3800 의 2채널은 성격이 다르다 — ch0=Conference(사람이 듣기 좋은),
    # ch1=ASR(빔 자동선택, 음성인식용). channels=1 로 열면 ALSA 가 둘을 평균내
    # 섞어버려 어느 쪽 장점도 남지 않는다. 2채널로 열고 ch1 만 뽑는다.
    channels = _device_channels(device, "input", config.INPUT_CHANNELS)
    pick = min(config.INPUT_PICK_CHANNEL, channels - 1)

    block = int(rate * BLOCK_SEC)
    ep = Endpointer(
        wait_blocks=max(1, int((wait_sec or config.LISTEN_TIMEOUT_SEC) / BLOCK_SEC)),
        silence_blocks=max(1, int((silence_sec or config.VAD_SILENCE_SEC) / BLOCK_SEC)),
        max_blocks=int((max_sec or config.VAD_MAX_SEC) / BLOCK_SEC),
        noise_init=noise_init,
    )
    # 저역 소음(팬·모터)을 판정용 레벨에서만 뺀다. 0 이면 기존 동작.
    rumble = _RumbleFilter(config.VAD_HPF_HZ, rate) if config.VAD_HPF_HZ > 0.0 else None
    preroll: deque = deque(maxlen=PREROLL_BLOCKS)
    frames: list[np.ndarray] = []

    with sd.InputStream(samplerate=rate, channels=channels, dtype="float32",
                        blocksize=block, device=device) as stream:
        while True:
            if should_stop and should_stop():
                st["outcome"] = "stopped"
                return None
            data, _ = stream.read(block)
            mono = data[:, pick].copy()
            # 저장되는 것은 mono(원본)이고, 판정에만 고역통과를 거친 레벨을 쓴다.
            state = ep.feed(_rms(rumble(mono) if rumble is not None else mono))

            if state == "timeout":
                # 말을 했는데 못 잡았다면 최대 음량이 문턱 바로 아래에 있다 — VAD_NOISE_RATIO 조정 근거
                # 최대 음량이 문턱을 넘었는데도 못 잡았다면 연속 블록 수(연속)가 답이다.
                # ONSET_BLOCKS 미만이면 소리가 토막나 들어온 것이고, 0 이면 warmup 에
                # 삼켰거나 문턱 자체를 넘지 못한 것이다.
                log.info("말 시작 감지 못함 (최대 음량 %.4f, 소음 %.4f, 문턱 %.4f, 연속 %d/%d, CV %.2f, CV거절 %d)",
                         ep.peak, ep.noise or 0.0, ep.threshold(),
                         ep.max_loud_run, ONSET_BLOCKS, ep.cv, ep.cv_rejects)
                st.update(_levels(ep), outcome="no_onset")
                return None              # 아무도 말하지 않았다
            if state == "wait":
                preroll.append(mono)
                continue
            if state == "start":
                frames.extend(preroll)
            frames.append(mono)
            if state == "end":
                break

    speech_sec = ep.spoken * BLOCK_SEC
    # speech_sec 은 감지 이후 길이이고, 실제 저장분에는 preroll 이 더 붙는다.
    log.info("녹음 %.1f초 (+앞 %.1f초, 소음 %.4f, 문턱 %.4f, CV %.2f%s)", speech_sec,
             len(preroll) * BLOCK_SEC if ep.spoken else 0.0, ep.noise or 0.0,
             ep.threshold(), ep.cv,
             ", 최대 길이 도달" if ep.spoken >= ep.max_blocks else "")
    st.update(_levels(ep), speech_sec=round(speech_sec, 2),
              max_len=ep.spoken >= ep.max_blocks)
    if speech_sec < config.VAD_MIN_SEC:
        st["outcome"] = "too_short"
        return None                      # 기침, 문 닫는 소리 등
    st["outcome"] = "ok"

    audio = np.concatenate(frames) if frames else np.zeros(0, dtype="float32")
    target = config.SAMPLE_RATE
    return _to_wav(_resample(audio, rate, target), target)


def wav_tail(wav: bytes, sec: float) -> bytes:
    """WAV 의 끝 sec 초 PCM 바이트. 상한에서 끊긴 녹음의 끝을 다음 녹음 앞에 이어 붙이는 데 쓴다."""
    with wave.open(io.BytesIO(wav), "rb") as w:
        n = w.getnframes()
        keep = min(n, int(w.getframerate() * sec))
        w.setpos(n - keep)
        return w.readframes(keep)


def prepend_pcm(pcm: bytes, wav: bytes) -> bytes:
    """같은 형식(16bit 모노)의 PCM 을 WAV 앞에 붙인다."""
    if not pcm:
        return wav
    with wave.open(io.BytesIO(wav), "rb") as w:
        rate, body = w.getframerate(), w.readframes(w.getnframes())
    buf = io.BytesIO()
    with wave.open(buf, "wb") as o:
        o.setnchannels(1)
        o.setsampwidth(2)
        o.setframerate(rate)
        o.writeframes(pcm + body)
    return buf.getvalue()


def wav_seconds(wav: bytes) -> float:
    """WAV 길이(초). STT 결과가 녹음 길이에 비해 지나치게 긴지(지어낸 문장) 판정에 쓴다."""
    with wave.open(io.BytesIO(wav), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)


def play_seconds(wav: bytes) -> float:
    """재생 길이(초) — play_wav 처럼 실제 데이터를 읽어 잰다.

    OpenAI TTS 의 WAV 는 스트리밍이라 헤더 길이 칸이 0xFFFFFFFF 다. 헤더(getnframes)를 믿으면
    3초 멘트가 89478초로 나온다(2026-10-08 실측) — wav_seconds 는 우리가 녹음한 WAV 에만 쓴다.
    앞에 붙이는 무음(PLAY_LEAD_SEC)까지 더한다 — 멘트 끝에 맞춘 출발 요청이 그만큼 이르지 않게.
    """
    with wave.open(io.BytesIO(wav), "rb") as w:
        frame_bytes = (w.getsampwidth() * w.getnchannels()) or 1
        sec = len(w.readframes(w.getnframes())) / frame_bytes / float(w.getframerate() or 1)
    return sec + max(0.0, config.PLAY_LEAD_SEC)


# 음성 대역 — 사람 말의 에너지는 대부분 여기 있고, 로봇 몸체 저음은 300Hz 아래에 몰린다
VOICE_BAND_HZ = (300.0, 3400.0)
# 소음 바닥(하위 20% 프레임)의 이 배수를 넘는 프레임을 '말소리' 로 센다
VOICED_RATIO = 4.0


def voiced_seconds(wav: bytes) -> float:
    """녹음에서 음성 대역 소리가 뚜렷한 시간(초).

    STT 가 비었을 때 "아무도 말하지 않았다(몸체 소음이 녹음을 열었다)" 와
    "말은 했는데 알아듣지 못했다" 를 가르는 데 쓴다 — config.REASK_VOICED_SEC.
    """
    with wave.open(io.BytesIO(wav), "rb") as w:
        rate = w.getframerate() or config.SAMPLE_RATE
        channels = w.getnchannels() or 1
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    if channels > 1:
        x = x.reshape(-1, channels)[:, 0]
    block = int(rate * BLOCK_SEC)
    n = len(x) // block if block else 0
    if n < 4:
        return 0.0
    freqs = np.fft.rfftfreq(block, 1.0 / rate)
    band = (freqs >= VOICE_BAND_HZ[0]) & (freqs < VOICE_BAND_HZ[1])
    spec = np.abs(np.fft.rfft(x[:n * block].reshape(n, block) * np.hanning(block), axis=1))
    energy = np.sqrt((spec[:, band] ** 2).sum(axis=1)) / 32768.0
    floor = max(float(np.percentile(energy, 20)), 1e-4)
    return float((energy > floor * VOICED_RATIO).sum()) * BLOCK_SEC


def _to_wav(audio: np.ndarray, rate: int) -> bytes:
    """float32 [-1,1] 을 16bit PCM WAV 로 만든다."""
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# 재생 중 종료 신호를 보는 간격과, 재생 길이 뒤에 더 기다리는 여유(장치 버퍼 비우는 시간)
PLAY_POLL_SEC = 0.05
PLAY_TAIL_SEC = 0.5


class _Player:
    """스피커 출력 스트림을 **열어 둔 채** 멘트를 차례로 흘려보낸다.

    sd.play 는 멘트마다 스트림을 새로 열고 닫는다. 이 USB 스피커(Jieli UACDemo)는 닫힌 직후
    다시 열면 앞부분을 버린다 — 2026-10-11 실측(신호음 두 개를 robot-voice 처럼 이어 재생,
    뒤 소리가 실제로 나온 양): 간격 0초 1~4% · 0.1초 21~36% · 0.3초 시작 0.27초 늦음 ·
    0.6초 정상. "잘 못 들었어요" + 질문, 도착 멘트 + 다음 질문처럼 이어지는 멘트의 앞이
    잘렸고, 질문 직후의 듣기 신호음도 같은 이유로 거의 안 들렸을 것이다.
    쉬는 동안에는 무음을 흘려 스트림을 살려 둔다. 장치가 빠지면(USB 재연결) 다음 재생 때 다시 연다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stream: Optional[sd.OutputStream] = None
        self._key: Optional[tuple] = None
        self._queue: deque = deque()
        self._pos = 0

    def _callback(self, outdata, frames, _time, _status) -> None:
        outdata.fill(0.0)
        filled = 0
        with self._lock:
            while filled < frames and self._queue:
                chunk = self._queue[0]
                take = min(frames - filled, len(chunk) - self._pos)
                outdata[filled:filled + take, 0] = chunk[self._pos:self._pos + take]
                filled += take
                self._pos += take
                if self._pos >= len(chunk):
                    self._queue.popleft()
                    self._pos = 0

    def _ensure(self, device, rate: int) -> sd.OutputStream:
        key = (device, rate)
        st = self._stream
        if st is not None and self._key == key and st.active:
            return st
        self.close()
        st = sd.OutputStream(device=device, samplerate=rate, channels=1, dtype="float32",
                             callback=self._callback)
        st.start()
        self._stream, self._key = st, key
        log.info("스피커 출력 스트림 열림 (%s Hz, 지연 %.0fms) — 이후 계속 열어 둔다",
                 rate, (st.latency or 0.0) * 1000)
        return st

    def busy(self) -> bool:
        with self._lock:
            return bool(self._queue)

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()
            self._pos = 0

    def close(self) -> None:
        st, self._stream, self._key = self._stream, None, None
        self.clear()
        if st is not None:
            try:
                st.abort()
                st.close()
            except Exception:  # noqa: BLE001 — 이미 빠진 장치
                pass

    def play(self, mono: np.ndarray, rate: int, device, tail_sec: float,
             should_stop: Optional[Callable[[], bool]] = None) -> None:
        """mono(float32, 장치 레이트)를 흘려보내고 다 나갈 때까지(+tail_sec) 기다린다."""
        st = self._ensure(device, rate)
        with self._lock:
            self._queue.append(np.ascontiguousarray(mono, dtype=np.float32))
        end = time.time() + len(mono) / float(rate) + max(tail_sec, st.latency or 0.0)
        while True:
            if should_stop and should_stop():
                self.clear()
                return
            if not st.active:
                # 재생 도중 장치가 빠졌다 — 다음 재생에서 다시 연다
                self.close()
                raise sd.PortAudioError("스피커 출력 스트림이 멈췄다(장치 분리?)")
            if time.time() >= end and not self.busy():
                return
            time.sleep(PLAY_POLL_SEC)


_player = _Player()


def stop_playback() -> None:
    """재생 중단. 세션이 끊기거나 사용자가 말을 끊을 때 쓴다."""
    _player.clear()
    try:
        sd.stop()
    except Exception:
        pass


def _to_mono(pcm: np.ndarray) -> np.ndarray:
    return pcm if pcm.ndim == 1 else pcm.mean(axis=1).astype("float32")


def _cue_pcm(rate: int) -> np.ndarray:
    """듣기 시작 신호음 — 짧은 종소리. 5ms 에 올라가 지수로 사라진다(딸깍 소리 없게 끝은 0)."""
    n = max(1, int(rate * config.LISTEN_CUE_SEC))
    t = np.arange(n, dtype=np.float32) / rate
    env = np.exp(-t * (5.0 / max(config.LISTEN_CUE_SEC, 1e-3)))
    attack = min(n, max(1, int(rate * 0.005)))
    env[:attack] *= np.linspace(0.0, 1.0, attack, dtype=np.float32)
    env[-1] = 0.0
    tone = np.sin(2.0 * np.pi * config.LISTEN_CUE_HZ * t)
    return (tone * env * config.LISTEN_CUE_VOLUME).astype("float32")


def play_cue() -> None:
    """듣기 시작 신호음을 낸다(블로킹, 0.15초 안팎).

    play_wav 와 달리 재생 뒤 여유(PLAY_TAIL_SEC)를 두지 않는다 — 신호음 뒤가 비면
    질문과 듣기 사이가 그만큼 벌어진다. 장치 레이트로 바로 만들어 리샘플도 없다.
    """
    device = resolve_device("output")
    rate = _device_rate(device, "output", 48000)
    try:
        if config.PLAY_KEEP_STREAM:
            _player.play(_cue_pcm(rate), rate, device, tail_sec=0.0)
            return
        sd.play(_cue_pcm(rate), samplerate=rate, device=device)
        sd.wait()
    except BaseException:
        stop_playback()
        raise


def play_wav(data: bytes, should_stop: Optional[Callable[[], bool]] = None) -> None:
    """WAV 바이트를 스피커로 재생한다(재생이 끝날 때까지 블로킹).

    Args:
        should_stop: 참이 되면 재생을 끊고 돌아온다(운영자가 안내를 종료한 경우).
            끊기는 **재생하는 이 스레드에서** 한다 — 다른 스레드에서 sd.stop() 을 부르면
            여기서 기다리는 sd.wait() 와 교착돼 프로세스 전체가 멈춘다(2026-09-30 실측).
    """
    with wave.open(io.BytesIO(data), "rb") as w:
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
        channels = w.getnchannels()

    pcm = np.frombuffer(frames, dtype=np.int16).astype("float32") / 32767.0
    if channels > 1:
        pcm = pcm.reshape(-1, channels)

    device = resolve_device("output")

    # OpenAI TTS 는 24kHz 로 내려오는데 hw: 장치가 그 레이트를 거부할 수 있다.
    # 장치 기본 레이트로 변환해서 재생한다.
    dev_rate = _device_rate(device, "output", rate)
    if dev_rate != rate:
        if pcm.ndim == 1:
            pcm = _resample(pcm, rate, dev_rate)
        else:
            pcm = np.column_stack([_resample(pcm[:, c], rate, dev_rate)
                                   for c in range(pcm.shape[1])])
        rate = dev_rate

    lead = int(rate * config.PLAY_LEAD_SEC)
    if lead > 0:
        # 쉬던 USB 스피커 앰프가 깨는 동안 첫 음절이 잘린다 — 무음으로 그 시간을 메운다
        pad = np.zeros((lead,) + pcm.shape[1:], dtype=pcm.dtype)
        pcm = np.concatenate([pad, pcm])

    try:
        if config.PLAY_KEEP_STREAM:
            _player.play(_to_mono(pcm), rate, device, PLAY_TAIL_SEC, should_stop)
            return
        sd.play(pcm, samplerate=rate, device=device)
        if should_stop is None:
            sd.wait()
            return
        # 재생 길이만큼 기다리되 짧게 끊어 종료 신호를 본다
        end = time.time() + len(pcm) / float(rate) + PLAY_TAIL_SEC
        while time.time() < end:
            if should_stop():
                stop_playback()
                return
            time.sleep(PLAY_POLL_SEC)
        sd.wait()
    except BaseException:
        # KeyboardInterrupt 포함. 정리 안 하면 스트림이 남아 다음 실행이
        # Device or resource busy 로 막힌다.
        stop_playback()
        raise


def _main() -> None:
    if "--list" in sys.argv:
        list_devices()
        print()
        print(f"기본 장치(입력, 출력): {sd.default.device}")
        for kind in ("input", "output"):
            try:
                dev = resolve_device(kind)
                info = sd.query_devices(dev, kind)
                print(f"{kind:6s} -> [{dev}] {info['name']} "
                      f"({int(info['default_samplerate'])}Hz)")
            except Exception as e:
                print(f"{kind:6s} -> 탐색 실패: {e}")

    elif "--test" in sys.argv:
        print("말씀하세요 (조용해지면 종료)...")
        wav = record_utterance()
        if wav is None:
            print("발화를 감지하지 못했다. VAD_THRESHOLD 를 조정할 것.")
        else:
            print(f"녹음 {len(wav)} bytes — 재생한다")
            play_wav(wav)


if __name__ == "__main__":
    try:
        _main()
    except KeyboardInterrupt:
        stop_playback()
        print("\n중단됨")
        sys.exit(130)
