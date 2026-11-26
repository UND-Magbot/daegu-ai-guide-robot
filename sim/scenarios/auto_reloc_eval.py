"""자동 재측위 — 지도 전체 탐색(scan_match.global_locate) 단독 평가(11-06).

  sim\\.venv\\Scripts\\python.exe sim\\scenarios\\auto_reloc_eval.py <백엔드 app 폴더의 부모>
  예) ... sim\\out\\cand_autoreloc

하네스는 numpy·cv2 를 가짜로 바꾸므로 계산은 여기서 실제로 돌리고, 결과를 sim\\out\\auto_reloc_fits.json 에 남긴다
(흐름 시나리오 auto_reloc_1106.py 가 이 결과를 쓴다).
자료: 11-06 맵 47 실제 라이다 스캔 6곳 30장(_자료/2026-11-06_전역재측위), 정답 = 받을 때 로봇 보고 위치.
그 밖: 아무 점 스캔 3장(지도와 안 맞음), 점 30개 스캔 1장(점 부족).
"""
import importlib.util
import json
import math
import os
import sys
import time

import numpy as np

CAND = sys.argv[1]
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA = os.path.join(ROOT, "_자료", "2026-11-06_전역재측위")
MAPS = os.path.join(ROOT, "Backend", "static", "maps")
OUT = os.path.join(ROOT, "sim", "out", "auto_reloc_fits.json")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sm = _load("scan_match", os.path.join(CAND, "app", "robot_control", "scan_match.py"))
# via_route 는 DB 모델을 import 하므로 지도 읽기 함수만 따로 쓴다
sys.path.insert(0, CAND)
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://u:p@127.0.0.1:1/x")
from app.navigation import via_route as vr  # noqa: E402

res, origin, negate, occ_th = vr._parse_yaml(os.path.join(MAPS, "UND_1F_병원.yaml"))
pix = vr._read_pgm(os.path.join(MAPS, "UND_1F_병원.pgm"))
occ = pix if negate else 1.0 - pix
unknown = (occ > vr._FREE_THRESH) & (occ <= occ_th)
grid = vr._OccGrid(vr._drop_small_blobs(occ > occ_th, res), res, origin, unknown)
f = sm.field_for_grid(47, grid)

cases = {}
names = {"scan_standby.json": "로봇대기장소", "scan_spot1.json": "간호실데스크(걷는 중)",
         "scan_spot2.json": "진료대기장소", "scan_spot3.json": "원무과 접수",
         "scan_spot4.json": "편의점입구", "scan_spot5.json": "1F_엘리베이터입구"}
last = None
for fn, label in names.items():
    for i, r in enumerate(json.load(open(os.path.join(DATA, fn), encoding="utf-8"))):
        p = r["pos_before"] if "x" in (r.get("pos_before") or {}) else r.get("pos_after") or {}
        p = p if "x" in p else last
        last = p
        cases[f"{fn}#{i}"] = (label, [q[:2] for q in r["points"]], (p["x"], p["y"], p["yaw"]))
rng = np.random.default_rng(1106)
for i in range(3):
    pts = rng.uniform(-8, 8, (250, 2)).tolist()
    cases[f"junk#{i}"] = ("아무 점", pts, None)
cases["few#0"] = ("점 30개", rng.uniform(-5, 5, (30, 2)).tolist(), None)

out = {}
print("| 자료 | 판정 | 찾은 위치 | 정답과 차이 | 점수 | 2등 차 | 사유 | 시간 |")
print("|---|---|---|---|---|---|---|---|")
ok_right = ok_wrong = rej = 0
for key, (label, pts, truth) in cases.items():
    t0 = time.time()
    r = sm.global_locate(f, pts)
    dt = time.time() - t0
    fit = r.get("fit")
    err = yerr = None
    at = None
    if truth and fit:
        err = math.hypot(fit["x"] - truth[0], fit["y"] - truth[1])
        yerr = abs(math.degrees(math.atan2(math.sin(fit["yaw"] - truth[2]), math.cos(fit["yaw"] - truth[2]))))
        at = sm.score_pose(f, sm.to_laser(pts), *truth)["score"]
    right = err is not None and err <= 0.4 and yerr <= 5
    if r["ok"]:
        if right:
            ok_right += 1
        else:
            ok_wrong += 1
    else:
        rej += 1
    out[key] = {"label": label, "ok": r["ok"], "reason": r.get("reason", ""), "fit": fit,
                "gap": r.get("gap"), "truth": truth, "at_truth": at, "err_m": err}
    where = f"({fit['x']:.2f},{fit['y']:.2f})" if fit else "-"
    print(f"| {label} {key.split('#')[1]} | {'채택' if r['ok'] else '보류'}{'' if (not r['ok'] or right or truth is None) else ' ★틀림'} | "
          f"{where} | {'' if err is None else f'{err:.2f}m {yerr:.1f}°'} | {fit['score'] if fit else '-'} | "
          f"{r.get('gap', '-')} | {r.get('reason', '')} | {dt:.1f}s |")
print(f"\n실제 스캔 30장 + 시험 4장: 맞게 채택 {ok_right} / 틀린 곳 채택 {ok_wrong} / 보류 {rej}")
os.makedirs(os.path.dirname(OUT), exist_ok=True)
json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
print("저장:", OUT)
