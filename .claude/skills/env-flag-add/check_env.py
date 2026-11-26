"""관제 백엔드 환경 변수가 .env → compose → 컨테이너로 제대로 이어지는지 대조한다.

    python .claude/skills/env-flag-add/check_env.py            # 전체 대조
    python .claude/skills/env-flag-add/check_env.py NAV_STALL_SEC   # 이 키만
    python .claude/skills/env-flag-add/check_env.py --running  # 실행 중 control-backend 의 값까지(값은 가림)

.env 값이 compose backend environment 목록에 없으면 컨테이너로 전달되지 않는다(10-06~10-22 8회 이상 재발).
비밀값은 출력하지 않는다. 종료 코드: 전달 안 되는 키가 있으면 1.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COMPOSES = ["docker-compose.local.yml", "docker-compose.windows.yml", "docker-compose.linux.yml"]
# 코드가 읽지만 운영자가 .env 로 정하는 값이 아닌 것
IGNORE = {"PATH", "OPENCV_FFMPEG_CAPTURE_OPTIONS", "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD"}
# .env 에만 쓰고 backend 로 넘기지 않는 것(다른 서비스·compose 자체용)
ENV_ONLY = {"COMPOSE_FILE", "DB_ROOT_PASSWORD", "DB_NAME", "DB_HOST_PORT", "BACKEND_PORT",
            "FRONTEND_PORT", "NEXT_PUBLIC_MEDIAMTX_HOST", "OPEN_AI_SECRET_KEY"}
# 컨테이너·실행기가 쓰는 값(앱 코드가 직접 안 읽어도 정상)
RUNTIME = {"PORT", "TZ"}
CODE_RE = re.compile(
    r"""(?:os\.getenv|os\.environ\.get|_env[a-z_]*|_seed_password)\(\s*["']([A-Z0-9_]+)["']"""
    r"""|os\.environ\[["']([A-Z0-9_]+)["']\]"""
)


def env_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\s*([A-Z0-9_]+)\s*=", line)
        if m:
            keys.add(m.group(1))
    return keys


def code_keys() -> set[str]:
    keys = set()
    for py in (ROOT / "Backend" / "app").rglob("*.py"):
        for a, b in CODE_RE.findall(py.read_text(encoding="utf-8", errors="replace")):
            keys.add(a or b)
    return keys - IGNORE


def compose_backend_keys(path: Path) -> set[str]:
    """backend 서비스의 environment 목록 키만 뽑는다(들여쓰기 기준, yaml 라이브러리 없이)."""
    keys, in_backend, in_env = set(), False, False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.match(r"^  [a-z][\w-]*:\s*$", line):
            in_backend, in_env = line.strip() == "backend:", False
            continue
        if not in_backend:
            continue
        if re.match(r"^    environment:\s*$", line):
            in_env = True
            continue
        if re.match(r"^    \S", line):
            in_env = False
        m = in_env and re.match(r"^\s+-\s*([A-Z0-9_]+)=", line)
        if m:
            keys.add(m.group(1))
    return keys


def running_keys() -> set[str] | None:
    try:
        # 값에 한글이 있으면(예: AUTO_INIT_POSE_PARK_PLACES) 윈도우 기본 cp949 해석이 실패한다
        out = subprocess.run(["docker", "exec", "control-backend", "env"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {ln.split("=", 1)[0] for ln in out.splitlines() if "=" in ln} or None


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only = set(args)
    dotenv, example, code = env_keys(ROOT / ".env"), env_keys(ROOT / ".env.example"), code_keys()
    compose = {c: compose_backend_keys(ROOT / c) for c in COMPOSES}
    running = running_keys() if "--running" in sys.argv else None
    keys = only or (dotenv | code | set().union(*compose.values())) - ENV_ONLY - IGNORE
    # 로봇 로그 수집(robotlog 서비스) 설정은 backend 로 가지 않는다 — compose robotlog 목록에 있다(11-05)
    keys = {k for k in keys if not k.startswith("ROBOTLOG_")}

    problems = 0
    print(f"{'키':34} {'.env':5} {'예시':5} {'코드':5} " + " ".join(f"{c.split('.')[1]:7}" for c in COMPOSES)
          + (" 실행중" if running is not None else ""))
    for k in sorted(keys):
        in_compose = [k in compose[c] for c in COMPOSES]
        row = [k in dotenv, k in example, k in code, *in_compose]
        notes = []
        if k in dotenv and not all(in_compose):
            notes.append("❌ .env 에 있는데 compose 목록에 없음 → 컨테이너로 안 감")
            problems += 1
        elif any(in_compose) and not all(in_compose):
            notes.append("⚠ compose 3개가 서로 다름")
        if k in dotenv and k not in example:
            notes.append("⚠ .env.example 에 없음")
        if any(in_compose) and k not in code and k not in RUNTIME:
            notes.append("· 코드가 안 읽음(남은 설정?)")
        if running is not None and k in dotenv and all(in_compose) and k not in running:
            notes.append("⚠ 컨테이너에 없음 → --force-recreate 필요")
        if only or notes:
            marks = " ".join(f"{'O' if v else '-':5}" for v in row[:3]) + " " + " ".join(
                f"{'O' if v else '-':7}" for v in row[3:])
            run_mark = (" " + ("O" if k in (running or set()) else "-")) if running is not None else ""
            print(f"{k:34} {marks}{run_mark}  {' / '.join(notes)}")
    print(f"\n전달 안 되는 키: {problems}개" + ("" if problems else " — 정상"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
