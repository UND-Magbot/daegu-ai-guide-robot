"""수동 녹화 → 영상 관리 재생 (현행 vs 변경, 11-03).

실측(11-03): 관제 컨테이너에 FFmpeg 가 없어 cv2 대체 모드가 mp4v 코덱으로 저장 → 브라우저 재생 불가.
변경: FFmpeg 없으면 녹화 시작 거부, 파일명에 녹화 레코드 번호(_r<id>)를 넣어 재생 시 정확히 찾기,
ffmpeg 진행 출력 끔(-nostats -loglevel error).

무대: 임시 녹화 폴더. 실제 ffmpeg·카메라는 쓰지 않는다.
가정: 녹화 워커 start() 는 가짜 — 실제 _run_ffmpeg 와 같은 규칙으로 접두사를 정하고
(주입된 접두사가 없으면 "<로봇>_cam<모듈>_<종류>_<시각>") "<접두사>_seg000.mp4" 를 하나 쓴다.
현행 이름의 시각은 실제 초 대신 녹화마다 1초씩 늘린 값을 쓴다(같은 초 덮어쓰기 방지).
"""
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from harness import scenario

ROBOT = 13


class Stage:
    def __init__(self, sim, ffmpeg=True):
        from app.database.database import SessionLocal
        from app.database import models as M
        import app.recording.service as svc
        import app.recording.manager as mgr
        import app.recording.routes as rt
        import app.recording.worker as wk
        import app.navigation.send_move as sm
        sim.rewire_late_modules()
        self.M, self.Session, self.mgr, self.rt, self.wk = M, SessionLocal, mgr, rt, wk

        self.tmp = tempfile.mkdtemp(prefix="recsim_")
        svc.BACKEND_ROOT = self.tmp
        svc.RECORDINGS_BASE = mgr.RECORDINGS_BASE = os.path.join(self.tmp, "recordings")

        shutil.which = (lambda name, *a, **k: "/usr/bin/ffmpeg") if ffmpeg else (lambda *a, **k: None)
        sm.is_nav_active = lambda: False
        svc.get_active_camera_modules = lambda db, rid: [
            MagicMock(id=m, camera_info=MagicMock(StreamType="rtsp")) for m in (1, 5, 10)]
        svc.build_stream_url = lambda db, module, robot=None: f"rtsp://cam/{module.id}"

        self.started = []
        self.n_segs = 1
        self.svc = svc
        clock = [datetime(2026, 11, 3, 13, 50, 0)]

        def fake_start(w):
            if not w.file_prefix:   # 현행 _run_ffmpeg 규칙
                clock[0] += timedelta(seconds=1)
                w.file_prefix = f"{w.robot_id}_cam{w.module_id}_{w.record_type}_{clock[0]:%Y%m%d_%H%M%S}"
            os.makedirs(w.output_dir, exist_ok=True)
            n = len(self.started) + 1
            for k in range(self.n_segs):   # 5분마다 1개 — n_segs 개면 그만큼 긴 녹화
                tag = f"영상#{n}" if self.n_segs == 1 else f"영상#{n}-{k}"
                with open(os.path.join(w.output_dir, f"{w.file_prefix}_seg{k:03d}.mp4"), "w", encoding="utf-8") as f:
                    f.write(tag)
            self.started.append(w.file_prefix)

        wk.CameraRecordingWorker.start = fake_start
        wk.CameraRecordingWorker.stop = lambda w: None

        db = SessionLocal()
        db.add(M.RobotInfo(id=ROBOT, RobotName="R13", SerialNumber="SN13"))
        db.commit()
        db.close()

    def record(self, module_id):
        r = self.mgr.start_manual_recording(ROBOT, module_id)
        if "error" in r:
            return {"start": r["error"]}
        self.mgr.stop_manual_recording(ROBOT, module_id)
        return {"start": "ok"}

    def recs(self):
        db = self.Session()
        try:
            return [(r.id, r.ModuleId, r.Status) for r in
                    db.query(self.M.RecordingInfo).order_by(self.M.RecordingInfo.id).all()]
        finally:
            db.close()

    def listing(self):
        """영상 관리 목록 API 의 (세그먼트 수, 재생 주소들) — 첫 그룹"""
        db = self.Session()
        try:
            items = self.svc.get_recordings_grouped(db, size=100)["items"]
            return [{"count": it["segment_count"], "urls": [s["stream_url"] for s in it["segments"]]}
                    for it in items]
        finally:
            db.close()

    def play_url(self, url):
        """목록이 준 재생 주소(/api/recordings/<id>/stream[?seg=N]) → 재생 API 가 고르는 파일 내용"""
        path, _, query = url.partition("?")
        rec_id = int(path.split("/")[3])
        db = self.Session()
        try:
            rec = db.query(self.M.RecordingInfo).get(rec_id)
            if query:
                f = self.rt._find_segment_file(rec, db, seg=int(query.split("=")[1]))
            else:
                f = self.rt._find_segment_file(rec, db)
            if not f:
                return None
            with open(f, encoding="utf-8") as fh:
                return fh.read()
        finally:
            db.close()

    def play(self, rec_id):
        """재생 API 가 고르는 파일의 내용(영상#N) — 없으면 None"""
        db = self.Session()
        try:
            rec = db.query(self.M.RecordingInfo).get(rec_id)
            path = self.rt._find_segment_file(rec, db)
            if not path:
                return None
            with open(path, encoding="utf-8") as f:
                return f.read()
        finally:
            db.close()


@scenario("F1 FFmpeg 없음 → 녹화 시작 거부, 레코드·파일 안 생김 (현행: 시작되어 재생 불가 파일 생성)",
          expect=lambda r: r["res"]["start"] != "ok" and r["recs"] == [] and r["files"] == 0)
def f1(sim):
    st = Stage(sim, ffmpeg=False)
    res = st.record(5)
    return {"res": res, "recs": st.recs(), "files": len(st.started)}


@scenario("F2 FFmpeg 있음 → 시작·중지 정상, 완료 상태 (회귀)",
          expect=lambda r: r["res"]["start"] == "ok" and r["recs"] == [(1, 5, "completed")]
          and r["play"] == "영상#1")
def f2(sim):
    st = Stage(sim)
    res = st.record(5)
    return {"res": res, "recs": st.recs(), "play": st.play(1), "prefix": st.started}


@scenario("P1 같은 날 같은 카메라 2회 녹화 → 두 번째 재생 시 두 번째 영상 (현행: 첫 영상)",
          expect=lambda r: r["play1"] == "영상#1" and r["play2"] == "영상#2")
def p1(sim):
    st = Stage(sim)
    st.record(5)
    st.record(5)
    return {"play1": st.play(1), "play2": st.play(2), "recs": st.recs()}


@scenario("P2 cam10 녹화가 먼저 있는 날 cam1 녹화 재생 → cam1 영상 (현행: cam10 영상)",
          expect=lambda r: r["play_cam1"] == "영상#2" and r["play_cam10"] == "영상#1")
def p2(sim):
    st = Stage(sim)
    st.record(10)
    st.record(1)
    return {"play_cam10": st.play(1), "play_cam1": st.play(2)}


@scenario("L1 예전 이름 파일(번호 없음) 레코드 재생 → 그대로 찾음 (회귀)",
          expect=lambda r: r["play"] == "예전")
def l1(sim):
    st = Stage(sim)
    d = os.path.join(st.tmp, "recordings", str(ROBOT), "2026-11-03")
    os.makedirs(d)
    with open(os.path.join(d, "13_cam5_manual_20261103_135050_seg000.mp4"), "w", encoding="utf-8") as f:
        f.write("예전")
    db = st.Session()
    db.add(st.M.RecordingInfo(id=263, RobotId=ROBOT, ModuleId=5, GroupId="g", RecordType="manual",
                              VideoPath="recordings/13/2026-11-03", Status="completed",
                              RecordStart=datetime(2026, 11, 3, 13, 50, 48)))
    db.commit()
    db.close()
    return {"play": st.play(263)}


@scenario("L2 예전 파일과 새 파일이 섞인 날 → 각자 자기 파일 (회귀+변경)",
          expect=lambda r: r["old"] == "예전" and r["new"] == "영상#1")
def l2(sim):
    st = Stage(sim)
    d = os.path.join(st.tmp, "recordings", str(ROBOT), datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(d)
    with open(os.path.join(d, "13_cam5_manual_20000101_000000_seg000.mp4"), "w", encoding="utf-8") as f:
        f.write("예전")
    db = st.Session()
    db.add(st.M.RecordingInfo(id=900, RobotId=ROBOT, ModuleId=5, GroupId="old", RecordType="manual",
                              VideoPath=f"recordings/13/{os.path.basename(d)}", Status="completed",
                              RecordStart=datetime(2000, 1, 1)))
    db.commit()
    db.close()
    st.record(5)
    new_id = [r[0] for r in st.recs() if r[0] != 900][0]
    return {"old": st.play(900), "new": st.play(new_id)}


@scenario("Q1 ffmpeg 녹화 명령에 진행 출력 끔(-nostats, -loglevel error) (현행: 없음 — 관찰용 차이)",
          expect=lambda r: "-nostats" in r["cmd"] and r["cmd"][r["cmd"].index("-loglevel") + 1] == "error")
def q1(sim):
    st = Stage(sim)
    wk = st.wk
    seen = {}

    w = wk.CameraRecordingWorker(url="rtsp://cam/5", output_dir=os.path.join(st.tmp, "q"),
                                 robot_id=ROBOT, module_id=5, record_type="manual")
    os.makedirs(w.output_dir)

    class FakeProc:
        stdin = stderr = None

        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            w._stop_event.set()

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    real = wk.subprocess.Popen
    wk.subprocess.Popen = FakeProc
    try:
        w._run_ffmpeg()
    finally:
        wk.subprocess.Popen = real
    return {"cmd": seen.get("cmd", [])}


# ── 5분 넘는 녹화(파일 여러 개) ──────────────────────────────
def _played(st):
    return [[st.play_url(u) for u in g["urls"]] for g in st.listing()]


@scenario("G1 15분 녹화(파일 3개) → 목록 세그먼트 3개, 차례로 3개 파일 재생 (현행: 1개, 첫 5분만)",
          expect=lambda r: r["played"] == [["영상#1-0", "영상#1-1", "영상#1-2"]])
def g1(sim):
    st = Stage(sim)
    st.n_segs = 3
    st.record(5)
    return {"played": _played(st), "list": st.listing()}


@scenario("G2 짧은 녹화(파일 1개) → 세그먼트 1개, 주소 그대로 (회귀)",
          expect=lambda r: r["list"][0]["count"] == 1 and "?" not in r["list"][0]["urls"][0]
          and r["played"] == [["영상#1"]])
def g2(sim):
    st = Stage(sim)
    st.record(5)
    return {"played": _played(st), "list": st.listing()}


@scenario("G3 긴 녹화 뒤 짧은 녹화(같은 카메라) → 각 그룹이 자기 파일만 (변경)",
          expect=lambda r: sorted(map(tuple, r["played"])) == [("영상#1-0", "영상#1-1"), ("영상#2",)])
def g3(sim):
    st = Stage(sim)
    st.n_segs = 2
    st.record(5)
    st.n_segs = 1
    st.record(5)
    return {"played": _played(st)}


@scenario("G4 없는 세그먼트 번호(seg=9) → 파일 없음(404) (변경)",
          expect=lambda r: r["seg9"] is None)
def g4(sim):
    st = Stage(sim)
    st.n_segs = 2
    st.record(5)
    try:
        return {"seg9": st.play_url("/api/recordings/1/stream?seg=9")}
    except TypeError as e:   # 현행 _find_segment_file 은 seg 를 모름
        return {"seg9": f"지원 안 함: {e}"}


@scenario("G5 예전 이름 파일 레코드 → 목록 세그먼트 1개·재생 (회귀)",
          expect=lambda r: r["list"] == [{"count": 1, "urls": ["/api/recordings/263/stream"]}]
          and r["played"] == [["예전"]])
def g5(sim):
    st = Stage(sim)
    d = os.path.join(st.tmp, "recordings", str(ROBOT), "2026-11-03")
    os.makedirs(d)
    with open(os.path.join(d, "13_cam5_manual_20261103_135050_seg000.mp4"), "w", encoding="utf-8") as f:
        f.write("예전")
    db = st.Session()
    db.add(st.M.RecordingInfo(id=263, RobotId=ROBOT, ModuleId=5, GroupId="g", RecordType="manual",
                              VideoPath="recordings/13/2026-11-03", Status="completed",
                              RecordStart=datetime(2026, 11, 3, 13, 50, 48),
                              RecordEnd=datetime(2026, 11, 3, 13, 50, 50)))
    db.commit()
    db.close()
    return {"list": st.listing(), "played": _played(st)}
