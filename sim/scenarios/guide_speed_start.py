"""안내 속도 '고속' 추가 · 안내 시작 '기본 시작 장소' — 현행 vs 변경.

속도: 운영자가 음성관리에서 고른 안내 속도가 관리 API(update_voice_scenario)를 지나 안내 단계 이동
(session_move)의 NAV speed 로 실리는지 본다. 호출 위치 이동·멘트 없는 이동은 속도를 싣지 않아야 한다.
하네스 기본 송신 대체는 speed 를 버리므로, 여기서 한 겹 감싸 speed 를 기록한다.

시작 장소: 화면이 기본 시작 장소를 미리 고르는 것은 프론트 변경이다(서버 수정 없음). 여기서는 서버가
기본 시작 장소로 열면 call_move 부터, 없이 열면 첫 단계부터 시작하는 기존 동작이 그대로인지만 본다.
"""
import os
from types import SimpleNamespace

from harness import scenario
from harness.backend import replace_everywhere

os.environ.setdefault("AUTO_DETOUR_DISABLED_MAPS", "1")

ROBOT = 10
START = 1        # 로봇 대기 장소 (2, 0)
CLINIC = 2       # 내과 (6, 0) — 안내 목적지


class Stage:
    def __init__(self, sim, speed="slow"):
        from app.database.database import SessionLocal
        from app.database import models as M
        import app.database.routes.voice_scenario as vs
        import app.database.routes.voice_scenario_admin as va
        sim.rewire_late_modules()
        self.sim, self.M, self.vs, self.va, self.Session = sim, M, vs, va, SessionLocal

        db = SessionLocal()
        db.add(M.FloorInfo(id=1, BusinessId=1, FloorName="1F"))
        db.add(M.RobotInfo(id=ROBOT, RobotName="SIM", BusinessId=1, CurrentMapId=1, CurrentFloorId=1))
        for pid, name, x in ((START, "로봇 대기 장소", 2.0), (CLINIC, "내과", 6.0)):
            db.add(M.LocationInfo(id=pid, RobotName="SIM", LacationName=name, FloorId=1, MapId=1,
                                  LocationX=x, LocationY=0.0, Yaw=0.0, Category="work"))
        db.add(M.VoiceScenario(id=1, RobotId=ROBOT, Code="hospital", DisplayName="병원 안내",
                               MapId=1, FloorId=1, IsActive=1, GuideSpeed=speed))
        db.add(M.VoiceScenarioStep(ScenarioId=1, StepCode="greet", StepOrder=1, SayText="안녕하세요"))
        db.add(M.VoiceScenarioStep(ScenarioId=1, StepCode="guide_move", StepOrder=2,
                                   ActionType="navigate", SayText="안내하겠습니다"))
        db.add(M.VoiceScenarioStep(ScenarioId=1, StepCode="silent_move", StepOrder=3,
                                   ActionType="navigate", SayText=""))
        db.commit()
        db.close()

        vs._prime_in_background = lambda rid: None     # 제자리 보행 미리 전환은 이번 비교 밖
        vs._ensure_standing = lambda rid: None         # 서 있다고 본다
        # 송신 대체를 한 겹 감싸 speed 를 남긴다
        self.speeds = []
        prev = sim.sender.send_nav_to_robot

        def nav(idx, x, y, yaw, speed=None, map_id=None, continuing=False):
            self.speeds.append(speed)
            return prev(idx, x, y, yaw, speed=speed, map_id=map_id, continuing=continuing)
        replace_everywhere(prev, nav)

    def put_speed(self, value):
        """운영자가 음성관리에서 안내 속도를 고른다(관리 API 그대로)."""
        db = self.Session()
        user = SimpleNamespace(id=1, UserId="sim", BusinessId=None, Permission="admin",
                               UserName="sim", _perms=None)
        req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={}, state=SimpleNamespace())
        try:
            self.va.update_voice_scenario(1, self.va.ScenarioPatchReq(guideSpeed=value), req, db=db,
                                          current_user=user)
            return "저장"
        except Exception as e:
            return f"{getattr(e, 'status_code', '?')} {getattr(e, 'detail', e)}"
        finally:
            db.close()

    def stored_speed(self):
        db = self.Session()
        try:
            return db.query(self.M.VoiceScenario).filter(self.M.VoiceScenario.id == 1).first().GuideSpeed
        finally:
            db.close()

    def open(self, start_id=None):
        db = self.Session()
        try:
            robot = db.query(self.M.RobotInfo).filter(self.M.RobotInfo.id == ROBOT).first()
            s = self.vs.open_session(db, robot, "hospital", start_location_id=start_id)
            return s.id, s.CurrentStepCode
        finally:
            db.close()

    def set_step(self, sid, code):
        db = self.Session()
        try:
            s = db.query(self.M.ServiceSession).filter(self.M.ServiceSession.id == sid).first()
            s.CurrentStepCode = code
            db.commit()
        finally:
            db.close()

    def move(self, sid, place_id):
        """Jetson 의 세션 이동 요청 → 끝까지 주행."""
        db = self.Session()
        try:
            self.vs.session_move(sid, place_id=place_id, db=db, _="sim")
        finally:
            db.close()
        sim, sm = self.sim, self.sim.sm
        t0 = sim.clock.elapsed()
        sim.run(sim.pl.nav_thread, until=lambda: not sm.is_navigating or sim.clock.elapsed() - t0 > 60,
                grace=1.0)


def _guide_trip(sim, speed_choice):
    st = Stage(sim)
    saved = st.put_speed(speed_choice)
    sid, first = st.open()
    st.set_step(sid, "guide_move")
    st.move(sid, CLINIC)
    return {"put": saved, "stored": st.stored_speed(), "first_step": first,
            "nav_speed": st.speeds[-1] if st.speeds else "NAV 없음"}


@scenario("[속도] 고속 선택 → 안내 이동 NAV speed=fast",
          expect=lambda r: r["put"] == "저장" and r["stored"] == "fast" and r["nav_speed"] == "fast"
          and r["pos"] == [6, 0] and r["errors"] == 0)
def fast_guide(sim):
    return _guide_trip(sim, "fast")


@scenario("[속도·회귀] 저속 선택 → speed=slow",
          expect=lambda r: r["put"] == "저장" and r["nav_speed"] == "slow" and r["pos"] == [6, 0])
def slow_guide(sim):
    return _guide_trip(sim, "slow")


@scenario("[속도·회귀] 보통 선택 → speed=normal",
          expect=lambda r: r["put"] == "저장" and r["nav_speed"] == "normal" and r["pos"] == [6, 0])
def normal_guide(sim):
    return _guide_trip(sim, "normal")


@scenario("[속도·경계] 목록에 없는 값(turbo) 거부, 저장값 그대로",
          expect=lambda r: r["put"].startswith("422") and r["stored"] == "slow")
def bad_speed(sim):
    st = Stage(sim)
    return {"put": st.put_speed("turbo"), "stored": st.stored_speed()}


@scenario("[속도·경계] 고속이어도 호출 위치 이동(call_move)은 속도 안 실음",
          expect=lambda r: r["first_step"] == "call_move" and r["nav_speed"] is None and r["pos"] == [2, 0])
def fast_call_move(sim):
    st = Stage(sim, speed="fast")
    sim.robot.place(9, 0)
    sid, first = st.open(start_id=START)
    st.move(sid, START)
    return {"first_step": first, "nav_speed": st.speeds[-1] if st.speeds else "NAV 없음"}


@scenario("[속도·경계] 고속이어도 멘트 없는 이동(마중)은 속도 안 실음",
          expect=lambda r: r["nav_speed"] is None and r["pos"] == [6, 0])
def fast_silent_move(sim):
    st = Stage(sim, speed="fast")
    sid, _ = st.open()
    st.set_step(sid, "silent_move")
    st.move(sid, CLINIC)
    return {"nav_speed": st.speeds[-1] if st.speeds else "NAV 없음"}


@scenario("[시작·회귀] 기본 시작 장소로 열기 → call_move 부터, 그 장소로 이동",
          expect=lambda r: r["first_step"] == "call_move" and r["pos"] == [2, 0] and r["errors"] == 0)
def start_at_default(sim):
    st = Stage(sim)
    sim.robot.place(9, 0)
    sid, first = st.open(start_id=START)
    st.move(sid, START)
    return {"first_step": first}


@scenario("[시작·회귀] 현재 위치에서 시작 → 첫 단계(greet), 이동 없음",
          expect=lambda r: r["first_step"] == "greet" and r["arrivals"] == 0)
def start_in_place(sim):
    st = Stage(sim)
    _, first = st.open()
    return {"first_step": first}
