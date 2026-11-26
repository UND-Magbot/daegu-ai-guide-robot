"""안내 사전 주행 — 안내 전에 시나리오 동선을 멘트 없이 고속으로 한 바퀴 (현행 vs 변경).

무대: 병원 안내(맵 1)와 관공서 안내(맵 2). 실제 로컬 DB 의 단계 구성을 줄여 옮겼다(2026-10-22 확인).
    병원 이동 단계: 원무과 접수 → 진료대기장소 → 편의점입구 → 진료대기장소 → [목적지] → 원무과수납
                    → 로봇대기장소 → [추가 목적지]
    목적지: 정형외과·안과 → 간호실데스크, 엘리베이터 입구·3층관공서 → 1F_엘리베이터입구
현행에는 사전 주행이 없다. 현행 칸은 '기능 없음' 으로 실패하는 것이 정상이고, 회귀 시나리오만 둘 다 통과해야 한다.
"""
import math
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

from harness import scenario
from harness.backend import replace_everywhere

os.environ.setdefault("AUTO_DETOUR_DISABLED_MAPS", "1")

ROBOT = 10
USER = MagicMock(id=1, BusinessId=None)
REQ = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={}, state=SimpleNamespace())

# id, 이름, 맵, x, y
PLACES = [
    (1, "로봇대기장소", 1, 0.0, 0.0),
    (2, "원무과 접수", 1, 4.0, 0.0),
    (3, "진료대기장소", 1, 4.0, 4.0),
    (4, "편의점입구", 1, 8.0, 4.0),
    (5, "간호실데스크", 1, 0.0, 6.0),
    (6, "1F_엘리베이터입구", 1, 8.0, 8.0),
    (7, "원무과수납", 1, 2.0, 2.0),
    (8, "안내데스크", 2, 0.0, 0.0),
    (9, "대기자리", 2, 3.0, 0.0),
    (10, "접수처", 2, 6.0, 0.0),
    (11, "로비", 2, 9.0, 0.0),
    (12, "신청서 작성", 2, 3.0, 3.0),
]
NAME = {p[0]: p[1] for p in PLACES}
XY = {p[0]: (p[3], p[4]) for p in PLACES}

# B 동선(사용자 결정) — 출발 장소(로봇대기장소)에서 시작해 돌아온다
HOSPITAL_B = ["로봇대기장소", "원무과 접수", "진료대기장소", "편의점입구", "진료대기장소", "간호실데스크",
              "원무과수납", "진료대기장소", "1F_엘리베이터입구", "원무과수납", "로봇대기장소"]
OFFICE_B = ["안내데스크", "신청서 작성", "대기자리", "접수처", "로비"]


class Stage:
    def __init__(self, sim, robot_at=(0.0, 0.0)):
        from app.database.database import SessionLocal
        from app.database import models as M
        import app.database.routes.voice_scenario as vs
        import app.database.routes.voice_scenario_admin as va
        try:
            from app.database import voice_prerun as pr
        except ImportError:
            pr = None
        sim.rewire_late_modules()
        self.sim, self.M, self.vs, self.va, self.pr, self.Session = sim, M, vs, va, pr, SessionLocal
        self.rt = sim.rt
        with self.rt._lock:   # 빈 dict 면 set_initpose_pending 이 무시한다
            self.rt._runtime.setdefault(ROBOT, {"robot_id": ROBOT, "robot_name": "SIM"})

        db = SessionLocal()
        db.add(M.FloorInfo(id=1, BusinessId=1, FloorName="1F"))
        db.add(M.RobotInfo(id=ROBOT, RobotName="SIM", BusinessId=1, CurrentMapId=1, CurrentFloorId=1))
        for pid, name, mid, x, y in PLACES:
            db.add(M.LocationInfo(id=pid, RobotName="SIM", LacationName=name, FloorId=1, MapId=mid,
                                  LocationX=x, LocationY=y, Yaw=0.0, Category="work"))
        db.add(M.VoiceScenario(id=1, RobotId=ROBOT, Code="hospital", DisplayName="병원 안내",
                               MapId=1, FloorId=1, IsActive=1, GuideSpeed="normal"))
        steps = [("greet", 10, "none", None, None, "안녕하세요"),
                 ("ticket_move", 50, "navigate", 2, None, "접수로 가요"),
                 ("seat_move", 80, "navigate", 3, None, "대기 장소로 가요"),
                 ("store_move", 86, "navigate", 4, None, "편의점으로 가요"),
                 ("store_return_move", 89, "navigate", 3, None, "돌아가요"),
                 ("clinic_move", 130, "navigate", None, "목적지", "진료실로 가요"),
                 ("payment_move", 160, "navigate", 7, None, "수납으로 가요"),
                 ("exit_move", 190, "navigate", 1, None, "배웅해요"),
                 ("extra_move", 230, "navigate", None, "목적지", "추가 목적지")]
        for code, order, action, target, slot, say in steps:
            db.add(M.VoiceScenarioStep(ScenarioId=1, StepCode=code, StepOrder=order, ActionType=action,
                                       TargetLocationId=target, SlotName=slot, SayText=say))
        db.add(M.VoiceScenario(id=2, RobotId=ROBOT, Code="public_office", DisplayName="관공서 안내",
                               MapId=2, FloorId=1, IsActive=1, GuideSpeed="normal"))
        for code, order, target, slot in (("ticket_move", 40, 8, None), ("form_move", 70, None, "목적지"),
                                          ("form_done", 90, 9, None), ("counter_move", 120, 10, None),
                                          ("lobby_move", 150, 11, None), ("extra_move", 190, None, "목적지")):
            db.add(M.VoiceScenarioStep(ScenarioId=2, StepCode=code, StepOrder=order, ActionType="navigate",
                                       TargetLocationId=target, SlotName=slot, SayText="이동"))
        # 경계: 장소 없는 단계·다른 맵 장소·목적지 연결 없음
        db.add(M.VoiceScenario(id=3, RobotId=ROBOT, Code="broken", DisplayName="깨진 시나리오",
                               MapId=1, FloorId=1, IsActive=1, GuideSpeed="normal"))
        for code, order, target, slot in (("a_move", 10, None, None), ("b_move", 20, 8, None),
                                          ("c_move", 30, 7, None), ("d_move", 40, None, "없는슬롯")):
            db.add(M.VoiceScenarioStep(ScenarioId=3, StepCode=code, StepOrder=order, ActionType="navigate",
                                       TargetLocationId=target, SlotName=slot, SayText="이동"))
        for sid, value, links in ((1, "정형외과", [(1, 5)]), (2, "안과", [(1, 5)]),
                                  (3, "엘리베이터 입구", [(1, 6)]), (4, "3층관공서", [(1, 6)]),
                                  (5, "기초연금", [(2, 12)])):
            db.add(M.VoiceSlotValue(id=sid, RobotId=ROBOT, SlotName="목적지", Value=value))
            for mid, lid in links:
                db.add(M.VoiceSlotLocation(SlotValueId=sid, MapId=mid, LocationId=lid))
        db.commit()
        db.close()

        vs._prime_in_background = lambda rid: None
        vs._ensure_standing = lambda *a, **k: None
        va.write_audit = lambda *a, **k: None
        sim.robot.place(*robot_at)

        # NAV 마다 (시각, 목표 장소 이름, speed) 를 남긴다
        self.navs = []
        prev = sim.sender.send_nav_to_robot

        def nav(idx, x, y, yaw, speed=None, map_id=None, continuing=False):
            name = next((n for i, n in NAME.items() if XY[i] == (x, y)), f"({x},{y})")
            self.navs.append((round(sim.clock.elapsed(), 1), name, speed))
            return prev(idx, x, y, yaw, speed=speed, map_id=map_id, continuing=continuing)
        replace_everywhere(prev, nav)
        if hasattr(sim.sender, "send_nav_multi_to_robot"):     # 다지점 묶음은 점마다 남긴다
            prev_multi = sim.sender.send_nav_multi_to_robot

            def nav_multi(points, map_id=None, **kw):      # continuing 등 새 인자는 그대로 넘긴다
                for p in points:
                    name = next((n for i, n in NAME.items() if XY[i] == (p["x"], p["y"])),
                                f"({p['x']},{p['y']})")
                    self.navs.append((round(sim.clock.elapsed(), 1), name, p.get("speed")))
                return prev_multi(points, map_id=map_id, **kw)
            replace_everywhere(prev_multi, nav_multi)

    # ── 화면 API ──
    def call(self, fn, *a, **k):
        from fastapi import HTTPException
        db = self.Session()
        try:
            return fn(*a, db=db, current_user=USER, **k)
        except HTTPException as e:
            return f"{e.status_code} {e.detail}"
        finally:
            db.close()

    def plan(self, code, start=None):
        return self.call(self.va.preview_voice_prerun, ROBOT,
                         self.va.SessionOpenReq(scenarioCode=code, startLocationId=start))

    def start(self, code="hospital", start=1):
        return self.call(self.va.start_voice_prerun, ROBOT,
                         self.va.SessionOpenReq(scenarioCode=code, startLocationId=start), REQ)

    def resume(self, from_failed):
        return self.call(self.va.resume_voice_prerun, ROBOT, self.va.PrerunResumeReq(fromFailed=from_failed), REQ)

    def stop(self):
        return self.call(self.va.stop_voice_prerun, ROBOT, REQ)

    def open_guide(self, start=None):
        return self.call(self.va.open_voice_session, ROBOT,
                         self.va.SessionOpenReq(scenarioCode="hospital", startLocationId=start), REQ)

    def state(self):
        return self.pr.snapshot() if self.pr else None

    def drive(self, until=None, max_sec=900):
        """nav_thread 를 돌린다. 기본: 사전 주행이 활성에서 벗어날 때까지."""
        sim, pr = self.sim, self.pr
        t0 = sim.clock.elapsed()
        stop = until or (lambda: not pr.is_active())
        sim.run(sim.pl.nav_thread, until=lambda: stop() or sim.clock.elapsed() - t0 > max_sec, grace=2.0)

    def lose_loc(self, at, sec):
        """측위 상실 주입. 상실한 로봇은 그 자리에 선다(NAV 를 거부한다 — 09-29 실측)."""
        rt, robot = self.rt, self.sim.robot

        def lost():
            rt.set_initpose_pending(ROBOT, True, rt.LOCALIZATION_LOST_REASON)
            robot.hold(sec if sec is not None else 1000)
        self.sim.at(at, lost)
        if sec is not None:
            self.sim.at(at + sec, lambda: rt.set_initpose_pending(ROBOT, False))

    def watch_wait_leg(self) -> dict:
        """측위 대기에 들어간 순간의 구간(멈춘 곳)을 잡는다 — 이어 가기는 여기서 시작해야 한다."""
        got = {}

        def look():
            s = self.state()
            if s and s["status"] == "loc_wait" and "leg" not in got:
                got["leg"] = s["leg"]
                got["pos"] = (self.sim.robot.x, self.sim.robot.y)
            elif "leg" not in got:
                self.sim.at(0.5, look)
        self.sim.at(0.5, look)
        return got

    def expected_resume(self, got: dict):
        """이어 갈 첫 장소 — 멈춘 구간. 그 장소 0.5m 안에서 멈췄으면 이미 온 것이라 다음 구간."""
        if "leg" not in got:
            return None
        legs, k = self.state()["legs"], got["leg"]
        if math.dist(got["pos"], (legs[k]["x"], legs[k]["y"])) < 0.5 and k + 1 < len(legs):
            k += 1
        return legs[k]["name"]

    def summary(self, extra=None):
        st = self.state() or {}
        out = {
            "status": st.get("status"),
            "reason": st.get("reason"),
            "done": f"{st.get('doneLegs')}/{st.get('total')}",
            "locLost": st.get("locLost"),
            "nav_places": [n for _, n, _ in self.navs],
            "nav_speeds": sorted({str(s) for _, _, s in self.navs}),
            "nav_n": len(self.navs),
        }
        out.update(extra or {})
        return out


NO_FEATURE = {"feature": "없음(현행에 사전 주행 기능 없음)"}


def _stage(sim, **kw):
    st = Stage(sim, **kw)
    return st, (st.pr is not None)


def _visited(navs):
    """보낸 NAV 목표를 연달아 같은 곳은 하나로 — 재전송·이어 가기 중복 제거."""
    out = []
    for _, n, _ in navs:
        if not out or out[-1] != n:
            out.append(n)
    return out


# ── 동선 계산 ──────────────────────────────────────
@scenario("[동선] 병원: 목적지마다 실제 동선(B), 출발 장소에서 시작·복귀",
          expect=lambda r: r.get("legs") == HOSPITAL_B and r.get("dests") == 2 and r.get("warn") == [])
def plan_hospital(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    p = st.plan("hospital", start=1)
    if isinstance(p, str):
        return {"err": p}
    return {"legs": [lg["name"] for lg in p["legs"]], "dests": p["destinations"], "warn": p["warnings"]}


@scenario("[동선] 관공서: 목적지 1곳, 현재 위치 출발(호출 위치 없음)",
          expect=lambda r: r.get("legs") == OFFICE_B and r.get("dests") == 1)
def plan_office(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    p = st.plan("public_office")
    return {"legs": [lg["name"] for lg in p["legs"]], "dests": p["destinations"], "warn": p["warnings"]}


@scenario("[동선·경계] 장소 없는 단계·다른 맵 장소·연결 없는 목적지는 건너뛰고 경고 3건",
          expect=lambda r: r.get("legs") == ["원무과수납"] and r.get("warn_n") == 3)
def plan_broken(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    p = st.plan("broken")
    return {"legs": [lg["name"] for lg in p["legs"]], "warn_n": len(p["warnings"]), "warn": p["warnings"]}


# ── 주행 ───────────────────────────────────────────
def _ok_full(r):
    return (r.get("status") == "done" and r.get("visited") == HOSPITAL_B[1:] and r.get("done") == "11/11"
            and r.get("nav_speeds") == ["fast"] and r.get("session") is None
            and r.get("pos") == [0, 0] and r["errors"] == 0)


@scenario("[주행] 정상 한 바퀴 — 고속·멘트 없음·안내 세션 안 열림, 출발 장소 복귀", expect=_ok_full,
          limit_sec=1500)
def full_lap(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    res = st.start()
    st.drive()
    db = st.Session()
    try:
        session = st.vs._active_session(db, "SIM")
    finally:
        db.close()
    return st.summary({"start": res if isinstance(res, str) else "ok", "visited": _visited(st.navs),
                       "session": session and session.id})


@scenario("[측위] 주행 중 상실 12초 → 상실 동안 NAV 0건, 회복 후 그 구간부터 이어서 완료",
          expect=lambda r: r.get("status") == "done" and r.get("locLost") == 1 and r.get("nav_while_lost") == 0
          and r.get("resent_from") == r.get("expect_from") and r["errors"] == 0, limit_sec=1500)
def loc_lost_recover(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    st.start()
    lost_at = sim.clock.elapsed() + 26.0
    st.lose_loc(26.0, 12.0)
    heading = st.watch_wait_leg()
    st.drive()
    while_lost = [n for t, n, _ in st.navs if lost_at < t < lost_at + 12.0]
    after = [n for t, n, _ in st.navs if t >= lost_at + 12.0]
    legs = st.state()["legs"]
    return st.summary({"nav_while_lost": len(while_lost),
                       "lost_heading": legs[heading["leg"]]["name"] if "leg" in heading else None,
                       "expect_from": st.expected_resume(heading),
                       "resent_from": after[0] if after else None})


@scenario("[측위] 상실 30초 넘김 → 멈춤(사유)·NAV 없음, 회복 뒤 '실패 지점부터 다시' 로 남은 구간만 완료",
          expect=lambda r: r.get("first") == "failed" and "30초" in (r.get("first_reason") or "")
          and r.get("nav_after_fail") == 0 and r.get("status") == "done"
          and r.get("redo_from") == r.get("expect_from") and r["errors"] == 0, limit_sec=1800)
def loc_lost_timeout(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    st.start()
    st.lose_loc(26.0, None)
    waited = st.watch_wait_leg()
    st.drive()
    first = st.state()
    n_fail = len(st.navs)
    sim.wait(20.0)                                  # 운영자가 위치를 다시 잡는 동안
    nav_after_fail = len(st.navs) - n_fail
    st.rt.set_initpose_pending(ROBOT, False)     # 운영자가 위치를 다시 잡았다 — 로봇도 다시 움직인다
    sim.robot.hold_until = -1e9
    res = st.resume(True)
    st.drive()
    return st.summary({"first": first["status"], "first_reason": first["reason"],
                       "failed_leg": first["legs"][waited["leg"]]["name"] if "leg" in waited else None,
                       "expect_from": st.expected_resume(waited),
                       "redo_from": st.navs[n_fail][1] if len(st.navs) > n_fail else None,
                       "nav_after_fail": nav_after_fail, "resume": res if isinstance(res, str) else "ok"})


@scenario("[이동 실패] 한 구간에서 계속 못 감(재전송 한도) → 다음 장소로 넘어가지 않고 멈춤·실패 장소 표시",
          expect=lambda r: r.get("status") == "failed" and "편의점입구" in (r.get("reason") or "")
          and r.get("pos") == [4, 4] and r.get("nav_after_fail") == 0 and not r["executing"], limit_sec=1800)
def unreachable_leg(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    st.start()
    # 편의점입구로 가는 동안 로봇이 제자리(장애물) — 이동 시작 감지 후 hang 재전송 3회 뒤 못 감
    def block():
        tgt = sim.robot.target
        if tgt and (tgt[0], tgt[1]) == XY[4]:
            sim.robot.hold(1000)
        else:
            sim.at(0.5, block)
    sim.at(0.5, block)
    st.drive()
    n = len(st.navs)
    sim.wait(30.0)
    return st.summary({"nav_after_fail": len(st.navs) - n})


@scenario("[다시] 멈춘 뒤 '처음부터 다시' → 첫 장소부터 한 바퀴 완료",
          expect=lambda r: r.get("status") == "done" and r.get("restart_first") == "로봇대기장소"
          and r["errors"] == 0, limit_sec=1800)
def restart_from_beginning(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    st.start()
    sim.at(25.0, st.stop)
    st.drive()
    n = len(st.navs)
    res = st.resume(False)
    st.drive()
    return st.summary({"restart_first": st.navs[n][1] if len(st.navs) > n else None,
                       "resume": res if isinstance(res, str) else "ok"})


@scenario("[중지] 운영자 중지 → 로봇 정지(CANCEL_NAV)·상태 stopped·이후 NAV 없음",
          expect=lambda r: r.get("status") == "stopped" and "CANCEL_NAV" in r["cmds"]
          and r.get("nav_after_stop") == 0 and not r["executing"])
def operator_stop(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    st.start()
    sim.at(20.0, st.stop)
    st.drive()
    n = len(st.navs)
    sim.wait(30.0)
    return st.summary({"nav_after_stop": len(st.navs) - n})


# ── 겹침 ───────────────────────────────────────────
@scenario("[겹침] 사전 주행 중 안내 시작 → 409, 사전 주행은 계속해 완료",
          expect=lambda r: str(r.get("guide")).startswith("409") and r.get("status") == "done",
          limit_sec=1500)
def guide_during_prerun(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    st.start()
    got = {}
    sim.at(15.0, lambda: got.setdefault("guide", st.open_guide()))
    st.drive()
    g = got.get("guide")
    return st.summary({"guide": g if isinstance(g, str) else "열림"})


@scenario("[겹침] 안내 진행 중 사전 주행 시작 → 409, 로봇 안 움직임",
          expect=lambda r: str(r.get("start")).startswith("409") and r.get("nav_n") == 0)
def prerun_during_guide(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    st.open_guide()
    res = st.start()
    return {"start": res if isinstance(res, str) else "시작됨", "nav_n": len(st.navs)}


@scenario("[겹침] 사전 주행 중 다른 장소 이동 명령 → 사전 주행 멈춤(사유), 새 이동은 그대로",
          expect=lambda r: r.get("status") == "failed" and "다른 이동" in (r.get("reason") or "")
          and r.get("pos") == [8, 8] and r["errors"] == 0, limit_sec=1500)
def other_move_during_prerun(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    st.start()

    def other():
        db = st.Session()
        try:
            sim.sm.start_place_move(6, db)
        finally:
            db.close()
    sim.at(15.0, other)
    st.drive(until=lambda: not st.pr.is_active() and not sim.sm.is_navigating)
    return st.summary()


@scenario("[경계] 위치 미확정(측위 상실) 상태에서 시작 → 409, 로봇 안 움직임",
          expect=lambda r: str(r.get("start")).startswith("409") and r.get("nav_n") == 0)
def start_while_lost(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    st.rt.set_initpose_pending(ROBOT, True, st.rt.LOCALIZATION_LOST_REASON)
    res = st.start()
    return {"start": res if isinstance(res, str) else "시작됨", "nav_n": len(st.navs)}


@scenario("[경계] 통신 두절 70초(안전 정지) → 사전 주행 멈춤, 안내 자동 시작 없음",
          expect=lambda r: r.get("status") == "failed" and r.get("session") is None and r["errors"] == 0,
          limit_sec=1500)
def comm_lost(sim):
    st, ok = _stage(sim)
    if not ok:
        return NO_FEATURE
    st.start()
    sim.at(20.0, lambda: sim.robot.drop_comm(70.0))
    st.drive()
    db = st.Session()
    try:
        s = st.vs._active_session(db, "SIM")
    finally:
        db.close()
    return st.summary({"session": s and s.id})


# ── 회귀 ───────────────────────────────────────────
@scenario("[회귀] 사전 주행 뒤 안내 열기 → 안내 이동은 시나리오 설정(normal), 설정값 그대로",
          # 10-29 오후부터 첫 구간 4m 이하는 출발 저속이라 첫 명령이 slow 일 수 있다 — 설정은 웨이포인트 속도로 본다
          expect=lambda r: r.get("wp_speed") == "normal" and r.get("guide_speed") in ("normal", "slow")
          and r.get("stored") == "normal"
          and r.get("pre_speeds") in (["fast"], None), limit_sec=1500)
def guide_after_prerun(sim):
    st, ok = _stage(sim)
    pre = None
    if ok:
        st.start()
        st.drive()
        pre = sorted({str(s) for _, _, s in st.navs})
        st.pr.dismiss()
    res = st.open_guide()
    sid = res["session"]["id"]
    db = st.Session()
    try:
        s = db.query(st.M.ServiceSession).filter(st.M.ServiceSession.id == sid).first()
        s.CurrentStepCode = "ticket_move"
        db.commit()
        n = len(st.navs)
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
        stored = db.query(st.M.VoiceScenario).filter(st.M.VoiceScenario.id == 1).first().GuideSpeed
        wp_speed = sim.sm.waypoints_list[-1].get("speed") if sim.sm.waypoints_list else None
    finally:
        db.close()
    st.drive(until=lambda: not sim.sm.is_navigating)
    return {"guide_speed": st.navs[n][2] if len(st.navs) > n else "NAV 없음", "stored": stored,
            "wp_speed": wp_speed, "pre_speeds": pre}


@scenario("[회귀] 기존 장소 이동(start_place_move) — 그대로 도착, 속도 실림",
          expect=lambda r: r.get("pos") == [8, 4] and r.get("speed") == "slow" and r["errors"] == 0)
def place_move_regression(sim):
    st = Stage(sim)
    db = st.Session()
    try:
        res = sim.sm.start_place_move(4, db, speed="slow")
    finally:
        db.close()
    st.drive(until=lambda: not sim.sm.is_navigating)
    return {"res": res.get("status"), "speed": st.navs[-1][2] if st.navs else None}
