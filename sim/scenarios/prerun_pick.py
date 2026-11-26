"""1회 주행 테스트 — 갈 곳 고르기·갈래 표시(2026-10-27).

변경:
- voice_prerun.plan_route: 구간마다 branch(대답에 따라 가는 곳) 표시 — 응답이 없을 때 실제로 가는 기본 흐름
  (_main_flow: 묻는 단계는 OnTimeout 이 앞으로 가면 그쪽, 아니면 NextStep, 되묻기·되돌아가기는 따라가지 않음)에
  없는 이동 단계.
- POST /voice-prerun: legIndexes(고른 구간 번호)·legCount(본 구간 수) — 고른 구간만, 연달아 같은 장소는 한 번.

무대: guide_prerun.py Stage(병원 맵 1) — 병원 시나리오 단계를 실제 DB(10-27 조회)의 흐름 연결 그대로 다시 넣는다.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "guide_prerun_base_pp", os.path.join(os.path.dirname(__file__), "guide_prerun.py"))
gp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gp)
del REGISTRY[_n:]

# (코드, 순서, WaitFor, 이동 장소 id | 슬롯, Next, OnNo, OnTimeout) — 실제 병원 안내(맵47) 흐름, 꺼진 단계 제외
# 장소 id 는 무대 것: 2 원무과 접수, 3 진료대기장소, 4 편의점입구, 7 원무과수납, 1 로봇대기장소(출구 대신)
REAL = [
    ("greet", 10, "none", None, "handle_guide", None, None),
    ("handle_guide", 20, "none", None, "ready_check", None, None),
    ("ready_check", 30, "user", None, "purpose_ask", "ready_check", "ready_check"),
    ("purpose_ask", 40, "user", None, "purpose_confirm", None, None),
    ("purpose_confirm", 50, "user", None, "ticket_move", "purpose_ask", None),
    ("ticket_move", 70, "arrival", 2, "ticket_wait", None, None),
    ("ticket_wait", 80, "operator", None, "ticket_announce", None, None),
    ("ticket_announce", 90, "none", None, "seat_move", None, None),
    ("seat_move", 100, "arrival", 3, "store_ask", None, None),
    ("store_ask", 110, "user", None, "store_move", "seat_stay", "seat_stay"),
    ("seat_stay", 120, "operator", None, "turn_ready", None, None),
    ("store_move", 130, "arrival", 4, "store_wait", None, None),
    ("store_wait", 140, "operator", None, "turn_ready", None, None),
    ("store_return_move", 150, "arrival", 3, "seat_wait", None, None),
    ("seat_wait", 160, "operator", None, "turn_ready", None, None),
    ("turn_ready", 170, "user", None, "clinic_move", "turn_ready", "turn_ready"),
    ("clinic_move", 190, "arrival", "목적지", "clinic_wait", None, None),
    ("clinic_wait", 200, "operator", None, "payment_ready", None, None),
    ("payment_ready", 210, "user", None, "payment_move", "payment_ready", "payment_ready"),
    ("payment_move", 230, "arrival", 7, "exit_ask", None, None),
    ("exit_ask", 250, "user", None, "extra_move", "exit_move", "exit_move"),
    ("exit_move", 260, "arrival", 1, "exit_arrive", None, None),
    ("exit_arrive", 270, "none", None, "finish", None, None),
    ("finish_ask", 280, "user", None, "extra_dest", "finish", "finish"),
    ("extra_dest", 290, "user", None, "extra_move", "finish", "finish"),
    ("extra_move", 300, "arrival", "목적지", "extra_arrive", None, None),
    ("extra_arrive", 310, "none", None, None, None, None),
    ("finish", 320, "none", None, None, None, None),
]


def _stage(sim):
    st, _ = gp._stage(sim)
    from app.database import models as M
    db = st.Session()
    try:
        db.query(M.VoiceScenarioStep).filter(M.VoiceScenarioStep.ScenarioId == 1).delete()
        for code, order, wait, tgt, nxt, on_no, on_to in REAL:
            nav = wait == "arrival"
            db.add(M.VoiceScenarioStep(
                ScenarioId=1, StepCode=code, StepOrder=order, WaitFor=wait, IsActive=1,
                ActionType="navigate" if nav else "none",
                TargetLocationId=tgt if isinstance(tgt, int) else None,
                SlotName=tgt if isinstance(tgt, str) else None,
                NextStepCode=nxt, OnNoStepCode=on_no, OnTimeoutStepCode=on_to, SayText=code))
        db.commit()
    finally:
        db.close()
    return st


def _start(st, idx=None, count=None):
    req = st.va.PrerunStartReq(scenarioCode="hospital", startLocationId=1, speed="fast",
                               legIndexes=idx, legCount=count)
    return st.call(st.va.start_voice_prerun, gp.ROBOT, req, gp.REQ)


def _flags(plan):
    return [(lg["name"], lg["step"], lg.get("branch")) for lg in plan["legs"]]


@scenario("K1 동선 미리보기 — 편의점(store_move)·대기 자리 복귀(store_return_move)만 '대답에 따라', 출구는 기본",
          expect=lambda r: (r["branch_steps"] == ["store_move", "store_return_move"]) or f"갈래={r['branch_steps']} 동선={r['legs']}")
def k1(sim):
    st = _stage(sim)
    plan = st.plan("hospital", 1)
    if isinstance(plan, str):
        return {"branch_steps": plan, "legs": None}
    return {"branch_steps": sorted({s for _, s, b in _flags(plan) if b}), "legs": _flags(plan)}


@scenario("K2 갈래 끄고 시작 → 편의점 안 가고 나머지 한 바퀴 완료", limit_sec=1500,
          expect=lambda r: (r["status"] == "done" and "편의점입구" not in r["legs"] and r["visited_store"] is False)
          or f"상태={r['status']} 동선={r['legs']} 편의점={r['visited_store']}")
def k2(sim):
    st = _stage(sim)
    plan = st.plan("hospital", 1)
    keep = [i for i, lg in enumerate(plan["legs"]) if not lg.get("branch")]
    out = _start(st, keep, len(plan["legs"]))
    if isinstance(out, str):
        return {"status": out, "legs": None, "visited_store": None}
    st.drive()
    snap = st.state() or {}
    store = gp.XY[4]
    return {"status": snap.get("status"), "legs": [lg["name"] for lg in snap.get("legs", [])],
            "visited_store": any(abs(a[1] - store[0]) < 0.05 and abs(a[2] - store[1]) < 0.05
                                 for a in sim.robot.arrivals)}


@scenario("K3 고르는 사이 동선이 바뀜(구간 수 다름) → 409, 주행 안 함",
          expect=lambda r: (str(r["out"]).startswith("409") and not r["active"]) or f"{r}")
def k3(sim):
    st = _stage(sim)
    plan = st.plan("hospital", 1)
    out = _start(st, [0, 1], len(plan["legs"]) + 1)
    return {"out": out, "active": st.pr.is_active()}


@scenario("K4 출발 장소만 남기고 다 끔 → 422 '갈 곳을 하나 이상'",
          expect=lambda r: ("422" in str(r["out"]) and "하나 이상" in str(r["out"])) or f"{r}")
def k4(sim):
    st = _stage(sim)
    plan = st.plan("hospital", 1)
    keep = [i for i, lg in enumerate(plan["legs"]) if lg["role"] == "start"]
    return {"out": _start(st, keep, len(plan["legs"]))}


@scenario("K5 [회귀] 고르기 없이 시작(예전 요청) → 동선 전체(편의점 포함)",
          expect=lambda r: (r["n"] == r["plan_n"] and "편의점입구" in r["legs"]) or f"{r}")
def k5(sim):
    st = _stage(sim)
    plan = st.plan("hospital", 1)
    out = _start(st)
    snap = st.state() or {}
    return {"n": len(snap.get("legs", [])), "plan_n": len(plan["legs"]),
            "legs": [lg["name"] for lg in snap.get("legs", [])], "out": str(out)[:80]}
