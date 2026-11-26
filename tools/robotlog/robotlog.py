"""로봇 통합 로그 조회 — 관제·NOS·본체·장애물 감지 기록·Jetson 을 한국 시각 하나로 맞춰 보여 준다(읽기 전용).

    python tools/robotlog/robotlog.py clock                                   # 장비별 시계 차이
    python tools/robotlog/robotlog.py timeline --at 09:47:50 --span 30        # 그 시각 앞뒤 30초, 전 출처
    python tools/robotlog/robotlog.py timeline --from 10:14 --to 10:17 --src 관제,NOS계획,좌표
    python tools/robotlog/robotlog.py pose --from 10:14:20 --to 10:17:10      # 로봇 자세 흐름 + 튐
    python tools/robotlog/robotlog.py jumps --date 2026-11-05                 # 하루 좌표 튐 자동 탐지
    python tools/robotlog/robotlog.py grep "Reach goal out of time" --days 3

공통 옵션: --date YYYY-MM-DD(기본 오늘) · --out 파일(결과 저장) · --width 줄 길이(기본 170) · --no-jetson
시각은 모두 한국 시각. 로봇(+62초 등)·관제 docker 로그(세계 표준시)는 실행할 때마다 재서 자동 보정한다.
출처 이름: 관제 · 관제사건 · 관제세션 · NOS측위 · NOS계획 · NOS충전 · NOS센서 · NOS서비스 · 본체작업 · 본체보행 · 본체MCU · 좌표 · 음성
잡음(반복 줄) 규칙은 agent.py FILE_SOURCES·JOURNAL 과 이 파일 CONTROL_NOISE·JETSON_NOISE 에 있다.

구성: 이 파일(PC) → agent.py(관제 컨테이너 안, 로봇 SSH) → remote.py(로봇 안, 표준 입력으로 실행 — 로봇에 파일을 남기지 않음).
컨테이너를 다시 만들어도 되도록 실행할 때마다 agent.py·remote.py 를 컨테이너 /tmp 로 복사한다.
"""
import argparse
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE.parents[1] / "backend_logs"     # compose 가 컨테이너 /backend_logs 로 연결하는 곳
CONTAINER = "control-backend"
KST = timezone(timedelta(hours=9))

CONTROL_NOISE = re.compile(
    r'"(GET|OPTIONS|HEAD) [^"]*" (200|304)|\[PERSISTENCE\] flush 완료|INFO:\s+connection (open|closed)')
JETSON_NOISE = re.compile(r"^$")

# 로봇 로그의 중국어 핵심 말 → 국문 덧붙임(원문은 그대로 둔다)
GLOSSARY = [
    ("定位丢失", "측위 상실"), ("匹配误差", "매칭 오차"), ("以精确位置重置定位", "위치 재설정(주입)"),
    ("Reach goal out of time", "목표 시간 초과"), ("Received cancellation mission", "이동 취소 받음"),
    ("Approached goal", "목표 근처 도착"), ("Goal reached!", "목표 도착"), ("Collision caused by turning", "회전 중 충돌"),
    ("取消导航目标点", "이동 목표 취소"), ("点位坐标", "이동 점 좌표"), ("wayPoints size", "경유점 수"),
    ("liedown_state", "엎드림"), ("standup_state", "일어섬"), ("rl_control", "보행 제어"),
    ("前往充电桩", "충전소로"), ("充电", "충전"), ("开始配准", "매칭 시작"), ("完成配准", "매칭 완료"),
]


def die(msg):
    sys.exit("❌ " + msg)


# ── 시각 ────────────────────────────────────────────────────────────────
def parse_hm(s, date):
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt).time()
            return datetime.combine(date, t, KST)
        except ValueError:
            pass
    die(f"시각 형식은 HH:MM 또는 HH:MM:SS — 받은 값 {s!r}")


def window(args):
    date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(KST).date()
    if getattr(args, "at", None):
        at = parse_hm(args.at, date)
        return at - timedelta(seconds=args.span), at + timedelta(seconds=args.span)
    if getattr(args, "frm", None):
        lo = parse_hm(args.frm, date)
        hi = parse_hm(args.to, date) if args.to else lo + timedelta(minutes=5)
        return lo, hi
    return datetime.combine(date, datetime.min.time(), KST), datetime.combine(date, datetime.max.time(), KST)


def kst(t):
    return datetime.fromtimestamp(t, KST).strftime("%H:%M:%S.%f")[:10]


# ── 실행 ────────────────────────────────────────────────────────────────
def sh(cmd, timeout=600):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def agent(req):
    for src, dst in (("agent.py", "/tmp/robotlog_agent.py"), ("remote.py", "/tmp/robotlog_remote.py")):
        code, _, err = sh(["docker", "cp", str(HERE / src), f"{CONTAINER}:{dst}"], 60)
        if code:
            die(f"관제 컨테이너 복사 실패({src}): {err.strip()} — control-backend 가 켜져 있는지 확인")
    code, out, err = sh(["docker", "exec", "-e", "PYTHONIOENCODING=utf-8", CONTAINER, "python",
                         "/tmp/robotlog_agent.py", json.dumps(req, ensure_ascii=False)])
    rows = []
    for line in out.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            if line.strip() and not line.startswith("[DB]"):
                rows.append({"kind": "error", "host": "agent", "text": line})
    if code and not rows:
        die(f"agent 실패: {err.strip()[-1500:]}")
    return rows


RUN_TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\] (.*)$")
RUN_NAME = re.compile(r"run_\d+_(\d{8}_\d{6})\.log$")


def _run_sources(lo, hi):
    """관제 실행 로그(backend_logs/run_*.log, 한국 시각) 중 구간과 겹칠 수 있는 것.
    docker logs 는 컨테이너를 다시 만들면 사라지므로 쓰지 않는다. 지난 날짜는 logs_YYYYMMDD.zip 안에 있다."""
    import zipfile
    found = []   # (시작 시각, 이름, 여는 함수)
    for f in LOG_DIR.glob("run_*.log"):
        m = RUN_NAME.search(f.name)
        if m:
            found.append((datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=KST), f.name,
                          lambda f=f: f.open("rb")))
    d = lo.date() - timedelta(days=1)
    while d <= hi.date():
        z = LOG_DIR / f"logs_{d:%Y%m%d}.zip"
        if z.exists():
            zf = zipfile.ZipFile(z)
            for info in zf.infolist():
                m = RUN_NAME.search(info.filename)
                if m:
                    found.append((datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=KST), info.filename,
                                  lambda zf=zf, n=info.filename: zf.open(n)))
        d += timedelta(days=1)
    found.sort(key=lambda x: x[0])
    # 실행 하나는 다음 실행이 시작될 때까지 이어진다
    picked = []
    for i, (start, name, opener) in enumerate(found):
        end = found[i + 1][0] if i + 1 < len(found) else datetime.max.replace(tzinfo=KST)
        if start <= hi and end >= lo:
            picked.append((name, opener))
    return picked


def control_logs(lo, hi):
    rows, dropped, files = [], 0, []
    a, b = lo.strftime("%Y-%m-%d %H:%M:%S"), hi.strftime("%Y-%m-%d %H:%M:%S.999")
    for name, opener in _run_sources(lo, hi):
        files.append(name)
        with opener() as f:
            for raw in f:
                if not raw.startswith(b"["):
                    continue
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                m = RUN_TS.match(line)
                if not m or m.group(1) < a:
                    continue
                if m.group(1) > b:
                    break
                text = m.group(2).strip()
                if not text or CONTROL_NOISE.search(text):
                    dropped += 1
                    continue
                t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=KST).timestamp()
                rows.append({"t": t, "src": "관제", "text": text})
    if not files:
        rows.append({"kind": "error", "host": "관제", "text": f"{LOG_DIR} 에 이 구간 실행 로그가 없다"})
    rows.append({"kind": "count", "src": "관제", "kept": len(rows), "dropped": dropped})
    return rows


ARCH_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) \[([^\]]+)\] (.*)$")


def archive_logs(lo, hi):
    """수집기(collector.py)가 쌓은 보관본 backend_logs/robot/<날짜>/*.log(.gz). 로봇에서 지워진 구간을 채운다."""
    import gzip
    rows, files = [], 0
    a, b = lo.strftime("%Y-%m-%d %H:%M:%S"), hi.strftime("%Y-%m-%d %H:%M:%S.999")
    d = lo.date()
    while d <= hi.date():
        folder = LOG_DIR / "robot" / f"{d:%Y-%m-%d}"
        for f in sorted(folder.glob("*.log")) + sorted(folder.glob("*.log.gz")) if folder.exists() else []:
            if f.name.startswith("clock"):
                continue
            files += 1
            opener = (lambda f=f: gzip.open(f, "rt", encoding="utf-8")) if f.suffix == ".gz" else \
                (lambda f=f: f.open(encoding="utf-8"))
            with opener() as fh:
                for line in fh:
                    m = ARCH_LINE.match(line.rstrip("\n"))
                    if not m or not (a <= m.group(1) <= b):
                        continue
                    t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=KST).timestamp()
                    rows.append({"t": t, "src": m.group(2), "text": m.group(3), "archived": True})
        d += timedelta(days=1)
    rows.append({"kind": "count", "src": "보관본", "kept": len(rows)})
    return rows


def merge_archive(rows, arch):
    """같은 줄은 한 번만. 좌표는 로봇에서 직접 읽은 게 있으면 그것(0.5초 간격)을 쓴다."""
    live_pose = any(r.get("kind") == "pose" for r in rows)
    seen = {(r.get("src"), round(r["t"], 1), r.get("text", "")[:60]) for r in rows if "t" in r and not r.get("kind")}
    out = list(rows)
    for r in arch:
        if "t" not in r:
            out.append(r)
            continue
        if r["src"] == "좌표" and live_pose:
            continue
        if (r["src"], round(r["t"], 1), r["text"][:60]) not in seen:
            out.append(r)
    return out


def jetson_logs(lo, hi):
    cmd = ["ssh", "-o", "ConnectTimeout=6", "-o", "BatchMode=yes", "jetson",
           f"date +%s.%N; journalctl -u robot-voice --no-pager -o json --since @{int(lo.timestamp())} --until @{int(hi.timestamp()) + 1}"]
    try:
        code, out, err = sh(cmd, 60)
    except subprocess.TimeoutExpired:
        return [{"kind": "error", "host": "jetson", "text": "시간 초과"}]
    if code:
        return [{"kind": "error", "host": "jetson", "text": "접속 실패: " + err.strip()[-300:]}]
    lines = out.splitlines()
    rows = []
    try:
        dJ = float(lines[0]) - datetime.now().timestamp()
        rows.append({"kind": "clock", "host": "jetson", "dE": dJ, "rtt": 0, "tz": ""})
    except (ValueError, IndexError):
        dJ = 0.0
    for raw in lines[1:]:
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        msg = d.get("MESSAGE")
        if isinstance(msg, list):
            msg = bytes(msg).decode("utf-8", "replace")
        if msg and not JETSON_NOISE.search(msg):
            rows.append({"t": int(d["__REALTIME_TIMESTAMP"]) / 1e6, "src": "음성", "text": msg.strip()})
    return rows


# ── 출력 ────────────────────────────────────────────────────────────────
class Printer:
    def __init__(self, args):
        self.width = args.width
        self.fh = open(args.out, "w", encoding="utf-8") if args.out else None

    def __call__(self, s=""):
        print(s)
        if self.fh:
            self.fh.write(s + "\n")


def gloss(text):
    hits = [ko for zh, ko in GLOSSARY if zh in text]
    return f"{text}  ‹{', '.join(dict.fromkeys(hits))}›" if hits else text


def fmt_row(r, prev_pose):
    k = r.get("kind")
    if k == "pose":
        v = ""
        if prev_pose:
            dt = r["t"] - prev_pose["t"]
            if 0 < dt <= 2.0:
                v = f"  {math.hypot(r['x'] - prev_pose['x'], r['y'] - prev_pose['y']) / dt:.2f}m/s"
        extra = f"  멈춤({r.get('obst') or '-'}, {r.get('minR')}m)" if r.get("blocked") else ""
        return "좌표", f"({r['x']:6.2f},{r['y']:6.2f}) 방향 {r['yaw']:5.2f}{v}{extra}"
    if k == "jump":
        return "좌표", (f"⚠ 튐 ({r['from'][0]:.2f},{r['from'][1]:.2f}) → ({r['to'][0]:.2f},{r['to'][1]:.2f}) "
                        f"{r['dist']:.2f}m / {r['dt']:.1f}s")
    if k == "gapjump":
        return "좌표", (f"⚠ 기록 끊김 전후 {r['dist']:.2f}m 차이 (끊김 {r['gap']:.0f}초, {kst(r['t'])}~) "
                        f"({r['from'][0]:.2f},{r['from'][1]:.2f}) → ({r['to'][0]:.2f},{r['to'][1]:.2f}) — 끊긴 동안 걸었는지 관제 줄과 대조")
    if k == "gap":
        return "좌표", f"… 감지 기록 끊김 {r['t2'] - r['t']:.0f}초 (안내 이동이 아닌 동안은 기록 안 됨)"
    return r.get("src", "?"), gloss(r.get("text", ""))


def print_rows(rows, p, args, srcs=None):
    clocks = [r for r in rows if r.get("kind") == "clock"]
    if clocks:
        p("시계 차이(장비 − 관제): " + " · ".join(
            f"{ {'nos': 'NOS', 'body': '본체', 'jetson': 'Jetson'}.get(c['host'], c['host'])} {c['dE']:+.1f}s"
            + (f" (벽시계 {c['dE'] + tz_shift(c.get('tz')):+.1f}s)" if c.get("tz") else "") for c in clocks)
          + " → 모두 한국 시각으로 맞춤")
    for e in (r for r in rows if r.get("kind") == "error"):
        p(f"⚠ {e.get('host')}: {e.get('text')}")
    events = [r for r in rows if "t" in r and r.get("kind") in (None, "pose", "jump", "gapjump", "gap")]
    all_events = list(events)
    if srcs:
        events = [r for r in events if fmt_row(r, None)[0] in srcs or r.get("src") in srcs]
    events.sort(key=lambda r: r["t"])
    p("─" * 100)
    prev_pose = None
    for r in events:
        label, text = fmt_row(r, prev_pose)
        if r.get("kind") == "pose":
            prev_pose = r
        line = f"{kst(r['t'])} [{label:<6}]{'*' if r.get('archived') else ' '}{text}"
        p(line if len(line) <= args.width else line[:args.width - 1] + "…")
    p("─" * 100)
    check_clock(all_events, p)
    counts = [r for r in rows if r.get("kind") == "count"]
    if counts:
        p("출처: " + " · ".join(
            f"{c['src']} {c['kept']}줄" + (f"(반복 {c['dropped']}줄 거름)" if c.get("dropped") else "")
            + (f"[journal {kst(c['journal_oldest'])} 부터 남아 있음]" if c.get("journal_oldest") else "")
            for c in counts))
    npose = sum(1 for r in events if r.get("kind") == "pose")
    if npose:
        p(f"좌표 {npose}점 (장애물 감지 기록 — 안내 이동 중에만 남음)")


def check_clock(events, p):
    """시계 보정 점검 — 관제가 이동 명령을 보낸 시각과 본체가 받은 시각을 맞대 본다.
    시계 차이는 '지금' 잰 값이라, 로봇이 그 사이 재부팅했으면 지난 기록은 1초 남짓 어긋날 수 있다(11-05 오전 기록에서 −0.7초)."""
    sent = [r["t"] for r in events if r.get("src") == "관제" and "FASTAPI → ROBOT NAV" in r.get("text", "")]
    recv = [r["t"] for r in events if r.get("src") == "본체작업"
            and ("单点任务下发" in r.get("text", "") or "多点任务下发" in r.get("text", ""))]
    if not sent or not recv:
        return
    deltas = sorted(min((b - a for b in recv), key=abs) for a in sent)
    deltas = [d for d in deltas if abs(d) < 5]
    if not deltas:
        return
    med = deltas[len(deltas) // 2]
    note = "정상" if 0 <= med <= 1.5 else "⚠ 시계 보정이 어긋났을 수 있다 — 로봇·관제 줄 순서를 이만큼 감안해서 볼 것"
    p(f"시계 점검: 관제 이동 명령 → 본체 수신 {med:+.1f}초 ({len(deltas)}건 중앙값) — {note}")


def tz_shift(tz):
    """로봇 시간대 표기(+0800)와 한국(+0900) 차이 — 로그 벽시계 숫자로 보는 차이를 함께 보여 주려고."""
    m = re.match(r"([+-])(\d\d)(\d\d)", tz or "")
    if not m:
        return 0.0
    off = (int(m.group(2)) * 3600 + int(m.group(3)) * 60) * (1 if m.group(1) == "+" else -1)
    return off - 9 * 3600


# ── 명령 ────────────────────────────────────────────────────────────────
def cmd_clock(args, p):
    rows = agent({"op": "clock"})
    if not args.no_jetson:
        now = datetime.now(KST)
        rows += [r for r in jetson_logs(now, now) if r.get("kind")]
    print_rows(rows, p, args)


def cmd_timeline(args, p):
    lo, hi = window(args)
    p(f"■ 통합 타임라인 {lo:%Y-%m-%d %H:%M:%S} ~ {hi:%H:%M:%S} (한국 시각)")
    rows = control_logs(lo, hi)
    if not args.archive_only:
        req = {"op": "timeline", "lo": lo.timestamp(), "hi": hi.timestamp(), "pose": not args.no_pose,
               "step": args.step, "hosts": args.hosts.split(","), "limit": args.limit}
        rows += agent(req) + agent({"op": "db", "lo": lo.timestamp(), "hi": hi.timestamp()})
        if not args.no_jetson:
            rows += jetson_logs(lo, hi)
    rows = merge_archive(rows, archive_logs(lo, hi))
    if args.no_pose:
        rows = [r for r in rows if r.get("src") != "좌표"]
    print_rows(rows, p, args, set(args.src.split(",")) if args.src else None)


def cmd_pose(args, p):
    lo, hi = window(args)
    p(f"■ 로봇 자세 흐름 {lo:%Y-%m-%d %H:%M:%S} ~ {hi:%H:%M:%S} (한국 시각, {args.step}초 간격)")
    print_rows(agent({"op": "pose", "lo": lo.timestamp(), "hi": hi.timestamp(), "step": args.step, "hosts": ["nos"]}), p, args)


def cmd_jumps(args, p):
    lo, hi = window(args)
    p(f"■ 좌표 튐 {lo:%Y-%m-%d %H:%M} ~ {hi:%H:%M} (한국 시각) — 1초 안 1m 넘게·초당 1.6m 넘게, 기록 끊김 전후 1m 넘게")
    rows = agent({"op": "jumps", "lo": lo.timestamp(), "hi": hi.timestamp(), "hosts": ["nos"]})
    print_rows(rows, p, args)
    n = sum(1 for r in rows if r.get("kind") in ("jump", "gapjump"))
    p(f"찾음 {n}건" if n else "튐 없음")


def cmd_grep(args, p):
    today = datetime.now(KST).date()
    days = [(today - timedelta(days=i)).strftime("%Y_%m%d") for i in range(args.days)]
    p(f"■ '{args.pattern}' — 최근 {args.days}일 ({days[-1]}~{days[0]}), 잡음 제외")
    rows = agent({"op": "grep", "pattern": args.pattern, "days": days, "hosts": args.hosts.split(","), "limit": args.limit})
    for r in rows:
        if "t" in r:
            r["text"] = f"{datetime.fromtimestamp(r['t'], KST):%m-%d} " + r["text"]
    print_rows(rows, p, args)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--date", help="YYYY-MM-DD (기본 오늘)")
        sp.add_argument("--out", help="결과를 이 파일에도 저장")
        sp.add_argument("--width", type=int, default=170)
        sp.add_argument("--no-jetson", action="store_true", help="Jetson 음성 로그 빼기(Jetson 이 다른 망일 때)")
        sp.add_argument("--hosts", default="nos,body", help="nos,body 중 고르기")
        sp.add_argument("--limit", type=int, default=4000, help="출처당 최대 줄 수")
        sp.add_argument("--step", type=float, default=0.5, help="좌표 간격(초)")

    def span(sp, default_span):
        sp.add_argument("--at", help="HH:MM[:SS] — 이 시각 앞뒤 --span 초")
        sp.add_argument("--span", type=float, default=default_span)
        sp.add_argument("--from", dest="frm", help="HH:MM[:SS]")
        sp.add_argument("--to", help="HH:MM[:SS] (기본 --from + 5분)")

    sp = sub.add_parser("clock", help="장비별 시계 차이")
    common(sp)
    sp = sub.add_parser("timeline", help="통합 타임라인")
    common(sp)
    span(sp, 60)
    sp.add_argument("--src", help="보일 출처만, 쉼표로 (예: 관제,NOS계획,좌표)")
    sp.add_argument("--no-pose", action="store_true", help="좌표 빼기")
    sp.add_argument("--archive-only", action="store_true",
                    help="로봇에 접속하지 않고 관제 로그 + 수집 보관본(backend_logs/robot)만 본다")
    sp = sub.add_parser("pose", help="로봇 자세 흐름")
    common(sp)
    span(sp, 60)
    sp = sub.add_parser("jumps", help="좌표 튐 탐지 (기본 하루 전체)")
    common(sp)
    span(sp, 60)
    sp = sub.add_parser("grep", help="여러 날 로그에서 찾기")
    common(sp)
    sp.add_argument("pattern", help="정규식 (grep -E)")
    sp.add_argument("--days", type=int, default=3)

    args = ap.parse_args()
    if args.cmd == "timeline" and not (args.at or args.frm):
        die("timeline 은 --at 또는 --from 이 필요하다")
    if args.cmd == "pose" and not (args.at or args.frm):
        die("pose 는 --at 또는 --from 이 필요하다")
    p = Printer(args)
    {"clock": cmd_clock, "timeline": cmd_timeline, "pose": cmd_pose, "jumps": cmd_jumps, "grep": cmd_grep}[args.cmd](args, p)


if __name__ == "__main__":
    main()
