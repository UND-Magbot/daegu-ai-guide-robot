"""리허설용 말소리 방향·크기 기록기 — 고정 빔을 써도 되는지 판정한다.

robot-voice 와 따로 돈다(오디오를 건드리지 않고 XVF3800 제어 인터페이스만 읽는다).

    record     0.1초마다 방위(AEC_AZIMUTH_VALUES)·음성 에너지(AEC_SPENERGY_VALUES)와
               스피커 재생 여부를 기록한다. Ctrl+C 또는 SIGTERM 으로 끝낸다.
    calibrate  사람이 로봇의 한 방향(앞·오른쪽)에서 5초 말하는 동안 방위를 잰다 —
               보드 각도를 로봇 기준 각도로 바꾸는 데 쓴다.
    report     기록을 발화 구간으로 나누고, 듣기 진단(diag turns.jsonl)과 맞춰
               방향 분포·빔 범위(1빔·2빔)·크기를 보고 고정 빔 적합 여부를 판정한다.

로봇 자신의 멘트는 스피커 재생 중(+여운 0.5초)을 빼서 제외한다.
고정 빔을 켠 상태에서도 자유주행 빔(index 2)은 계속 말하는 사람을 따라가므로
켜기 전·후 리허설을 같은 방법으로 비교할 수 있다.
고도(위아래)는 이 보드가 알려 주지 않는다 — 방위만 판정한다.
"""

import argparse
import glob
import json
import math
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

XVF_DIR = os.environ.get("XVF_DIR", "/home/und02/project/voice/voice_assistant/xvf_host")
DIAG_DIR = os.environ.get("VOICE_DIAG_DIR", "/var/lib/robot-voice/diag")
PROBE_DIR = os.path.join(DIAG_DIR, "beam")     # diag 정리는 날짜 폴더만 지우므로 안전하다
CMDS = ("AEC_AZIMUTH_VALUES", "AEC_SPENERGY_VALUES")
FREE = 2                  # 자유주행 빔 — 고정 빔을 켜도 말하는 사람을 따라간다
AUTO = 3                  # 자동 선택 빔 — 실제 ASR 출력이 쓰는 빔
TTS_TAIL_SEC = 0.5        # 멘트가 끝난 뒤 여운까지 로봇 목소리로 본다
MERGE_GAP_SEC = 0.3       # 이보다 짧게 끊긴 말소리는 한 발화로 잇는다
MIN_SEG_SEC = 0.3         # 이보다 짧은 말소리는 버린다(문소리·기침)
MIN_SEGMENTS = 8          # 판정에 필요한 최소 발화 수
COVER_OK = 0.90           # 2빔 범위가 이 비율 이상이면 고정 빔 적합
COVER_MAYBE = 0.75        # 이 사이는 조건부 — 켜고 한 번 더 리허설해 비교한다
UNSTABLE_DEG = 40.0       # 한 발화 안에서 방위가 이만큼 흔들리면 반사·여러 사람이 섞인 것


# ── 기록 ────────────────────────────────────────────────────────────────────
def _xvf_read(cmd_file: str) -> dict:
    """xvf_host 한 번으로 두 값을 읽는다(약 36ms).

    새 세션으로 띄워 Ctrl+C 가 전송 도중의 xvf_host 에 닿지 않게 한다.
    제어 오류가 보이면 RuntimeError — 호출한 쪽이 기록을 멈춘다.
    """
    res = subprocess.run([os.path.join(XVF_DIR, "xvf_host"), "-e", cmd_file],
                         capture_output=True, text=True, timeout=3, start_new_session=True,
                         env={**os.environ, "LD_LIBRARY_PATH": XVF_DIR})
    out = res.stdout + res.stderr
    if "Error" in out or "control_ret_t error" in out:
        raise RuntimeError(" / ".join(l for l in out.splitlines() if "rror" in l)[:200])
    out = res.stdout
    vals = {}
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] in CMDS:
            nums = [p for p in parts[1:] if not p.startswith("(") and p != "deg)"]
            vals[parts[0]] = [float(n) for n in nums]
    return vals


def _alsa_running(kind: str) -> bool:
    """kind='p' 재생 / 'c' 녹음 중인 PCM 이 하나라도 있는가."""
    for f in glob.glob(f"/proc/asound/card*/pcm*{kind}/sub*/status"):
        try:
            with open(f) as fh:
                if "RUNNING" in fh.readline():
                    return True
        except OSError:
            pass
    return False


def _fixed_state() -> str:
    """고정 빔 켜짐 여부와 방위 — 켜기 전·후 기록을 구분하려고 머리줄에 남긴다."""
    cmd_file = os.path.join(PROBE_DIR, ".fixed.txt")
    with open(cmd_file, "w") as f:
        f.write("AEC_FIXEDBEAMSONOFF\nAEC_FIXEDBEAMSAZIMUTH_VALUES\n")
    out = subprocess.run([os.path.join(XVF_DIR, "xvf_host"), "-e", cmd_file],
                         capture_output=True, text=True, timeout=2,
                         env={**os.environ, "LD_LIBRARY_PATH": XVF_DIR}).stdout
    return " ".join(l.strip() for l in out.splitlines() if l.startswith("AEC_"))


def _sample_loop(path: str, hz: float, duration: float = 0.0) -> int:
    cmd_file = os.path.join(PROBE_DIR, ".cmds.txt")
    with open(cmd_file, "w") as f:
        f.write("\n".join(CMDS) + "\n")
    stop = {"v": False}
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: stop.update(v=True))
    period, n, t0 = 1.0 / hz, 0, time.time()
    with open(path, "a", encoding="utf-8") as out:
        out.write(json.dumps({"header": True, "start": datetime.now().isoformat(timespec="seconds"),
                              "hz": hz, "fixed": _fixed_state()}, ensure_ascii=False) + "\n")
        while not stop["v"] and (duration <= 0 or time.time() - t0 < duration):
            t = time.time()
            try:
                v = _xvf_read(cmd_file)
            except (subprocess.SubprocessError, OSError, RuntimeError) as e:
                # 2026-10-11: 초당 10회 시험 직후 보드 제어가 멈춰 녹음 장치까지 안 열렸다
                # (USB 재연결로 복구). 원인은 확정 못 했지만 계속 두드리지 않는다.
                print(f"!! 보드 제어 오류 — 기록을 멈춘다: {e}", file=sys.stderr)
                break
            az, en = v.get(CMDS[0]), v.get(CMDS[1])
            if az and en and len(az) == 4 and len(en) == 4:
                out.write(json.dumps({"t": round(t, 2), "az": [round(math.degrees(a) % 360, 1) for a in az],
                                      "en": [round(e, 4) for e in en], "play": _alsa_running("p"),
                                      "cap": _alsa_running("c")}) + "\n")
                n += 1
                if n % 50 == 0:
                    out.flush()
            time.sleep(max(0.0, period - (time.time() - t)))
    return n


def cmd_record(a: argparse.Namespace) -> None:
    os.makedirs(PROBE_DIR, exist_ok=True)
    path = a.out or os.path.join(PROBE_DIR, f"{datetime.now():%Y%m%d_%H%M%S}.jsonl")
    print(f"기록 시작: {path}  (고정 빔: {_fixed_state()})  — Ctrl+C 로 끝낸다", flush=True)
    n = _sample_loop(path, a.hz)
    print(f"기록 끝: 표본 {n}개 → {path}")


def cmd_calibrate(a: argparse.Namespace) -> None:
    os.makedirs(PROBE_DIR, exist_ok=True)
    path = os.path.join(PROBE_DIR, f"calib_{a.label}_{datetime.now():%H%M%S}.jsonl")
    print(f"[{a.label}] 로봇의 {a.label} 1m 에 서서 {a.sec:.0f}초 동안 계속 말하세요…", flush=True)
    _sample_loop(path, 4.0, a.sec)
    rows = [r for r in _load(path) if max(r["en"]) > 0 and not r["play"]]
    if len(rows) < 5:
        print("말소리를 거의 못 잡았다 — 더 크게/가까이에서 다시 하세요")
        return
    mean, spread = _circ_mean([r["az"][FREE] for r in rows], [r["en"][FREE] or 1e-6 for r in rows])
    print(f"[{a.label}] 보드 방위 {mean:.0f}° (흔들림 ±{spread:.0f}°, 표본 {len(rows)})")
    print(f"  → report 에 --{a.label} {mean:.0f} 로 넘기세요")


# ── 분석 ────────────────────────────────────────────────────────────────────
def _load(path: str) -> list:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue        # 강제 종료로 잘린 마지막 줄
            if not r.get("header"):
                rows.append(r)
    return rows


def _headers(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.startswith('{"header"')]


def _circ_mean(degs: list, weights: list) -> tuple:
    """가중 원형 평균과 원형 표준편차(도)."""
    s = sum(w * math.sin(math.radians(d)) for d, w in zip(degs, weights))
    c = sum(w * math.cos(math.radians(d)) for d, w in zip(degs, weights))
    tot = sum(weights) or 1.0
    r = min(1.0, math.hypot(s, c) / tot)
    std = math.degrees(math.sqrt(-2 * math.log(r))) if r > 1e-9 else 180.0
    return math.degrees(math.atan2(s, c)) % 360, std


def _diff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def segments(rows: list) -> list:
    """사람 말소리 구간 — 스피커 재생 중·직후는 뺀다."""
    last_play, cur, segs = -1e9, [], []
    for r in rows:
        if r["play"]:
            last_play = r["t"]
        human = max(r["en"]) > 0 and r["t"] - last_play > TTS_TAIL_SEC
        if not human:
            continue
        if cur and r["t"] - cur[-1]["t"] > MERGE_GAP_SEC:
            segs.append(cur)
            cur = []
        cur.append(r)
    if cur:
        segs.append(cur)
    out = []
    for s in segs:
        dur = s[-1]["t"] - s[0]["t"] + 0.1
        if dur < MIN_SEG_SEC:
            continue
        w = [max(r["en"][FREE], 1e-6) for r in s]
        doa, spread = _circ_mean([r["az"][FREE] for r in s], w)
        ens = sorted(max(r["en"]) for r in s)
        out.append({"start": s[0]["t"], "end": s[-1]["t"], "dur": round(dur, 1), "doa": doa,
                    "spread": spread, "energy": ens[len(ens) // 2]})
    return out


def _load_turns(day_dirs: list) -> list:
    turns = []
    for d in day_dirs:
        p = os.path.join(d, "turns.jsonl")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    t = json.loads(line)
                    t["_ts"] = datetime.fromisoformat(t["ts"]).timestamp()
                    turns.append(t)
                except (ValueError, KeyError):
                    pass
    return turns


def match_turns(segs: list, turns: list) -> None:
    """발화 구간마다 그 녹음을 처리한 듣기 기록을 붙인다.

    diag 기록 시각은 STT 가 끝난 뒤다 — 발화 끝 + STT 시간 + 여유 2초 안의 첫 기록을 고른다.
    """
    for s in segs:
        for t in turns:
            lag = t["_ts"] - s["end"]
            stt = (t.get("stt") or {}).get("ms", 0) / 1000.0
            if -0.5 <= lag <= stt + 2.0 and t.get("outcome") != "no_onset":
                s["turn"] = t
                break


def best_beams(doas: list, half: float) -> tuple:
    """5° 간격으로 훑어 1빔·2빔 범위가 가장 넓은 방위를 고른다."""
    grid = range(0, 360, 5)
    cover = {g: {i for i, d in enumerate(doas) if _diff(d, g) <= half} for g in grid}
    def closeness(beams: tuple) -> float:
        # 같은 범위라면 발화가 빔 중심에 가까운 쪽 — 두 빔이 한 무리에 겹치지 않게 한다
        return -sum(min(_diff(d, b) for b in beams) for d in doas)

    b1 = max(grid, key=lambda g: (len(cover[g]), closeness((g,))))
    b2 = max(((g, h) for g in grid for h in grid if g <= h),
             key=lambda p: (len(cover[p[0]] | cover[p[1]]), closeness(p)))
    n = len(doas) or 1
    return b1, len(cover[b1]) / n, b2, len(cover[b2[0]] | cover[b2[1]]) / n


def _robot_angle(board: float, front: float | None, right: float | None) -> float | None:
    """보드 방위 → 로봇 기준(앞 0°, 시계 방향 +). 오른쪽 측정이 없으면 반시계로 가정."""
    if front is None:
        return None
    sign = -1
    if right is not None:
        sign = 1 if (right - front) % 360 < 180 else -1
    return (sign * (board - front)) % 360


SECTORS = ("앞", "오른쪽 앞", "오른쪽", "오른쪽 뒤", "뒤", "왼쪽 뒤", "왼쪽", "왼쪽 앞")


def _sector(robot_deg: float) -> str:
    return SECTORS[int(((robot_deg + 22.5) % 360) // 45)]


def cmd_report(a: argparse.Namespace) -> None:
    files = a.probe or sorted(glob.glob(os.path.join(PROBE_DIR, "2*.jsonl")))[-1:]
    if not files:
        sys.exit(f"기록이 없다: {PROBE_DIR}")
    rows, heads = [], []
    for p in files:
        rows += _load(p)
        heads += _headers(p)
    rows.sort(key=lambda r: r["t"])
    segs = segments(rows)
    days = {datetime.fromtimestamp(r["t"]).strftime("%Y%m%d") for r in rows[:1] + rows[-1:]}
    turns = _load_turns([os.path.join(a.diag, d) for d in sorted(days)])
    if rows:
        turns = [t for t in turns if rows[0]["t"] - 5 <= t["_ts"] <= rows[-1]["t"] + 30]
    match_turns(segs, turns)

    span = (rows[-1]["t"] - rows[0]["t"]) / 60 if rows else 0
    print(f"■ 기록 {len(files)}개 · {span:.0f}분 · 표본 {len(rows)} · 고정 빔 상태: "
          f"{heads[0]['fixed'] if heads else '?'}")
    print(f"■ 사람 발화 {len(segs)}건 (멘트 재생 중 제외) · 같은 시간 듣기 기록 {len(turns)}건")
    if not segs:
        print("발화가 없다 — 기록 중에 실제로 말했는지, 시각이 맞는지 확인할 것")
        return

    print("\n■ 발화별")
    print("  시각      길이  보드방위  로봇기준        흔들림  에너지   결과")
    for s in segs:
        ra = _robot_angle(s["doa"], a.front, a.right)
        where = f"{ra:5.0f}° {_sector(ra):<6}" if ra is not None else "   (보정 없음)"
        t = s.get("turn") or {}
        res = t.get("outcome", "-") + (f" \"{t['text'][:14]}\"" if t.get("text") else "")
        flag = " ⚠흔들림" if s["spread"] > UNSTABLE_DEG else ""
        print(f"  {datetime.fromtimestamp(s['start']):%H:%M:%S} {s['dur']:5.1f}s  {s['doa']:6.0f}°  "
              f"{where}  ±{s['spread']:3.0f}°  {s['energy']:7.4f}  {res}{flag}")

    stable = [s for s in segs if s["spread"] <= UNSTABLE_DEG]
    doas = [s["doa"] for s in stable]
    b1, c1, (g, h), c2 = best_beams(doas, a.half)
    unstable = len(segs) - len(stable)
    print(f"\n■ 방향 (반폭 ±{a.half:.0f}° 기준, 흔들린 발화 {unstable}건 제외)")
    if a.front is not None:
        cnt: dict = {}
        for s in stable:
            k = _sector(_robot_angle(s["doa"], a.front, a.right))
            cnt[k] = cnt.get(k, 0) + 1
        print("  " + " · ".join(f"{k} {cnt[k]}" for k in SECTORS if k in cnt))
    print(f"  1빔 최적 {b1}° → {c1:.0%} 포함")
    print(f"  2빔 최적 {g}°/{h}° → {c2:.0%} 포함")

    # 결과별 방향 — 놓친 말이 특정 방향에 몰렸는지
    by_out: dict = {}
    for s in segs:
        o = (s.get("turn") or {}).get("outcome", "미매칭")
        by_out.setdefault(o, []).append(s)
    if len(by_out) > 1 or "미매칭" not in by_out:
        print("\n■ 결과별 (발화 기준)")
        for o, ss in sorted(by_out.items(), key=lambda kv: -len(kv[1])):
            inb = sum(1 for s in ss if min(_diff(s["doa"], g), _diff(s["doa"], h)) <= a.half)
            print(f"  {o:<16} {len(ss):3d}건 · 2빔 안 {inb}건")

    # 크기 — diag 레벨(robot-voice 가 실제로 본 값)
    heard = [t for t in turns if t.get("peak") is not None]
    print("\n■ 크기 (듣기 기록 기준)")
    if heard:
        snr = sorted(t["peak"] / max(t.get("noise") or 1e-4, 1e-4) for t in heard if t.get("outcome") != "no_onset")
        quiet = [t for t in heard if t.get("outcome") == "no_onset" and t.get("expect")
                 and (t.get("noise") or 0) * 1.3 < t["peak"] < t.get("threshold", 1)]
        noises = sorted(t.get("noise") or 0 for t in heard)
        if snr:
            print(f"  녹음된 발화 SNR(최대/소음) 중앙 {snr[len(snr)//2]:.1f}배 · 최저 {snr[0]:.1f}배")
        print(f"  소음 바닥 중앙 {noises[len(noises)//2]:.4f} · 최대 {noises[-1]:.4f}")
        print(f"  대답 기다리다 문턱 아래 소리만 온 경우 {len(quiet)}건"
              + (" — 목소리가 작거나 문턱(VAD_THRESHOLD·NOISE_RATIO)이 높다" if quiet else ""))
    else:
        print("  같은 시간의 듣기 기록이 없다")

    # 판정
    print("\n■ 판정")
    if len(stable) < MIN_SEGMENTS:
        print(f"  보류 — 안정된 발화 {len(stable)}건(최소 {MIN_SEGMENTS}). 더 모아서 다시 볼 것.")
        return
    if c2 >= COVER_OK:
        verdict = "적합"
    elif c2 >= COVER_MAYBE:
        verdict = "조건부 — 켜고 한 번 더 리허설해 결과를 비교"
    else:
        verdict = "부적합 — 자유주행(360°) 유지"
    print(f"  고정 빔 {verdict}  (2빔 범위 {c2:.0%}, 기준 {COVER_OK:.0%})")
    if unstable > len(segs) * 0.3:
        print(f"  ⚠ 방위가 흔들린 발화가 {unstable}/{len(segs)}건 — 반사가 심하거나 여러 사람이 말했다."
              " 고정 빔 효과가 기대보다 작을 수 있다")
    if c2 >= COVER_MAYBE:
        print("  /etc/respeaker-tune.conf 후보(보드 기준 각도):")
        print(f"    FIXED_BEAMS=1\n    FIXED_AZ1_DEG={g}\n    FIXED_AZ2_DEG={h}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="리허설 동안 방위·에너지 기록")
    r.add_argument("--hz", type=float, default=4.0)
    r.add_argument("--out")
    c = sub.add_parser("calibrate", help="로봇 기준 방향 재기")
    c.add_argument("label", choices=("front", "right"))
    c.add_argument("--sec", type=float, default=5.0)
    rp = sub.add_parser("report", help="고정 빔 적합 판정")
    rp.add_argument("--probe", nargs="*", help="기록 파일(기본: 가장 최근 것)")
    rp.add_argument("--diag", default=DIAG_DIR)
    rp.add_argument("--front", type=float, help="calibrate front 결과(보드 방위)")
    rp.add_argument("--right", type=float, help="calibrate right 결과(보드 방위)")
    rp.add_argument("--half", type=float, default=30.0, help="빔 반폭(도)")
    a = p.parse_args()
    {"record": cmd_record, "calibrate": cmd_calibrate, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
