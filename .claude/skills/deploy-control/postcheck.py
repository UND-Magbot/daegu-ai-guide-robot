"""관제 배포 사후 확인(읽기 전용).

    PYTHONIOENCODING=utf-8 python .claude/skills/deploy-control/postcheck.py [--since 10m]

정상 기동 대기 → 응답 코드(8010 /api 200, /robot/status 로그인 없이 401, 3001 / 200)
→ 기동 로그 오류 → 컨테이너 코드가 작업 폴더와 같은지.
"""
import sys
import time
import urllib.error
import urllib.request

from common import backend_code_diff, print_list, sh

since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else "10m"
fails: list[str] = []

print("■ 1. 정상 기동 대기 (최대 180초)")
state = ""
for _ in range(36):
    _, state = sh(["docker", "inspect", "-f", "{{.State.Health.Status}}", "control-backend"])
    state = state.strip()
    if state in ("healthy", "unhealthy"):
        break
    time.sleep(5)
print(f"  control-backend: {state}")
if state != "healthy":
    fails.append(f"backend 상태 {state}")
_, fe = sh(["docker", "inspect", "-f", "{{.State.Status}}", "control-frontend"])
print(f"  control-frontend: {fe.strip()}")


def code_of(url: str) -> int:
    try:
        return urllib.request.urlopen(url, timeout=8).status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0


print("\n■ 2. 응답 코드")
for url, want in (("http://localhost:8010/api", 200), ("http://localhost:8010/robot/status", 401),
                  ("http://localhost:3001/", 200)):
    got = code_of(url)
    print(f"  {url:38} {got} (기대 {want}) {'✅' if got == want else '❌'}")
    if got != want:
        fails.append(f"{url} → {got}")

print(f"\n■ 3. 기동 로그 오류 (최근 {since})")
_, logs = sh(["docker", "logs", "--since", since, "control-backend"])
hits = [ln for ln in logs.splitlines() if any(k in ln for k in ("Traceback", "ERROR", "Error:"))]
mig = [ln for ln in logs.splitlines() if "마이그레이션" in ln]
print_list("오류 줄", hits, 15)
print_list("마이그레이션 줄", mig, 10)
if hits:
    fails.append(f"로그 오류 {len(hits)}줄")

print("\n■ 4. 컨테이너 코드 = 작업 폴더?")
diff = backend_code_diff()
if diff is None:
    print("  (읽지 못함)")
    fails.append("컨테이너 코드 대조 실패")
else:
    left = sum(map(len, diff))
    print("  ✅ 같음" if not left else f"  ❌ 다른 파일 {left}개 — 빌드가 반영되지 않았거나 배포 뒤 작업 폴더가 바뀜")
    for title, items in zip(("바뀜", "새 파일", "없어짐"), diff):
        if items:
            print_list(title, items, 10)
    if left:
        fails.append("컨테이너 코드 불일치")

print("\n■ 판정")
if fails:
    print("  ❌ " + " / ".join(fails) + " — 되돌리기 여부를 사용자에게 묻는다.")
    sys.exit(1)
print("  ✅ 배포 확인 완료")
