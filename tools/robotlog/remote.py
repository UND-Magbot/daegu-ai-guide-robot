"""로봇(본체·NOS) 안에서 도는 읽기 전용 조각 — agent.py 가 SSH 표준 입력으로 보내 `python3 - '<작업 JSON>'` 로 실행한다.

로봇에 파일을 남기지 않는다. 로봇 Python 이 3.8 이라 그 문법만 쓴다.
시각은 모두 '실제 epoch'(관제 기준)로 주고받는다. 로봇 시계와의 차이 dE(로봇 epoch − 실제 epoch)는 agent 가 재서 넘긴다.
출력: 한 줄에 JSON 하나 — {"t": 실제 epoch, "src": 출처, "text": 내용} 또는 {"kind": ...} 요약.
"""
import glob
import json
import math
import os
import re
import subprocess
import sys
import time

LOGDIR = "/var/opt/robot/log"
REC_GLOB = "/home/user/Control_system/obstacle_rec/rec_*.jsonl"
IDLE_STEP = 10.0   # 감지 꺼진 동안 수집 간격(초) — tail_rec
TS_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.(\d{3})")

os.nice(19)   # 로봇 쪽 작업보다 항상 뒤로


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def line_epoch(line):
    """'[2026-11-05 09:47:52.123.456]' → 로봇 epoch. 시각 없는 줄은 None."""
    m = TS_RE.match(line)
    if not m:
        return None
    return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) + int(m.group(2)) / 1000.0


def days_between(lo, hi):
    """로봇 epoch 구간에 걸친 날짜 폴더 이름(YYYY_MMDD)."""
    out_days, t = [], lo - 86400
    while t <= hi + 86400:
        d = time.strftime("%Y_%m%d", time.localtime(t))
        if d not in out_days:
            out_days.append(d)
        t += 3600
    return [d for d in out_days if lo - 86400 <= time.mktime(time.strptime(d, "%Y_%m%d")) <= hi]


def seek_time(f, size, target):
    """파일에서 target(로봇 epoch) 직전 줄 위치를 이분 탐색으로 찾는다 — 수십 MB 파일을 다 읽지 않으려고."""
    lo, hi = 0, size
    while hi - lo > 65536:
        mid = (lo + hi) // 2
        f.seek(mid)
        f.readline()
        t = None
        for _ in range(50):
            raw = f.readline()
            if not raw:
                break
            t = line_epoch(raw.decode("utf-8", "replace"))
            if t is not None:
                break
        if t is None or t >= target:
            hi = mid
        else:
            lo = mid
    return lo


def read_logs(job):
    """제조사 날짜별 로그에서 구간 안 줄을 잡음을 빼고 낸다."""
    dE = job["dE"]
    lo, hi = job["lo"] + dE, job["hi"] + dE
    limit = job.get("limit", 4000)
    for src in job["sources"]:
        noise = re.compile(src["noise"]) if src.get("noise") else None
        n = dropped = 0
        for day in days_between(lo, hi):
            path = "%s/%s/%s.%s.log" % (LOGDIR, day, src["file"], day)
            if not os.path.exists(path):
                continue
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(seek_time(f, size, lo))
                pend = None          # [시각, 내용] — 시각 없는 다음 줄(본체의 여러 줄 출력)을 이어 붙인다

                def flush():
                    nonlocal n, dropped
                    if pend is None:
                        return
                    body = pend[1].strip()
                    if not body or (noise and noise.search(body)):
                        dropped += 1
                        return
                    if n < limit:
                        out({"t": pend[0] - dE, "src": src["label"], "text": body})
                    n += 1

                for raw in f:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    t = line_epoch(line)
                    if t is None:
                        if pend is not None and line.strip():
                            pend[1] += " | " + line.strip()
                        continue
                    flush()
                    pend = None
                    if t < lo:
                        continue
                    if t > hi:
                        break
                    body = TS_RE.sub("", line).split("] ", 1)[-1]
                    body = re.sub(r"^\s*\[(INFO|WARN|ERROR|DEBUG) *\]\s*", "", body)
                    pend = [t, body]
                flush()
        out({"kind": "count", "src": src["label"], "kept": n, "dropped": dropped})


def read_journal(job):
    """우리 서비스(systemd) journal 구간. 라이다 출력 때문에 몇 분만 남아 있을 수 있다."""
    dE = job["dE"]
    lo, hi = job["lo"] + dE, job["hi"] + dE
    noise = re.compile(job["noise"]) if job.get("noise") else None
    cmd = ["journalctl", "--no-pager", "-o", "json", "--since", "@%d" % int(lo), "--until", "@%d" % int(hi + 1)]
    for u in job["units"]:
        cmd += ["-u", u]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = dropped = 0
    for raw in p.stdout.splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        msg = d.get("MESSAGE")
        if isinstance(msg, list):
            msg = bytes(msg).decode("utf-8", "replace")
        if not msg or (noise and noise.search(msg)):
            dropped += 1
            continue
        t = int(d["__REALTIME_TIMESTAMP"]) / 1e6
        unit = (d.get("_SYSTEMD_UNIT") or "").replace(".service", "")
        out({"t": t - dE, "src": job["label"], "text": "%s: %s" % (unit, msg.strip())})
        n += 1
    # journal 이 어디까지 남아 있는지(가장 오래된 줄) — 구간이 그 전이면 '기록 없음'이라고 알려야 한다
    head = subprocess.run("journalctl --no-pager -o json | head -1", shell=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        oldest_t = int(json.loads(head.stdout.splitlines()[0])["__REALTIME_TIMESTAMP"]) / 1e6 - dE
    except (ValueError, IndexError, KeyError):
        oldest_t = None
    out({"kind": "count", "src": job["label"], "kept": n, "dropped": dropped, "journal_oldest": oldest_t})


def rec_files(lo, hi):
    """구간(로봇 epoch)과 겹칠 수 있는 장애물 감지 기록 파일."""
    files = []
    for path in sorted(glob.glob(REC_GLOB)):
        m = re.search(r"rec_(\d{8}_\d{6})", path)
        start = time.mktime(time.strptime(m.group(1), "%Y%m%d_%H%M%S")) if m else 0
        if start <= hi and os.path.getmtime(path) >= lo:
            files.append(path)
    return files


def rec_iter(lo, hi):
    """구간 안 감지 기록 프레임(로봇 epoch 순)."""
    for path in rec_files(lo, hi):
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            a, b = 0, size
            while b - a > 200000:
                mid = (a + b) // 2
                f.seek(mid)
                f.readline()
                try:
                    t = json.loads(f.readline())["t"]
                except (ValueError, KeyError):
                    a = mid
                    continue
                if t < lo:
                    a = mid
                else:
                    b = mid
            f.seek(a)
            f.readline()
            for raw in f:
                try:
                    d = json.loads(raw)
                    t = d["t"]
                except (ValueError, KeyError):
                    continue
                if t < lo:
                    continue
                if t > hi:
                    break
                yield d


def pose(job):
    """로봇 자세 흐름 — step 초마다 한 점 + 멈춤(blocked) 바뀔 때. 감지 기록이 끊긴 곳은 표시한다."""
    dE, step = job["dE"], job.get("step", 0.5)
    lo, hi = job["lo"] + dE, job["hi"] + dE
    last_t = last_out = None
    last_blocked = None
    for d in rec_iter(lo, hi):
        t, p = d["t"], d.get("pose")
        if not p:
            continue
        blocked = bool(d.get("blocked"))
        if last_t is not None and t - last_t > 2.0:
            out({"kind": "gap", "t": last_t - dE, "t2": t - dE})
        if last_out is None or t - last_out >= step or blocked != last_blocked or (last_t is not None and t - last_t > 2.0):
            out({"kind": "pose", "t": t - dE, "x": p[0], "y": p[1], "yaw": p[2], "age": d.get("poseAge"),
                 "blocked": blocked, "obst": d.get("kind"), "minR": d.get("minR")})
            last_out = t
        last_t, last_blocked = t, blocked


class JumpDetector:
    """좌표 튐 찾기. 0.5초 평균 위치 두 개를 비교해 1초 안에 dist_m 넘게·초당 speed 넘게 움직이면 튐.
    기록이 0.3초 넘게 끊기면 앞뒤를 잇지 않는다(감지가 꺼진 동안 걸었을 수 있다) — 끊긴 시간 동안 걸어서는
    갈 수 없는 거리일 때만 '끊김 전후'로 낸다. 상태는 JSON 으로 수집 주기 사이에 넘긴다(state())."""

    def __init__(self, dE, st=None, dist_m=1.0, speed=1.6, walk_mps=1.3):
        self.dE, self.dist_m, self.speed, self.walk_mps = dE, dist_m, speed, walk_mps
        st = st or {}
        self.win = [tuple(w) for w in st.get("win", [])]
        self.prev = tuple(st["prev"]) if st.get("prev") else None
        self.last = tuple(st["last"]) if st.get("last") else None
        self.last_hit = st.get("last_hit", 0)

    def state(self):
        return {"win": self.win, "prev": self.prev, "last": self.last, "last_hit": self.last_hit}

    def feed(self, t, x, y, age):
        if age is not None and age > 0.3:
            return
        dE = self.dE
        if self.last is not None and t - self.last[0] > 0.3:
            gap = t - self.last[0]
            jump = math.hypot(x - self.last[1], y - self.last[2])
            # 감지는 안내 이동이 시작되고 몇 초 뒤에 켜진다 — 그동안 걸은 거리는 정상이다(11-05 576: 2.5초에 1.9m).
            if gap > 2.0 and jump > self.dist_m and jump > gap * self.walk_mps:
                out({"kind": "gapjump", "t": self.last[0] - dE, "t2": t - dE, "from": [self.last[1], self.last[2]],
                     "to": [x, y], "dist": jump, "gap": gap})
            self.win, self.prev = [], None
        self.last = (t, x, y)
        self.win.append(self.last)
        if self.win[-1][0] - self.win[0][0] < 0.5:
            return
        mx = sum(w[1] for w in self.win) / len(self.win)
        my = sum(w[2] for w in self.win) / len(self.win)
        mt = self.win[-1][0]
        self.win = []
        if self.prev and mt - self.prev[0] < 0.8:
            dist = math.hypot(mx - self.prev[1], my - self.prev[2])
            dt = mt - self.prev[0]
            if dist > self.dist_m and dist / dt > self.speed and mt - self.last_hit > 5:
                self.last_hit = mt
                out({"kind": "jump", "t": mt - dE, "from": [self.prev[1], self.prev[2]], "to": [mx, my],
                     "dist": dist, "dt": dt})
        self.prev = (mt, mx, my)


def jumps(job):
    dE = job["dE"]
    det = JumpDetector(dE, None, job.get("dist_m", 1.0), job.get("speed", 1.6), job.get("walk_mps", 1.3))
    for d in rec_iter(job["lo"] + dE, job["hi"] + dE):
        p = d.get("pose")
        if p:
            det.feed(d["t"], p[0], p[1], d.get("poseAge"))


# ── 수집(collector.py)용: 지난번에 읽은 곳 다음부터 ─────────────────────────
def _day_dirs():
    return sorted(d for d in os.listdir(LOGDIR) if re.match(r"^\d{4}_\d{4}$", d))


def _emit_line(pend, noise, label, dE):
    body = pend[1].strip()
    if body and not (noise and noise.search(body)):
        out({"t": pend[0] - dE, "src": label, "text": body})


def tail_files(job):
    """날짜별 제조사 로그를 지난 위치 다음부터 읽는다. 날짜가 바뀌면 다음 날짜 폴더로 넘어간다.
    처음이면 오늘 0시부터. 제조사 정리로 파일이 작아졌으면(지워졌다 다시 생김) 처음부터."""
    dE, max_bytes = job["dE"], job.get("max_bytes", 30 * 1024 * 1024)
    today = time.strftime("%Y_%m%d")
    for f in job["files"]:
        noise = re.compile(f["noise"]) if f.get("noise") else None
        day, off, last_t = f.get("day") or today, f.get("offset", 0), f.get("last_t")
        budget = max_bytes
        while True:
            path = "%s/%s/%s.%s.log" % (LOGDIR, day, f["file"], day)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if off > size:
                off = 0
            if size > off and budget > 0:
                with open(path, "rb") as fh:
                    fh.seek(off)
                    chunk = fh.read(min(size - off, budget))
                cut = chunk.rfind(b"\n") + 1          # 쓰다 만 줄은 다음 주기에
                off += cut
                budget -= cut
                pend = None
                for raw in chunk[:cut].split(b"\n"):
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    t = line_epoch(line)
                    if t is None:
                        if line.strip():
                            if pend is not None:
                                pend[1] += " | " + line.strip()
                            elif last_t is not None:
                                pend = [last_t, line.strip()]
                        continue
                    if pend is not None:
                        _emit_line(pend, noise, f["label"], dE)
                    body = TS_RE.sub("", line).split("] ", 1)[-1]
                    pend = [t, re.sub(r"^\s*\[(INFO|WARN|ERROR|DEBUG) *\]\s*", "", body)]
                    last_t = t
                if pend is not None:
                    _emit_line(pend, noise, f["label"], dE)
            if off >= size and day < today:
                later = [d for d in _day_dirs() if d > day]
                if later:
                    day, off = later[0], 0
                    continue
            break
        out({"kind": "state", "key": f["key"], "day": day, "offset": off, "last_t": last_t})


def tail_journal(job):
    """journal 을 지난 위치표(cursor) 다음부터. 위치표가 없거나 이미 지워졌으면 since 부터 — 빈 구간이 생겼으면 알린다."""
    dE = job["dE"]
    noise = re.compile(job["noise"]) if job.get("noise") else None
    base = ["journalctl", "--no-pager", "-o", "json"]
    for u in job["units"]:
        base += ["-u", u]
    cursor, gap, p = job.get("cursor"), False, None
    if cursor:
        p = subprocess.run(base + ["--after-cursor", cursor], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            p, gap = None, True
    if p is None:
        since = (job.get("last_t") or (time.time() - dE - 600)) + dE
        p = subprocess.run(base + ["--since", "@%d" % int(since)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    last_t = job.get("last_t")
    for raw in p.stdout.splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        cursor = d.get("__CURSOR") or cursor
        msg = d.get("MESSAGE")
        if isinstance(msg, list):
            msg = bytes(msg).decode("utf-8", "replace")
        t = int(d["__REALTIME_TIMESTAMP"]) / 1e6 - dE
        last_t = t
        if not msg or (noise and noise.search(msg)):
            continue
        unit = (d.get("_SYSTEMD_UNIT") or "").replace(".service", "")
        out({"t": t, "src": job["label"], "text": "%s: %s" % (unit, msg.strip())})
    # 지난번 마지막 줄보다 journal 맨 앞이 뒤면 그 사이가 지워졌다(약 8분 넘게 못 가져옴)
    head = subprocess.run("journalctl --no-pager -o json | head -1", shell=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        oldest = int(json.loads(head.stdout.splitlines()[0])["__REALTIME_TIMESTAMP"]) / 1e6 - dE
    except (ValueError, IndexError, KeyError):
        oldest = None
    if job.get("last_t") and oldest and oldest > job["last_t"]:
        gap = True
    out({"kind": "state", "key": "journal", "cursor": cursor, "last_t": last_t, "gap": gap,
         "gap_from": job.get("last_t"), "gap_to": oldest})


def rec_seek(fh, size, target):
    a, b = 0, size
    while b - a > 200000:
        mid = (a + b) // 2
        fh.seek(mid)
        fh.readline()
        try:
            t = json.loads(fh.readline())["t"]
        except (ValueError, KeyError):
            a = mid
            continue
        if t < target:
            a = mid
        else:
            b = mid
    fh.seek(a)
    fh.readline()
    return fh.tell()


def tail_rec(job):
    """장애물 감지 기록을 지난 위치 다음부터 — step 초마다 한 점 + 멈춤 바뀔 때, 튐 탐지는 전체 프레임으로."""
    dE, step, max_bytes = job["dE"], job.get("step", 1.0), job.get("max_bytes", 40 * 1024 * 1024)
    st = job.get("st") or {}
    files = sorted(glob.glob(REC_GLOB))
    if not files:
        out({"kind": "state", "key": "rec", "st": st})
        return
    path, off = st.get("path"), st.get("offset", 0)
    if path not in files:
        # 처음이면 6시간 전부터 — 그 시각이 든 파일(이름의 시작 시각이 그보다 앞인 마지막 파일)에서 시작해 새 파일로 넘어간다.
        # 가장 최근 파일만 고르면 방금 새로 생긴 빈 파일일 수 있다(11-05 11:54 실제로 그랬다).
        target = time.time() - 6 * 3600
        older = [f for f in files if time.mktime(time.strptime(re.search(r"rec_(\d{8}_\d{6})", f).group(1),
                                                               "%Y%m%d_%H%M%S")) <= target]
        path = older[-1] if older else files[0]
        with open(path, "rb") as fh:
            off = rec_seek(fh, os.path.getsize(path), target)
    det = JumpDetector(dE, st.get("jump"))
    last_out, last_blocked = st.get("last_out"), st.get("last_blocked")
    last_xy = st.get("last_xy")
    while True:
        size = os.path.getsize(path)
        if off > size:
            off = 0
        with open(path, "rb") as fh:
            fh.seek(off)
            chunk = fh.read(min(size - off, max_bytes))
        read_to_end = off + len(chunk) >= size
        cut = chunk.rfind(b"\n") + 1
        off += cut
        for raw in chunk[:cut].split(b"\n"):
            try:
                d = json.loads(raw)
                t, p = d["t"], d.get("pose")
            except (ValueError, KeyError):
                continue
            if not p:
                continue
            det.feed(t, p[0], p[1], d.get("poseAge"))
            blocked = bool(d.get("blocked"))
            idle = str(d.get("skip") or "").startswith("감지 꺼짐")
            # 감지 꺼진 동안(서 있음·충전 중 1초 기록, 11-05 NOS 변경)은 10초마다 또는 0.1m 넘게 바뀌었을 때만 낸다
            moved = last_xy is None or abs(p[0] - last_xy[0]) + abs(p[1] - last_xy[1]) > 0.1
            due = last_out is None or t - last_out >= (IDLE_STEP if idle else step)
            if due or blocked != last_blocked or (idle and moved):
                out({"kind": "pose", "t": t - dE, "x": p[0], "y": p[1], "yaw": p[2], "age": d.get("poseAge"),
                     "blocked": blocked, "obst": d.get("kind"), "minR": d.get("minR"),
                     "idle": d.get("skip") if idle else None})
                last_out, last_xy = t, (p[0], p[1])
            last_blocked = blocked
        newer = [f for f in files if f > path]
        # 새 파일이 생겼으면 옛 파일은 더 안 늘어난다 — 재부팅으로 끝 줄이 줄바꿈 없이 잘려도(11-09 09:12) 넘어간다
        if newer and read_to_end:
            path, off = newer[0], 0
            continue
        break
    out({"kind": "state", "key": "rec", "st": {"path": path, "offset": off, "last_out": last_out,
                                               "last_blocked": last_blocked, "last_xy": last_xy,
                                               "jump": det.state()}})


def uptime(job):
    """부팅 시각(실제 epoch) — 재부팅 감지용."""
    with open("/proc/uptime") as fh:
        up = float(fh.read().split()[0])
    out({"kind": "boot", "boot": time.time() - up - job["dE"]})


def procs(job):
    """지정한 프로세스의 시작 시각(실제 epoch)·누적 CPU 틱·메모리 — /proc 만 읽는다(11-05 측위 튐 원인 확인용).
    python3 은 실행 파일 이름 대신 스크립트 이름(py:receiver.py)으로 묶는다."""
    names = set(job["names"])
    hz = os.sysconf("SC_CLK_TCK")
    with open("/proc/uptime") as fh:
        boot = time.time() - float(fh.read().split()[0])
    rows = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % pid) as fh:
                st = fh.read()
            comm = st[st.index("(") + 1:st.rindex(")")]
            f = st[st.rindex(")") + 2:].split()
            name = comm
            if comm.startswith("python"):
                with open("/proc/%s/cmdline" % pid, "rb") as fh:
                    args = [a for a in fh.read().split(b"\0")[1:] if a and not a.startswith(b"-")]
                if args and args[0].startswith(b"{"):
                    continue                        # 이 수집 자신(python3 - '<작업 JSON>')
                name = "py:" + (os.path.basename(args[0].decode("utf-8", "replace")) if args else "?")
            if comm not in names and name not in names and not (name.startswith("py:") and "py:*" in names):
                continue
            rss = 0
            with open("/proc/%s/status" % pid) as fh:
                for ln in fh:
                    if ln.startswith("VmRSS:"):
                        rss = int(ln.split()[1])
            # stat 필드: ')' 뒤 기준 utime=11, stime=12, starttime=19 (0부터)
            rows.append({"pid": int(pid), "name": name, "ticks": int(f[11]) + int(f[12]),
                         "start": boot + int(f[19]) / hz - job["dE"], "rss_kb": rss})
        except (OSError, ValueError, IndexError):
            continue                                # 그 사이 끝난 프로세스
    out({"kind": "procs", "t": time.time() - job["dE"], "hz": hz, "rows": rows})


def grep(job):
    """여러 날 로그에서 같은 말 찾기(잡음 제외)."""
    dE = job["dE"]
    pat = re.compile(job["pattern"])
    limit = job.get("limit", 300)
    for src in job["sources"]:
        noise = re.compile(src["noise"]) if src.get("noise") else None
        n = 0
        for day in job["days"]:
            path = "%s/%s/%s.%s.log" % (LOGDIR, day, src["file"], day)
            if not os.path.exists(path):
                continue
            p = subprocess.run(["grep", "-aE", job["pattern_grep"], path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            for raw in p.stdout.splitlines():
                line = raw.decode("utf-8", "replace")
                t = line_epoch(line)
                body = re.sub(r"^\[[^]]*\]\s*(\[(INFO|WARN|ERROR|DEBUG) *\]\s*)?", "", line).strip()
                if t is None or not pat.search(body) or (noise and noise.search(body)):
                    continue
                if n < limit:
                    out({"t": t - dE, "src": src["label"], "text": body})
                n += 1
        out({"kind": "count", "src": src["label"], "kept": n})


def main():
    job = json.loads(sys.argv[1])
    ops = {"logs": read_logs, "journal": read_journal, "pose": pose, "jumps": jumps, "grep": grep,
           "tail_files": tail_files, "tail_journal": tail_journal, "tail_rec": tail_rec, "uptime": uptime, "procs": procs}
    for one in job.get("batch", [job]):      # 수집은 SSH 한 번에 여러 작업을 묶어 보낸다
        ops[one["op"]](one)


if __name__ == "__main__":
    main()
