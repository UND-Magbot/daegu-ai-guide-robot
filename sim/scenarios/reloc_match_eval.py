"""위치 재조정 맞음 점수·자동 맞춤 평가(11-05) — 단독 실행.

  sim\\.venv\\Scripts\\python.exe sim\\scenarios\\reloc_match_eval.py <백엔드 app 폴더의 부모> <맵 자료 폴더>
  예) ... sim\\out\\cand_reloc_match sim\\out\\map_quality

맞춤에 쓰는 것은 2D 지도(PGM)뿐이다. 가짜 스캔은 같은 맵의 **3D 점군(full_cloud.pcd)** 에서 만든다 —
맞춤과 다른 자료라 '자기 지도로 자기 시험' 이 되지 않는다.
가짜 스캔: 참 위치에서 바닥 위 0.1~1.8m 점을 0.5° 마다 가장 가까운 것만(가림), 20m 안,
         바닥 반사 300점(0.5~6m) 섞음, 잡음 σ 0.02m, 1500점으로 줄임(relay_map 상한).
가정(실기 미확인): 실제 /LOC_BODY_POINTS 의 점 분포·바닥 섞임 정도, 지도에 없는 사람·물건.
"""
import importlib.util
import json
import math
import os
import sys
import time

import numpy as np

CAND, DATA = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "docs", "분석보고", "2026-11-05", "_자료"))
spec = importlib.util.spec_from_file_location("scan_match", os.path.join(CAND, "app", "robot_control", "scan_match.py"))
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)
mw = importlib.import_module("map_wall_thickness")   # PGM·PCD 읽기(분석보고 자료)

RNG = np.random.default_rng(1105)
N_POSES = 40


class Grid:
    """via_route._OccGrid 의 raw·resolution·origin 만 흉내 — 판정은 via_route 와 같게(어두운 칸 > 0.65)."""

    def __init__(self, mid):
        g = mw.read_pgm(f"{DATA}/map{mid}.pgm").astype(np.float32) / 255.0
        y = open(f"{DATA}/map{mid}.yaml", encoding="utf-8").read()
        import re
        self.resolution = float(re.search(r"resolution:\s*([\d.]+)", y).group(1))
        o = re.search(r"origin:\s*\[\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)", y)
        self.origin = (float(o.group(1)), float(o.group(2)))
        occ = 1.0 - g
        self.raw = occ > 0.65
        self.free = occ < 0.196
        self.pix = g


def free_poses(grid, f, n):
    """빈 칸 중 벽에서 0.6m 이상 떨어진 곳(로봇이 서 있을 만한 자리)."""
    rows, cols = np.nonzero(grid.free & (f.dist >= 0.6))
    pick = RNG.choice(len(rows), n, replace=False)
    h = grid.raw.shape[0]
    xs = grid.origin[0] + (cols[pick] + 0.5) * grid.resolution
    ys = grid.origin[1] + (h - 1 - rows[pick] + 0.5) * grid.resolution
    return [(float(x), float(y), float(RNG.uniform(-math.pi, math.pi))) for x, y in zip(xs, ys)]


ZBASE = 0.0


def fake_scan(cloud, pose):
    x, y, a = pose
    zc = cloud[:, 2]
    d = cloud[:, :2] - [x, y]
    r = np.hypot(d[:, 0], d[:, 1])
    m = r < 20.0
    d, r = d[m], r[m]
    ang = np.arctan2(d[:, 1], d[:, 0])
    # 여러 줄 라이다처럼: 방향 0.5° × 높이 0.2m 층마다 가장 가까운 점(가림). 한 방향에 한 점만 두면
    # 바닥 근처 턱이 뒤의 벽을 다 가려 실제 라이다와 달라진다(첫 실행에서 확인).
    layer = np.floor((zc[m] - ZBASE) / 0.2).astype(int)
    b = np.floor((np.degrees(ang) + 180) / 0.5).astype(int) * 100 + layer
    o = np.lexsort((r, b))
    first = np.ones(len(o), bool)
    first[1:] = b[o][1:] != b[o][:-1]
    vis = d[o[first]]
    # 지도 프레임 → 로봇 기준
    c, s = math.cos(-a), math.sin(-a)
    body = np.stack([vis[:, 0] * c - vis[:, 1] * s, vis[:, 0] * s + vis[:, 1] * c], 1)
    fr = RNG.uniform(0.5, 6.0, 300)
    fa = RNG.uniform(-math.pi, math.pi, 300)
    body = np.concatenate([body, np.stack([fr * np.cos(fa), fr * np.sin(fa)], 1)])
    body += RNG.normal(0, 0.02, body.shape)
    if len(body) > 1500:
        body = body[RNG.choice(len(body), 1500, replace=False)]
    return body


def ang_err(a, b):
    return abs(math.degrees(math.atan2(math.sin(a - b), math.cos(a - b))))


summary = {}
for mid in (47, 49):
    grid = Grid(mid)
    f = sm._Field(grid.raw, grid.resolution, grid.origin)
    pts, _ = mw.read_pcd(f"{DATA}/map{mid}_full_cloud.pcd")
    z0 = np.percentile(pts[:, 2], 5)
    ZBASE = z0 + 0.1
    cloud = pts[(pts[:, 2] > z0 + 0.1) & (pts[:, 2] < z0 + 2.1)]
    base_poses = free_poses(grid, f, N_POSES)
    # 오늘(11-05) 1F 튐 2건 자리 — 동서 통로(대기장소↔원무과, 편의점입구). 통로 방향(동쪽) 바라봄
    corridor = [(-2.0, 5.8, 0.0), (-1.0, 5.85, 0.0), (0.0, 5.75, 0.0), (-0.7, -2.1, 0.0)] if mid == 47 else []
    variants = {
        "기본(시드 0.2~0.8m·5~25°)": (base_poses, (0.2, 0.8), (5, 25), 0, None),
        "사람·물건 4개(지도에 없음)": (base_poses, (0.2, 0.8), (5, 25), 4, None),
        "큰 시드 오차(0.8~1.0m·20~30°)": (base_poses, (0.8, 1.0), (20, 30), 0, None),
        "창 밖 시드(1.5m)": (base_poses[:20], (1.5, 1.5), (5, 10), 0, None),
    }
    if corridor:
        variants["1F 통로(튐 자리)·통로 방향 0.8m 어긋난 시드"] = (corridor * 5, (0.8, 0.8), (3, 8), 2, 0.0)
    out = {}
    for name, (poses, (r0, r1), (y0, y1), n_people, seed_dir) in variants.items():
        s_true, s_off30, s_off5d, ok, ok_cur, amb, amb_wrong, t_ref, errs, scores_after = [], [], [], 0, 0, 0, 0, [], [], []
        for (x, y, a) in poses:
            scan = fake_scan(cloud, (x, y, a))
            for _ in range(n_people):      # 지름 0.4m 기둥 모양, 1~4m, 40점
                pr, pa = RNG.uniform(1.0, 4.0), RNG.uniform(-math.pi, math.pi)
                cx, cy = pr * math.cos(pa), pr * math.sin(pa)
                t = RNG.uniform(0, 2 * math.pi, 40)
                scan = np.concatenate([scan, np.stack([cx + 0.2 * np.cos(t), cy + 0.2 * np.sin(t)], 1)])
            laser = sm.to_laser(scan)
            s_true.append(sm.score_pose(f, laser, x, y, a)["score"])
            th = RNG.uniform(-math.pi, math.pi)
            s_off30.append(sm.score_pose(f, laser, x + 0.3 * math.cos(th), y + 0.3 * math.sin(th), a)["score"])
            s_off5d.append(sm.score_pose(f, laser, x, y, a + math.radians(5))["score"])
            dr = RNG.uniform(r0, r1)
            dth = seed_dir if seed_dir is not None else RNG.uniform(-math.pi, math.pi)
            dyaw = math.radians(RNG.uniform(y0, y1)) * RNG.choice([-1, 1])
            seed = (x + dr * math.cos(dth), y + dr * math.sin(dth), a + dyaw)
            t0 = time.perf_counter()
            rf = sm.match(f, scan.tolist(), *seed, refine=True)["refined"]
            t_ref.append(time.perf_counter() - t0)
            e_xy, e_yaw = math.hypot(rf["x"] - x, rf["y"] - y), ang_err(rf["yaw"], a)
            errs.append((e_xy, e_yaw))
            good = e_xy <= 0.10 and e_yaw <= 2.0
            ok += good
            ok_cur += math.hypot(seed[0] - x, seed[1] - y) <= 0.10 and ang_err(seed[2], a) <= 2.0
            amb += rf["ambiguous"]
            amb_wrong += (not good) and rf["ambiguous"]
            scores_after.append((rf["score"], good))
        q = lambda v: [round(float(t), 1) for t in np.percentile(v, (10, 50, 90))] if v else []
        bad = [e for e in errs if not (e[0] <= 0.10 and e[1] <= 2.0)]
        n = len(poses)
        out[name] = {
            "점수_참위치(10/50/90%)": q(s_true),
            "점수_0.3m어긋남": q(s_off30),
            "점수_5°어긋남": q(s_off5d),
            "자동맞춤_성공": f"{ok}/{n}",
            "현행_시드그대로_성공": f"{ok_cur}/{n}",
            "자동맞춤_오차_중앙(m,°)": [round(float(np.median([e[0] for e in errs])), 3), round(float(np.median([e[1] for e in errs])), 2)],
            "실패_오차(m,°)": [(round(e[0], 2), round(e[1], 1)) for e in bad][:8],
            "맞춘뒤점수_성공(10/50%)": q([s for s, g in scores_after if g])[:2],
            "맞춘뒤점수_실패(50/90%)": q([s for s, g in scores_after if not g])[1:],
            "모호_표시": f"{amb}/{n}",
            "실패중_모호_표시": f"{amb_wrong}/{len(bad)}",
            "시간_중앙·최대(s)": [round(float(np.median(t_ref)), 3), round(float(max(t_ref)), 3)],
        }
    summary[mid] = out

# 모호함: 특징 없는 긴 통로(폭 2m, 길이 40m, 벽 두 줄뿐) — 통로 방향으로는 어디든 같은 모양이라 '모호' 가 떠야 한다.
# 비교로 같은 통로 중간에 기둥 하나(옆벽 붙음)를 두면 모호가 사라지는지.
def corridor_case(with_pillar):
    walls = np.zeros((40, 420), bool)            # 0.1m 칸, 세로 4m·가로 42m
    walls[9, 10:410] = True; walls[30, 10:410] = True
    if with_pillar:
        walls[27:30, 200:203] = True             # 행은 위에서부터 — y 1.05~1.25(아래 벽 붙음)
    f = sm._Field(walls, 0.1, (0.0, 0.0))
    # 로봇 (21.0, 2.0) 동쪽. 보이는 벽: y=0.95, 3.05(칸 중심) — 20m 안
    xs = np.linspace(1.5, 40.5, 400)
    pts = [(x, 0.95) for x in xs] + [(x, 3.05) for x in xs]
    if with_pillar:
        pil = [(20.0 + i * 0.1, 1.05 + j * 0.1) for i in range(3) for j in range(3)]
        # 기둥 뒤 벽은 가려 안 보인다(실제 라이다) — 기둥이 차지하는 방향의 더 먼 벽 점을 뺀다
        ang = lambda px, py: math.atan2(py - 2.0, px - 21.0)
        lo, hi = min(ang(*q) for q in pil), max(ang(*q) for q in pil)
        pts = [q for q in pts if not (lo <= ang(*q) <= hi)] + pil
    body = np.array([(px - 21.0, py - 2.0) for px, py in pts]) + RNG.normal(0, 0.02, (len(pts), 2))
    r = sm.match(f, body.tolist(), 21.6, 2.0, 0.0, refine=True)["refined"]
    return {"모호": r["ambiguous"], "x오차": round(abs(r["x"] - 21.0), 2), "점수": r["score"], "다른후보": r.get("alt")}

summary["특징없는통로"] = corridor_case(False)
summary["통로+기둥"] = corridor_case(True)

# 경계: 점이 적을 때
f = sm._Field(Grid(47).raw, 0.1, Grid(47).origin)
few = sm.match(f, [[1.0, 0.0]] * 10, 0, 0, 0, refine=True)
summary["경계_점10개"] = few["status"]
empty = sm.match(f, [], 0, 0, 0, refine=False)
summary["경계_점0개"] = empty["status"]
print(json.dumps(summary, ensure_ascii=False, indent=1))
