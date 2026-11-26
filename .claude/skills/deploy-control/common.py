"""관제 배포 점검 스크립트 공용 도구(읽기 전용)."""
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ["docker", "compose", "-f", "docker-compose.local.yml", "-p", "und-control"]
CONTAINERS = ["control-backend", "control-frontend", "control-db", "control-mediamtx"]


def sh(args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, cwd=ROOT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", errors="replace")


def dotenv() -> dict[str, str]:
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*([A-Z0-9_]+)\s*=(.*)", line)
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


def db_query(sql: str) -> str:
    env = dotenv()
    code, out = sh(["docker", "exec", "-e", f"MYSQL_PWD={env.get('DB_ROOT_PASSWORD', '1234')}",
                    "control-db", "mariadb", "-uroot", "-N", "-B",
                    env.get("DB_NAME", "control_system_dev"), "-e", sql])
    return out.strip() if code == 0 else f"(조회 실패: {out.strip()[:120]})"


def container_state() -> dict[str, str]:
    code, out = sh(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"])
    rows = dict(ln.split("\t", 1) for ln in out.splitlines() if "\t" in ln)
    return {c: rows.get(c, "없음") for c in CONTAINERS}


_CONTAINER_HASH = r"""
import hashlib, json, os
out = {}
for d, _, fs in os.walk('/app/app'):
    for f in fs:
        if f.endswith('.py'):
            p = os.path.join(d, f)
            out[os.path.relpath(p, '/app/app').replace(os.sep, '/')] = hashlib.md5(open(p, 'rb').read()).hexdigest()
print(json.dumps(out))
"""


def backend_code_diff() -> tuple[list[str], list[str], list[str]] | None:
    """(바뀐, 새로 생김, 없어짐) — 작업 폴더 Backend/app 대 실행 중 컨테이너 /app/app 의 .py."""
    code, out = sh(["docker", "exec", "control-backend", "python", "-c", _CONTAINER_HASH])
    if code != 0:
        return None
    try:
        remote = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    base = ROOT / "Backend" / "app"
    local = {p.relative_to(base).as_posix(): hashlib.md5(p.read_bytes()).hexdigest()
             for p in base.rglob("*.py") if "__pycache__" not in p.parts}
    changed = sorted(k for k in local if k in remote and local[k] != remote[k])
    added = sorted(k for k in local if k not in remote)
    removed = sorted(k for k in remote if k not in local)
    return changed, added, removed


def print_list(title: str, items: list[str], limit: int = 30) -> None:
    print(f"  {title}: {len(items)}개")
    for x in items[:limit]:
        print(f"    - {x}")
    if len(items) > limit:
        print(f"    … 외 {len(items) - limit}개")
