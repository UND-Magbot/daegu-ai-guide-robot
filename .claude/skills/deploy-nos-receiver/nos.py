"""NOS 중계기(receiver.py) 확인·배포 — robot_receiver/tools/nos_receiver.py 를 관제 컨테이너 안에서 돌린다.

    PYTHONIOENCODING=utf-8 python .claude/skills/deploy-nos-receiver/nos.py check    # 로컬 대 NOS 차이(읽기 전용)
    PYTHONIOENCODING=utf-8 python .claude/skills/deploy-nos-receiver/nos.py status   # 서비스 상태·최근 로그(읽기 전용)
    PYTHONIOENCODING=utf-8 python .claude/skills/deploy-nos-receiver/nos.py speed    # 고속 매핑(읽기 전용)
    PYTHONIOENCODING=utf-8 python .claude/skills/deploy-nos-receiver/nos.py deploy --yes   # 백업·교체·3개 서비스 재시작
    ... nos.py guard-check | guard-status                     # 장애물 감지 노드(읽기 전용)
    ... nos.py guard-deploy --yes | guard-install --yes | guard-remove --yes   # 감지 노드만(중계기 재시작 없음)
    ... nos.py guard-live --yes | guard-record --yes     # 실제 정지 ↔ 기록만(OBSTACLE_RECORD_ONLY 0/1), 감지 노드만 재시작

컨테이너를 다시 만들면 /tmp 가 비므로 매번 복사부터 한다. Git Bash 경로 변환을 피하려고 subprocess 로 직접 부른다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy-control"))
from common import ROOT, db_query, sh  # noqa: E402

GUARD_WRITE = ("guard-deploy", "guard-install", "guard-remove", "guard-live", "guard-record")
MODES = ("check", "status", "speed", "deploy", "guard-check", "guard-status") + GUARD_WRITE
mode = sys.argv[1] if len(sys.argv) > 1 else ""
if mode not in MODES:
    sys.exit(f"사용법: nos.py {'|'.join(MODES)} [--yes]")

if mode in GUARD_WRITE:
    if "--yes" not in sys.argv:
        sys.exit("❌ 사용자 승인 뒤 --yes 를 붙여 실행한다(guard-check 결과를 먼저 보일 것)")
    for name in ("obstacle_core.py", "obstacle_guard.py"):
        code, out = sh([sys.executable, "-m", "py_compile", str(ROOT / "robot_receiver" / name)])
        if code != 0:
            sys.exit(f"❌ 로컬 {name} 문법 오류 — 배포 중단\n{out}")
    code, out = sh([sys.executable, "-m", "json.tool", str(ROOT / "robot_receiver" / "obstacle_ignore.json")])
    if code != 0:
        sys.exit(f"❌ 로컬 obstacle_ignore.json 형식 오류 — 배포 중단\n{out}")

if mode == "deploy":
    sessions = db_query("SELECT COUNT(*) FROM service_session WHERE EndedAt IS NULL")
    runs = db_query("SELECT COUNT(*) FROM schedule_run WHERE Result='running'")
    print(f"진행 중 안내 세션 {sessions} · 작업 회차 {runs}")
    if sessions != "0" or runs != "0":
        sys.exit("❌ 진행 중 작업이 있어 배포 보류 — 끝난 뒤 다시")
    if "--yes" not in sys.argv:
        sys.exit("❌ 사용자 승인 뒤 --yes 를 붙여 실행한다(check 결과를 먼저 보일 것)")
    # nos_receiver.py deploy 는 교체 뒤 문법 검사가 실패해도 재시작하므로 올리기 전에 막는다
    for name in ("receiver.py", "relay_map.py"):
        code, out = sh([sys.executable, "-m", "py_compile", str(ROOT / "robot_receiver" / name)])
        if code != 0:
            sys.exit(f"❌ 로컬 {name} 문법 오류 — 배포 중단\n{out}")

for src, dst in (("robot_receiver/tools/nos_receiver.py", "/tmp/nos_receiver.py"),
                 ("robot_receiver/receiver.py", "/tmp/receiver_new.py"),
                 ("robot_receiver/relay_map.py", "/tmp/relay_map_new.py"),
                 ("robot_receiver/obstacle_core.py", "/tmp/obstacle_core_new.py"),
                 ("robot_receiver/obstacle_guard.py", "/tmp/obstacle_guard_new.py"),
                 ("robot_receiver/obstacle_ignore.json", "/tmp/obstacle_ignore_new.json")):
    code, out = sh(["docker", "cp", str(ROOT / src), f"control-backend:{dst}"])
    if code != 0:
        sys.exit(f"❌ 복사 실패({src}): {out.strip()}")

code, out = sh(["docker", "exec", "-e", "PYTHONIOENCODING=utf-8", "control-backend",
                "python", "/tmp/nos_receiver.py", mode], timeout=180)
print(out)
if code != 0:
    print("❌ 실패 — NOS(10.21.31.106) 접속이 안 되면 노트북이 로봇 망에 붙어 있는지부터 본다.")
sys.exit(code)
