"""자동 재측위 — 지도 전체 탐색이 같은 프로세스의 다른 스레드(위치·상태 조회)를 얼마나 막는지 잰다(11-06).

  sim\\.venv\\Scripts\\python.exe sim\\scenarios\\auto_reloc_gil.py <백엔드 app 폴더의 부모>

관제는 프로세스 하나(작업자 1개)에서 위치 조회 스레드와 이 탐색이 같이 돈다. 파이썬 GIL 때문에 계산 중 다른 스레드가
늦게 깨어날 수 있다. 0.1초마다 깨는 '조회 흉내' 스레드의 최대 지연을 방식별로 잰다.
  A) 지금 안: 같은 스레드 계산   B) 방향 묶음마다 time.sleep(0.005) 로 양보   C) 별도 프로세스에서 계산
"""
import importlib.util
import json
import multiprocessing as mp
import os
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CAND = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "sim", "out", "cand_autoreloc")
DATA = os.path.join(ROOT, "_자료", "2026-11-06_전역재측위")
MAPS = os.path.join(ROOT, "Backend", "static", "maps")


def _setup():
    spec = importlib.util.spec_from_file_location("scan_match", os.path.join(CAND, "app", "robot_control", "scan_match.py"))
    sm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sm)
    sys.path.insert(0, CAND)
    os.environ.setdefault("DATABASE_URL", "mysql+pymysql://u:p@127.0.0.1:1/x")
    from app.navigation import via_route as vr
    res, origin, negate, occ_th = vr._parse_yaml(os.path.join(MAPS, "UND_1F_병원.yaml"))
    pix = vr._read_pgm(os.path.join(MAPS, "UND_1F_병원.pgm"))
    occ = pix if negate else 1.0 - pix
    unknown = (occ > vr._FREE_THRESH) & (occ <= occ_th)
    grid = vr._OccGrid(vr._drop_small_blobs(occ > occ_th, res), res, origin, unknown)
    return sm, sm.field_for_grid(47, grid)


def _scans():
    rows = json.load(open(os.path.join(DATA, "scan_standby.json"), encoding="utf-8"))
    return [[p[:2] for p in r["points"]] for r in rows[:3]]


def _proc_job(scans):
    sm, f = _setup()
    return [sm.global_locate(f, s)["ok"] for s in scans]


def measure(label, work):
    gaps, stop = [], threading.Event()

    def ticker():
        last = time.perf_counter()
        while not stop.is_set():
            time.sleep(0.1)
            now = time.perf_counter()
            gaps.append(now - last - 0.1)
            last = now
    t = threading.Thread(target=ticker, daemon=True)
    t.start()
    time.sleep(0.5)
    t0 = time.perf_counter()
    work()
    dt = time.perf_counter() - t0
    time.sleep(0.3)
    stop.set()
    t.join()
    gaps.sort()
    print(f"| {label} | {dt:.1f}s | {max(gaps)*1000:.0f}ms | {gaps[int(len(gaps)*0.95)]*1000:.0f}ms |")


if __name__ == "__main__":
    sm, f = _setup()
    scans = _scans()
    sm.global_locate(f, scans[0])          # 후보 칸 캐시·첫 실행 비용 제외
    print("| 방식 | 3장 계산 시간 | 조회 스레드 최대 지연 | 95% 지연 |")
    print("|---|---|---|---|")
    measure("A 같은 스레드", lambda: [sm.global_locate(f, s) for s in scans])

    orig = sm._pose_dists

    def yielding(*a, **k):
        r = orig(*a, **k)
        time.sleep(0.005)
        return r
    sm._pose_dists = yielding
    measure("B 묶음마다 양보", lambda: [sm.global_locate(f, s) for s in scans])
    sm._pose_dists = orig

    ctx = mp.get_context("spawn")
    with ctx.Pool(1) as pool:
        pool.apply(_proc_job, (scans[:1],))   # 프로세스 띄우기·지도 읽기 비용은 미리
        measure("C 별도 프로세스", lambda: pool.apply(_proc_job, (scans,)))
