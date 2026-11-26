"""간선 옆 출발·도착은 간선 끝도 후보로(2026-10-29) — 가까운 노드가 진행 반대쪽이면 물러났다 돌아온다.

  sim\\run.bat sim\\scenarios\\node_edge_start.py --cand sim\\out\\cand_edgestart
실기(10-29 16:42, 맵49 3F): 종합민원실(5.88,-6.18) → 대기석(6.31,-9.30). 종합민원실은 간선 N377(6.13,-5.46)–N362(6.34,-9.38)
옆(0.29m)인데 가장 가까운 노드 N377(0.76m, 대기석 반대쪽)에 붙어 출발 제자리 회전 152° → N377 → 돌아서 대기석.
N377 을 건너뛰지 못한 건 출발 자리가 벽(창구)에서 0.40m 라 지름길 검사(벽 여유 0.45m)에 걸리고, N377 이 출발→목표
직선 띠 밖(뒤)이라 _simplify 도 못 줄였기 때문(실제 지도 재계산 docs/분석보고/2026-10-29/_자료/map49_*.py).
변경: 붙은 노드가 0.3m 넘게 떨어져 있고 간선 옆이면 그 간선 끝도 출발·도착 후보로 짜 보고, 최종 경로가 짧을 때만 쓴다.
무대: 3F 좌표 그대로 그래프만 만들고, 출발 자리 옆 지름길은 벽 여유 부족으로 막는다(실제 지도 결과와 같게).
"""
import math

from harness import scenario

START, GOAL = (5.884, -6.183), (6.307, -9.296)
NODES = {361: (5.95, -2.69), 362: (6.34, -9.38), 377: (6.13, -5.46), 363: (1.88, -1.69)}
EDGES = [(361, 377, False), (377, 362, False), (361, 363, False), (377, 363, True)]   # (a, b, 한쪽 방향)


def _graph(nr):
    g = nr.Graph(map_id=49)
    g.nodes = dict(NODES)
    for a, b, oneway in EDGES:
        g.adj.setdefault(a, []).append(b)
        if oneway:
            g.oneway.add((a, b))
        else:
            g.adj.setdefault(b, []).append(a)
    return g


def _plan(sim, start, goal):
    import app.navigation.node_route as nr
    g = _graph(nr)
    nr.load_graph = lambda db, map_id: g
    nr._seg_clear = lambda mid, a, b: True
    # 종합민원실 자리에서 시작·끝나는 직선은 벽 여유 부족(실제 지도 0.40m < 0.45m), 나머지는 통과
    near = lambda p: math.dist(p, START) < 0.05
    nr._shortcut_clear = lambda mid, a, b, **_: not (near(a) or near(b))
    p = nr.plan_leg(None, 49, start, {"x": goal[0], "y": goal[1], "name": "목표"})
    pts = [start] + [(w["x"], w["y"]) for w in p.waypoints] + [goal]
    back = 0.0     # 목표 반대쪽으로 간 거리 — 첫 구간이 목표 방향과 반대(내적 < 0)면 그 길이
    if len(pts) > 2:
        d0 = (goal[0] - start[0], goal[1] - start[1])
        d1 = (pts[1][0] - start[0], pts[1][1] - start[1])
        if d0[0] * d1[0] + d0[1] * d1[1] < 0:
            back = math.hypot(*d1)
    return {"nodes": [w["name"] for w in p.waypoints], "len_m": round(sum(math.dist(a, b) for a, b in zip(pts, pts[1:])), 2),
            "back_m": round(back, 2), "err": p.error}


@scenario("E1 [재현] 3F 종합민원실 → 대기석 — 뒤쪽 N377 로 물러나지 않고 N362 쪽으로",
          expect=lambda r: (r["back_m"] == 0 and r["len_m"] <= 3.4 and not r["err"]) or f"{r}")
def e1(sim):
    return _plan(sim, START, GOAL)


@scenario("E2 [재현] 3F 대기석 → 종합민원실 — 종합민원실을 지나 N377 까지 갔다 돌아오지 않음",
          expect=lambda r: (r["len_m"] <= 3.4 and "N377" not in r["nodes"] and not r["err"]) or f"{r}")
def e2(sim):
    return _plan(sim, GOAL, START)


@scenario("E3 [회귀] 안내창구(N361 위) → 대기석 — 노드 위 장소는 그 노드 그대로, N377 거쳐 내려감",
          expect=lambda r: (r["back_m"] == 0 and not r["err"]) or f"{r}")
def e3(sim):
    return _plan(sim, (5.935, -2.678), GOAL)


# ── 겹친 간선 저장 거부(10-29) — node_route.py 병원 무대의 저장 API ──
import importlib.util as _ilu
import os as _os

from harness import REGISTRY as _REG

_n0 = len(_REG)
_sp = _ilu.spec_from_file_location("node_route_base_nes", _os.path.join(_os.path.dirname(__file__), "node_route.py"))
_nrb = _ilu.module_from_spec(_sp)
_sp.loader.exec_module(_nrb)
del _REG[_n0:]


def _save_with(sim, extra):
    st = _nrb.Stage(sim)
    if not st.on:
        return {"code": None, "msg": "노드망 꺼짐"}
    nn, db, user, save, validate = _nrb._api(st)
    try:
        out, code, msg = save(extra)
    finally:
        db.close()
    return {"code": code, "msg": msg[:160]}


@scenario("O1 [변경] 간선 N3–N5 가 연결 안 된 N4 위를 지나감 → 저장 거부(409, 어느 간선인지 알림)", env=_nrb.ON,
          expect=lambda r: (r.get("code") == 409 and "N04" in r.get("msg", "") and "지나갑니다" in r.get("msg", "")) or f"{r}")
def o1(sim):
    from app.map import nav_nodes as nn
    # 복도 y=3.1 의 N3(2.65)–N5(5.85) 를 직접 이으면 가운데 N4(4.55) 위를 지난다
    return _save_with(sim, [nn.EdgeIn(a="3", b="5", kind="manual", direction="both")])


@scenario("O2 [회귀] 겹치지 않는 수동 간선(N9–N10 같은 줄 아님) → 저장 그대로 200", env=_nrb.ON,
          expect=lambda r: r.get("code") == 200 or f"{r}")
def o2(sim):
    from app.map import nav_nodes as nn
    return _save_with(sim, [nn.EdgeIn(a="9", b="2", kind="manual", direction="both")])
