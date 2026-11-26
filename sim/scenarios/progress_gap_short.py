"""진행 멘트 여유 기준 5초 → 3.5초(10-30 B) — 관공서 3F 처럼 짧은 구간에서 진행 멘트가 들어가는가(관찰용 + 판정).

무대는 progress_cue.py(병원, 가짜 로봇 0.5 m/s, 보통 명령 — 안내 시점은 보낸 속도 하한·잰 속도로 계산).
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("progress_cue_base_gap", os.path.join(os.path.dirname(__file__), "progress_cue.py"))
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)
del REGISTRY[_n:]


def _straight(sim, length):
    st = pc._stage(sim, progress=pc.P, robot_at=(4.0 - length, 0.0))
    return pc._guide_move(st, sim)


for L in (6, 7, 8, 10, 12):
    def mk(L):
        @scenario(f"[직선 {L}m] 진행 멘트가 들어가는가 · 안내끼리 3초 넘게 떨어지는가(관찰)",
                  expect=lambda r: r["errors"] == 0 and (r.get("min_gap") is None or r["min_gap"] >= 3.0))
        def f(sim):
            return _straight(sim, L)
        return f
    globals()[f"s{L}"] = mk(L)


@scenario("[관공서 로비 닮음] 4m 직진 → 왼쪽 꺾고 9m → 도착 · 진행 멘트(관찰)",
          expect=lambda r: r["errors"] == 0 and (r.get("min_gap") is None or r["min_gap"] >= 3.0))
def lobby_like(sim):
    st = pc._stage(sim, vias=[(-9.0, 0.0)], progress=pc.P, robot_at=(-13.0, 0.0))
    return pc._guide_move(st, sim)
