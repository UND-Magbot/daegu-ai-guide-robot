#!/usr/bin/env python3
"""자동문 열림 판정용 카메라 기록기 — 관제 PC 에서 실행(읽기 전용·무모션).

라이다는 NOS 에서 `robot_receiver/elevator_align/ev_scan_recorder.py` 로 같은 시간에 기록한다.
두 기록은 서로 다른 시계(관제 PC·NOS)를 쓰므로, 시작 직후 '맞춤 표식'을 한 번 남긴다:
로봇 정면 1m 에서 판자(또는 사람)가 왼쪽→오른쪽으로 한 번 지나간다. 분석기가 라이다 점 급증과
영상 변화가 겹치는 시점으로 두 시계를 맞춘다.

  # 기본: 로봇 본체 카메라 두 개를 초당 2장씩, 10분
  python door_cam_recorder.py --label door1f_closed
  # 앞 카메라만, 5분, 초당 4장
  python door_cam_recorder.py --label door1f_open --streams video1 --hz 4 --duration 300
  # 원본 주소 직접 지정(영상 파일로 시험할 때도 같은 방식)
  python door_cam_recorder.py --label test --src front=rtsp://10.21.31.103:8554/video1

출력: <out>/<label>_<시각>/
  meta.json            시작 시각(관제 PC 시계)·원본 주소·설정
  <스트림>/<ms>.jpg     프레임(파일 이름 = 관제 PC 시계 epoch 밀리초)
  index.jsonl          {"s": 스트림, "wall": epoch초, "t": 경과초, "f": 상대 경로, "w":, "h":}
                       연결 끊김·재연결은 {"s":, "t":, "ev": "lost"|"open"} 로 남긴다.
매 줄 flush 하므로 중간에 끊겨도 그때까지는 읽힌다. Ctrl+C 로 언제든 종료.

opencv-python 만 쓴다(robot_vision/requirements.txt 에 이미 있음).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time

import cv2

DEFAULT_URL_BASE = "rtsp://10.21.31.103:8554"   # 로봇 본체 카메라(MediaMTX 의 videoN_src 원본과 같음)
DEFAULT_STREAMS = "video1,video2"
OPEN_TIMEOUT_MS = 8000
READ_TIMEOUT_MS = 5000
RECONNECT_SEC = 3.0
MAX_WIDTH = 1280                                  # 이보다 넓으면 비율 유지 축소(용량 절약)
PROGRESS_SEC = 30.0


def parse_sources(streams: str, url_base: str, extra: list[str]) -> dict[str, str]:
    """--streams·--src → {이름: 주소}. --src 가 있으면 그것만 쓴다."""
    if extra:
        out = {}
        for item in extra:
            name, sep, url = item.partition("=")
            if not sep or not name or not url:
                raise ValueError(f"--src 는 이름=주소 형식이어야 합니다: {item}")
            out[name] = url
        return out
    names = [s.strip() for s in streams.split(",") if s.strip()]
    return {n: f"{url_base.rstrip('/')}/{n}" for n in names}


def fit_width(frame, max_width: int = MAX_WIDTH):
    h, w = frame.shape[:2]
    if w <= max_width:
        return frame
    scale = max_width / w
    return cv2.resize(frame, (max_width, round(h * scale)), interpolation=cv2.INTER_AREA)


def open_capture(url: str) -> cv2.VideoCapture:
    params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS,
              cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS]
    if url.startswith("rtsp://"):
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    return cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)


class StreamRecorder(threading.Thread):
    """스트림 하나를 계속 읽어(버퍼 지연 방지) 주기마다 최신 프레임을 저장한다."""

    def __init__(self, name: str, url: str, run_dir: str, hz: float, quality: int,
                 start_mono: float, writer, stop: threading.Event):
        super().__init__(name=f"rec-{name}", daemon=True)
        self.sname, self.url, self.hz, self.quality = name, url, hz, quality
        self.dir = os.path.join(run_dir, name)
        self.start_mono, self.writer, self.stop = start_mono, writer, stop
        self.saved = 0
        self.is_file = os.path.exists(url)
        os.makedirs(self.dir, exist_ok=True)

    def _elapsed(self) -> float:
        return round(time.monotonic() - self.start_mono, 3)

    def run(self) -> None:
        while not self.stop.is_set():
            cap = open_capture(self.url)
            if not cap.isOpened():
                self.writer({"s": self.sname, "t": self._elapsed(), "ev": "open_fail"})
                print(f"[CAM] {self.sname} 연결 실패 — {RECONNECT_SEC:.0f}s 뒤 재시도", flush=True)
                self.stop.wait(RECONNECT_SEC)
                continue
            self.writer({"s": self.sname, "t": self._elapsed(), "ev": "open"})
            print(f"[CAM] {self.sname} 연결됨 ({self.url})", flush=True)
            self._loop(cap)
            cap.release()
            if self.is_file:
                return
            if not self.stop.is_set():
                self.writer({"s": self.sname, "t": self._elapsed(), "ev": "lost"})
                print(f"[CAM] {self.sname} 끊김 — 재연결", flush=True)
                self.stop.wait(RECONNECT_SEC)

    def _loop(self, cap: cv2.VideoCapture) -> None:
        period = 1.0 / self.hz
        last_save = -1e9
        file_fps = cap.get(cv2.CAP_PROP_FPS) if self.is_file else 0
        while not self.stop.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                return
            if file_fps > 0:            # 파일 시험: 실시간 속도로 흘려 실제 기록과 같은 간격을 만든다
                time.sleep(1.0 / file_fps)
            now = time.monotonic()
            if now - last_save < period:
                continue
            last_save = now
            self._save(fit_width(frame))

    def _save(self, frame) -> None:
        wall = time.time()
        rel = os.path.join(self.sname, f"{int(wall * 1000)}.jpg")
        cv2.imwrite(os.path.join(os.path.dirname(self.dir), rel), frame,
                    [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        h, w = frame.shape[:2]
        self.writer({"s": self.sname, "wall": round(wall, 3), "t": self._elapsed(),
                     "f": rel.replace(os.sep, "/"), "w": w, "h": h})
        self.saved += 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="자동문 열림 판정용 카메라 기록기(무모션)")
    ap.add_argument("--label", required=True, help="기록 이름(예: door1f_closed)")
    ap.add_argument("--out", default="door_rec", help="저장 폴더")
    ap.add_argument("--streams", default=DEFAULT_STREAMS, help="스트림 이름 목록(쉼표)")
    ap.add_argument("--url-base", default=DEFAULT_URL_BASE)
    ap.add_argument("--src", action="append", default=[], help="이름=주소 (반복 가능, 지정 시 --streams 무시)")
    ap.add_argument("--hz", type=float, default=2.0, help="스트림별 저장 주기")
    ap.add_argument("--duration", type=float, default=600.0, help="기록 시간(초)")
    ap.add_argument("--quality", type=int, default=85, help="JPEG 품질")
    args = ap.parse_args(argv)
    # Windows 콘솔(cp949)에서 표현 못 하는 문자가 있어도 기록이 멈추지 않게 한다.
    sys.stdout.reconfigure(errors="replace")

    try:
        sources = parse_sources(args.streams, args.url_base, args.src)
    except ValueError as e:
        print(f"[ERROR] {e}")
        return 2
    if not sources or args.hz <= 0:
        print("[ERROR] 스트림이 없거나 --hz 가 0 이하입니다.")
        return 2

    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(args.out, f"{args.label}_{stamp}")
    os.makedirs(run_dir, exist_ok=True)
    start_wall, start_mono = time.time(), time.monotonic()
    meta = {"label": args.label, "host": socket.gethostname(), "sources": sources, "hz": args.hz,
            "start_wall": start_wall, "start_local": time.strftime("%Y-%m-%d %H:%M:%S"),
            "clock": "관제 PC", "sync": "시작 직후 정면 1m 판자 통과(라이다 기록과 시계 맞춤용)"}
    with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    index = open(os.path.join(run_dir, "index.jsonl"), "a", encoding="utf-8")
    lock = threading.Lock()

    def write(obj: dict) -> None:
        with lock:
            index.write(json.dumps(obj, ensure_ascii=False) + "\n")
            index.flush()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    recs = [StreamRecorder(n, u, run_dir, args.hz, args.quality, start_mono, write, stop)
            for n, u in sources.items()]
    for r in recs:
        r.start()
    print(f"[REC] 시작 {meta['start_local']} (관제 PC 시계) → {run_dir}", flush=True)
    print("[REC] 지금 로봇 정면 1m 에서 판자를 한 번 지나가게 해 주세요(시계 맞춤 표식).", flush=True)

    last_progress = 0.0
    try:
        while not stop.is_set():
            elapsed = time.monotonic() - start_mono
            if elapsed >= args.duration or not any(r.is_alive() for r in recs):
                break
            if elapsed - last_progress >= PROGRESS_SEC:
                last_progress = elapsed
                counts = ", ".join(f"{r.sname} {r.saved}" for r in recs)
                print(f"[REC] {elapsed:.0f}s 저장 {counts}", flush=True)
            stop.wait(0.5)
    finally:
        stop.set()
        for r in recs:
            r.join(timeout=OPEN_TIMEOUT_MS / 1000 + 2)
        index.close()
        counts = ", ".join(f"{r.sname} {r.saved}" for r in recs)
        print(f"[REC] 종료 — {counts}, {time.monotonic() - start_mono:.0f}s → {run_dir}", flush=True)
    return 0 if any(r.saved for r in recs) else 1


if __name__ == "__main__":
    sys.exit(main())
