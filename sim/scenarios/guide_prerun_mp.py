"""안내 사전 주행 — 장소마다 따로 보내기(A) vs 한 바퀴 다지점 묶음(B) 비교.

  sim\run.bat sim\scenarios\guide_prerun_mp.py --base sim\out\cand_guide_prerun --cand sim\out\cand_guide_prerun_mp
무대·시나리오 함수는 guide_prerun.py 를 그대로 쓰고, 다지점(NAV_MULTIPOINT_ENABLED)을 켠 상태로 돌린다.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("guide_prerun_base",
                                               os.path.join(os.path.dirname(__file__), "guide_prerun.py"))
gp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gp)
base = {s.fn.__name__: s for s in REGISTRY[_n:]}
del REGISTRY[_n:]

MP = {"NAV_MULTIPOINT_ENABLED": "true"}
for key in ("full_lap", "loc_lost_recover", "loc_lost_timeout", "unreachable_leg", "operator_stop",
            "comm_lost", "guide_after_prerun"):
    s = base[key]
    scenario("[다지점 켬] " + s.name, expect=s.expect, env=MP, limit_sec=s.limit_sec)(s.fn)
