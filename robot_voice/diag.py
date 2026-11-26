"""듣기 진단 기록 — 듣기 한 번마다 결과·레벨을 남기고, 녹음이 있으면 wav 도 저장한다.

세션이 무응답으로 끝났을 때 원인을 가르려고 만들었다.
    no_onset      말 시작을 못 잡았다 — peak/threshold/max_loud_run 으로 문턱·빔 문제를 본다
    stt_empty     녹음은 했는데 STT 가 비웠다(로컬 서버 품질 필터) — wav 를 들어 본다
    dropped       환각·힌트 되풀이로 버렸다
    accepted      대답으로 썼다

저장 위치(config.DIAG_DIR):
    YYYYMMDD/turns.jsonl            한 줄에 듣기 한 번
    YYYYMMDD/HHMMSS_mmm_<tag>.wav   그 녹음(turns.jsonl 의 "wav" 가 파일명)

기록은 음성 흐름을 절대 막지 않는다 — 실패하면 경고만 남기고 넘어간다.
"""

import json
import logging
import os
import re
import shutil
import threading
from datetime import datetime, timedelta
from typing import Optional

from robot_voice import config

log = logging.getLogger("robot_voice.diag")

_lock = threading.Lock()
_writes = 0
# 이만큼 기록할 때마다 보관 기한·용량을 점검한다
PRUNE_EVERY = 50
_DAY_DIR = re.compile(r"^\d{8}$")


def _safe(tag: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "", tag)[:40] or "x"


def record(event: dict, wav: Optional[bytes] = None) -> None:
    """듣기 한 번의 결과를 남긴다. event 는 JSON 으로 직렬화할 수 있어야 한다."""
    global _writes
    if not config.DIAG_ENABLED:
        return
    try:
        now = datetime.now()
        day_dir = os.path.join(config.DIAG_DIR, now.strftime("%Y%m%d"))
        row = {"ts": now.isoformat(timespec="milliseconds"), **event}
        with _lock:
            os.makedirs(day_dir, exist_ok=True)
            if wav and config.DIAG_SAVE_WAV:
                name = f"{now:%H%M%S}_{now.microsecond // 1000:03d}_{_safe(event.get('tag', ''))}.wav"
                with open(os.path.join(day_dir, name), "wb") as f:
                    f.write(wav)
                row["wav"] = name
            with open(os.path.join(day_dir, "turns.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            _writes += 1
            due = _writes % PRUNE_EVERY == 0
        if due:
            threading.Thread(target=prune, daemon=True).start()
    except Exception as e:  # noqa: BLE001 — 진단 때문에 안내가 멈추면 안 된다
        log.warning("진단 기록 실패: %s", e)


def prune() -> None:
    """보관 기한이 지난 날짜 폴더를 지우고, wav 총량이 상한을 넘으면 오래된 wav 부터 지운다."""
    root = config.DIAG_DIR
    if not os.path.isdir(root):
        return
    try:
        cutoff = (datetime.now() - timedelta(days=config.DIAG_KEEP_DAYS)).strftime("%Y%m%d")
        days = sorted(d for d in os.listdir(root) if _DAY_DIR.match(d))
        for d in days:
            if d < cutoff:
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                log.info("진단 기록 정리: %s 삭제(보관 %d일 초과)", d, config.DIAG_KEEP_DAYS)

        # 날짜 폴더·파일명(HHMMSS_mmm)이 시간순이라 경로 정렬이 곧 오래된 순이다
        wavs = []
        for d in sorted(d for d in os.listdir(root) if _DAY_DIR.match(d)):
            p = os.path.join(root, d)
            wavs += [os.path.join(p, f) for f in sorted(os.listdir(p)) if f.endswith(".wav")]
        sizes = {w: os.path.getsize(w) for w in wavs}
        total = sum(sizes.values())
        limit = config.DIAG_MAX_MB * 1024 * 1024
        removed = 0
        for w in wavs:
            if total <= limit:
                break
            os.remove(w)
            total -= sizes[w]
            removed += 1
        if removed:
            log.info("진단 기록 정리: 오래된 wav %d개 삭제(상한 %.0fMB)", removed, config.DIAG_MAX_MB)
    except Exception as e:  # noqa: BLE001
        log.warning("진단 기록 정리 실패: %s", e)
