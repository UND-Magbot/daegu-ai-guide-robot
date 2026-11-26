#!/usr/bin/env python3
"""안내 중 전방 카메라 사람 인식 — 기록 단계(C1, 2026-10-29). 관제 PC 의 vision 컨테이너에서 실행한다.

라이다 감지(NOS robot_receiver/obstacle_guard.py)와 합치기 전에, 어안 전방 카메라에서 '앞 1.8m 안의 사람' 이
화면 어디에 어떤 크기로 잡히는지 정하려고 기록만 한다. 로봇을 멈추지 않는다.

    영상   MediaMTX 가 이미 받는 로봇 원본(video1_src, 640x360 약 30장/초)을 도커 망 안에서 읽는다 — Wi-Fi 부담 없음
    켜기   관제(navigation/obstacle_hold)가 안내 시나리오 이동 중 매 주기 UDP 로 보낸다(NOS 감지 노드와 같은 내용 + 위치).
           ttl 초 안에 다음 신호가 없으면 인식을 멈춘다(영상은 계속 받아 둔다 — 다시 열면 1.4초 걸림)
    인식   YOLOv8n(robot_vision/yolov8n.pt) 사람(class 0)만, CPU, 초당 VISION_HZ 장
    기록   VISION_REC_DIR/<시작 시각>/index.jsonl 한 줄 = {"t": 관제 시계 epoch, "pose", "persons": [[x1,y1,x2,y2,conf]], "ms", "w", "h"}
           + VISION_JPG_EVERY_SEC 마다 jpg(파일 이름 = epoch 밀리초). 폴더가 VISION_REC_MAX_MB 를 넘으면 사진부터 멈춘다
    답     UDP 요청마다 {"armed", "fps", "frameAge", "persons", "inferMs"}
    NOS    (C3) 켜져 있는 동안 추론마다 사람 박스를 몸 기준 위치로 바꿔 NOS 감지 노드에 보낸다(VISION_NOS_HOST 비면 안 보냄)
           {"action": "CAMERA", "persons": [[앞 x, 왼쪽 y, conf]], "age": 찍힌 뒤 지난 초} — 사람이 없어도 빈 목록
"""
from __future__ import annotations

import json
import math
import os
import socket
import threading
import time

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|flags;low_delay")

import cv2  # noqa: E402

RTSP_URL = os.environ.get("VISION_RTSP_URL", "rtsp://mediamtx:8554/video1_src")
MODEL = os.environ.get("VISION_MODEL", os.path.join(os.path.dirname(os.path.abspath(__file__)), "yolov8n.pt"))
PORT = int(os.environ.get("VISION_PORT", "40110"))
HZ = float(os.environ.get("VISION_HZ", "10"))
IMGSZ = int(os.environ.get("VISION_IMGSZ", "416"))
CONF = float(os.environ.get("VISION_CONF", "0.35"))
THREADS = int(os.environ.get("VISION_THREADS", "2"))
REC_DIR = os.environ.get("VISION_REC_DIR", "/rec")
REC_MAX_MB = float(os.environ.get("VISION_REC_MAX_MB", "500"))
JPG_EVERY = float(os.environ.get("VISION_JPG_EVERY_SEC", "1.0"))
RECONNECT_SEC = 3.0
NOS_HOST = os.environ.get("VISION_NOS_HOST", "")
NOS_PORT = int(os.environ.get("VISION_NOS_PORT", "40100"))

# 보정(10-29 669 서서, 사람 정면 1.0~3.0m·좌우 0.5m, 라이다와 짝 62개). 640x360 원본 기준
CAL_HORIZON = 181.0    # 직선거리 r = CAL_K / (박스 아래 끝 − CAL_HORIZON)   (잔차 8.7px)
CAL_K = 190.0
CAL_CX = 314.0         # 방향(도, 왼쪽 +) = (CAL_CX − 박스 가운데 x) / CAL_PX_PER_DEG   (잔차 12.6px)
CAL_PX_PER_DEG = 5.54
CAL_LATENCY = 0.40     # 영상이 라이다보다 늦은 시간(초) — 받은 시각보다 이만큼 앞에 찍혔다
CAL_CLIP = 355         # 아래 끝이 이 이상이면 발이 화면 밖 — 거리는 1.06m 이하로만 안다
CLIP_MIN_H = 180       # 잘린 박스는 이 높이 이상만(멀리 선 작은 오검출 제외)
H_RATIO = 0.5          # 안 잘린 박스는 높이가 거리로 예상한 높이(26 + 395/r)의 이 비율 이상만


def to_body(box: list) -> list | None:
    """[x1,y1,x2,y2,conf] → [앞 x, 왼쪽 y, conf] (몸 기준 m). 보정 범위 밖이거나 크기가 거리와 안 맞으면 None."""
    x1, y1, x2, y2, c = box
    h = y2 - y1
    if y2 >= CAL_CLIP:
        if h < CLIP_MIN_H:
            return None
        y2 = CAL_CLIP
    elif y2 <= CAL_HORIZON + 20:
        return None                                  # 5m 너머 — 판단 범위 밖
    r = CAL_K / (y2 - CAL_HORIZON)
    if y2 < CAL_CLIP and h < H_RATIO * (26 + 395 / r):
        return None
    th = math.radians((CAL_CX - (x1 + x2) / 2) / CAL_PX_PER_DEG)
    return [round(r * math.cos(th), 2), round(r * math.sin(th), 2), c]


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Grabber:
    """영상을 계속 받아 가장 최근 장만 들고 있는다. 끊기면 다시 연다."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.lock = threading.Lock()
        self.frame = None
        self.t = 0.0
        self.count = 0
        self.ok = False
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self) -> None:
        while True:
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                self.ok = False
                log(f"영상 열기 실패 — {RECONNECT_SEC:.0f}초 뒤 다시: {self.url}")
                time.sleep(RECONNECT_SEC)
                continue
            log(f"영상 열림: {self.url}")
            self.ok = True
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                with self.lock:
                    self.frame, self.t, self.count = frame, time.time(), self.count + 1
            cap.release()
            self.ok = False
            log("영상 끊김 — 다시 연다")
            time.sleep(1.0)

    def latest(self):
        with self.lock:
            return self.frame, self.t


class Recorder:
    def __init__(self, root: str, max_mb: float) -> None:
        self.dir = os.path.join(root, time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(self.dir, exist_ok=True)
        self.index = open(os.path.join(self.dir, "index.jsonl"), "a", encoding="utf-8")
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.size = 0
        self.last_jpg = 0.0
        self.full = False
        log(f"기록 시작: {self.dir} (최대 {max_mb:.0f}MB)")

    def write(self, row: dict, frame) -> None:
        if self.full:
            return
        if frame is not None and row["t"] - self.last_jpg >= JPG_EVERY:
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                name = f"{int(row['t'] * 1000)}.jpg"
                with open(os.path.join(self.dir, name), "wb") as f:
                    f.write(buf.tobytes())
                self.size += len(buf)
                self.last_jpg = row["t"]
                row["jpg"] = name
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.index.write(line)
        self.index.flush()
        self.size += len(line)
        if self.size > self.max_bytes:
            self.full = True
            self.index.close()
            log(f"기록 상한 도달 — 멈춘다: {self.dir}")


class Watch:
    def __init__(self) -> None:
        import torch
        torch.set_num_threads(THREADS)
        from ultralytics import YOLO
        import numpy as np
        self.model = YOLO(MODEL)
        # 첫 추론은 모델 준비로 1초 가까이 걸린다 — 켠 뒤 첫 이동의 앞 1초를 놓치지 않게 미리 한 번 돌린다
        self.model.predict(np.zeros((360, 640, 3), np.uint8), imgsz=IMGSZ, classes=[0], verbose=False)
        self.grab = Grabber(RTSP_URL)
        self.lock = threading.Lock()
        self.armed_until = 0.0
        self.pose = None
        self.persons: list = []
        self.infer_ms = None
        self.last_infer = 0.0
        self.rec: Recorder | None = None
        self.was_armed = False
        self.done = 0
        self.nos = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if NOS_HOST else None
        threading.Thread(target=self.serve, daemon=True).start()
        log(f"전방 사람 인식 시작: {RTSP_URL}, 모델 {os.path.basename(MODEL)}, 초당 {HZ:g}장, 크기 {IMGSZ}, conf {CONF}, "
            f"NOS {NOS_HOST + ':' + str(NOS_PORT) if NOS_HOST else '안 보냄'}")

    def serve(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", PORT))
        while True:
            try:
                data, addr = sock.recvfrom(8192)
                req = json.loads(data.decode())
            except (OSError, ValueError):
                continue
            now = time.time()
            with self.lock:
                if req.get("arm"):
                    if now >= self.armed_until:
                        log("인식 켬(관제 안내 이동)")
                    self.armed_until = now + float(req.get("ttl", 3.0))
                    self.pose = req.get("pose")
                frame, ft = self.grab.latest()
                reply = {"armed": now < self.armed_until, "frameAge": round(now - ft, 2) if ft else None,
                         "persons": self.persons, "inferMs": self.infer_ms}
            if req.get("reply", True):
                try:
                    sock.sendto(json.dumps(reply).encode(), addr)
                except OSError:
                    pass

    def step(self) -> None:
        now = time.time()
        with self.lock:
            armed = now < self.armed_until
            pose = self.pose
        if not armed:
            if self.was_armed:
                log("인식 끔(관제 신호 끊김)")
                self.was_armed = False
            self.persons = []
            return
        self.was_armed = True
        frame, ft = self.grab.latest()
        if frame is None or now - ft > 1.0:
            return                                   # 영상이 없거나 묵었다 — 판단하지 않는다
        t0 = time.perf_counter()
        r = self.model.predict(frame, imgsz=IMGSZ, classes=[0], conf=CONF, verbose=False)[0]
        ms = round((time.perf_counter() - t0) * 1000, 1)
        persons = [[round(v) for v in b] + [round(c, 2)] for b, c in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist())]
        body = [b for b in map(to_body, persons) if b is not None]
        with self.lock:
            self.persons, self.infer_ms = persons, ms
        if self.nos is not None:
            msg = {"action": "CAMERA", "persons": body, "age": round(time.time() - ft + CAL_LATENCY, 3)}
            try:
                self.nos.sendto(json.dumps(msg).encode(), (NOS_HOST, NOS_PORT))
            except OSError:
                pass                                     # 망 끊김 — NOS 는 카메라 정보가 묵으면 쓰지 않는다
        if self.rec is None and REC_DIR:
            self.rec = Recorder(REC_DIR, REC_MAX_MB)       # 서비스가 떠 있는 동안 한 폴더에 이어 쓴다
        if self.rec is not None:
            h, w = frame.shape[:2]
            self.rec.write({"t": round(ft, 3), "pose": pose, "persons": persons, "body": body, "ms": ms, "w": w, "h": h}, frame)
        self.done += 1

    def run(self) -> None:
        period = 1.0 / HZ
        while True:
            t0 = time.time()
            try:
                self.step()
            except Exception as e:  # noqa: BLE001 — 한 장 실패로 서비스가 죽지 않게
                log(f"인식 실패(계속): {e}")
            time.sleep(max(0.0, period - (time.time() - t0)))


if __name__ == "__main__":
    Watch().run()
