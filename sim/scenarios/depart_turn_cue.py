"""출발 제자리 회전 직전 방향 안내(11-02 메모) — 현행 vs 변경.

현행: 출발 멘트("…천천히 출발하겠습니다") 뒤 아무 말 없이 제자리에서 돈다(11-02 16:43 세션 536, 86°).
변경: 이동 전 확인(move/check)이 회전 방향 안내 문장(departTurn)을 돌려주고 엔진이 회전 직전에 말한다.
     계산은 실제 출발(_expand_node_leg)과 같아야 한다 — 안내했는데 안 돌거나, 돌았는데 안내가 없으면 안 된다.
무대: depart_turn.py 와 같은 복도(node_edge_attach). 미리 계산한 문장과 실제로 넣은 '출발 회전' 점을 비교한다.
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "depart_turn_base_cue", os.path.join(os.path.dirname(__file__), "depart_turn.py"))
dt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dt)
del REGISTRY[_n:]
ea = dt.ea
ON, OFF = dt.ON, dt.OFF
NORTH, WEST = dt.NORTH, dt.WEST


def run(sim, start, yaw, name):
    st = ea.stage(sim, "both")
    sim.robot.place(*start, yaw)
    st.samples.append(start)
    from app.database.database import SessionLocal
    from app.database.models import LocationInfo
    from app.navigation import send_move
    from app.robot_io import runtime
    db = SessionLocal()
    try:
        place = db.query(LocationInfo).filter(LocationInfo.id == st.place_ids[name]).first()
        rid = sim.robot_id if hasattr(sim, "robot_id") else None
        pos = runtime.get_position(rid) if rid else None
        if not pos:
            pos = {"x": start[0], "y": start[1], "yaw": yaw, "timestamp": 1.0}
        fn = getattr(send_move, "depart_turn_preview", None)
        cue = fn(db, pos, place) if fn else None
    finally:
        db.close()
    r = ea.go(sim, st, name)
    turn = next((w for w in sim.sm.waypoints_list if w.get("name") == "출발 회전"), None)
    r.update(cue=cue, turn="출발 회전" in r["targets"], turn_signed=turn and turn.get("turn_signed"))
    return r


def same(r, want):
    good = r["arrived"] and r["errors"] == 0 and r["cue"] == want and (r["cue"] is None) == (not r["turn"])
    return good or f"안내={r['cue']!r} 회전점={r['turn']}({r['turn_signed']}) 도착={r['arrived']}"


@scenario("C1 [재현] 북쪽 보고 서서 동쪽 장소로 → 오른쪽으로 돈다 → '오른쪽으로 방향을 바꾸겠습니다.'", env=ON,
          expect=lambda r: same(r, "오른쪽으로 방향을 바꾸겠습니다."))
def c1(sim):
    return run(sim, ea.MID2, NORTH, "복도 가운데")


@scenario("C2 [재현] 서쪽(반대) 보고 서서 원무과 접수 → 뒤로 돈다 → '뒤로 방향을 바꾸겠습니다.'", env=ON,
          expect=lambda r: same(r, "뒤로 방향을 바꾸겠습니다."))
def c2(sim):
    return run(sim, ea.MID, WEST, "원무과 접수")


@scenario("C3 남쪽 보고 서서 동쪽 장소로 → 왼쪽으로 돈다 → '왼쪽으로 방향을 바꾸겠습니다.'", env=ON,
          expect=lambda r: same(r, "왼쪽으로 방향을 바꾸겠습니다."))
def c3(sim):
    return run(sim, ea.MID2, -math.pi / 2, "복도 가운데")


@scenario("C4 [회귀] 진행 방향을 보고 섰으면 → 회전 점 없음 · 안내 없음", env=ON,
          expect=lambda r: same(r, None))
def c4(sim):
    return run(sim, ea.MID2, 0.3, "복도 가운데")


@scenario("C5 [회귀] 출발 회전을 끈 설정(NAV_DEPART_TURN_DEG=0) → 회전 점 없음 · 안내 없음", env=OFF,
          expect=lambda r: same(r, None))
def c5(sim):
    return run(sim, ea.MID2, NORTH, "복도 가운데")
