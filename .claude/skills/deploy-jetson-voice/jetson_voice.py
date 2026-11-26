"""Jetson 음성(robot-voice) 3곳 대조와 배포 명령 만들기.

    PYTHONIOENCODING=utf-8 python .claude/skills/deploy-jetson-voice/jetson_voice.py diff              # 3곳 해시 표(읽기 전용)
    PYTHONIOENCODING=utf-8 python .claude/skills/deploy-jetson-voice/jetson_voice.py show scenario.py  # 로컬 대 설치본 줄 차이(읽기 전용)
    PYTHONIOENCODING=utf-8 python .claude/skills/deploy-jetson-voice/jetson_voice.py stage a.py b.py   # /tmp 와 작업 폴더에 올리고 sudo 명령 출력
    PYTHONIOENCODING=utf-8 python .claude/skills/deploy-jetson-voice/jetson_voice.py check             # 서비스 상태·최근 로그·실행 중 설정 키

--host <ssh 별칭>  기본 jetson(172.30.1.44). 사무실 망은 jetson-office, 데모 망은 사용자에게 주소를 묻는다.
robot-voice 는 /opt/robot/voice/robot_voice 설치본으로 돈다. 작업 폴더만 바꾸면 적용되지 않는다(10-12).
"""
import datetime as dt
import difflib
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOCAL = ROOT / "robot_voice"
PROJECT = "/home/und02/project/robot_voice"
INSTALLED = "/opt/robot/voice/robot_voice"

args = sys.argv[1:]
host = "jetson"
if "--host" in args:
    i = args.index("--host")
    host = args[i + 1]
    del args[i:i + 2]
mode, files = (args[0] if args else ""), args[1:]


def ssh(cmd: str, stdin: bytes | None = None, timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", host, cmd],
                           input=stdin, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "시간 초과"
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", errors="replace")


def md5(b: bytes) -> str:
    return hashlib.md5(b.replace(b"\r", b"")).hexdigest()[:10]


def remote_hashes(folder: str) -> dict[str, str]:
    code, out = ssh(f"cd {folder} 2>/dev/null && for f in *.py; do printf '%s ' \"$f\"; tr -d '\\r' < \"$f\" | md5sum | cut -c1-10; done")
    if code != 0:
        return {}
    return dict(ln.split() for ln in out.splitlines() if len(ln.split()) == 2)


def need_connection() -> None:
    code, out = ssh("echo ok")
    if code != 0:
        sys.exit(f"❌ {host} 접속 실패: {out.strip()[:150]}\n   노트북이 로봇 층 Wi-Fi 에 붙어 있는지, 데모 망이면 --host 를 바꿨는지 확인.")


if mode == "diff":
    need_connection()
    local = {p.name: md5(p.read_bytes()) for p in sorted(LOCAL.glob("*.py"))}
    proj, inst = remote_hashes(PROJECT), remote_hashes(INSTALLED)
    print(f"{'파일':22} {'로컬':11} {'Jetson 작업폴더':15} {'설치본(/opt)':12} 판정")
    for name in sorted(set(local) | set(proj) | set(inst)):
        l, p, i = local.get(name, "-"), proj.get(name, "-"), inst.get(name, "-")
        if l == i:
            verdict = "같음" + ("" if p == l else " (Jetson 작업폴더만 다름)")
        elif i == "-":
            verdict = "설치본에 없음(새 파일)"
        elif l == "-":
            verdict = "로컬에 없음 — 설치본에만 있음"
        elif p == i:
            verdict = "⚠ 로컬이 다름 → 배포 대상 후보"
        else:
            verdict = "⚠⚠ 설치본이 로컬·작업폴더 둘 다와 다름 — show 로 먼저 확인"
        print(f"{name:22} {l:11} {p:15} {i:12} {verdict}")
    print("\n'로컬이 다름' 이 전부 이번 요청분인지 확인한다. 다른 작업분이 섞였으면 그 파일은 빼거나 사용자에게 묻는다.")

elif mode == "show":
    need_connection()
    for name in files:
        code, remote = ssh(f"cat {INSTALLED}/{name}")
        if code != 0:
            print(f"== {name}: 설치본 없음")
            continue
        a = remote.replace("\r", "").splitlines()
        b = (LOCAL / name).read_text(encoding="utf-8", errors="replace").replace("\r", "").splitlines()
        print(f"== {name} (- 설치본 / + 로컬)")
        print("\n".join(difflib.unified_diff(a, b, "installed", "local", n=1, lineterm="")) or "(같음)")

elif mode == "stage":
    if not files:
        sys.exit("올릴 파일 이름을 적는다: stage a.py b.py")
    for name in files:
        if not (LOCAL / name).exists():
            sys.exit(f"❌ 로컬에 없음: robot_voice/{name}")
        if subprocess.run([sys.executable, "-m", "py_compile", str(LOCAL / name)],
                          capture_output=True).returncode != 0:
            sys.exit(f"❌ 문법 오류: {name}")
    need_connection()
    day = dt.date.today().strftime("%Y%m%d")
    stage = f"/tmp/voice_deploy_{day}"
    ssh(f"mkdir -p {stage}")
    for name in files:
        data = (LOCAL / name).read_bytes().replace(b"\r", b"")
        # 이 노트북의 scp 가 실패한 적이 있어(OpenSSH 10.3) 표준입력으로 보낸다
        c1, o1 = ssh(f"cat > {stage}/{name}", stdin=data)
        c2, o2 = ssh(f"cp {PROJECT}/{name} {PROJECT}/{name}.bak_{day} 2>/dev/null; cat > {PROJECT}/{name}", stdin=data)
        ok = c1 == 0 and c2 == 0 and md5(data) == remote_hashes(stage).get(name)
        print(f"  {name}: {'올림' if ok else '❌ 실패 ' + (o1 + o2).strip()[:100]}")
    names = " ".join(files)
    # 재시작은 30초 뒤로 예약한다 — ssh 로그인이 und02 pulseaudio 를 띄워 USB 스피커를 잠깐 잡는데,
    # 그 순간 시작한 robot-voice 는 장치 목록에서 스피커를 빠뜨려 안내가 aborted_tts 로 끝난다(2026-10-27 2회 재현)
    # 단위 이름은 고정 — 30초 안에 두 번 실행하면 두 번째는 거절돼 재시작이 겹치지 않는다
    restart = "systemd-run --on-active=30 --unit=robot-voice-delayed-restart systemctl restart robot-voice"
    print(f"""
아래는 sudo 비밀번호가 필요하다 — 사용자가 직접 실행:

! ssh -t {host} "sudo bash -c 'cd {INSTALLED} && for f in {names}; do cp -p \\$f \\$f.bak_{day} 2>/dev/null; install -m 0644 {stage}/\\$f \\$f; done && {restart} && echo 교체 완료 — 30초 뒤 재시작 예약됨'"

실행 뒤 1분 동안 Jetson 에 ssh 접속하지 않는다. 그다음 check 로 확인하고 안내 시작으로 첫 멘트를 들어 본다.

되돌리기:
! ssh -t {host} "sudo bash -c 'cd {INSTALLED} && for f in {names}; do cp -p \\$f.bak_{day} \\$f; done && {restart} && echo 되돌림 — 30초 뒤 재시작 예약됨'"
""")

elif mode == "check":
    need_connection()
    for cmd in ("systemctl is-active robot-voice stt-server",
                "journalctl -u robot-voice -n 20 --no-pager -o cat",
                "tr '\\0' '\\n' < /proc/$(systemctl show -p MainPID --value robot-voice)/environ 2>/dev/null | cut -d= -f1 | tr '\\n' ' '"):
        code, out = ssh(cmd)
        print(f"$ {cmd.split('<')[0].strip()[:60]}\n{out.strip() or '(권한 없음 또는 비어 있음)'}\n")
    print("diff 를 다시 돌려 배포한 파일이 '같음' 인지 확인한다.")

else:
    sys.exit(__doc__)
