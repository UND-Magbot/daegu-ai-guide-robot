"""로봇 로그 수집기 — 별도 컨테이너(control-robotlog)에서 계속 돈다. 읽기 전용(로봇에 파일을 남기지 않음).

계획보고 docs/계획보고/2026-11-05/계획보고_2026-11-05_로봇내부로그_조회환경.md 8절.

  1분(ROBOTLOG_FAST_SEC): NOS 우리 서비스 journal(약 8분이면 지워짐) · 장애물 감지 좌표(1초 간격·튐) · Jetson robot-voice · 시계·부팅
  5분(ROBOTLOG_SLOW_SEC): NOS·본체 제조사 로그(잡음 거름) · CPU·메모리(rte_checker 원본)
  사건(좌표 튐·목표 시간 초과·충돌·측위 상실): 앞뒤 60초 제조사 원본 전부를 events/ 에 따로
  보관: /data/<YYYY-MM-DD>/<출처>.log (한국 시각 한 줄 = 'YYYY-MM-DD HH:MM:SS.mmm [출처] 내용')
        지난 날짜는 .gz 로 압축, ROBOTLOG_RETENTION_DAYS(90) 지나면 삭제. 읽은 위치는 /data/state.json.
로봇이 망에 없으면 그 주기는 건너뛰고, 다시 붙으면 읽은 위치 다음부터 이어 받는다.
"""
import gzip
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ROBOTLOG_REMOTE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "remote.py"))
import agent  # noqa: E402  (같은 폴더: 접속·시계·잡음 규칙을 1단계 도구와 공유)

KST = timezone(timedelta(hours=9))
DATA = os.environ.get("ROBOTLOG_DATA", "/data")
FAST = float(os.environ.get("ROBOTLOG_FAST_SEC", "60"))
SLOW = float(os.environ.get("ROBOTLOG_SLOW_SEC", "300"))
RETENTION = int(os.environ.get("ROBOTLOG_RETENTION_DAYS", "90"))
JETSON_HOST = os.environ.get("ROBOTLOG_JETSON_HOST", "")
JETSON_USER = os.environ.get("ROBOTLOG_JETSON_USER", "und02")
JETSON_KEY = os.environ.get("ROBOTLOG_JETSON_KEY", "/keys/jetson")
EVENT_MAX_PER_DAY = int(os.environ.get("ROBOTLOG_EVENT_MAX_PER_DAY", "40"))
STATE_PATH = os.path.join(DATA, "state.json")

# 사건: 이 말이 나오면 앞뒤 60초 원본을 따로 남긴다
# 측위 상실은 0.5초마다 '定位丢失持续N秒' 로 이어 찍히므로 첫 줄만(같은 출처 30초 안 반복은 사건으로 안 셈 — event())
EVENT_RE = re.compile(r"Reach goal out of time|Collision caused|定位丢失(?!持续)|\[8b09\]")
# 사건 원본 보관 때 읽을 파일(잡음 거르지 않음) — 라이다(rsdriver)·코어별 사용률(cpu_node) 포함
RAW_FILES = {
    "nos": ["localization", "planner", "handler", "charge_manager", "rsdriver", "cpu_node", "rte_checker",
            "reflective_column_node"],
    "body": ["Basic_server", "rl_deploy", "ctrlmcu_node", "height_map_nav", "rsdriver", "cpu_node", "rte_checker",
             "lio_perception"],
}
SLOW_EXTRA = {"nos": [{"label": "NOS부하", "file": "rte_checker", "noise": r"Buffers: .* Cached"}],
              "body": [{"label": "본체부하", "file": "rte_checker", "noise": r"Buffers: .* Cached"}]}
FILE_KEY = {"좌표": "nos.pose", "NOS서비스": "nos.journal", "음성": "jetson.voice"}
# 프로세스 자원 추적(5분) — 여러 날 켜 두면 측위가 나빠지는지 보려고(계획보고 '측위튐_원인확인방안' B2, 11-05)
PROC_NAMES = {
    "nos": ["localization_dd", "localPlanner", "rslidar", "handler", "pcl_pass_grid", "charge_manager",
            "map_server", "yesense_node", "reflective_colu", "py:*"],
    "body": ["basic_server", "rl_deploy", "lio_ddsnode", "height_map_nav", "rslidar", "yesense_node",
             "ctrlmcu_node"],
}
LOC_PROC = {"nos": "localization_dd", "body": "lio_ddsnode"}   # '측위 켜진 뒤 경과'의 기준


def log(msg):
    print(f"{datetime.now(KST):%m-%d %H:%M:%S} {msg}", flush=True)


def kst_str(t):
    return datetime.fromtimestamp(t, KST).strftime("%Y-%m-%d %H:%M:%S.%f")[:23]


# ── 상태·쓰기 ─────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


class Writer:
    """출처별·날짜별 파일에 덧붙인다. 한 주기 안에서 파일을 한 번씩만 연다."""

    def __init__(self):
        self.buf = {}

    def add(self, t, key, label, text):
        day = datetime.fromtimestamp(t, KST).strftime("%Y-%m-%d")
        self.buf.setdefault((day, key), []).append((t, f"{kst_str(t)} [{label}] {text}"))

    def flush(self):
        n = 0
        for (day, key), rows in self.buf.items():
            d = os.path.join(DATA, day)
            os.makedirs(d, exist_ok=True)
            rows.sort(key=lambda r: r[0])
            with open(os.path.join(d, key + ".log"), "a", encoding="utf-8") as fh:
                for _, line in rows:
                    fh.write(line.replace("\n", " ") + "\n")
            n += len(rows)
        self.buf = {}
        return n


def pose_text(r):
    extra = f" 멈춤({r.get('obst') or '-'}, {r.get('minR')}m)" if r.get("blocked") else ""
    if r.get("idle"):                       # 서 있음·충전 중(감지 꺼짐) — NOS 11-05 변경 뒤부터
        extra += f" [{r['idle']}]"
    return f"({r['x']:.2f},{r['y']:.2f}) 방향 {r['yaw']:.2f}{extra}"


def jump_text(r):
    if r["kind"] == "jump":
        return (f"⚠ 튐 ({r['from'][0]:.2f},{r['from'][1]:.2f}) → ({r['to'][0]:.2f},{r['to'][1]:.2f}) "
                f"{r['dist']:.2f}m / {r['dt']:.1f}s")
    return (f"⚠ 기록 끊김 전후 {r['dist']:.2f}m (끊김 {r['gap']:.0f}초) "
            f"({r['from'][0]:.2f},{r['from'][1]:.2f}) → ({r['to'][0]:.2f},{r['to'][1]:.2f})")


# ── 수집 ─────────────────────────────────────────────────────────────────
class Collector:
    def __init__(self):
        self.st = load_state()
        self.w = Writer()
        self.last_slow = 0.0
        self.last_maint = 0.0

    def event(self, t, kind, text):
        ev = self.st.setdefault("events", {"pending": [], "count": {}, "last": 0})
        seen = ev.setdefault("seen", {})
        if abs(t - seen.get(kind, 0)) < 30:          # 같은 출처가 30초 안에 또 → 한 사건
            return
        seen[kind] = t
        self.w.add(t, "events", "사건", f"{kind}: {text}{self.age_text(t)}")
        day = datetime.fromtimestamp(t, KST).strftime("%Y-%m-%d")
        if ev["count"].get(day, 0) >= EVENT_MAX_PER_DAY or t - ev.get("last", 0) < 120:
            return                                  # 원본 보관은 2분에 하나·하루 상한까지
        ev["pending"].append({"t": t, "kind": kind})
        ev["last"] = t
        ev["count"][day] = ev["count"].get(day, 0) + 1

    def age_text(self, t):
        """사건 시각 기준 'NOS 부팅 뒤 N시간 · 측위 켜진 뒤 M시간'. 사건이 그 시작보다 앞이면(따라잡기) 뺀다."""
        nos = self.st.get("nos") or {}
        parts = []
        for label, start in (("NOS 부팅 뒤", nos.get("boot")), ("측위 켜진 뒤", nos.get("loc_start"))):
            if start and t >= start:
                parts.append(f"{label} {(t - start) / 3600:.1f}시간")
        return f" [{' · '.join(parts)}]" if parts else ""

    def procs_lines(self, host, r):
        """프로세스별 한 줄: 켜진 뒤 경과·메모리·지난 기록 뒤 CPU 평균."""
        hs = self.st.setdefault(host, {})
        prev = hs.get("procs") or {}
        cur = {}
        for p in sorted(r["rows"], key=lambda p: p["name"]):
            key = str(p["pid"])
            cur[key] = {"t": r["t"], "ticks": p["ticks"]}
            cpu = ""
            if key in prev and r["t"] > prev[key]["t"]:
                cpu = f" · CPU {(p['ticks'] - prev[key]['ticks']) / r['hz'] / (r['t'] - prev[key]['t']) * 100:.0f}%"
            self.w.add(r["t"], f"{host}.procs", "프로세스",
                       f"{p['name']}({p['pid']}) 켜진 뒤 {(r['t'] - p['start']) / 3600:.1f}시간 · "
                       f"메모리 {p['rss_kb'] / 1024:.0f}MB{cpu}")
            if p["name"] == LOC_PROC.get(host):
                old = hs.get("loc_start")
                if old and abs(p["start"] - old) > 30:
                    self.w.add(p["start"], "events", "사건", f"{host} 측위 프로그램 다시 시작({p['name']})")
                hs["loc_start"] = p["start"]
        hs["procs"] = cur

    def sink_for(self, host, key_of_label):
        def sink(r):
            k = r.get("kind")
            if k == "procs":
                self.procs_lines(host, r)
            elif k == "state":
                self.st.setdefault(host, {})[r["key"]] = r
            elif k == "boot":
                prev = self.st.setdefault(host, {}).get("boot")
                if prev and abs(r["boot"] - prev) > 30:
                    self.w.add(r["boot"], "events", "사건", f"{host} 재부팅 (부팅 {kst_str(r['boot'])[11:19]})")
                self.st[host]["boot"] = r["boot"]
            elif k == "error":
                log(f"⚠ {host}: {r.get('text', '')[:300]}")
            elif k == "pose":
                self.w.add(r["t"], "nos.pose", "좌표", pose_text(r))
            elif k in ("jump", "gapjump"):
                self.w.add(r["t"], "nos.pose", "좌표", jump_text(r))
                self.event(r["t"], "좌표 튐", jump_text(r))
            elif "t" in r and r.get("src"):
                self.w.add(r["t"], key_of_label(r["src"]), r["src"], r["text"])
                if EVENT_RE.search(r["text"]):
                    self.event(r["t"], r["src"], r["text"][:200])
        return sink

    def robot_cycle(self, host, slow):
        try:
            c = agent.connect(agent.HOSTS[host])
        except Exception as e:  # noqa: BLE001 — 다른 망이면 이번 주기는 건너뜀
            if self.st.get(host, {}).get("online", True):
                log(f"{host} 접속 안 됨 — 다시 붙으면 이어 받음 ({e!r:.120})")
            self.st.setdefault(host, {})["online"] = False
            return
        try:
            hs = self.st.setdefault(host, {})
            if not hs.get("online", True):
                log(f"{host} 다시 연결 — 이어 받기")
            hs["online"] = True
            dE, rtt, tz = agent.measure_clock(c)
            self.w.add(time.time(), "clock", "시계", f"{host} 차이 {dE:+.2f}s (왕복 {rtt * 1000:.0f}ms, {tz})")
            batch = [{"op": "uptime", "dE": dE}]
            labels = {}
            if host == "nos":
                batch.append(dict(agent.JOURNAL, op="tail_journal", dE=dE,
                                  cursor=(hs.get("journal") or {}).get("cursor"),
                                  last_t=(hs.get("journal") or {}).get("last_t")))
                batch.append({"op": "tail_rec", "dE": dE, "step": 1.0, "st": (hs.get("rec") or {}).get("st")})
            if slow:
                batch.append({"op": "procs", "dE": dE, "names": PROC_NAMES[host]})
                files = []
                for src in agent.FILE_SOURCES[host] + SLOW_EXTRA[host]:
                    key = f"{host}.{src['file']}"
                    labels[src["label"]] = key
                    prev = hs.get(key) or {}
                    files.append(dict(src, key=key, day=prev.get("day"), offset=prev.get("offset", 0),
                                      last_t=prev.get("last_t")))
                batch.append({"op": "tail_files", "dE": dE, "files": files})
            agent.run_remote(c, {"batch": batch}, host,
                             self.sink_for(host, lambda lb: labels.get(lb) or FILE_KEY.get(lb) or f"{host}.misc"))
            j = hs.get("journal") or {}
            if j.get("gap") and j.get("gap_from"):
                self.w.add(j["gap_from"], "nos.journal", "NOS서비스",
                           f"⚠ 수집 빈 구간 — journal 이 이미 지워져 {kst_str(j['gap_from'])[11:19]}~"
                           f"{kst_str(j['gap_to'])[11:19] if j.get('gap_to') else '?'} 기록 없음")
                j["gap"] = False
            self.snapshots(c, host, dE)
        finally:
            c.close()

    def snapshots(self, c, host, dE):
        """사건 앞뒤 60초 제조사 원본 — 사건 뒤 65초가 지나야 '뒤'가 다 쌓인다."""
        ev = self.st.get("events") or {}
        done_key = f"snap_{host}"
        now = time.time()
        for e in ev.get("pending", []):
            if e.get(done_key) or now < e["t"] + 65:
                continue
            d = os.path.join(DATA, datetime.fromtimestamp(e["t"], KST).strftime("%Y-%m-%d"), "events",
                             datetime.fromtimestamp(e["t"], KST).strftime("%H%M%S_") + re.sub(r"\W+", "_", e["kind"]))
            os.makedirs(d, exist_ok=True)
            rows = []
            srcs = [{"label": f, "file": f, "noise": ""} for f in RAW_FILES[host]]
            agent.run_remote(c, {"op": "logs", "dE": dE, "lo": e["t"] - 60, "hi": e["t"] + 60, "sources": srcs,
                                 "limit": 200000}, host, rows.append)
            by = {}
            for r in rows:
                if "t" in r:
                    by.setdefault(r["src"], []).append(f"{kst_str(r['t'])} {r['text']}")
            for name, lines in by.items():
                with gzip.open(os.path.join(d, f"{host}.{name}.log.gz"), "wt", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            e[done_key] = True
            log(f"사건 원본 보관 {host} {kst_str(e['t'])[11:19]} {e['kind']} → {d} ({sum(map(len, by.values()))}줄)")
        hosts_needed = ("snap_nos", "snap_body")
        ev["pending"] = [e for e in ev.get("pending", [])
                         if not all(e.get(k) for k in hosts_needed) and now - e["t"] < 3600]

    def jetson_cycle(self):
        if not JETSON_HOST or not os.path.isfile(JETSON_KEY):
            return
        import paramiko
        js = self.st.setdefault("jetson", {})
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(JETSON_HOST, username=JETSON_USER, key_filename=JETSON_KEY, timeout=8,
                      look_for_keys=False, allow_agent=False)
        except Exception as e:  # noqa: BLE001
            if js.get("online", True):
                log(f"jetson 접속 안 됨 — 다시 붙으면 이어 받음 ({e!r:.120})")
            js["online"] = False
            return
        try:
            js["online"] = True
            t0 = time.time()
            _, o, _ = c.exec_command("date +%s.%N; cat /proc/uptime")
            lines = o.read().decode().split()
            t1 = time.time()
            dJ = float(lines[0]) - (t0 + t1) / 2
            boot = time.time() - float(lines[1]) - dJ
            if js.get("boot") and abs(boot - js["boot"]) > 30:
                self.w.add(boot, "events", "사건", f"jetson 재부팅 (부팅 {kst_str(boot)[11:19]})")
            js["boot"] = boot
            self.w.add(time.time(), "clock", "시계", f"jetson 차이 {dJ:+.2f}s")
            cur = js.get("cursor")
            q = f"--after-cursor '{cur}'" if cur else f"--since @{int(time.time() - 600)}"
            _, o, _ = c.exec_command(f"journalctl -u robot-voice --no-pager -o json {q} || "
                                     f"journalctl -u robot-voice --no-pager -o json --since @{int(time.time() - 600)}")
            for raw in o.read().decode("utf-8", "replace").splitlines():
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                js["cursor"] = d.get("__CURSOR") or js.get("cursor")
                msg = d.get("MESSAGE")
                if isinstance(msg, list):
                    msg = bytes(msg).decode("utf-8", "replace")
                if msg:
                    # Jetson 시계는 부팅 직후 틀릴 수 있다(11-05) — 지금 잰 차이로 보정
                    self.w.add(int(d["__REALTIME_TIMESTAMP"]) / 1e6 - dJ, "jetson.voice", "음성", msg.strip())
        finally:
            c.close()

    def maintenance(self):
        """지난 날짜 압축·오래된 날짜 삭제 — 한 시간에 한 번."""
        today = datetime.now(KST).date()
        for name in os.listdir(DATA):
            p = os.path.join(DATA, name)
            try:
                day = datetime.strptime(name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if (today - day).days > RETENTION:
                shutil.rmtree(p, ignore_errors=True)
                log(f"보관 기간 지남 — 삭제 {name}")
            elif day < today:
                for f in os.listdir(p):
                    if f.endswith(".log"):
                        src = os.path.join(p, f)
                        with open(src, "rb") as a, gzip.open(src + ".gz", "ab") as b:
                            shutil.copyfileobj(a, b)
                        os.remove(src)

    def run(self):
        os.makedirs(DATA, exist_ok=True)
        log(f"시작 — 빠른 주기 {FAST:.0f}s · 느린 주기 {SLOW:.0f}s · 보관 {RETENTION}일 · 저장 {DATA}"
            f" · Jetson {JETSON_HOST or '없음'}{'' if os.path.isfile(JETSON_KEY) else '(키 없음)'}")
        while True:
            t0 = time.time()
            slow = t0 - self.last_slow >= SLOW
            for host in ("nos", "body"):
                try:
                    self.robot_cycle(host, slow)
                except Exception as e:  # noqa: BLE001 — 한 장비 실패가 수집 전체를 멈추지 않게
                    log(f"⚠ {host} 주기 실패: {e!r:.300}")
            try:
                self.jetson_cycle()
            except Exception as e:  # noqa: BLE001
                log(f"⚠ jetson 주기 실패: {e!r:.300}")
            n = self.w.flush()
            save_state(self.st)
            if slow:
                self.last_slow = t0
                log(f"느린 주기 끝 — {n}줄 저장 ({time.time() - t0:.1f}s)")
            if t0 - self.last_maint >= 3600:
                self.maintenance()
                self.last_maint = t0
            time.sleep(max(1.0, FAST - (time.time() - t0)))


if __name__ == "__main__":
    if "--once" in sys.argv:       # 시험: 빠른·느린 주기를 한 번씩
        col = Collector()
        for h in ("nos", "body"):
            col.robot_cycle(h, True)
        col.jetson_cycle()
        print("저장", col.w.flush(), "줄")
        save_state(col.st)
    else:
        Collector().run()
