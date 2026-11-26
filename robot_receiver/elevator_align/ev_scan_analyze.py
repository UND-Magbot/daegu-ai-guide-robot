#!/usr/bin/env python3
"""엘리베이터 안/밖 판정 오프라인 분석기 — PC 에서 실행(로봇 명령 없음).

ev_scan_recorder.py 로 받은 기록을 프레임마다 판정하고, 운영자가 적은 탑승·하차 시각
(labels.csv)과 맞춰 본다.

  cd robot_receiver
  python -m elevator_align.ev_scan_analyze ev_scan/scan_trip1_*.jsonl.gz --labels ev_scan/trip1_labels.csv
  python -m elevator_align.ev_scan_analyze ev_scan/scan_corridor_*.jsonl.gz --expect out

labels.csv (스톱워치 초, 기록기 시작 = 0):
  start,end,label
  0,95,out_hall_1f
  110,260,in_1to3
  275,400,out_hall_3f
label 은 in/out 으로 시작하면 채점하고, 뒤 이름별로 따로 집계한다(복도 오판 확인용).
경계 몇 초(문 통과 중)는 비워 두면 채점에서 빠진다. 음성 녹음 시각이면 --offset 으로 맞춘다.

출력: 콘솔 요약 + <기록>.csv(프레임별 지표) + <기록>.html(시간축 그래프).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import statistics
from dataclasses import dataclass

from .ev_auto_truth import find_rides, labels_from_rides
from .self_mask import DEFAULT_SELF_MASK
from .sides_fit import fit_sides

# 판정 기준 초기값 — 실데이터로 조정한다(--width-min 등).
WIDTH_MIN = 0.9         # 엘리베이터 폭 하한(m)
WIDTH_MAX = 2.8         # 상한
FAR_R = 3.0             # '먼 점' 기준 거리(m)
FAR_RATIO_MAX = 0.15    # 안이면 먼 점 비율이 이 이하
SMOOTH_FRAMES = 5       # 다수결 창(2Hz 기준 약 2.5초)
BIN_DEG = 15.0
ENCLOSE_R = 2.0


@dataclass
class FrameStat:
    t: float
    n: int
    ok: bool
    width: float
    left: float
    right: float
    far_ratio: float
    enclosure: float
    z_max_near: float | None
    motion_state: int | None
    raw_in: bool = False
    judged_in: bool = False


def load_record(path: str) -> tuple[dict, list[dict], dict[str, list]]:
    """jsonl.gz → (meta, 라이다 프레임, {"imu","odom","motion","topics"}).

    기록이 중간에 끊겨 꼬리가 깨져 있어도 읽은 데까지 돌려준다.
    """
    meta, frames = {}, []
    extra: dict[str, list] = {"imu": [], "odom": [], "motion": [], "topics": []}
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    break
                if "meta" in obj:
                    meta = obj["meta"]
                elif "pts" in obj:
                    frames.append(obj)
                elif "i" in obj:
                    extra["imu"].append(obj)
                elif "o" in obj:
                    extra["odom"].append(obj)
                elif "m" in obj:
                    extra["motion"].append(obj)
                elif "topics" in obj:
                    extra["topics"] = obj["topics"]
    except (EOFError, OSError):
        pass  # 끊긴 gzip 꼬리
    return meta, frames, extra


def frame_stat(frame: dict, zmin: float | None, zmax: float | None, args) -> FrameStat:
    pts3 = frame.get("pts") or []
    if zmin is not None or zmax is not None:
        lo = -math.inf if zmin is None else zmin
        hi = math.inf if zmax is None else zmax
        pts3 = [p for p in pts3 if lo <= (p[2] if len(p) > 2 else 0.0) <= hi]
    xy = DEFAULT_SELF_MASK.apply([(p[0], p[1]) for p in pts3])
    sf = fit_sides(xy)
    rs = [math.hypot(x, y) for (x, y) in xy]
    far_ratio = (sum(1 for r in rs if r > FAR_R) / len(rs)) if rs else 0.0
    nbins = int(360 / BIN_DEG)
    hit = set()
    for (x, y), r in zip(xy, rs):
        if r < ENCLOSE_R:
            hit.add(int((math.degrees(math.atan2(y, x)) % 360) // BIN_DEG) % nbins)
    near_z = [p[2] for p in pts3 if len(p) > 2 and math.hypot(p[0], p[1]) < 1.5]
    st = FrameStat(
        t=float(frame.get("t", 0.0)), n=len(xy), ok=sf.ok, width=sf.width,
        left=sf.left, right=sf.right, far_ratio=far_ratio, enclosure=len(hit) / nbins,
        z_max_near=max(near_z) if near_z else None, motion_state=frame.get("ms"),
    )
    st.raw_in = (st.ok and args.width_min <= st.width <= args.width_max
                 and st.far_ratio <= args.far_ratio_max)
    return st


def smooth(stats: list[FrameStat], k: int) -> None:
    """앞뒤 k 프레임 다수결 — 한두 프레임 튐(사람 지나감 등)을 누른다."""
    half = k // 2
    for i, s in enumerate(stats):
        win = stats[max(0, i - half): i + half + 1]
        s.judged_in = sum(w.raw_in for w in win) * 2 > len(win)


def load_labels(path: str) -> list[tuple[float, float, str]]:
    """label 은 in/out 으로 시작해야 채점한다(예: out_corridor, in_1to3). 나머지는 무시."""
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            lab = row["label"].strip().lower()
            if lab.startswith(("in", "out")):
                out.append((float(row["start"]), float(row["end"]), lab))
    return out


def label_at(t: float, labels) -> str | None:
    for a, b, lab in labels:
        if a <= t <= b:
            return lab
    return None


def truth_of(label: str) -> str:
    return "out" if label.startswith("out") else "in"


def segments(stats: list[FrameStat]) -> list[tuple[float, float, bool]]:
    segs = []
    for s in stats:
        if segs and segs[-1][2] == s.judged_in:
            segs[-1] = (segs[-1][0], s.t, s.judged_in)
        else:
            segs.append((s.t, s.t, s.judged_in))
    return segs


def _dist(vals: list[float]) -> str:
    if not vals:
        return "-"
    return f"최소 {min(vals):.2f} · 중앙 {statistics.median(vals):.2f} · 최대 {max(vals):.2f}"


def write_csv(path: str, stats: list[FrameStat], labels) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["t", "points", "sides_ok", "width", "left", "right", "far_ratio",
                    "enclosure", "z_max_near", "motion_state", "raw_in", "judged_in", "label"])
        for s in stats:
            w.writerow([s.t, s.n, int(s.ok), f"{s.width:.3f}", f"{s.left:.3f}", f"{s.right:.3f}",
                        f"{s.far_ratio:.3f}", f"{s.enclosure:.3f}",
                        "" if s.z_max_near is None else f"{s.z_max_near:.3f}",
                        "" if s.motion_state is None else s.motion_state,
                        int(s.raw_in), int(s.judged_in), label_at(s.t, labels) or ""])


def write_html(path: str, title: str, stats: list[FrameStat], labels, args) -> None:
    """시간축 SVG — 폭·먼 점 비율·판정·정답 띠. 외부 라이브러리 없이 연다."""
    if not stats:
        return
    W, H, pad = 1100, 120, 50
    t0, t1 = stats[0].t, max(stats[-1].t, stats[0].t + 1)

    def X(t):
        return pad + (t - t0) / (t1 - t0) * (W - 2 * pad)

    def line(vals, vmax, color):
        pts = " ".join(f"{X(s.t):.1f},{H - 10 - min(v, vmax) / vmax * (H - 20):.1f}"
                       for s, v in zip(stats, vals))
        return f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{pts}"/>'

    def band(y, items, h=18):
        return "".join(f'<rect x="{X(a):.1f}" y="{y}" width="{max(1, X(b) - X(a)):.1f}" '
                       f'height="{h}" fill="{c}"/>' for a, b, c in items)

    judged = [(a, b, "#d9534f" if v else "#5cb85c") for a, b, v in segments(stats)]
    truth = [(a, b, "#d9534f" if truth_of(lab) == "in" else "#5cb85c") for a, b, lab in labels]
    ticks = "".join(f'<text x="{X(t):.0f}" y="14" font-size="10">{t:.0f}s</text>'
                    for t in range(int(t0), int(t1) + 1, max(10, int((t1 - t0) / 20) // 10 * 10 or 10)))
    width_ref = (f'<line x1="{pad}" x2="{W - pad}" y1="{H - 10 - args.width_max / 4 * (H - 20):.1f}" '
                 f'y2="{H - 10 - args.width_max / 4 * (H - 20):.1f}" stroke="#999" stroke-dasharray="4"/>')
    html = f"""<!doctype html><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:sans-serif;background:#fff;color:#222;margin:16px}}h3{{margin:12px 0 4px}}</style>
<h2>{title}</h2>
<p>빨강 = 엘리베이터 안, 초록 = 밖. 판정 기준: 옆벽 인식 + 폭 {args.width_min}~{args.width_max}m
 + 먼 점(>{FAR_R}m) 비율 ≤ {args.far_ratio_max}, {args.smooth}프레임 다수결.</p>
<svg width="{W}" height="{H * 2 + 120}">
{ticks}
<text x="0" y="40" font-size="11">정답</text>{band(28, truth)}
<text x="0" y="68" font-size="11">판정</text>{band(56, judged)}
<g transform="translate(0,90)"><text x="0" y="12" font-size="11">옆벽 폭(m, 0~4)</text>
{width_ref}{line([s.width if s.ok else 0 for s in stats], 4.0, "#337ab7")}</g>
<g transform="translate(0,{90 + H})"><text x="0" y="12" font-size="11">먼 점 비율(0~1)</text>
{line([s.far_ratio for s in stats], 1.0, "#f0ad4e")}</g>
</svg>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def analyze(path: str, args) -> dict:
    meta, frames, extra = load_record(path)
    labels = load_labels(args.labels) if args.labels else []
    # 음성 녹음 시각으로 적었으면 '기록 시작'을 말한 시각만큼 뺀다
    labels = [(a - args.offset, b - args.offset, lab) for a, b, lab in labels]
    if args.expect:
        labels = [(-1e9, 1e9, args.expect)]
    stats = [frame_stat(fr, args.zmin, args.zmax, args) for fr in frames]
    smooth(stats, args.smooth)

    name = os.path.basename(path)
    print(f"\n=== {name}  (label={meta.get('label')}, 시작 {meta.get('start_local')}, 프레임 {len(stats)})")
    if not stats:
        print("  프레임 없음 — 토픽 이름·ROS2 환경을 확인하세요.")
        return {"file": name, "frames": 0}
    if args.auto:
        labels = auto_labels(extra, stats)
    print(f"  점 수/프레임: {_dist([s.n for s in stats])}")
    zs = [s.z_max_near for s in stats if s.z_max_near is not None]
    if zs:
        print(f"  가까운 점 최대 z(m): {_dist(zs)}  ← 천장이 잡히면 안/밖 차이가 날 수 있음")

    print("  판정 구간:")
    for a, b, v in segments(stats):
        print(f"    {a:7.1f}s ~ {b:7.1f}s  {'안' if v else '밖'}")

    result = {"file": name, "frames": len(stats)}
    if labels:
        conf = {("in", True): 0, ("in", False): 0, ("out", True): 0, ("out", False): 0}
        per: dict[str, list[FrameStat]] = {}
        for s in stats:
            lab = label_at(s.t, labels)
            if lab:
                conf[(truth_of(lab), s.judged_in)] += 1
                per.setdefault(lab, []).append(s)
        scored = sum(conf.values())
        correct = conf[("in", True)] + conf[("out", False)]
        print(f"  채점: {correct}/{scored} 프레임 일치" + (f" ({correct / scored:.0%})" if scored else ""))
        print(f"    정답 안 → 판정 안 {conf[('in', True)]} / 밖 {conf[('in', False)]}")
        print(f"    정답 밖 → 판정 안 {conf[('out', True)]} / 밖 {conf[('out', False)]}  ← 오판(잘못 '안')")
        for lab, ss in sorted(per.items()):
            judged = sum(s.judged_in for s in ss)
            print(f"  [{lab}] 판정 안 {judged}/{len(ss)}, 옆벽 인식 {sum(s.ok for s in ss)}/{len(ss)}, "
                  f"폭 {_dist([s.width for s in ss if s.ok])}, 먼 점 비율 {_dist([s.far_ratio for s in ss])}")
        result.update({"scored": scored, "correct": correct,
                       "false_in": conf[("out", True)], "missed_in": conf[("in", False)]})

    base = path[:-len(".jsonl.gz")] if path.endswith(".jsonl.gz") else os.path.splitext(path)[0]
    write_csv(base + ".csv", stats, labels)
    write_html(base + ".html", name, stats, labels, args)
    print(f"  저장: {base}.csv, {base}.html")
    return result


def auto_labels(extra: dict, stats: list[FrameStat]) -> list[tuple[float, float, str]]:
    """IMU 로 찾은 탑승 구간을 정답으로 쓴다(ev_auto_truth)."""
    print(f"  IMU {len(extra['imu'])}샘플, 오도메트리 {len(extra['odom'])}, MotionInfo {len(extra['motion'])}")
    rides = find_rides(extra["imu"], extra["odom"])
    if not rides:
        print("  자동 정답: 엘리베이터 운행을 찾지 못했습니다(IMU 없음·기준 미달). --labels 로 채점하세요.")
        return []
    for k, r in enumerate(rides, 1):
        dz = "" if r.dz is None else f", 높이 변화 {r.dz:+.1f}m"
        print(f"  자동 정답 운행{k}({'상승' if r.direction == 'up' else '하강'}): 정지 {r.still_start:.0f}~"
              f"{r.still_end:.0f}s, 출발 {r.depart:.0f}s → 도착 {r.arrive:.0f}s, 가속 {r.peak:.2f}m/s²{dz}")
    return labels_from_rides(rides, stats[-1].t if stats else 0.0)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="엘리베이터 안/밖 판정 오프라인 분석기")
    ap.add_argument("files", nargs="+", help="ev_scan_recorder 기록(.jsonl.gz)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--labels", help="start,end,label CSV(label 은 in…/out…) — 한 기록에 하나")
    g.add_argument("--expect", choices=["in", "out"], help="기록 전체의 정답(지점 캡처용)")
    g.add_argument("--auto", action="store_true",
                   help="IMU 로 엘리베이터 운행을 찾아 정답을 자동으로 만든다(운영자 기록 불필요)")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="labels 시각에서 뺄 초(음성 녹음에서 '기록 시작'을 말한 시각)")
    ap.add_argument("--zmin", type=float, default=None, help="z 하한(바닥 점 제거용)")
    ap.add_argument("--zmax", type=float, default=None, help="z 상한(천장 점 제거용)")
    ap.add_argument("--width-min", type=float, default=WIDTH_MIN)
    ap.add_argument("--width-max", type=float, default=WIDTH_MAX)
    ap.add_argument("--far-ratio-max", type=float, default=FAR_RATIO_MAX)
    ap.add_argument("--smooth", type=int, default=SMOOTH_FRAMES)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.labels and len(args.files) > 1:
        print("[ERROR] --labels 는 기록 하나에만 씁니다.")
        return 2
    results = [analyze(p, args) for p in args.files]
    scored = sum(r.get("scored", 0) for r in results)
    if scored:
        correct = sum(r.get("correct", 0) for r in results)
        print(f"\n전체: {correct}/{scored} ({correct / scored:.0%}), "
              f"잘못 '안' {sum(r.get('false_in', 0) for r in results)}, "
              f"놓친 '안' {sum(r.get('missed_in', 0) for r in results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
