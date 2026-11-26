"""자율 모드 크게 꺾는 점에서 서서 돌기 (2026-10-29 15시).

10-29 11:20~11:30 실주행(669, 노드망 자율 모드): 다지점 통과 점에 들어오는 방향을 줘서 점에서 서지 않고 걸으며
크게 돌았다(NOS 계획기 (LP) (1.00, ±0.60, ±0.80)) — 사용자 "반경이 너무 크다". 직선 모드는 점마다 서서 돌지만
돈 직후 0 → 1.20 급출발(앞바퀴 들림). 자율 출발은 가속이 부드러웠다(14:05:49, 0.1초당 0.01, 1건).

변경: NODE_AUTO_TURN_STOP_DEG(기본 0=끔). 자율 모드 묶음에서 이 각도 이상 꺾는 통과 점은 다음 구간 방향을 준다
— "다음 구간 방향을 주면 점에서 서서 돌고 간다"(send_move 주석, 10-20 실기). 완만한 점·직선 모드·끝 점은 그대로.

가짜 로봇은 걸으며 도는 곡선을 흉내 내지 않는다 — 판정은 보낸 방향(들어오는/나가는)이다.
무대: node_edge_attach 병원 복도. 진료실 1 → N09(38° 꺾음) → N02(90°) → N06(90°) → N11(묶음 끝) → 원무과 접수.
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "node_edge_attach_base_at", os.path.join(os.path.dirname(__file__), "node_edge_attach.py"))
ea = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ea)
del REGISTRY[_n:]

AUTO = {**ea.ON, "NAV_MULTIPOINT_ENABLED": "true", "NODE_NAV_MODE": "auto", "NAV_SOFT_START": "true",
        "NAV_TURN_SLOW_DEG": "30"}
STOP = {**AUTO, "NODE_AUTO_TURN_STOP_DEG": "45"}
NORTH, EAST = math.pi / 2, 0.0


def _heading(a, b):
    return math.atan2(b[1] - a[1], b[0] - a[0])


def _same(a, b):
    return abs((a - b + math.pi) % (2 * math.pi) - math.pi) < 0.02


def run(sim, start, yaw, name):
    st = ea.stage(sim)
    sim.robot.place(*start, yaw)
    st.samples.append(start)
    sent = []
    orig = sim.sender.send_nav_multi_to_robot

    def send(points, map_id=None, continuing=False):
        sent.append([dict(p) for p in points])
        return orig(points, map_id=map_id, continuing=continuing)
    sim.sender.send_nav_multi_to_robot = send
    sm = sim.sm
    from app.database.database import SessionLocal
    out = {}

    def go():
        db = SessionLocal()
        try:
            out.update(sm.start_place_move(st.place_ids[name], db))
        finally:
            db.close()
        if out.get("status") == "ok":
            sim.pl.nav_thread()
    sim.run(go, until=lambda: out and (out.get("status") != "ok" or not sm.is_navigating))
    x, y, _ = ea.MY_PLACES[name]
    arrived = abs(sim.robot.x - x) < 0.05 and abs(sim.robot.y - y) < 0.05
    # 묶음마다 점 방향이 들어오는(in)·나가는(out)·그 밖(fix) 중 어느 쪽인지
    names = {(round(w["x"], 3), round(w["y"], 3)): w["name"] for w in sm.waypoints_list}
    kinds = []
    for b in sent:
        for i, p in enumerate(b):
            key = names.get((round(p["x"], 3), round(p["y"], 3)), "?")
            j = next((k for k, w in enumerate(sm.waypoints_list) if w["name"] == key), None)
            if i == len(b) - 1 or j is None or j + 1 >= len(sm.waypoints_list):
                kinds.append((key, "끝"))
                continue
            prv = (sm.waypoints_list[j - 1]["x"], sm.waypoints_list[j - 1]["y"]) if j > 0 else start
            nxt = sm.waypoints_list[j + 1]
            here = (p["x"], p["y"])
            if _same(p["yaw"], _heading(prv, here)):
                kinds.append((key, "in"))
            elif _same(p["yaw"], _heading(here, (nxt["x"], nxt["y"]))):
                kinds.append((key, "out"))
            else:
                kinds.append((key, "fix"))
    r = st.result(arrived=arrived, msg=out.get("msg"))
    r.update(kinds=kinds, modes=sorted({p.get("mode") for b in sent for p in b}),
             speeds=[p.get("speed") or "normal" for b in sent for p in b], bundles=[len(b) for b in sent])
    return r


def _ok(r, want):
    k = dict(r["kinds"])
    good = r["arrived"] and r["wall_cross"] == 0 and r["errors"] == 0 and (k.get("N09"), k.get("N02"), k.get("N06")) == want
    return good or f"벽={r['wall_cross']} 도착={r['arrived']} 방향={r['kinds']} 모드={r['modes']} 묶음={r['bundles']} 오류={r['errors']}"


LONG = ((1.0, 1.1), NORTH, "원무과 접수")


@scenario("A1 [재현] 자율·끔 — 90° 꺾는 N02·N06 도 들어오는 방향(걸으며 돎)", env=AUTO, expect=lambda r: _ok(r, ("in", "in", "in")))
def a1(sim):
    return run(sim, *LONG)


@scenario("A2 [변경] 자율·45° — N02·N06(90°) 나가는 방향(서서 돎), N09(38°) 그대로", env=STOP,
          expect=lambda r: _ok(r, ("in", "out", "out")))
def a2(sim):
    return run(sim, *LONG)


@scenario("A3 [경계] 자율·30° — 38° 꺾는 N09 도 서서 돎", env={**STOP, "NODE_AUTO_TURN_STOP_DEG": "30"},
          expect=lambda r: _ok(r, ("out", "out", "out")))
def a3(sim):
    return run(sim, *LONG)


@scenario("A4 [회귀] 직선 모드 — 설정 켜도 지금과 같음(들어오는 방향, 꺾인 뒤 저속)",
          env={**STOP, "NODE_NAV_MODE": "straight"},
          expect=lambda r: (_ok(r, ("in", "in", "in")) is True and r["modes"] == ["straight"] and "slow" in r["speeds"])
          or f"방향={r['kinds']} 모드={r['modes']} 속도={r['speeds']}")
def a4(sim):
    return run(sim, *LONG)


@scenario("A5 [11-03 변경] 자율·45° — 직선식 꺾인 뒤 저속은 없고, 서서 돈 점 뒤 3m 이하 구간만 저속(N11 1.05·접수 1.27m)",
          env=STOP, expect=lambda r: (r["modes"] == ["auto"] and r["speeds"] == ["slow", "normal", "normal", "slow", "slow"]) or
          f"모드={r['modes']} 속도={r['speeds']}")
def a5(sim):
    return run(sim, *LONG)


@scenario("A6 [10-30 변경] 자율·5° — 꺾는 점은 모두 서서 돎(N09 38°·N02·N06 90°)", env={**STOP, "NODE_AUTO_TURN_STOP_DEG": "5"},
          expect=lambda r: _ok(r, ("out", "out", "out")))
def a6(sim):
    return run(sim, *LONG)
