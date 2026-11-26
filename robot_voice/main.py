"""음성 안내 메인 루프.

관제가 세션을 열면(사양 1M — 운영자 수동 안내시작) 단계 엔진이 시나리오를 끝까지 돌린다.
그 전에는 조용히 대기한다. 상시 대기 근무의 응대 방식이 '단순 동작'이면(motion_cmd)
대기 장소에서 "큐리야, 앉아 / 일어서" 만 듣는다.

호출어는 **응답 대기 중에만 면제**된다. 로봇이 묻지 않은 시점에 들린 말은 옆 사람 대화일
가능성이 높으므로 "큐리야" 가 필요하다.
"""

import logging
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from robot_voice import ai, audio, auto_answer, config, diag, motion_cmd
from robot_voice.backend import BackendClient, BackendError
from robot_voice.scenario import DEST_INTENTS, FIXED_PHRASES, WAIT_ARRIVAL, ScenarioRunner, turn_cue_phrases
from robot_voice.wakeword import WakeGate, is_wake_only

log = logging.getLogger("robot_voice")

NOT_HEARD = "잘 못 들었어요. 다시 말씀해 주시겠어요?"

_wake = WakeGate()

# 로봇이 말한 뒤 아직 듣기 신호음을 내지 않았다 — 질문 한 번에 신호음 한 번.
# 환각을 버리고 다시 듣거나(listen 안의 반복) 동행 대기처럼 말없이 계속 듣는 동안에는
# 울리지 않는다. speak 와 listen 은 같은 스레드에서 차례로 불린다.
_cue_pending = False


# 발화 실패 시 다시 시도하는 횟수(첫 시도 포함). 합성 일시 오류·오디오 장치 점유는 한 번 더 하면 대개 된다.
TTS_ATTEMPTS = 2
TTS_RETRY_DELAY_SEC = 0.5


def speak(text: str, should_stop: Callable[[], bool] | None = None,
          on_start: Callable[[float], None] | None = None) -> bool:
    """TTS 로 말한다. 실패해도 루프를 끊지 않는다.

    합성(네트워크)이 느린 동안 안내가 종료됐으면 재생하지 않고, 재생 도중이면 끊는다.

    Args:
        on_start: 재생 직전에 음성 길이(초)를 받는다 — 멘트가 끝날 때에 맞춰 출발을 미리 요청하는 데 쓴다.

    Returns:
        말했으면(또는 종료로 일부러 안 말했으면) True, 다시 시도해도 못 말했으면 False.
    """
    global _cue_pending
    if not text:
        return True
    log.info("로봇: %s", text)
    for attempt in range(1, TTS_ATTEMPTS + 1):
        try:
            wav = ai.synthesize_cached(text)
            if should_stop and should_stop():
                return True
            if on_start:
                on_start(audio.play_seconds(wav))
            audio.play_wav(wav, should_stop=should_stop)
            _cue_pending = True
            return True
        except Exception as e:
            log.error("TTS 실패(%d/%d): %s", attempt, TTS_ATTEMPTS, e)
            if attempt < TTS_ATTEMPTS:
                time.sleep(TTS_RETRY_DELAY_SEC)
    return False


def _play_listen_cue() -> None:
    """질문 직후 첫 듣기에서만 신호음을 낸다. 실패해도 듣기는 계속한다."""
    global _cue_pending
    pending, _cue_pending = _cue_pending, False
    if not (pending and config.LISTEN_CUE_ENABLED):
        return
    try:
        audio.play_cue()
        if config.LISTEN_CUE_GAP_SEC > 0:
            time.sleep(config.LISTEN_CUE_GAP_SEC)
    except Exception as e:  # noqa: BLE001 — 신호음 때문에 대답을 못 받으면 안 된다
        log.warning("듣기 신호음 실패: %s", e)


def listen(timeout_sec: float, expect_response: bool = True,
           hints: list[dict] | None = None,
           should_stop: Callable[[], bool] | None = None,
           tag: str = "", prompt: str | None = None,
           max_sec: float | None = None, max_tokens: int = 0,
           rescue_short: bool = False, stop_on_garbled: bool = False,
           result: dict | None = None, silence_sec: float | None = None,
           retry_prompt: str | None = None) -> str | None:
    """발화를 받아 텍스트로.

    Args:
        timeout_sec: 발화 **시작** 대기 시간 (단계의 ResponseTimeoutSec)
        expect_response: True 면 로봇이 방금 질문한 상황 — 호출어를 면제한다
        hints: STT 어휘 힌트로 쓸 목적지 [{"name": ...}] — 진료과·민원명 오인식을 줄인다
        tag: 진단 기록에 붙일 이름(세션·단계) — diag
        prompt: hints 대신 STT 에 줄 힌트(예/아니요 단계) — ai.transcribe_ex
        max_sec: 말 시작 뒤 최대 녹음 길이 — audio.record_utterance
        max_tokens: 로컬 STT 생성 길이 상한 — ai.transcribe_ex
        rescue_short: 품질 필터가 버린 짧은 예/아니요를 살려 쓴다(예/아니요 단계) — ai.rescue_short_answer
        stop_on_garbled: 말소리는 있었는데 못 알아들은 녹음이 나오면 대기 시간이 남아도 멈춘다
            — 부른 쪽이 바로 되묻게(config.REASK_VOICED_SEC)
        result: 주면 {"garbled": 그 이유로 멈췄는가} 를 채운다
        silence_sec: 말 끝으로 볼 무음 길이 — audio.record_utterance(비우면 VAD_SILENCE_SEC)
        retry_prompt: 주면 품질 미달로 버려진 녹음만 이 힌트로 한 번 더 전사한다(신청서 단계)
            — _retry_with_hint

    호출어 면제는 **말이 대기 시간 안에 시작됐는가** 로 판단한다. 예전에는 면제 창을
    대기 시간만큼만 열고 STT 가 끝난 뒤에 확인해서, 제때 대답해도 발화+침묵+STT 가
    10초를 넘으면 '옆 사람 말' 로 버려졌다 — 목적 질문이 끝없이 반복된 원인이다.
    """
    if expect_response:
        # 대기 시간 계산 전에 낸다 — 신호음 시간이 대답 시간을 깎지 않게
        _play_listen_cue()
    deadline = time.time() + timeout_sec

    # STT 는 뒤에서 돌리고 그동안 다음 녹음을 이어 간다. 예전에는 녹음 → STT(약 2초) → 녹음
    # 순서라 STT 동안 마이크가 닫혀 있었다. 배경이 문턱을 늘 넘는 곳에서는 소음으로 녹음이
    # 시작돼 상한에서 끊기는 일이 반복되고, 듣는 시간의 32% 가 닫혀 그 사이 대답을 놓쳤다
    # (2026-10-08 세션 277). 앞 녹음의 결과가 대답이면 진행 중인 녹음은 버린다.
    pending: Future | None = None
    carry = b""                        # 상한에서 끊긴 녹음의 끝 — 다음 녹음 앞에 붙인다
    noise: float | None = None         # 앞 녹음의 소음 바닥 — 이어 들을 때 다시 재지 않는다
    answer: str | None = None
    fatal = False
    garbled = False

    def final(res: dict) -> bool:
        """이 결과로 듣기를 끝낸다 — 대답이 왔거나, STT 가 실패했거나, 되물어야 한다."""
        return (res.get("answer") is not None or res.get("fatal", False)
                or (stop_on_garbled and res.get("garbled", False)))

    def settled() -> bool:
        """앞 녹음의 결과가 나왔고 더 들을 필요가 없다."""
        return pending is not None and pending.done() and final(pending.result())

    def stop_rec() -> bool:
        return bool(should_stop and should_stop()) or settled()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt") as pool:
        while True:
            remaining = deadline - time.time()
            wav = None
            stats: dict = {}
            if remaining > 0:
                wav = audio.record_utterance(wait_sec=remaining, should_stop=stop_rec, stats=stats,
                                             max_sec=max_sec, noise_init=noise, silence_sec=silence_sec)
                noise = stats.get("noise") or noise
            if should_stop and should_stop():
                break
            # 앞 녹음의 결과를 먼저 본다(아직이면 기다린다) — 순서대로 처리한다
            if pending is not None:
                res, pending = pending.result(), None
                answer, fatal = res.get("answer"), res.get("fatal", False)
                if final(res):
                    garbled = answer is None and not fatal
                    break
            if remaining <= 0:
                break
            base = {"tag": tag, "expect": expect_response, "wait_sec": round(remaining, 1), **stats}
            if wav is None:
                diag.record(base)
                log.info("(응답 없음 %.0f초)", timeout_sec)
                break
            if carry:
                base["carry_sec"] = round(len(carry) / 2 / config.SAMPLE_RATE, 2)
                wav = audio.prepend_pcm(carry, wav)
            carry = audio.wav_tail(wav, config.VAD_PREROLL_SEC) if stats.get("max_len") else b""
            pending = pool.submit(_judge_utterance, wav, base, hints, prompt, max_tokens,
                                  expect_response, rescue_short, retry_prompt)

    _wake.close()
    if result is not None:
        result["garbled"] = garbled
    return answer


def _unheard(base: dict, wav: bytes, expect_response: bool) -> dict:
    """알아듣지 못한 녹음의 결과. 질문에 대한 대답 자리에서 음성 대역 소리가 뚜렷했으면
    '말은 했는데 못 알아들음'(garbled) — 부른 쪽이 기다리지 않고 되묻는다."""
    if not (expect_response and config.REASK_VOICED_SEC > 0):
        return {"answer": None}
    voiced = audio.voiced_seconds(wav)
    base["voiced_sec"] = round(voiced, 2)
    return {"answer": None, "garbled": voiced >= config.REASK_VOICED_SEC}


def _retry_with_hint(wav: bytes, hints: list[dict] | None, prompt: str, max_tokens: int,
                     base: dict) -> str:
    """품질 미달로 버려진 녹음을 힌트를 주고 한 번 더 전사한다. 확신도가 기준 이상일 때만 결과를 쓴다.

    힌트를 처음부터 주면 다른 말·잡음도 힌트 말로 받아쓴다 — 녹음 320개 재전사(2026-11-02):
    다른 단계 녹음 285개 중 11개가 "작성 완료"로 바뀌었고("준비됐어요" 포함) 확신도로도 가를 수 없었다.
    힌트 없이 뭔가 말소리를 냈지만 버려진 녹음만 다시 하면 실제 작성 완료 12/18 → 17/18, 오통과 0/285.
    """
    try:
        text, meta = ai.transcribe_ex(wav, hints, prompt, max_tokens)
    except Exception as e:
        log.warning("힌트 재전사 실패: %s", e)
        return ""
    low = min((sg.get("logprob", 0.0) for sg in meta.get("segments") or []), default=0.0)
    base["stt_retry"] = {**meta, "text": text}
    if not text or low < config.FORM_DONE_RETRY_MIN_LOGPROB:
        log.info("(힌트 재전사도 못 씀: %r 확신도 %.2f)", text[:40], low)
        return ""
    log.info("STT(힌트 재전사): %s (확신도 %.2f)", text, low)
    return text


def _judge_utterance(wav: bytes, base: dict, hints: list[dict] | None, prompt: str | None,
                     max_tokens: int, expect_response: bool, rescue_short: bool = False,
                     retry_prompt: str | None = None) -> dict:
    """녹음 하나를 전사·판정한다(STT 스레드). 반환: {"answer": 대답 또는 None, "fatal": STT 실패}"""
    # 최대 길이까지 찬 녹음은 발화가 아니라 '끊기지 않은 잡음'이다 — VAD 가 침묵을
    # 못 찾은 것이다. 사람이 한 번에 거는 말은 길어야 10초다. STT 를 호출하지 않는다.
    # 선행 구현 실측: 56초 덩어리 → "대기구역, 원무과, 접수, 진료실으로 안내해줘."
    # → 로봇이 실제로 출발했다.
    wav_sec = audio.wav_seconds(wav)
    base["wav_sec"] = round(wav_sec, 2)
    # 최대 길이까지 찬 녹음은 보통 끊기지 않은 잡음이라 STT 를 아꼈다. 그런데 그 근거는
    # ① 유료 API 낭비와 ② 어휘 힌트 에코 환각이었고, 로컬 STT(STT_LOCAL_URL)에서는
    # 둘 다 없다. 그리고 로컬 Whisper 는 잡음 속 짧은 발화를 잘 분리한다
    # (2026-09-30 실측: 8초 녹음에서 발화 2회를 [0-2s]·[2-4s] 로 정확히 나눠 냈다).
    # 팬 소음으로 15초 덩어리가 만들어지는 환경에서 **사용자 발화가 그 안에 묻혀
    # 통째로 버려지는 것**이 실제로 반복됐다(같은 날 3회 연속). 그래서 로컬일 때는
    # 버리지 않고 보낸다.
    if wav_sec >= config.VAD_MAX_SEC - 0.1 and not config.STT_LOCAL_URL:
        log.info("(버림: 최대 길이 %.1f초 도달 — 잡음) STT 를 호출하지 않는다", wav_sec)
        diag.record({**base, "outcome": "dropped", "reason": "최대 길이"}, wav)
        return {"answer": None}

    try:
        text, meta = ai.transcribe_ex(wav, hints, prompt, max_tokens)
    except Exception as e:
        log.error("STT 실패: %s", e)
        diag.record({**base, "outcome": "stt_error", "reason": str(e)[:120]}, wav)
        return {"answer": None, "fatal": True}
    base["stt"] = meta

    if not text and rescue_short:
        saved = ai.rescue_short_answer(meta.get("dropped_segments") or [],
                                       config.YES_NO_RESCUE_MAX_NO_SPEECH,
                                       config.YES_NO_RESCUE_MIN_LOGPROB)
        if saved:
            log.info("STT: %s (품질 미달로 버려진 짧은 대답을 살림)", saved)
            diag.record({**base, "outcome": "accepted", "rescued": True, "text": saved}, wav)
            return {"answer": saved}
    if not text and retry_prompt and meta.get("dropped"):
        text = _retry_with_hint(wav, hints, retry_prompt, max_tokens, base)
    if not text:
        log.info("(STT 결과 없음%s)",
                 f" — 품질 미달 {meta['dropped']}구간 버림" if meta.get("dropped") else "")
        res = _unheard(base, wav, expect_response)
        diag.record({**base, "outcome": "stt_empty"}, wav)
        return res
    fake = ai.is_hallucination(text, wav_sec)
    if not fake and ai.is_hint_echo(text, hints):
        fake = "어휘 힌트 되풀이"
    if not fake and hints:
        # 목적지 이름을 힌트로 준 듣기(목적지 단계)에서만 본다
        fake = ai.is_dest_noise(text, hints)
    if fake:
        # 소음을 받아쓴 문장 — 대답으로 쓰면 엉뚱하게 진행한다. 버리고 계속 듣는다.
        log.info("(버림: %s) %s", fake, text[:60])
        res = _unheard(base, wav, expect_response)
        diag.record({**base, "outcome": "dropped", "reason": fake, "text": text}, wav)
        return res
    log.info("STT: %s", text)

    passed, body, _ = _wake.check(text)
    if expect_response:
        # 로봇이 물었고 이 말은 대기 시간 안에 시작됐다 — 응답이다. 호출어가 섞였으면 뗀다.
        diag.record({**base, "outcome": "accepted", "text": text}, wav)
        return {"answer": body or text}
    if not passed:
        # 옆 사람 대화. 응답하지 않고 계속 듣는다.
        log.info("(무시) %s", text)
        diag.record({**base, "outcome": "ignored_no_wake", "text": text}, wav)
        return {"answer": None}
    diag.record({**base, "outcome": "accepted", "text": text}, wav)
    return {"answer": body or text}


def _diag_tag(session: dict | None) -> str:
    """진단 기록 이름 — 세션 번호와 지금 단계. DB 의 service_session 과 맞춰 볼 수 있게."""
    s = session or {}
    return f"s{s.get('id', 0)}_{s.get('currentStepCode') or ''}"


def _expects_yes_no(runner: ScenarioRunner) -> bool:
    """지금 단계가 예/아니요 대답을 기다리는가. 이동 단계(WaitFor=arrival)의 듣기는 동행 확인
    ("잘 따라오고 계신가요?")이라 역시 예/아니요다."""
    code = (runner.session or {}).get("currentStepCode")
    step = runner.steps.get(code or "") or {}
    return step.get("expectedIntent") == "YES_NO" or step.get("waitFor") == WAIT_ARRIVAL


_auto = auto_answer.AutoAnswer()
_NO_AUTO = object()     # 자동 대답이 꺼져 있거나 이 단계엔 넣을 말이 없다 — 마이크로 듣는다


def _auto_answer(runner: ScenarioRunner, timeout: float, stopped: Callable[[], bool]):
    """시험용 자동 대답(auto_answer.json 이 있을 때). 대답 문자열, 안내가 끝났으면 None, 아니면 _NO_AUTO."""
    conf = auto_answer.settings()
    if conf is None:
        return _NO_AUTO
    session = runner.session or {}
    step = runner.steps.get(session.get("currentStepCode") or "") or {}
    dests = auto_answer.dest_choices(runner.dest_slots(step.get("expectedIntent")))
    ans = _auto.answer(step, session, dests, conf)
    if ans is None:
        return _NO_AUTO
    runner.last_garbled = False
    _auto.last_given = None
    if auto_answer.wait(_auto.delay(conf), timeout, stopped):
        return None
    log.info("자동 대답[%s]: %s", session.get("currentStepCode") or "-", ans)
    _auto.last_given = ans
    return ans


def _listen_for_step(runner: ScenarioRunner, timeout: float,
                     stopped: Callable[[], bool]) -> str | None:
    """단계에 맞춰 듣는다. 예/아니요면 목적지 힌트 대신 "네, 아니요" · 녹음 최대 4초 · 전사 24토큰.

    runner.stop_on_garbled 가 참이면 못 알아들은 말소리에서 멈추고, 그랬는지를
    runner.last_garbled 에 남긴다 — ScenarioRunner._listen_once 가 바로 되묻는다.
    """
    auto = _auto_answer(runner, timeout, stopped)
    if auto is not _NO_AUTO:
        return auto
    tag = _diag_tag(runner.session)
    out: dict = {}
    opts = {"expect_response": True, "should_stop": stopped, "tag": tag,
            "stop_on_garbled": bool(getattr(runner, "stop_on_garbled", False)), "result": out}
    step = runner.steps.get((runner.session or {}).get("currentStepCode") or "") or {}
    if step.get("waitFor") == "operator" and step.get("voiceExitKeywords"):
        # 운영자 대기 중 음성 출구 — 목적지 이름 대신 알아들을 말을 힌트로("대기 자리로 안내해 줘")
        opts.update(prompt=", ".join(step["voiceExitKeywords"]),
                    max_sec=config.DEST_MAX_SEC, max_tokens=config.DEST_MAX_TOKENS)
    elif step.get("expectedIntent") == "FORM_COMPLETE":
        # 신청서 작성 대기 — 목적지 이름 힌트를 주지 않는다(받아쓰기가 목적지 이름으로 끌려갔다, 10-30)
        opts.update(prompt=config.STT_FORM_DONE_HINT, retry_prompt=config.STT_FORM_DONE_RETRY_HINT or None)
    elif _expects_yes_no(runner):
        opts.update(prompt=config.STT_YES_NO_HINT, max_sec=config.YES_NO_MAX_SEC,
                    max_tokens=config.YES_NO_MAX_TOKENS, rescue_short=True)
        # 질문에 대한 짧은 대답만 말 끝을 앞당긴다 — 이동 중 동행 확인은 걷는 소음·숨소리로 끊길 수 있어 그대로
        if step.get("expectedIntent") == "YES_NO" and config.YES_NO_SILENCE_SEC > 0:
            opts["silence_sec"] = config.YES_NO_SILENCE_SEC
    else:
        # 목적지 이름을 STT 힌트로 준다 — 판정 후보와 같은 목록(진료 목적이면 층 이동 목적지 제외)
        opts["hints"] = [{"name": sl["value"]}
                         for sl in runner.dest_slots(step.get("expectedIntent")) if sl.get("value")]
        if step.get("expectedIntent") in DEST_INTENTS:
            # 목적지 단계는 녹음 8초 · 전사 48토큰(병원 시험: 정확도 같고 7초 빠름)
            opts.update(max_sec=config.DEST_MAX_SEC, max_tokens=config.DEST_MAX_TOKENS)
    heard = listen(timeout, **opts)
    runner.last_garbled = bool(out.get("garbled"))
    return heard


_motion_link = motion_cmd.ReceiverLink()
_motion_pending: tuple[str, float] | None = None   # 마지막으로 보낸 (명령, 시각) — 동작 중 판정


def _standby_motion_once(ctx: dict) -> None:
    """단순 동작 근무 중 — 짧게 듣고 "큐리야, 앉아 / 일어서" 면 receiver 로 보낸다. 관제에는 올리지 않는다.

    호출어가 있는 말만 받는다(expect_response=False). "큐리야" 만 부르고 멈추면
    "네, 말씀하세요" 뒤 호출어 없이 한 번 더 듣는다.
    """
    _standby_invite(ctx)
    heard = listen(config.STANDBY_LISTEN_SEC, expect_response=False, tag="motion",
                   max_sec=config.MOTION_MAX_SEC, max_tokens=config.YES_NO_MAX_TOKENS)
    if not heard:
        return
    action = motion_cmd.classify(heard)
    if action is None and is_wake_only(heard):
        speak(config.WAKE_ACK)
        heard = listen(config.MOTION_FOLLOW_SEC, expect_response=True, tag="motion_follow",
                       max_sec=config.MOTION_MAX_SEC, max_tokens=config.YES_NO_MAX_TOKENS)
        if not heard:
            return
        action = motion_cmd.classify(heard)
    if action is None:
        log.info("동작 명령 아님: %s", heard)
        speak(motion_cmd.SAY_HELP)
        return
    _send_motion(action, heard)


def _send_motion(action: str, heard: str) -> None:
    """동작 명령을 판정해 receiver 로 보낸다(동작 중이면 거절 멘트)."""
    global _motion_pending
    ok, say = motion_cmd.decide(action, _motion_link, _motion_pending, time.time())
    log.info("동작 명령 %s (%s) — %s", action, heard, "보냄" if ok else say)
    speak(say)
    if ok:
        _motion_link.send(action)
        _motion_pending = (action, time.time())


_standby_was = None          # 마지막 상시대기 듣기 상태 — 바뀔 때만 기록한다
_invite_at = 0.0             # 마지막 대기 장소 안내 멘트 시각(monotonic). 0 = 듣기가 켜진 뒤 아직 안 함


def _standby_context(backend: BackendClient | None) -> dict | None:
    """지금 상시대기 근무로 손님 말을 들어야 하면 관제가 준 문맥을, 아니면 None.

    관제가 판정한다: 마이크 호출 방식 + 대기 장소 도착 + 응대 중 아님(agent_bridge.agent_standby_context).
    근무가 없을 때(mode=free)는 듣지 않는다 — 근무 밖 시간에 옆 사람 말로 로봇이 떠나지 않게.
    """
    global _standby_was, _invite_at
    if backend is None or not config.STANDBY_LISTEN_ENABLED:
        return None
    try:
        ctx = backend.standby_context()
    except BackendError as e:
        log.warning("상시대기 확인 실패: %s", e)
        ctx = {}
    on = bool(ctx.get("listening")) and ctx.get("mode") != "free"
    state = ("켜짐 — " + (ctx.get("workName") or "") + f" ({ctx.get('responseMode') or 'single'})") if on \
        else ("꺼짐" + (f" — {ctx.get('reason')}" if ctx.get("reason") else ""))
    if state != _standby_was:
        log.info("상시대기 듣기 %s", state)
        _standby_was = state
    if not on:
        _invite_at = 0.0     # 다음에 듣기가 켜지면(근무 시작·응대 뒤 복귀) 바로 안내 멘트
    return ctx if on else None


def _standby_invite(ctx: dict) -> None:
    """대기 장소 안내 멘트 — 듣기가 켜진 직후 한 번, 그 뒤 STANDBY_INVITE_SEC 마다."""
    global _invite_at
    now = time.monotonic()
    if _invite_at and (config.STANDBY_INVITE_SEC <= 0 or now - _invite_at < config.STANDBY_INVITE_SEC):
        return
    _invite_at = now
    mode = ctx.get("responseMode")
    text = (config.STANDBY_INVITE_MOTION if mode == "motion"
            else config.STANDBY_INVITE_SCENARIO if mode == "scenario"
            else config.STANDBY_INVITE_SINGLE)
    if text:
        speak(text)


def _standby_listen_once(backend: BackendClient, ctx: dict) -> None:
    """상시대기 근무 중 — "큐리야 …" 를 듣고 관제에 올린다.

    시나리오 응대면 관제가 세션을 열고 다음 run_once 가 이어받는다(들은 말은 쓰지 않는다).
    단발 응대면 관제가 목적지를 판정해 말하고 이동한다 — "큐리야" 만 들리면 목적지를 한 번 묻는다.
    단순 동작 응대면 앉아/일어서만 처리하고 관제에는 아무것도 올리지 않는다.
    """
    if ctx.get("responseMode") == "motion":
        _standby_motion_once(ctx)
        return
    _standby_invite(ctx)
    heard = listen(config.STANDBY_LISTEN_SEC, expect_response=False, tag="standby",
                   max_sec=config.DEST_MAX_SEC, max_tokens=config.DEST_MAX_TOKENS)
    if not heard:
        return
    if is_wake_only(heard) and ctx.get("responseMode") != "scenario":
        speak(config.STANDBY_ASK)
        hints = [{"name": d} for d in ctx.get("destinations") or [] if d]
        heard = listen(config.STANDBY_ASK_WAIT_SEC, expect_response=True, tag="standby_ask",
                       hints=hints or None, max_sec=config.DEST_MAX_SEC, max_tokens=config.DEST_MAX_TOKENS)
        if not heard:
            return
    log.info("상시대기 호출: %s", heard)
    try:
        res = backend.standby_call(heard)
    except BackendError as e:
        log.warning("상시대기 호출 거절: %s", e)
        speak(config.STANDBY_REJECT)
        return
    log.info("상시대기 호출 결과: %s", {k: res.get(k) for k in ("ok", "mode", "result", "sessionId", "message")})


def _idle(backend: BackendClient | None = None) -> None:
    """세션이 없을 때 — 상시대기 근무면 손님 호출(또는 단순 동작 명령)을 듣고, 아니면 쉰다."""
    ctx = _standby_context(backend)
    if ctx is not None:
        _standby_listen_once(backend, ctx)
    else:
        time.sleep(config.SESSION_POLL_SEC)


def run_once(backend: BackendClient) -> None:
    """관제가 연 세션을 감지해 시나리오를 돌린다."""
    try:
        cur = backend.current_session()
    except BackendError as e:
        log.warning("세션 폴링 실패: %s", e)
        _idle()
        return

    if not cur:
        _idle(backend)
        return

    runner = ScenarioRunner(backend, speak, lambda t: None)
    # 관제에서 안내를 종료하면 멘트·대답 대기를 그 자리에서 끊는다
    stopped = runner.closed_by_operator
    # interrupt: 방향·도착 안내를 말하는 중 장애물 멘트가 오면 끊는다(ScenarioRunner._speak_cue)
    runner.speak = lambda text, on_start=None, interrupt=None: speak(
        text, should_stop=(lambda: stopped() or interrupt()) if interrupt else stopped, on_start=on_start)
    runner.listen = lambda t: _listen_for_step(runner, t, stopped)
    runner._patch = _auto.tag_heard(runner._patch)
    try:
        # 관제가 이미 세션을 열어뒀으므로 엔진은 그것을 이어받는다.
        runner.resume(cur)
    finally:
        _wake.close()


def _prewarm(backend: BackendClient) -> None:
    try:
        scenarios = backend.bundle().get("scenarios", [])
    except BackendError:
        return
    steps = [st for sc in scenarios for st in sc.get("steps", [])]
    texts = [st.get("sayText") or "" for st in steps if "{" not in (st.get("sayText") or "")]
    # 치환 멘트("{목적지} 업무가 맞으신가요?")는 처음 말할 때 합성하느라 0.6~2초 늦었다(10-28 3F) — 목적지마다 채워 둔다
    for sc in scenarios:
        runner = ScenarioRunner(backend, lambda t: None, lambda t: None)
        runner.apply(sc)
        texts += runner.filled_texts(config.FIXED_TICKET_NO, config.FIXED_COUNTER_NO)
    texts += list(FIXED_PHRASES)
    # 단계마다 부르는 이름이 붙은 도착 안내("곧 원무과에 도착합니다.")와 꺾는 안내를 붙인 문장
    # 고객 목적지 단계는 목적지 사전의 도착지 음성명으로 말한다(관제 session_move) — 그 이름도 채워 둔다
    try:
        slot_names = [sl.get("spokenName") for sl in backend.slots()]
    except BackendError:
        slot_names = []
    texts += list(turn_cue_phrases([*(st.get("spokenPlaceName") for st in steps), *slot_names]))
    # 이동 중 진행 멘트(관제 send_move 가 /robot/nav cue 로 낸다). 방향 안내 앞에 붙은 문장은 합성 때 만든다
    texts += [st["progressText"] for st in steps if st.get("progressText")]
    if config.STANDBY_LISTEN_ENABLED:
        texts += [config.WAKE_ACK, *motion_cmd.FIXED_PHRASES]
        texts += [t for t in (config.STANDBY_ASK, config.STANDBY_REJECT, config.STANDBY_INVITE_SCENARIO,
                              config.STANDBY_INVITE_SINGLE, config.STANDBY_INVITE_MOTION) if t]
    made = ai.prewarm_tts(texts)
    log.info("멘트 음성 캐시 준비: 새로 %d개 (%s)", made, config.TTS_CACHE_DIR)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    missing = config.validate()
    if missing:
        log.error("필수 설정 누락: %s", ", ".join(missing))
        return 1

    if config.SPEAKER_VOLUME_PCT is not None:
        res = audio.set_speaker_volume(config.SPEAKER_MIXER_CARD, config.SPEAKER_MIXER_CONTROL, config.SPEAKER_VOLUME_PCT)
        log.info("스피커 음량 %d%% (%s %s): %s", config.SPEAKER_VOLUME_PCT, config.SPEAKER_MIXER_CARD,
                 config.SPEAKER_MIXER_CONTROL, res)

    backend = BackendClient()

    # 번들을 미리 받아둔다 — 관제가 죽어 있어도 캐시로 동작하려면 한 번은 받아야 한다.
    try:
        b = backend.bundle(force=True)
        log.info("번들 수신: 목적지 %d개, 시나리오 %d개",
                 len(b.get("slots", [])), len(b.get("scenarios", [])))
    except BackendError as e:
        log.error("번들 수신 실패: %s", e)

    # 단계 멘트를 미리 합성해 둔다 — 첫 안내부터 질문이 바로 나오게. 대기 루프를 막지 않게 뒤에서.
    threading.Thread(target=_prewarm, args=(backend,), daemon=True).start()
    if config.DIAG_ENABLED:
        log.info("진단 기록: %s (보관 %d일, wav %s, 상한 %.0fMB)", config.DIAG_DIR,
                 config.DIAG_KEEP_DAYS, "저장" if config.DIAG_SAVE_WAV else "안 함",
                 config.DIAG_MAX_MB)
        threading.Thread(target=diag.prune, daemon=True).start()

    if _wake.enabled:
        log.info("호출어: %s (응답 대기 중에만 면제)", " / ".join(config.WAKE_WORDS[:3]))

    log.info("세션 대기 — 관제에서 안내를 시작하면 시나리오가 돈다")
    if config.STANDBY_LISTEN_ENABLED:
        log.info("상시대기 음성 시작: 마이크 방식 근무 중 대기 장소에서 \"%s …\" 를 %.0f초씩 듣는다",
                 config.WAKE_WORDS[0] if config.WAKE_WORDS else "호출어", config.STANDBY_LISTEN_SEC)

    while True:
        try:
            run_once(backend)
        except KeyboardInterrupt:
            log.info("중단됨")
            audio.stop_playback()
            return 0
        except Exception as e:
            log.exception("루프 오류: %s", e)
            time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
