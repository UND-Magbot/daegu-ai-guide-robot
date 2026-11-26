"""직선 모드 꺾인 뒤·출발 구간을 앞부분만 저속 (2026-10-29 15시, 사용자: "꺾을 때·회전할 때만 저속, 이후 설정 속도").

10-29 14:05~15:12 실기(669, NAV_TURN_SLOW_DEG=30): 맵47 노드망은 꺾임이 거의 다 70~95° 라 꺾인 뒤 구간이 통째로
저속(0.50) — 목표점 19/21 이 최고 0.50, 엘리베이터 26m 79초. 본체는 점마다 서서 돈 뒤 0 → 그 구간 최고 속도로 뛴다
(NOS 계획기 14:11:44: 21° 점에서도 0.25 까지 감속 → 제자리 회전 0.8s → 0 → 1.20).

변경:
- NAV_SOFT_START_MAX_M 4 → 3
- NAV_TURN_SLOW_M(기본 0=끔): 저속으로 갈 구간(꺾인 뒤, 또는 3m 넘는 첫 구간)이 NAV_TURN_SLOW_M + 1m 보다 길면
  앞 NAV_TURN_SLOW_M 에 통과 점 '저속 끝'을 끼운다. 그 점에서 꺾임 0 → 뒤 구간은 설정 속도(_send_bundle 판정 그대로).
- 문 간선 양끝 사이, 저속 시나리오, 자율 모드에는 끼우지 않는다.

가짜 로봇은 속도를 흉내 내지 않는다 — 송신 함수를 감싸 점마다 나간 (이름, 속도)를 기록한다.
무대: node_edge_attach 의 병원 복도(N3–N6 5.2m 수동 간선).
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "node_edge_attach_base_ts", os.path.join(os.path.dirname(__file__), "node_edge_attach.py"))
ea = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ea)
del REGISTRY[_n:]

BASE = {**ea.ON, "NAV_MULTIPOINT_ENABLED": "true", "NODE_NAV_MODE": "straight", "NAV_SOFT_START": "true",
        "NAV_TURN_SLOW_DEG": "30"}
SPLIT = {**BASE, "NAV_TURN_SLOW_M": "1.5"}
EAST, WEST, NORTH = 0.0, math.pi, math.pi / 2


def run(sim, start, yaw, name, speed=None):
    st = ea.stage(sim)
    sim.robot.place(*start, yaw)
    st.samples.append(start)
    sent = []
    orig = sim.sender.send_nav_multi_to_robot

    def send(points, map_id=None, continuing=False):
        sent.append([(round(p["x"], 2), round(p["y"], 2), p.get("speed") or "normal") for p in points])
        return orig(points, map_id=map_id, continuing=continuing)
    sim.sender.send_nav_multi_to_robot = send
    orig1 = sim.sender.send_nav_to_robot

    def send1(*a, **k):
        sent.append([("single", k.get("speed") or "normal")])
        return orig1(*a, **k)
    sim.sender.send_nav_to_robot = send1
    sm = sim.sm
    from app.database.database import SessionLocal
    out = {}

    def go():
        db = SessionLocal()
        try:
            out.update(sm.start_place_move(st.place_ids[name], db, speed=speed))
        finally:
            db.close()
        if out.get("status") == "ok":
            sim.pl.nav_thread()
    sim.run(go, until=lambda: out and (out.get("status") != "ok" or not sm.is_navigating))
    x, y, _ = ea.MY_PLACES[name]
    arrived = abs(sim.robot.x - x) < 0.05 and abs(sim.robot.y - y) < 0.05
    wps = [(w["name"], w.get("sent_speed") or w.get("speed") or "normal") for w in sm.waypoints_list]
    splits = sum(1 for w in sm.waypoints_list if w.get("slow_split"))
    r = st.result(arrived=arrived, msg=out.get("msg"))
    r.update(wps=wps, splits=splits, bundles=[len(b) for b in sent], sent=sent)
    return r


def _ok(r, splits, pattern=None):
    good = r["arrived"] and r["wall_cross"] == 0 and r["errors"] == 0 and r["splits"] == splits \
        and max(r["bundles"] or [0]) <= 4
    if pattern is not None:
        good = good and [s for _, s in r["wps"]] == pattern
    return good or f"도착={r['arrived']} 끼운점={r['splits']} 점·속도={r['wps']} 묶음={r['bundles']} 오류={r['errors']}"


S, N, F = "slow", "normal", "fast"


# 진료실 1(1.0,1.1) → N09 1.2m → N02(38° 꺾임) 1.05m → N06(90°) 6.1m → N11(90°) 1.05m → 원무과 접수(42°) 1.3m
LONG = ((1.0, 1.1), NORTH, "원무과 접수")


@scenario("T1 [재현] 지금(30°) — 꺾인 뒤 6.1m 직선이 통째로 저속", env=BASE,
          expect=lambda r: _ok(r, 0) if dict(r["wps"]).get("N06") == S else f"N06={dict(r['wps']).get('N06')} {r['wps']}")
def t1(sim):
    return run(sim, *LONG)


@scenario("T2 [변경] 끼움 1.5m — 6.1m 직선은 앞 1.5m 만 저속, 나머지 보통", env=SPLIT,
          expect=lambda r: _ok(r, 1, [S, S, S, N, S, S]))
def t2(sim):
    return run(sim, *LONG)


@scenario("T3 [변경] 서 있다 출발, 첫 구간 3.85m(>3m) — 앞 1.5m 저속 뒤 보통", env=SPLIT,
          expect=lambda r: _ok(r, 1) and r["wps"][0] == ("저속 끝", S) and dict(r["wps"]).get("N06") == N
          or f"점·속도={r['wps']}")
def t3(sim):
    return run(sim, ea.MID2, EAST, "원무과 접수")


@scenario("T4 [경계] 끼움 없이 3m 한도만 — 첫 구간 3.85m 는 통째로 보통(4m 한도였으면 저속)", env=BASE,
          expect=lambda r: _ok(r, 0) and dict(r["wps"]).get("N06") == N or f"점·속도={r['wps']}")
def t4(sim):
    return run(sim, ea.MID2, EAST, "원무과 접수")


@scenario("T5 첫 구간 1.85m(≤3m) — 통째로 저속, 끼움 없음", env=SPLIT,
          expect=lambda r: _ok(r, 0) and r["wps"][0] == ("N06", S) or f"점·속도={r['wps']}")
def t5(sim):
    return run(sim, (6.0, 3.0), EAST, "원무과 접수")


@scenario("T6 고속 시나리오 — 끼운 점 뒤는 고속", env=SPLIT,
          expect=lambda r: _ok(r, 1) and dict(r["wps"]).get("N06") == F or f"점·속도={r['wps']}")
def t6(sim):
    return run(sim, *LONG, speed="fast")


@scenario("T7 저속 시나리오 — 끼움 없음, 모두 저속", env=SPLIT,
          expect=lambda r: _ok(r, 0) and all(s == S for _, s in r["wps"]) or f"점·속도={r['wps']}")
def t7(sim):
    return run(sim, *LONG, speed="slow")


@scenario("T8 자율 모드 — 끼움 없음(꺾임 저속도 안 붙음)", env={**SPLIT, "NODE_NAV_MODE": "auto"},
          expect=lambda r: _ok(r, 0))
def t8(sim):
    return run(sim, *LONG)


@scenario("T9 꺾임 저속 끔(NAV_TURN_SLOW_DEG=0) — 끼움 없음", env={**SPLIT, "NAV_TURN_SLOW_DEG": "0"},
          expect=lambda r: _ok(r, 0))
def t9(sim):
    return run(sim, *LONG)
