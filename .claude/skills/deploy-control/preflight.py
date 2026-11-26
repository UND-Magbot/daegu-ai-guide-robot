"""관제 배포 사전 점검(읽기 전용). 아무것도 바꾸지 않는다.

    PYTHONIOENCODING=utf-8 python .claude/skills/deploy-control/preflight.py

1) 컨테이너 상태 2) 진행 중 안내 세션·작업 회차 3) 이번 배포로 바뀔 백엔드 파일(컨테이너 대 작업 폴더)
4) 화면 이미지 이후 바뀐 화면 파일 5) 환경 변수 전달 대조 6) 실행할 명령(백업 이름표·배포·되돌리기)
"""
import datetime as dt
import subprocess
import sys
from pathlib import Path

from common import ROOT, backend_code_diff, container_state, db_query, print_list, sh

blockers: list[str] = []

print("■ 1. 컨테이너")
for name, status in container_state().items():
    print(f"  {name:18} {status}")
    if name in ("control-backend", "control-db") and not status.startswith("Up"):
        blockers.append(f"{name} 가 떠 있지 않음")

print("\n■ 2. 진행 중 작업 (0건이어야 배포)")
sessions = db_query("SELECT COUNT(*) FROM service_session WHERE EndedAt IS NULL")
runs = db_query("SELECT COUNT(*) FROM schedule_run WHERE Result='running'")
print(f"  안내 세션(끝나지 않음): {sessions}")
print(f"  작업 회차(running): {runs}")
for label, val in (("안내 세션", sessions), ("작업 회차", runs)):
    if val != "0":
        blockers.append(f"진행 중 {label} {val}")
if sessions != "0":
    print("  " + db_query("SELECT id, RobotName, StartedAt FROM service_session "
                          "WHERE EndedAt IS NULL ORDER BY id DESC LIMIT 5").replace("\n", "\n  "))

print("\n■ 3. 이번 배포로 바뀔 백엔드 파일 (실행 중 컨테이너 대 작업 폴더)")
diff = backend_code_diff()
if diff is None:
    print("  (컨테이너 코드를 읽지 못함)")
else:
    changed, added, removed = diff
    print_list("바뀜", changed)
    print_list("새 파일", added)
    print_list("없어짐", removed)
    if not (changed or added or removed):
        print("  → 백엔드 코드는 지금 실행본과 같다. 화면만 배포하면 backend 는 빼도 된다.")
    print("  ※ 작업 폴더 전체가 들어간다 — 다른 세션의 커밋 안 된 작업이 섞였는지 사용자에게 목록으로 확인받는다.")

print("\n■ 4. 화면 이미지 이후 바뀐 화면 파일")
code, created = sh(["docker", "inspect", "-f", "{{.Created}}", "und-control-frontend:latest"])
if code == 0:
    built = dt.datetime.fromisoformat(created.strip()[:19]).replace(tzinfo=dt.timezone.utc).timestamp()
    newer = sorted(str(p.relative_to(ROOT / "Frontend")).replace("\\", "/")
                   for d in ("app", "public") for p in (ROOT / "Frontend" / d).rglob("*")
                   if p.is_file() and p.stat().st_mtime > built and "__tests__" not in p.parts)
    newer += [f for f in ("next.config.js", "package.json", "package-lock.json", "eslint.config.mjs")
              if (ROOT / "Frontend" / f).stat().st_mtime > built]
    print(f"  화면 이미지 생성: {created.strip()[:19]} (UTC)")
    print_list("이후 바뀐 파일", newer)
    if not newer:
        print("  → 화면은 바뀐 것이 없다. frontend 는 빼도 된다.")
else:
    print("  (화면 이미지 정보 없음)")

print("\n■ 5. 환경 변수 전달 대조")
env_check = ROOT / ".claude" / "skills" / "env-flag-add" / "check_env.py"
p = subprocess.run([sys.executable, str(env_check), "--running"], capture_output=True, cwd=ROOT,
                   env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
print("  " + p.stdout.decode("utf-8", "replace").strip().replace("\n", "\n  "))
if p.returncode != 0:
    blockers.append("compose 목록에 없는 .env 키가 있음")

print("\n■ 6. 실행할 명령 (승인 후)")
tag = "bak_" + dt.date.today().strftime("%Y%m%d")
_, images = sh(["docker", "images", "--format", "{{.Tag}}", "und-control-backend"])
n = 2
while tag in images.split():
    tag = f"bak_{dt.date.today():%Y%m%d}_{n}"
    n += 1
print(f"""  # 백업 이름표
  docker tag und-control-backend:latest und-control-backend:{tag}
  docker tag und-control-frontend:latest und-control-frontend:{tag}
  # 배포 (바뀐 쪽만 남겨도 된다)
  docker compose -f docker-compose.local.yml -p und-control up -d --build --no-deps backend frontend
  # 되돌리기 (--build 없이)
  docker tag und-control-backend:{tag} und-control-backend:latest
  docker tag und-control-frontend:{tag} und-control-frontend:latest
  docker compose -f docker-compose.local.yml -p und-control up -d --no-deps backend frontend""")

print("\n■ 판정")
if blockers:
    print("  ❌ 배포 보류: " + " / ".join(blockers))
    sys.exit(1)
print("  ✅ 배포 가능 — 3번·4번 목록을 사용자에게 보이고 승인을 받는다.")
