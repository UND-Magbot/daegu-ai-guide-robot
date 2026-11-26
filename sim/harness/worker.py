"""시나리오 한 건을 새 프로세스에서 돈다(Backend 모듈 전역 상태가 시나리오 사이에 새지 않게).

사용: python -m harness.worker <시나리오 파일> <시나리오 번호>
마지막 줄에 '@@SIMRESULT <json>' 을 찍는다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import traceback

from . import REGISTRY
from .backend import Sim, boot
from .robot import RobotParams

MARK = "@@SIMRESULT "


def load_scenarios(path: str):
    spec = importlib.util.spec_from_file_location("sim_scenarios", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(REGISTRY)


def judge(sc, result: dict) -> tuple[bool | None, str]:
    if result.get("crash"):
        return False, "시나리오 실행 중 예외"
    if sc.expect is None:
        return None, ""
    try:
        v = sc.expect(result)
    except Exception as e:
        return False, f"expect 오류: {e}"
    if isinstance(v, str):
        return (v == ""), v
    return bool(v), ""


def main() -> None:
    path, idx = sys.argv[1], int(sys.argv[2])
    scs = load_scenarios(path)
    sc = scs[idx]
    os.environ.update({k: str(v) for k, v in sc.env.items()})
    boot()
    result: dict = {}
    sim = None
    try:
        sim = Sim(RobotParams(**sc.params), limit_sec=sc.limit_sec)
        extra = sc.fn(sim)
        sim.flush()
        result = sim.summary()
        if isinstance(extra, dict):
            result.update(extra)
    except BaseException:
        result = sim.summary() if sim else {}
        result["crash"] = traceback.format_exc(limit=8)
    ok, why = judge(sc, result)
    result["ok"], result["why"] = ok, why
    result["log_tail"] = (sim.log()[-3000:] if sim else "")
    sys.stdout.write("\n" + MARK + json.dumps(result, ensure_ascii=False, default=str) + "\n")


if __name__ == "__main__":
    main()
