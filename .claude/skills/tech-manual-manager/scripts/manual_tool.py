"""tech-manual-manager 보조 도구.

매뉴얼 저장소(MANUAL_ROOT) 경로를 찾고 점검하며, 매뉴얼 파일이 공통 양식(_TEMPLATE.md)의
머리표·장 제목을 지키는지 검사한다. 양식 내용은 복사하지 않고 매번 _TEMPLATE.md 에서 읽는다.

사용법 (프로젝트 루트에서):
    python .claude/skills/tech-manual-manager/scripts/manual_tool.py resolve
    python .claude/skills/tech-manual-manager/scripts/manual_tool.py set-root <경로>
    python .claude/skills/tech-manual-manager/scripts/manual_tool.py inventory
    python .claude/skills/tech-manual-manager/scripts/manual_tool.py search <단어> [<단어> ...]
    python .claude/skills/tech-manual-manager/scripts/manual_tool.py check <매뉴얼 파일> [...]
    python .claude/skills/tech-manual-manager/scripts/manual_tool.py init-log
    python .claude/skills/tech-manual-manager/scripts/manual_tool.py source-info

종료 코드: 0 정상, 1 점검 실패, 2 경로 미설정·사용법 오류.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SKILL_DIR / "config.local.json"
ENV_KEY = "M20_MANUAL_ROOT"

REQUIRED_FILES = ("README.md", "_TEMPLATE.md")
CATEGORY_DIRS = (
    "01_Setup",
    "02_Robot_Control",
    "03_Control_Platform",
    "04_Service_Functions",
    "05_Troubleshooting",
    "06_Test_Validation",
)
MANAGEMENT_FILES = {"README.md", "_TEMPLATE.md", "MANUAL_LOG.md", "MANUAL_BACKLOG.md"}
EXAMPLE_PREFIX = "(예시)"
SKIP_DIRS ={".git", ".obsidian", ".bkit", ".claude", "assets", ".trash"}

LOG_HEADER = """# 매뉴얼 변경 기록 (MANUAL_LOG)

> 매뉴얼 생성·의미 있는 갱신만 누적한다. 오탈자 수정은 기록하지 않는다.
> 실제 파일에 반영한 작업만 적는다. 기존 줄은 지우거나 고치지 않는다(정정은 새 줄로).
> 검증 상태는 `_TEMPLATE.md`·`README.md` 의 정의(초안 / 검증 중 / 검증 완료 / 개정 필요)를 따른다.

| 날짜 | 문서 경로 | 작업 유형 | 변경 내용 | 검증 상태 |
|---|---|---|---|---|
"""


def _print(obj: object) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def _configured_root() -> tuple[str | None, str]:
    """설정된 MANUAL_ROOT 와 그 출처를 돌려준다. 환경 변수가 설정 파일보다 우선한다."""
    env_value = os.environ.get(ENV_KEY)
    if env_value:
        return env_value, f"환경 변수 {ENV_KEY}"
    value = _load_config().get("manual_root")
    if value:
        return value, str(CONFIG_PATH.name)
    return None, "미설정"


def _write_test(root: Path) -> str | None:
    probe = root / f".tmm_write_test_{uuid.uuid4().hex[:8]}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return None
    except OSError as exc:
        return str(exc)


def inspect_root(root: Path) -> dict:
    """저장소 필수 파일·분류 폴더·쓰기 가능 여부를 점검한다."""
    result: dict = {"manual_root": str(root), "exists": root.is_dir(), "problems": []}
    if not root.is_dir():
        result["problems"].append("폴더가 없음")
        result["ok"] = False
        return result
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        result["problems"].append(f"필수 파일 없음: {', '.join(missing)}")
    result["category_dirs"] = {d: (root / d).is_dir() for d in CATEGORY_DIRS}
    absent_dirs = [d for d, ok in result["category_dirs"].items() if not ok]
    if absent_dirs:
        result["problems"].append(f"분류 폴더 없음(임의 생성 금지): {', '.join(absent_dirs)}")
    result["manual_log_exists"] = (root / "MANUAL_LOG.md").is_file()
    write_error = _write_test(root)
    result["writable"] = write_error is None
    if write_error:
        result["problems"].append(f"쓰기 불가: {write_error}")
    result["ok"] = not result["problems"]
    return result


def _require_root() -> Path:
    value, source = _configured_root()
    if not value:
        _print({"ok": False, "error": "MANUAL_ROOT 미설정", "hint": "set-root <경로> 로 지정"})
        sys.exit(2)
    root = Path(value)
    if not root.is_dir():
        _print({"ok": False, "error": f"MANUAL_ROOT 폴더 없음: {value}", "source": source})
        sys.exit(2)
    return root


def _iter_manuals(root: Path):
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if len(rel.parts) == 1 and rel.name in MANAGEMENT_FILES:
            continue
        # (예시) 파일은 작성 참고용이라 매뉴얼 목록·검색에서 뺀다
        if rel.name.startswith(EXAMPLE_PREFIX):
            continue
        yield path, rel


def _meta_value(text: str, key: str) -> str | None:
    match = re.search(rf"^\|\s*{re.escape(key)}\s*\|\s*(.*?)\s*\|\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def _title(text: str) -> str:
    match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def cmd_resolve() -> int:
    value, source = _configured_root()
    if not value:
        _print({"ok": False, "source": source, "error": "MANUAL_ROOT 미설정",
                "hint": "실제 경로를 확인한 뒤 set-root <경로> 실행"})
        return 2
    result = inspect_root(Path(value))
    result["source"] = source
    _print(result)
    return 0 if result["ok"] else 1


def cmd_set_root(path_text: str) -> int:
    root = Path(path_text).expanduser().resolve()
    result = inspect_root(root)
    if not result["exists"] or not (root / "_TEMPLATE.md").is_file():
        result["saved"] = False
        result["error"] = "실제 매뉴얼 저장소가 아님(폴더 또는 _TEMPLATE.md 없음). 저장하지 않음"
        _print(result)
        return 1
    config = _load_config()
    config["manual_root"] = str(root)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["saved"] = True
    result["config"] = str(CONFIG_PATH)
    _print(result)
    return 0


def cmd_inventory() -> int:
    root = _require_root()
    items = []
    for path, rel in _iter_manuals(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        top = rel.parts[0] if len(rel.parts) > 1 else "(루트·미분류)"
        items.append({
            "path": rel.as_posix(),
            "category": top,
            "title": _title(text),
            "status": _meta_value(text, "문서 상태"),
            "last_verified": _meta_value(text, "최종 검증일"),
            "last_modified": _meta_value(text, "최종 수정일"),
        })
    _print({"manual_root": str(root), "count": len(items), "manuals": items})
    return 0


def cmd_search(terms: list[str]) -> int:
    """제목·본문에서 단어를 찾아 많이 맞는 순으로 보여 준다(대소문자 무시)."""
    root = _require_root()
    lowered = [t.lower() for t in terms]
    hits = []
    for path, rel in _iter_manuals(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        low = text.lower()
        counts = {t: low.count(t) for t in lowered}
        score = sum(1 for c in counts.values() if c) * 100 + sum(counts.values())
        if not score:
            continue
        scope = re.search(r"\*\*적용 범위:\*\*\s*(.+)", text)
        hits.append({
            "path": rel.as_posix(),
            "title": _title(text),
            "score": score,
            "matched": {t: c for t, c in counts.items() if c},
            "scope": scope.group(1).strip()[:200] if scope else None,
        })
    hits.sort(key=lambda h: -h["score"])
    _print({"terms": terms, "hits": hits[:20]})
    return 0


def _template_structure(root: Path) -> tuple[list[str], list[str]]:
    """_TEMPLATE.md 에서 머리표 항목 이름과 '## ' 장 제목을 읽는다."""
    text = (root / "_TEMPLATE.md").read_text(encoding="utf-8")
    head = text.split("\n## ", 1)[0]
    keys = re.findall(r"^\|\s*([^|]+?)\s*\|", head, re.MULTILINE)
    keys = [k for k in keys if k != "항목" and not set(k) <= {"-", ":"}]
    sections = re.findall(r"^##\s+(\d+\.\s*.+?)\s*$", text, re.MULTILINE)
    return keys, sections


def _section_number(heading: str) -> str:
    return heading.split(".", 1)[0].strip()


def cmd_check(files: list[str]) -> int:
    root = _require_root()
    keys, sections = _template_structure(root)
    statuses = ("초안", "검증 중", "검증 완료", "개정 필요")
    reports = []
    for file_text in files:
        path = Path(file_text)
        if not path.is_absolute():
            path = root / path
        problems: list[str] = []
        if not path.is_file():
            reports.append({"file": file_text, "ok": False, "problems": ["파일 없음"]})
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for key in keys:
            if _meta_value(text, key) is None:
                problems.append(f"머리표 항목 없음: {key}")
        if _title(text).startswith("["):
            problems.append("제목이 양식 자리표시 그대로임")
        status = _meta_value(text, "문서 상태") or ""
        if " / " in status:
            problems.append(f"문서 상태가 하나로 정해지지 않음: {status}")
        elif status and not any(status.startswith(s) for s in statuses):
            problems.append(f"문서 상태 값이 정의 밖: {status}")
        found = re.findall(r"^##\s+(\d+\.\s*.+?)\s*$", text, re.MULTILINE)
        found_numbers = [_section_number(h) for h in found]
        for heading in sections:
            if _section_number(heading) not in found_numbers:
                problems.append(f"장 없음: {heading}")
        expected_order = [_section_number(h) for h in sections]
        present = [n for n in found_numbers if n in expected_order]
        if present != sorted(present, key=expected_order.index):
            problems.append("장 순서가 양식과 다름")
        if re.search(r"(sk-[A-Za-z0-9_\-]{16,}|sk-ant-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,})", text):
            problems.append("비밀값으로 보이는 문자열 포함")
        placeholders = re.findall(r"\[(?:예:|선택:)[^\]]*\]|YYYY-MM-DD", text)
        reports.append({
            "file": path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path),
            "ok": not problems,
            "problems": problems,
            "template_placeholders_left": len(placeholders),
        })
    _print({"template_keys": keys, "template_sections": sections, "reports": reports})
    return 0 if all(r["ok"] for r in reports) else 1


def cmd_init_log() -> int:
    root = _require_root()
    log_path = root / "MANUAL_LOG.md"
    if log_path.exists():
        _print({"created": False, "path": str(log_path), "reason": "이미 있음(보존)"})
        return 0
    log_path.write_text(LOG_HEADER, encoding="utf-8", newline="\n")
    _print({"created": True, "path": str(log_path)})
    return 0


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True,
                             encoding="utf-8", check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def cmd_source_info() -> int:
    """매뉴얼 원본 참조에 적을 프로젝트 저장소 정보(사용자 정보를 뺀 URL·커밋)."""
    remote = _git("remote", "get-url", "origin")
    remote = re.sub(r"//[^/@]+@", "//", remote)
    top = _git("rev-parse", "--show-toplevel")
    _print({
        "repo_name": Path(top).name if top else None,
        "repo_url": remote or None,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or None,
        "head": _git("rev-parse", "--short", "HEAD") or None,
        "dirty_files": len([l for l in _git("status", "--porcelain").splitlines() if l]),
    })
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, args = argv[0], argv[1:]
    if command == "resolve":
        return cmd_resolve()
    if command == "set-root" and len(args) == 1:
        return cmd_set_root(args[0])
    if command == "inventory":
        return cmd_inventory()
    if command == "search" and args:
        return cmd_search(args)
    if command == "check" and args:
        return cmd_check(args)
    if command == "init-log":
        return cmd_init_log()
    if command == "source-info":
        return cmd_source_info()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
