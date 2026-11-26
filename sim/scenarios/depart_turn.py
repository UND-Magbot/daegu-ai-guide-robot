"""출발 제자리 회전(2026-10-28) — 진행 방향과 크게 다른 쪽을 보고 서 있다가 목표 하나로 바로 보내면
걸으면서 돌아 호를 그리며 출발한다.

실측(10-28 11:40 3F 맵49): 안내창구(방향 0°, 동쪽 창구)에서 남쪽 3.5m 종합민원실로 '노드 없이 바로' 단일 NAV.
사용자: "회전반경에서 제대로 회전 안 한 상태로 출발".
변경: send_move._depart_turn — 서 있다 출발하고 첫 진행 방향과 지금 방향이 NAV_DEPART_TURN_DEG(45°) 이상 다르면
다지점 묶음 첫 점으로 '지금 위치·진행 방향' 점을 넣어 제자리에서 먼저 돈다.

가짜 로봇은 점에 닿을 때 방향을 맞춘다(경유점 감속·회전 2.5s, 10-20 실기) — 걷는 중의 회전은 흉내 내지 않는다.
지표 depart_off: 로봇이 처음 움직이기 시작할 때 몸 방향과 이동 방향의 차이(도). 실제 로봇에서는 이 차이를 걸으면서 돈다.

무대: node_edge_attach 와 같은 복도(N3–N6 수동 간선, 간선 옆 장소 '복도 가운데'·'복도 가운데2').
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "node_edge_attach_base_dt", os.path.join(os.path.dirname(__file__), "node_edge_attach.py"))
ea = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ea)
del REGISTRY[_n:]

ON = {**ea.ON, "NAV_MULTIPOINT_ENABLED": "true"}
OFF = {**ON, "NAV_DEPART_TURN_DEG": "0"}
NORTH, EAST, WEST = math.pi / 2, 0.0, math.pi


def _watch(sim):
    """처음 움직이는 순간의 몸 방향 − 이동 방향(도)."""
    robot = sim.robot
    w = {"last": (robot.x, robot.y), "off": None}

    def tick(dt):
        p = (robot.x, robot.y)
        q = w["last"]
        if w["off"] is None and math.dist(p, q) > 1e-4:
            head = math.atan2(p[1] - q[1], p[0] - q[0])
            w["off"] = round(abs(math.degrees((head - robot.yaw + math.pi) % (2 * math.pi) - math.pi)))
        w["last"] = p
    sim.clock.add_ticker(tick)
    return w


def run(sim, start, yaw, name, direction="both"):
    st = ea.stage(sim, direction)
    sim.robot.place(*start, yaw)
    st.samples.append(start)
    w = _watch(sim)
    r = ea.go(sim, st, name)
    r.update(depart_off=w["off"], multi=sim.robot.multi_sent, turn="출발 회전" in r["targets"],
             first=r["targets"][:1])
    return r


def ok(r, cond):
    return (r["arrived"] and r["wall_cross"] == 0 and r["errors"] == 0 and cond) or \
        f"도착={r['arrived']} 출발 어긋남={r['depart_off']}° 회전점={r['turn']} 목표={r['targets']} 묶음={r['multi']}"


@scenario("D1 [재현] 북쪽 보고 선 채 동쪽 간선 옆 장소로(같은 간선, 노드 없이 바로) → 먼저 돌고 출발", env=ON,
          expect=lambda r: ok(r, r["turn"] and r["depart_off"] <= 10))
def d1(sim):
    return run(sim, ea.MID2, NORTH, "복도 가운데")


@scenario("D2 [회귀] 진행 방향(동쪽)을 보고 섰으면 → 회전 점 없음, 지금과 같은 명령", env=ON,
          expect=lambda r: ok(r, not r["turn"] and r["depart_off"] <= 10))
def d2(sim):
    return run(sim, ea.MID2, 0.3, "복도 가운데")


@scenario("D3 반대(서쪽)를 보고 섰다가 노드 거쳐 원무과 접수 → 첫 점 회전, 묶음 4점 이하로 끝까지", env=ON,
          expect=lambda r: ok(r, r["turn"] and r["depart_off"] <= 10 and max(r["multi"] or [0]) <= 4))
def d3(sim):
    return run(sim, ea.MID, WEST, "원무과 접수")


@scenario("D4 [경계] 44° 어긋남 → 회전 점 없음(문턱 45°)", env=ON,
          expect=lambda r: ok(r, not r["turn"]))
def d4(sim):
    return run(sim, ea.MID2, math.radians(44 + math.degrees(math.atan2(0.3, 1.2))), "복도 가운데")


@scenario("D5 [끔] NAV_DEPART_TURN_DEG=0 → 지금과 같음(회전 점 없음)", env=OFF,
          expect=lambda r: (r["arrived"] and not r["turn"]) or f"도착={r['arrived']} 회전점={r['turn']}")
def d5(sim):
    return run(sim, ea.MID2, NORTH, "복도 가운데")


@scenario("D6 [다지점 끔] NAV_MULTIPOINT_ENABLED=false → 회전 점 넣지 않음(단일 NAV 는 지금 위치 점을 바로 지나침)",
          env={**ON, "NAV_MULTIPOINT_ENABLED": "false"},
          expect=lambda r: (r["arrived"] and not r["turn"]) or f"도착={r['arrived']} 회전점={r['turn']}")
def d6(sim):
    return run(sim, ea.MID2, NORTH, "복도 가운데")


@scenario("D7 [방향 간선] 한쪽 간선에서 못 가는 경우 → 회전도 하지 않고 움직이지 않음", env=ON,
          expect=lambda r: (not r["arrived"] and r["arrivals"] == 0 and r["cmds"] == [])
          or f"도착={r['arrived']} 명령={r['cmds']}")
def d7(sim):
    return run(sim, (8.65, 3.1), NORTH, "복도 가운데", direction="forward")


# ── 충전소 근처 출발 회전 생략(2026-10-28 13:52 맵47 실측) ──
# 출도킹 직후 도킹 점(로봇충전소-1)에서 92° 제자리 회전을 시작하자 0.2s 뒤 측위가 흔들려 상실(1.5s), 회전을 못 끝내고
# 본체 '목표 시간 초과'. 출도킹 구역은 측위가 약한 곳(13:14·10-13 상실) — 그 안에서는 걸으면서 도는 예전 출발로 둔다.
# 무대 충전소 (1.4,5.45)·충전소-1 (0.8,5.6)·충전소-2 (1.4,5.0). 남쪽(아래)으로 나가는 길이라 북쪽을 보고 서 있으면 돌아야 한다.
# 맵47 처럼 도킹 점이 하나(충전소-1)뿐이면 출도킹 통로 앞부분(outbound_prefix)이 붙지 않아 회전 점이 들어간다 —
# 무대의 충전소-2 자리(노드망에 붙는 점)를 충전소-1 로 쓰고 충전소-2 를 뺀다.
DOCK1 = (1.4, 5.0)
NEAR_DOCK_OUT = (2.8, 5.0)     # 도킹 점·충전소에서 1.4m 넘게 떨어짐 — 생략 범위 밖


def run_one_dock(sim, start, yaw, name):
    places = {k: v for k, v in ea.MY_PLACES.items() if k != "충전소-2"}
    places["충전소-1"] = (*DOCK1, "waypoint")
    st = ea.Stage(sim, nodes=ea.MY_NODES, places=places)
    from app.database.database import SessionLocal
    from app.database import models as m
    db = SessionLocal()
    try:
        db.add(m.NavEdge(MapId=ea.MAP, NodeA=3, NodeB=6, Kind="manual", Direction="both"))
        db.commit()
    finally:
        db.close()
    sim.robot.place(*start, yaw)
    st.samples.append(start)
    w = _watch(sim)
    r = ea.go(sim, st, name)
    r.update(depart_off=w["off"], multi=sim.robot.multi_sent, turn="출발 회전" in r["targets"],
             first=r["targets"][:1])
    return r


@scenario("D8 [재현] 출도킹 직후 도킹 점(충전소-1)에서 북쪽 보고 출발 → 회전 점 넣지 않음(걸으면서 돌기)", env=ON,
          expect=lambda r: ok(r, not r["turn"]))
def d8(sim):
    return run_one_dock(sim, DOCK1, NORTH, "진료실 1")


@scenario("D9 [경계] 도킹 점·충전소에서 1.0m 밖(2.8,5.0)이면 → 지금처럼 먼저 돌고 출발", env=ON,
          expect=lambda r: ok(r, r["turn"] and r["depart_off"] <= 10))
def d9(sim):
    return run_one_dock(sim, NEAR_DOCK_OUT, NORTH, "진료실 1")
