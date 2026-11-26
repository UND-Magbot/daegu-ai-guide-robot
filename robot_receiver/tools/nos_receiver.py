"""NOS receiver.py 확인·배포 — 관제 backend 컨테이너 안에서 실행한다(.env 의 NOS_HOST/PORT/USER/PASSWORD 사용).

    docker cp robot_receiver/tools/nos_receiver.py control-backend:/tmp/nos_receiver.py
    docker cp robot_receiver/receiver.py control-backend:/tmp/receiver_new.py     # check·deploy 때만
    docker cp robot_receiver/relay_map.py control-backend:/tmp/relay_map_new.py   # 있으면 relay_map 도 함께
    docker exec control-backend python /tmp/nos_receiver.py speed|check|status|deploy

  speed  — NOS receiver 의 안내 속도 매핑(NAV_SPEED_CODES, 고속 fast → 2)만 읽는다. 읽기 전용
  check  — 로컬 receiver(·relay_map) 와 NOS 본의 차이(diff). 읽기 전용
  status — receiver·relay 서비스 상태와 receiver 최근 로그. 읽기 전용
  deploy — 백업 후 로컬 파일을 올리고 서비스 재시작(sudo). 로컬≠NOS 인 부분이 있으니 check 먼저

장애물 감지 노드(obstacle_guard.py·obstacle_core.py, 10-29) — 중계기 서비스 3개는 건드리지 않는다:
    docker cp robot_receiver/obstacle_core.py  control-backend:/tmp/obstacle_core_new.py
    docker cp robot_receiver/obstacle_guard.py control-backend:/tmp/obstacle_guard_new.py
    docker cp robot_receiver/obstacle_ignore.json control-backend:/tmp/obstacle_ignore_new.json
  guard-check   — 로컬 대 NOS 차이, 서비스·설정 파일 유무. 읽기 전용
  guard-status  — 서비스 상태·최근 로그·기록 파일 크기. 읽기 전용
  guard-deploy  — 두 파일 백업·교체·NOS 문법 검사(실패면 되돌림), 서비스가 있으면 그것만 재시작(sudo)
  guard-install — 서비스(obstacle-guard)와 설정 파일(obstacle_guard.env, 없을 때만 — 기본 '기록만')을 만들고 켠다(sudo)
  guard-remove  — 서비스를 끄고 지운다. 파일·설정·기록은 남긴다(sudo)
  guard-live / guard-record — 설정 OBSTACLE_RECORD_ONLY 를 0(실제 정지)/1(기록만)로 바꾸고 감지 노드만 재시작(sudo)

컨테이너를 다시 만들면 /tmp 가 비워지므로 매번 docker cp 부터 한다(10-22 재배포 때 사라진 것을 저장소로 옮김).
서비스 이름은 NOS 마다 다르다(296: receiver·relay_charge·relay_motion, 669: control-receiver·relay-charge·
relay-motion) — 접속 뒤 실제로 있는 이름을 골라 쓴다.
relay_map 은 서비스가 아니라 관제가 매핑·재조정 때 띄우므로 재시작 대상이 아니다(다음 기동부터 새 판).
"""
import hashlib
import os
import sys
import time

import paramiko

REMOTE_DIR = "/home/user/Control_system"
# (로컬 업로드본, NOS 경로) — 로컬본이 /tmp 에 있는 것만 다룬다. receiver 는 필수.
FILES = [("/tmp/receiver_new.py", f"{REMOTE_DIR}/receiver.py"),
         ("/tmp/relay_map_new.py", f"{REMOTE_DIR}/relay_map.py")]
REMOTE = FILES[0][1]
SERVICE_CANDIDATES = [("receiver", "control-receiver", "robot-receiver"),
                      ("relay_charge", "relay-charge", "robot-relay-charge"),
                      ("relay_motion", "relay-motion")]
GUARD_FILES = [("/tmp/obstacle_core_new.py", f"{REMOTE_DIR}/obstacle_core.py"),
               ("/tmp/obstacle_guard_new.py", f"{REMOTE_DIR}/obstacle_guard.py"),
               # 무시 구역(지도 이름별) — 저장소 robot_receiver/obstacle_ignore.json 이 정본, 배포 때 덮는다
               ("/tmp/obstacle_ignore_new.json", f"{REMOTE_DIR}/obstacle_ignore.json")]
GUARD_UNIT = "obstacle-guard"
GUARD_ENV = f"{REMOTE_DIR}/obstacle_guard.env"
GUARD_REC_DIR = f"{REMOTE_DIR}/obstacle_rec"
# 669 의 relay-motion 과 같은 꼴(root·ROS2 환경). 0~3번 코어에서 다른 일과 겹치므로 우선순위를 낮추고 CPU 를 묶는다.
GUARD_UNIT_TEXT = f"""[Unit]
Description=Obstacle guard (ROS2 /LOC_BODY_POINTS -> CANCEL_NAV via receiver.py, 2026-10-29)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={REMOTE_DIR}
# HOME 이 없으면 rclpy.init 이 로그 폴더를 못 찾아 죽는다(10-29 실기) — relay-motion 과 같게
Environment=HOME=/root
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-{GUARD_ENV}
ExecStart=/bin/bash -c 'source /opt/robot/scripts/setup_ros2.sh && exec python3 obstacle_guard.py'
Nice=10
CPUQuota=60%
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=obstacle-guard

[Install]
WantedBy=multi-user.target
"""
# 처음 설치할 때만 쓴다(있으면 그대로 둔다 — 실기에서 고친 값을 지우지 않게)
GUARD_ENV_TEXT = f"""# 장애물 감지 노드 설정(obstacle_core.Params·obstacle_guard). 바꾼 뒤: sudo systemctl restart {GUARD_UNIT}
# 판단은 관제가 안내 시나리오 이동 중에 켤 때만 한다. 실기 확인 전에는 '기록만'(NOS 안전장치) — 막혀도 CANCEL_NAV 를 보내지 않는다.
# 실제 정지: 이 값을 0 으로 바꾸고 관제 .env OBSTACLE_GUARD_MODE=on.
OBSTACLE_RECORD_ONLY=1
OBSTACLE_RECORD_DIR={GUARD_REC_DIR}
# 기록: 파일 100MB 마다 새 파일, 합계 2GB 넘으면 오래된 것부터 지움. 정지 사건(앞뒤 5초, 원본 점 포함)은 기록 폴더 옆 obstacle_rec_events/ 에 30일·500MB
OBSTACLE_RECORD_FILE_MB=100
OBSTACLE_RECORD_TOTAL_MB=2000
OBSTACLE_EVENT_KEEP_DAYS=30
OBSTACLE_EVENT_TOTAL_MB=500
OBSTACLE_RAW_EVERY=10
OBSTACLE_EVERY_N=1
# 바닥·천장 점 거르기 — 기록의 zBox·zWide 분포를 본 뒤 정한다(비우면 거르지 않음)
#OBSTACLE_Z_MIN=-0.2
#OBSTACLE_Z_MAX=1.5
"""
GUARD_MODES = ("guard-check", "guard-status", "guard-deploy", "guard-install", "guard-remove", "guard-live", "guard-record")
MODES = ("speed", "check", "status", "deploy") + GUARD_MODES

mode = sys.argv[1] if len(sys.argv) > 1 else ""
if mode not in MODES:
    sys.exit(f"사용법: python nos_receiver.py {'|'.join(MODES)}")

pw = os.environ["NOS_PASSWORD"]
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(os.environ["NOS_HOST"], port=int(os.environ.get("NOS_PORT") or 22),
            username=os.environ["NOS_USER"], password=pw, timeout=8)


def run(cmd, sudo=False):
    if sudo:
        cmd = f"sudo -S -p '' {cmd}"
    i, o, e = cli.exec_command(cmd, timeout=30)
    if sudo:
        i.write(pw + "\n")
        i.flush()
    return o.read().decode(errors="replace") + e.read().decode(errors="replace")


def norm_md5(b):
    return hashlib.md5(b.replace(b"\r", b"")).hexdigest()


def resolve_services():
    """후보 이름 중 NOS 에 실제로 있는 유닛을 고른다. 없으면 None."""
    found = []
    for names in SERVICE_CANDIDATES:
        hit = next((n for n in names
                    if run(f"systemctl cat {n}.service >/dev/null 2>&1 && echo HAVE").strip() == "HAVE"),
                   None)
        found.append(hit)
    return found


def guard_main():
    """감지 노드 모드 — 중계기 서비스는 재시작하지 않는다."""
    sftp = cli.open_sftp()
    have_unit = run(f"systemctl cat {GUARD_UNIT}.service >/dev/null 2>&1 && echo HAVE").strip() == "HAVE"

    def remote_bytes(path):
        try:
            return sftp.open(path, "rb").read()
        except IOError:
            return None

    targets = [(lp, rp) for lp, rp in GUARD_FILES if os.path.exists(lp)]
    if mode in ("guard-deploy", "guard-check") and len(targets) != len(GUARD_FILES):
        sys.exit("❌ /tmp/obstacle_core_new.py·obstacle_guard_new.py·obstacle_ignore_new.json 을 먼저 복사한다")
    if mode == "guard-check":
        for lp, rp in targets:
            cur = remote_bytes(rp)
            print(f"== {rp}\nlocal ", norm_md5(open(lp, "rb").read()))
            print("remote", norm_md5(cur) if cur is not None else "(없음)")
        print("서비스:", "있음" if have_unit else "없음", "/ 설정:", "있음" if remote_bytes(GUARD_ENV) is not None else "없음")
    elif mode == "guard-deploy":
        stamp = time.strftime('%Y%m%d-%H%M%S')
        done = []
        for lp, rp in targets:
            had = remote_bytes(rp) is not None
            bak = f"{rp}.bak-{stamp}"
            if had:
                print(run(f"cp -p {rp} {bak} && ls -l {bak}"))
            with sftp.open(rp, "wb") as f:
                f.write(open(lp, "rb").read().replace(b"\r", b""))
            done.append((rp, bak if had else None))
            print("after ", rp, norm_md5(sftp.open(rp, "rb").read()))
        # NOS 파이썬 3.8 로 검사 — 하나라도 실패하면 둘 다 되돌린다(새 파일이면 지운다)
        bad = [rp for _, rp in targets if rp.endswith(".py")
               and "COMPILE_OK" not in run(f"python3 -m py_compile {rp} && echo COMPILE_OK")]
        bad += [rp for _, rp in targets if rp.endswith(".json")
                and "JSON_OK" not in run(f"python3 -m json.tool {rp} >/dev/null && echo JSON_OK")]
        if bad:
            for rp, bak in done:
                print(run(f"cp -p {bak} {rp} && echo RESTORED {rp}" if bak else f"rm -f {rp} && echo REMOVED {rp}"))
            sys.exit(f"❌ NOS 에서 문법 검사 실패({', '.join(bad)}) — 되돌림, 재시작 안 함")
        print("백업 꼬리표:", f".bak-{stamp}")
        if have_unit:
            print(run(f"systemctl restart {GUARD_UNIT}", sudo=True))
            time.sleep(4)
        else:
            print("서비스 없음 — 재시작 안 함(guard-install 로 만든다)")
    elif mode == "guard-install":
        for _, rp in GUARD_FILES:
            if remote_bytes(rp) is None:
                sys.exit(f"❌ {rp} 가 없다 — guard-deploy 를 먼저 한다")
        unit_tmp, env_tmp = f"/tmp/{GUARD_UNIT}.service", "/tmp/obstacle_guard.env"
        with sftp.open(unit_tmp, "wb") as f:
            f.write(GUARD_UNIT_TEXT.encode())
        print(run(f"install -m 0644 {unit_tmp} /etc/systemd/system/{GUARD_UNIT}.service && echo UNIT_OK", sudo=True))
        if remote_bytes(GUARD_ENV) is None:
            with sftp.open(env_tmp, "wb") as f:
                f.write(GUARD_ENV_TEXT.encode())
            print(run(f"cp {env_tmp} {GUARD_ENV} && echo 'ENV_OK(기록만)'"))
        else:
            print("설정 파일 있음 — 그대로 둔다:", GUARD_ENV)
        # sudo 는 명령 하나에만 붙는다 — '&&' 로 이으면 뒤 systemctl 이 권한 없이 돈다(10-29 실기 실패). 하나씩 보낸다
        print(run("systemctl daemon-reload", sudo=True))
        print(run(f"systemctl enable {GUARD_UNIT}", sudo=True))
        print(run(f"systemctl restart {GUARD_UNIT}", sudo=True))       # 이미 돌거나 재시작 반복 중이어도 새 정의로
        time.sleep(4)
    elif mode in ("guard-live", "guard-record"):
        # 실제 정지 시험(live) ↔ 기록만(record) — 설정 한 줄만 바꾸고 감지 노드만 재시작. 관제 MODE 도 on 이어야 실제로 선다
        if remote_bytes(GUARD_ENV) is None:
            sys.exit(f"❌ {GUARD_ENV} 가 없다 — guard-install 을 먼저 한다")
        val = "0" if mode == "guard-live" else "1"
        print(run(f"sed -i 's/^OBSTACLE_RECORD_ONLY=.*/OBSTACLE_RECORD_ONLY={val}/' {GUARD_ENV} && grep -c '^OBSTACLE_RECORD_ONLY={val}$' {GUARD_ENV}"))
        print(run(f"systemctl restart {GUARD_UNIT}", sudo=True))
        time.sleep(4)
    elif mode == "guard-remove":
        print(run(f"systemctl disable --now {GUARD_UNIT}", sudo=True))
        print(run(f"rm -f /etc/systemd/system/{GUARD_UNIT}.service", sudo=True))
        print(run("systemctl daemon-reload", sudo=True))
    if mode in ("guard-status", "guard-deploy", "guard-install", "guard-remove", "guard-live", "guard-record"):
        print("상태:", run(f"systemctl is-active {GUARD_UNIT}"))
        print(run(f"journalctl -u {GUARD_UNIT} -n 15 --no-pager -o cat", sudo=True))
        print("설정:", run(f"grep -v '^#' {GUARD_ENV} 2>/dev/null | grep . || echo 없음"))
        print("기록:", run(f"ls -l {GUARD_REC_DIR} 2>/dev/null | tail -3 || echo 없음"))
        print("CPU:", run("ps -eo pid,pcpu,etime,args | grep -E 'obstacle_guard' | grep -v grep || echo 안 돎"))
    cli.close()
    sys.exit(0)


if mode in GUARD_MODES:
    guard_main()

services = resolve_services()
print("서비스:", ", ".join(f"{s or '없음'}" for s in services))
if None in services:
    missing = [names[0] for names, s in zip(SERVICE_CANDIDATES, services) if s is None]
    if mode == "deploy":
        sys.exit(f"❌ 서비스를 찾지 못함({', '.join(missing)}) — 재시작할 수 없어 배포 중단")
    services = [s for s in services if s]
RECEIVER_UNIT = services[0] if services else "receiver"

sftp = cli.open_sftp()
remote = sftp.open(REMOTE, "rb").read()
targets = [(lp, rp) for lp, rp in FILES if os.path.exists(lp)]

if mode == "speed":
    text = remote.decode(errors="replace").replace("\r", "")
    lines = [ln for ln in text.splitlines() if "NAV_SPEED_CODES" in ln or '"fast"' in ln or "'fast'" in ln]
    print("\n".join(lines) or "(NAV_SPEED_CODES 없음)")
    has_fast = '"fast": 2' in text or "'fast': 2" in text
    print("고속(fast → 2):", "있음" if has_fast else "없음 — 고속을 고르면 보통 속도로 간다")

if mode == "check":
    import difflib
    for lp, rp in targets:
        local = open(lp, "rb").read()
        cur = sftp.open(rp, "rb").read()
        print(f"== {rp}\nlocal ", norm_md5(local), len(local))
        print("remote", norm_md5(cur), len(cur))
        a = cur.decode(errors="replace").replace("\r", "").splitlines()
        b = local.decode(errors="replace").replace("\r", "").splitlines()
        for line in difflib.unified_diff(a, b, "nos", "local", n=1, lineterm=""):
            print(line)
elif mode == "deploy":
    stamp = time.strftime('%Y%m%d-%H%M%S')
    done = []
    for lp, rp in targets:
        local = open(lp, "rb").read()
        bak = f"{rp}.bak-{stamp}"
        print(run(f"cp -p {rp} {bak} && ls -l {bak}"))
        with sftp.open(rp, "wb") as f:
            f.write(local.replace(b"\r", b""))
        done.append((rp, bak))
        print("after ", rp, norm_md5(sftp.open(rp, "rb").read()))
        # NOS 파이썬(669 는 3.8)으로 검사한다 — 로컬 검사만으로는 판 차이를 못 잡는다
        if "COMPILE_OK" not in run(f"python3 -m py_compile {rp} && echo COMPILE_OK"):
            for p, b in done:
                print(run(f"cp -p {b} {p} && echo RESTORED {p}"))
            sys.exit(f"❌ NOS 에서 문법 검사 실패({rp}) — 백업으로 되돌림, 재시작 안 함")
    print("백업 꼬리표:", f".bak-{stamp}")
    print(run("systemctl restart " + " ".join(services), sudo=True))
    time.sleep(4)
if mode in ("deploy", "status"):
    print(run("systemctl is-active " + " ".join(services)))
    print(run(f"journalctl -u {RECEIVER_UNIT} -n 15 --no-pager", sudo=True))
cli.close()
