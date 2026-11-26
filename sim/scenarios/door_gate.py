"""자동문 통과 게이트(2026-11-06) — 현행 vs 변경.

변경:
- send_move: 문 간선 앞 노드에 멈춘 뒤, 다음 점이 같은 문의 반대편 노드면 door_gate.run_door_gate 로 문 앞에서 기다린다.
- door_fsm: DetectType=vision 인데 판독값이 없으면 열렸다고 가정하지 않는다(시간 초과 → 조작자 확인).
             문이 열려도 측위 상실·위치 확인 필요면 들어가지 않고 회복을 기다린다.
- door.py: '등록 완료'는 맵·문 앞 장소가 실제로 있고 개폐 방식이 자동 통과 지원일 때만. 사유(problem)를 돌려준다.

무대는 node_route.py 의 Stage(병원 1층 배치). 간호실(6.5,5.0) ↔ 약제실(9.0,5.2) 사이 문 간선 N15(6.95,5.0)–N16(7.45,5.0).
자동문 정보(DoorInfo)는 시나리오가 직접 넣는다. 가짜 로봇에는 측위 상태가 없어 get_localization_state 를 바꿔 재현한다.

현행에는 게이트가 없으므로 '[재현]' 시나리오는 현행 열이 실패해야 정상이다. '[회귀]' 는 둘 다 통과해야 한다.
"""
from __future__ import annotations

import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "node_route_base_door", os.path.join(os.path.dirname(__file__), "node_route.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
Stage, PLACES, MAP, FLOOR, ON, arrived_at = _base.Stage, _base.PLACES, _base.MAP, _base.FLOOR, _base.ON, _base.arrived_at

ENV = {**ON, "NAV_MULTIPOINT_ENABLED": "true"}
N15, N16 = (6.95, 5.0), (7.45, 5.0)
DOOR_PLACES = {**PLACES, "문 안쪽": (6.8, 5.0, "waypoint"), "문 바깥쪽": (7.6, 5.0, "waypoint")}


def _gate_mod():
    try:
        import app.navigation.door_gate as g
        return g
    except ImportError:
        return None


def _seed_door(st, control="auto_sensor", detect="none", open_timeout=10, door_id=1, map_id=MAP):
    from app.database.database import SessionLocal
    from app.database import models as m
    db = SessionLocal()
    try:
        db.query(m.DoorInfo).delete()        # Stage(door=True) 가 넣은 기본 자동문을 이 시나리오 설정으로 바꾼다
        db.add(m.DoorInfo(id=door_id, BusinessId=1, Name="시험문", FloorId=FLOOR, MapId=map_id,
                          NearLocationId=st.place_ids["문 안쪽"], FarLocationId=st.place_ids["문 바깥쪽"],
                          ControlType=control, DetectType=detect, OpenTimeoutSec=open_timeout, PassTimeoutSec=30))
        db.commit()
    finally:
        db.close()


class Probe:
    """N15 도착 시각·N16 NAV 시각·통과 세션 모습을 잰다."""

    def __init__(self, sim, st):
        self.sim, self.st = sim, st
        self.nav_at: dict[str, float] = {}
        self.session_seen: list[dict] = []
        robot, orig = sim.robot, sim.robot.nav

        def nav(x, y, yaw, continuing=False):
            self.nav_at.setdefault(st._name_of(x, y), sim.clock.elapsed())
            return orig(x, y, yaw, continuing)
        robot.nav = nav

        def tick(dt):
            try:
                from app.navigation import door_op
                fsm = door_op.get_session(10)
                if fsm is not None:
                    snap = fsm.snapshot()
                    if not self.session_seen or self.session_seen[-1]["phase"] != snap["phase"]:
                        self.session_seen.append(snap)
            except ImportError:
                pass
        sim.clock.add_ticker(tick)

    def arrive_t(self, xy):
        for t, x, y in self.sim.robot.arrivals:
            if math.dist((x, y), xy) < 0.05:
                return t
        return None

    def result(self, **extra):
        a15 = self.arrive_t(N15)
        n16 = self.nav_at.get("N16")
        gap = round(n16 - a15, 1) if a15 is not None and n16 is not None else None
        try:
            from app.navigation import door_op
            left = door_op.get_session(10) is not None
        except ImportError:
            left = None
        phases = [s["phase"] for s in self.session_seen]
        rev = [s["reverse"] for s in self.session_seen]
        return self.st.result(gap=gap, n16_sent=n16 is not None, phases=phases, reverse=rev,
                              session_left=left, navigating=self.sim.sm.is_navigating, **extra)


def ev(r, kind):
    return any(f" {kind}:" in e for e in r["events"])


def _setup(sim, **door):
    st = Stage(sim, places=DOOR_PLACES, door=True)
    if _gate_mod() is not None:
        sim.rewire_late_modules()
    _seed_door(st, **door)
    return st, Probe(sim, st)


# ── 정상 흐름 ──
@scenario("D1 [재현] 판독 없음(DetectType=none)·센서식: N15 에서 3초 열림 유예 뒤 N16 — 현행은 바로 N16", env=ENV,
          expect=lambda r: arrived_at(r, "약제실") and r["gap"] is not None and r["gap"] >= 3
          and "OPEN_WAIT" in r["phases"] and r["phases"][-1] == "PASS" and ev(r, "door_pass_go") and not r["session_left"])
def d1(sim):
    st, pr = _setup(sim)
    sim.robot.place(6.5, 5.0)
    st.go_place("약제실")
    return pr.result()


@scenario("D2 [재현] 카메라 확인(vision)인데 판정기 없음: 가정 진입 안 함 → 10초 뒤 조작자 확인 → 수동 확인으로 통과", env=ENV,
          expect=lambda r: arrived_at(r, "약제실") and r["gap"] is not None and r["gap"] >= 10
          and "FALLBACK" in r["phases"] and ev(r, "door_pass_fallback") and r["confirmed"])
def d2(sim):
    st, pr = _setup(sim, detect="vision", open_timeout=10)
    sim.robot.place(6.5, 5.0)
    out = {"confirmed": False}
    g = _gate_mod()

    def confirm():
        from app.navigation import door_op
        from app.navigation.door_fsm import Intent
        fsm = door_op.get_session(10)
        if fsm is not None:
            door_op._apply(fsm, Intent.CONFIRM_PASSED, 10)
            out["confirmed"] = True
    if g is not None:
        sim.at(25, confirm)
    st.go_place("약제실")
    return pr.result(**out)


@scenario("D3 [재현] 카메라가 '열림' 판독: 3초 유예 없이 통과(D1 보다 짧게, 5초 미만)", env=ENV,
          expect=lambda r: arrived_at(r, "약제실") and r["gap"] is not None and r["gap"] < 5
          and "OPEN_WAIT" in r["phases"] and r["phases"][-1] == "PASS" and ev(r, "door_pass_go"))
def d3(sim):
    st, pr = _setup(sim, detect="vision")
    g = _gate_mod()
    if g is not None:
        g.open_reader = lambda door_id, rid: True
    sim.robot.place(6.5, 5.0)
    st.go_place("약제실")
    return pr.result()


@scenario("D4 [재현] 문이 열렸는데 측위 상실(10-27 1F 실기 모습) 8초: 회복할 때까지 문 앞에서 기다림", env=ENV,
          expect=lambda r: arrived_at(r, "약제실") and r["gap"] is not None and r["gap"] >= 8
          and r["lost_until"] is not None and r["n16_after_recover"])
def d4(sim):
    st, pr = _setup(sim, detect="vision", open_timeout=30)
    g = _gate_mod()
    state = {"lost_until": None}
    if g is not None:
        g.open_reader = lambda door_id, rid: True

    real = sim.rt.get_localization_state

    def loc(rid):
        # N15 근처에서 문이 열리는 순간부터 8초 동안 상실
        if math.dist((sim.robot.x, sim.robot.y), N15) < 0.3:
            if state["lost_until"] is None:
                state["lost_until"] = sim.clock.elapsed() + 8
        if state["lost_until"] is not None and sim.clock.elapsed() < state["lost_until"]:
            return 1
        return 0
    sim.rt.get_localization_state = loc
    sim.robot.place(6.5, 5.0)
    st.go_place("약제실")
    sim.rt.get_localization_state = real
    n16 = pr.nav_at.get("N16")
    return pr.result(lost_until=state["lost_until"],
                     n16_after_recover=n16 is not None and state["lost_until"] is not None and n16 >= state["lost_until"])


@scenario("D5 [재현] 역방향(약제실→간호실): 같은 게이트, 통과 표시가 역방향", env=ENV,
          expect=lambda r: arrived_at(r, "간호실") and r["gap16"] is not None and r["gap16"] >= 3 and r["reverse"][:1] == [True])
def d5(sim):
    st, pr = _setup(sim)
    sim.robot.place(9.0, 5.2)
    st.go_place("간호실")
    a16 = pr.arrive_t(N16)
    n15 = pr.nav_at.get("N15")
    return pr.result(gap16=round(n15 - a16, 1) if a16 is not None and n15 is not None else None)


# ── 장애·경계 ──
@scenario("D6 [재현] 문 앞 대기 중 운영자 정지: N16 안 보냄, 통과 표시 정리, 주행 끝", env=ENV,
          expect=lambda r: not r["n16_sent"] and r["session_left"] is False and not r["navigating"]
          and "CANCEL_NAV" in r["cmds"])
def d6(sim):
    st, pr = _setup(sim, detect="vision", open_timeout=60)
    sim.robot.place(6.5, 5.0)
    if _gate_mod() is not None:
        sim.at(15, lambda: sim.sm.stop_navigation_internal("시험 정지"))
    st.go_place("약제실")
    sim.wait(5)
    return pr.result()


@scenario("D7 [재현] 개폐 방식 미지원(gpio): 문 앞에서 멈추고 주행 끝 — 벽 너머 노드를 보내지 않음", env=ENV,
          expect=lambda r: not r["n16_sent"] and not r["navigating"] and ev(r, "door_pass_abort"))
def d7(sim):
    st, pr = _setup(sim, control="gpio")
    sim.robot.place(6.5, 5.0)
    st.go_place("약제실")
    return pr.result()


@scenario("D8 [재현] 자동문 정보가 지워짐(문 간선만 남음): 문 앞에서 멈춤", env=ENV,
          expect=lambda r: not r["n16_sent"] and not r["navigating"] and ev(r, "door_pass_abort"))
def d8(sim):
    st, pr = _setup(sim)
    from app.database.database import SessionLocal
    from app.database import models as m
    db = SessionLocal()
    db.query(m.DoorInfo).delete()
    db.commit()
    db.close()
    sim.robot.place(6.5, 5.0)
    st.go_place("약제실")
    return pr.result()


@scenario("D9 [재현] 조작자 응답 없음: 판정기 없는 카메라 문 앞에서 10분 뒤 주행 끝(무한 대기 안 함)", env=ENV, limit_sec=900,
          expect=lambda r: not r["n16_sent"] and not r["navigating"] and ev(r, "door_pass_abort")
          and r["session_left"] is False)
def d9(sim):
    st, pr = _setup(sim, detect="vision", open_timeout=10)
    sim.robot.place(6.5, 5.0)
    st.go_place("약제실")
    return pr.result()


# ── 회귀 ──
@scenario("R1 [회귀] 문 없는 경로(로비→원무과)는 그대로: 벽 안 넘고 도착, 게이트 없음", env=ENV,
          expect=lambda r: arrived_at(r, "원무과 접수") and r["wall_cross"] == 0 and r["errors"] == 0
          and not ev(r, "door_pass_start") and r["phases"] == [])
def r1(sim):
    st, pr = _setup(sim)
    sim.robot.place(2.0, 5.0)
    st.go_place("원무과 접수")
    return pr.result()


@scenario("R2 [회귀] 문 양쪽 노드는 묶음에 안 넣고 하나씩(N15·N16 단일 NAV)", env=ENV,
          expect=lambda r: arrived_at(r, "약제실") and r["targets"][:2] == ["N15", "N16"]
          and r["cmds"][:2] == ["NAV", "NAV"] and r["wall_cross"] == 0)
def r2(sim):
    st, pr = _setup(sim)
    sim.robot.place(6.5, 5.0)
    st.go_place("약제실")
    return pr.result()


# ── 등록 완료 판정(door.py) ──
def _doors_api(sim, st):
    from app.database.database import SessionLocal
    from app.database import models as m
    from app.map import door as dm
    db = SessionLocal()
    try:
        db.add(m.RobotMapInfo(id=MAP, BusinessId=1, FloorId=FLOOR, MapName="SIM"))
        a, b = st.place_ids["문 안쪽"], st.place_ids["문 바깥쪽"]
        rows = [
            m.DoorInfo(id=11, BusinessId=1, Name="정상", MapId=MAP, NearLocationId=a, FarLocationId=b, ControlType="auto_sensor"),
            m.DoorInfo(id=12, BusinessId=1, Name="맵삭제", MapId=48, NearLocationId=a, FarLocationId=b, ControlType="auto_sensor"),
            m.DoorInfo(id=13, BusinessId=1, Name="장소삭제", MapId=MAP, NearLocationId=444, FarLocationId=446, ControlType="auto_sensor"),
            m.DoorInfo(id=14, BusinessId=1, Name="gpio", MapId=MAP, NearLocationId=a, FarLocationId=b, ControlType="gpio"),
            m.DoorInfo(id=15, BusinessId=1, Name="빈칸", MapId=MAP),
        ]
        for r in rows:
            r.DetectType, r.OpenTimeoutSec, r.PassTimeoutSec = "none", 20, 30
            db.add(r)
        db.commit()
        out = {d["name"]: d["complete"] for d in dm.list_doors(1, db=db, current_user=None)}
        fac = {f["name"]: f["complete"] for f in dm.list_facilities(1, db=db, current_user=None) if f["kind"] == "door"}
        prob = {d["name"]: d.get("problem") for d in dm.list_doors(1, db=db, current_user=None)}
    finally:
        db.close()
    return out, fac, prob


@scenario("C1 [재현] 등록 완료 판정: 맵·장소가 지워졌거나 미지원 개폐 방식이면 미완료 + 사유", env=ENV,
          expect=lambda r: r["complete"] == {"정상": True, "맵삭제": False, "장소삭제": False, "gpio": False, "빈칸": False}
          and r["facility"] == r["complete"] and all(r["problem"][k] for k in ("맵삭제", "장소삭제", "gpio", "빈칸"))
          and r["problem"]["정상"] is None)
def c1(sim):
    st = Stage(sim, places=DOOR_PLACES)
    out, fac, prob = _doors_api(sim, st)
    return {"complete": out, "facility": fac, "problem": prob}
