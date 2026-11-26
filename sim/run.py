"""사전 시뮬레이션 실행기 — 같은 시나리오를 현행·변경 Backend 에서 돌려 비교표를 낸다.

  python sim/run.py SCENARIO.py                          # 작업 폴더 Backend 만
  python sim/run.py SCENARIO.py --cand DIR               # 현행(작업 폴더) vs 변경(DIR)
  python sim/run.py SCENARIO.py --base git:HEAD --cand DIR
  python sim/run.py snapshot DIR                         # 변경안 작업용 Backend/app 사본 만들기

DIR 은 app/ 을 담은 폴더다. 결과 표(마크다운)는 화면에, 전체 결과는 sim/out/*.json 에 남는다.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SIM_DIR)
OUT_DIR = os.path.join(SIM_DIR, "out")
MARK = "@@SIMRESULT "
METRICS = ("switches", "rejects", "races", "charge_mode0", "dropped", "errors")
METRIC_KO = {"switches": "보행전환", "rejects": "NAV거부", "races": "경합",
             "charge_mode0": "충전중Mode0", "dropped": "복귀버림", "errors": "오류"}


# ── Backend 준비 ──
def snapshot(dest: str) -> str:
    """작업 폴더 Backend/app 을 dest/app 으로 복사한다(변경안은 여기서 고친다)."""
    src = os.path.join(ROOT, "Backend", "app")
    dst = os.path.join(dest, "app")
    if os.path.exists(dst):
        raise SystemExit(f"이미 있습니다: {dst}")
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*SKIP_PATTERNS))
    return dest


# 네이티브 라이브러리(arm DLL)는 import 때 읽으므로 빼지 않는다.
# 경로가 길면 Windows 260자 한도에 걸리니 사본은 sim/out 아래에 둔다.
SKIP_PATTERNS = ("__pycache__", "*.pyc")


def _skipped(rel: str) -> bool:
    import fnmatch
    return any(fnmatch.fnmatch(part, pat) for part in rel.split("/") for pat in SKIP_PATTERNS)


def from_git(ref: str) -> str:
    """git REF 의 Backend/app 을 sim/out/base_<ref> 로 풀어 둔다."""
    safe = ref.replace("/", "_").replace(":", "_")
    dest = os.path.join(OUT_DIR, f"base_{safe}")
    if os.path.isdir(os.path.join(dest, "app")):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)
    data = subprocess.run(["git", "-C", ROOT, "archive", "--format=tar", ref, "Backend/app"],
                          check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            rel = m.name.split("/", 1)[1] if "/" in m.name else ""   # Backend/ 떼기
            if not rel or _skipped(rel):
                continue
            m.name = rel
            tar.extract(m, dest, filter="data")
    return dest


def resolve(spec: str | None) -> str:
    if not spec:
        return os.path.join(ROOT, "Backend")
    if spec.startswith("git:"):
        return from_git(spec[4:])
    return os.path.abspath(spec)


# ── 실행 ──
def count_scenarios(path: str) -> list[str]:
    code = ("import sys; sys.path.insert(0, r'%s'); from harness.worker import load_scenarios; "
            "import json; print(json.dumps([s.name for s in load_scenarios(r'%s')], ensure_ascii=False))"
            % (SIM_DIR, path))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         encoding="utf-8", env=_env(None))
    if out.returncode:
        raise SystemExit(f"시나리오 파일을 읽지 못했습니다:\n{out.stderr}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def _env(backend: str | None) -> dict:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    if backend:
        env["SIM_BACKEND"] = backend
    return env


def run_one(path: str, idx: int, backend: str) -> dict:
    p = subprocess.run([sys.executable, "-m", "harness.worker", path, str(idx)], cwd=SIM_DIR,
                       capture_output=True, text=True, encoding="utf-8", env=_env(backend),
                       timeout=600)
    for line in reversed(p.stdout.splitlines()):
        if line.startswith(MARK):
            return json.loads(line[len(MARK):])
    return {"ok": False, "why": "워커 비정상 종료", "crash": (p.stderr or p.stdout)[-3000:]}


# ── 보고 ──
def _cell(r: dict) -> str:
    if r.get("crash"):
        return "💥 예외"
    mark = {True: "✅", False: "❌", None: "·"}[r.get("ok")]
    bits = [f"{r.get('sim_sec', '?')}s", f"{r.get('gait')}/{r.get('mode')}"]
    bits += [f"{METRIC_KO[m]} {r[m]}" for m in METRICS if r.get(m)]
    why = f" — {r['why']}" if r.get("why") else ""
    return f"{mark} " + ", ".join(bits) + why


def report(names: list[str], variants: list[str], rows: dict) -> str:
    lines = ["| # | 시나리오 | " + " | ".join(variants) + (" | 차이 |" if len(variants) > 1 else " |"),
             "|---|---|" + "---|" * len(variants) + ("---|" if len(variants) > 1 else "")]
    for i, n in enumerate(names):
        cells = [_cell(rows[(v, i)]) for v in variants]
        line = f"| {i + 1} | {n} | " + " | ".join(cells)
        if len(variants) > 1:
            a, b = rows[(variants[0], i)], rows[(variants[1], i)]
            diff = [METRIC_KO[m] for m in METRICS if a.get(m) != b.get(m)]
            if a.get("cmds") != b.get("cmds"):
                diff.append("명령열")
            if a.get("ok") != b.get("ok"):
                diff.append("판정")
            line += " | " + (", ".join(diff) or "같음")
        lines.append(line + " |")
    total = {v: sum(1 for i in range(len(names)) if rows[(v, i)].get("ok") is True) for v in variants}
    judged = sum(1 for i in range(len(names)) if rows[(variants[-1], i)].get("ok") is not None)
    lines.append("")
    lines.append("통과: " + ", ".join(f"{v} {total[v]}/{judged}" for v in variants))
    lines.append("")
    lines.append("명령열(마지막 변형 기준, 다른 것만 현행 병기):")
    for i, n in enumerate(names):
        last = rows[(variants[-1], i)]
        lines.append(f"- {i + 1}. {' → '.join(last.get('cmds', [])) or '(없음)'}")
        if len(variants) > 1 and rows[(variants[0], i)].get("cmds") != last.get("cmds"):
            lines.append(f"  - {variants[0]}: {' → '.join(rows[(variants[0], i)].get('cmds', []))}")
    crashes = [(v, i) for (v, i), r in rows.items() if r.get("crash")]
    for v, i in crashes:
        lines.append(f"\n💥 {v} / {names[i]}\n```\n{rows[(v, i)]['crash'].strip()[-1500:]}\n```")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "snapshot":
        if len(sys.argv) != 3:
            raise SystemExit("사용: python sim/run.py snapshot DIR")
        print(snapshot(os.path.abspath(sys.argv[2])))
        return
    ap = argparse.ArgumentParser(description="사전 시뮬레이션 실행기")
    ap.add_argument("scenario")
    ap.add_argument("--base", help="현행 Backend 폴더 또는 git:REF (기본: 작업 폴더 Backend)")
    ap.add_argument("--cand", help="변경안 Backend 폴더(app/ 포함)")
    ap.add_argument("--only", help="이 문자열이 들어간 시나리오만")
    ap.add_argument("-j", type=int, default=4, help="동시 실행 수")
    a = ap.parse_args()

    path = os.path.abspath(a.scenario)
    names = count_scenarios(path)
    picks = [i for i, n in enumerate(names) if not a.only or a.only in n]
    variants = {"현행": resolve(a.base)}
    if a.cand:
        variants["변경"] = resolve(a.cand)
    if len(variants) == 1:
        variants = {"현재": variants["현행"]}

    jobs = [(v, i) for v in variants for i in picks]
    with ThreadPoolExecutor(max_workers=max(1, a.j)) as ex:
        res = list(ex.map(lambda vi: run_one(path, vi[1], variants[vi[0]]), jobs))
    rows = {}
    for (v, i), r in zip(jobs, res):
        rows[(v, picks.index(i))] = r
    sel = [names[i] for i in picks]

    md = report(sel, list(variants), rows)
    print(md)
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(OUT_DIR, f"{os.path.splitext(os.path.basename(path))[0]}_{stamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"variants": variants, "scenarios": sel,
                   "results": {f"{v}#{i}": r for (v, i), r in rows.items()}},
                  f, ensure_ascii=False, indent=1)
    print(f"\n전체 결과: {out}")
    failed = any(r.get("ok") is False for (v, _), r in rows.items() if v == list(variants)[-1])
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
