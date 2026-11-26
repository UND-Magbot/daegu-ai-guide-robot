"""안내 중 장애물 정지·멘트·자동 재개(10-29) — 현행 vs 변경.

무대는 standby_scenario.py 의 Stage(로비 대기 (6,0)=id 1 → 내과 (6,6)=id 2). Jetson 엔진은 돌리지 않고,
관제가 /robot/nav notice 로 싣는 멘트를 그대로 모은다(음성 엔진은 seq 가 바뀌면 그 글을 말한다).

가짜 감지 노드(FakeGuard) — robot_receiver/obstacle_guard.py 의 판정 규칙을 시간 단위로만 옮겼다.
    관제 신호(arm)를 ttl 안에 받은 동안만 감지. 앞이 0.3초 막히면 CANCEL_NAV 를 로봇에 보내고 stopped·episode 를 올린다.
    2초 비면 clear. 남은 정지 지점 1.2m 안·감지 끔(suppress) 이면 보지 않는다. 라이다 점 판정은 별도 시뮬
    (sim/out/obstacle_nos/obstacle_nos_sim.py)에서 본다.
가정(추정, 실기 미확인)
    감지가 꺼져 있으면(현행) 사람 앞에서 로봇 자체 기능이 실행 상태(3)로 제자리에 선다 = robot.hold.
    운영자 '다시 출발'(감지 끔) 뒤 지도에 없는 의자는 로봇 자체 회피로 지나간다 = 막아도 서지 않음.
"""
import math
import os

from harness import scenario

_base_path = os.path.join(os.path.dirname(__file__), "standby_scenario.py")
import importlib.util  # noqa: E402

from harness import REGISTRY  # noqa: E402

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("standby_scenario_base_obstacle", _base_path)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
del REGISTRY[_n:]
Stage, CLINIC, _no_err = _base.Stage, _base.CLINIC, _base._no_err

ON = {"OBSTACLE_GUARD_ENABLED": "1"}
ON_MP = {**ON, "NAV_MULTIPOINT_ENABLED": "true"}
STOP = "앞쪽에 장애물이 있어 잠시 멈추겠습니다. 그 자리에서 기다려 주세요."
RESUME = "다시 출발하겠습니다."
STAFF = "직원을 불렀습니다. 잠시만 기다려 주세요."
WAIT = "아직 앞이 막혀 있습니다. 조금 더 기다리겠습니다."
ASK = "이제 다시 이동할 수 있습니다. 준비되시면 '준비됐어요'라고 말씀해 주세요."
DEMO = {**{"OBSTACLE_GUARD_ENABLED": "1"}, "OBSTACLE_ANNOUNCE_SEC": "1.5", "OBSTACLE_ASK_AFTER_SEC": "0"}


class FakeVoice:
    """가짜 Jetson — obstacle.ask 가 새로 뜨면 delay 초 뒤 answers 의 다음 대답을 보낸다(yes|no|none, None=안 보냄)."""

    def __init__(self, sim, answers, delay=6.0):
        self.sim, self.answers, self.delay, self.done, self.sent = sim, list(answers), delay, set(), []
        from app.navigation import obstacle_hold as oh
        self.oh = oh
        self._loop()

    def _loop(self):
        ask = (self.oh.snapshot() or {}).get("ask")
        if ask and ask["id"] not in self.done:
            self.done.add(ask["id"])
            ans = self.answers.pop(0) if self.answers else "none"
            if ans is not None:
                aid = ask["id"]
                self.sim.clock.after(self.delay, lambda: self.sent.append((aid, ans, self.oh.submit_answer(aid, ans))))
        self.sim.clock.after(0.5, self._loop)


class FakeGuard:
    K_ON, T_CLEAR, GOAL_M, DT = 0.3, 2.0, 1.2, 0.1
    KIND = None                           # 정지 중 답에 싣는 구분(C3 감지 노드) — None 이면 옛 노드처럼 안 실음

    def __init__(self, sim, blocked):
        self.sim, self.robot, self.clock = sim, sim.robot, sim.clock
        self.blocked = blocked            # () -> bool : 지금 앞을 막고 있는가(세계)
        self.armed_until = -1e9
        self.suppress = False
        self.stops = []
        self.state, self.stopped, self.episode = "unknown", False, 0
        self.on_since = self.clear_since = None
        self.cancels = 0
        self.arms = 0
        self.arm_log = []
        self.record = False
        self.records = 0
        self.notices = []
        self.notice_t = []
        self.end_t = None
        self.wired = False
        try:
            from app.navigation import obstacle_hold as oh
        except ImportError:
            oh = None
        if oh is not None:
            sim.rewire_late_modules()      # 늦게 불러온 모듈 — 가상 시계·스레드 위에서 돌게
            oh._exchange = self.handle
            orig = oh._say

            def say(text):
                self.notices.append(text)
                self.notice_t.append((round(self.clock.elapsed(), 2), text))
                if text.startswith("앞이 계속 막혀"):
                    self.end_t = round(self.clock.elapsed(), 1)
                orig(text)
            oh._say = say
            self.wired = True
        self._loop()

    def _loop(self):
        """실제 노드처럼 10Hz 로 판정한다 — 하네스 시계는 sleep 길이만큼 건너뛰므로 사건으로 0.1초마다 깨운다."""
        self.tick(self.DT)
        self.clock.after(self.DT, self._loop)

    def handle(self, req):
        if not self.robot.comm_ok():
            return None
        self.arms += 1
        self.arm_log.append(round(self.clock.elapsed(), 1))
        self.armed_until = self.clock.now() + req.get("ttl", 3.0)
        self.suppress = bool(req.get("suppress"))
        self.record = bool(req.get("record"))
        self.records += self.record
        self.stops = req.get("stops") or []
        return {"armed": True, "recordOnly": self.record, "state": self.state,
                "stopped": self.stopped and not self.record, "episode": self.episode, "lastDist": 1.3,
                **({"kind": self.KIND if self.stopped else None} if self.KIND else {})}

    def tick(self, dt):
        now, r = self.clock.now(), self.robot
        world = self.blocked()
        # 세계: 막혀 있으면 로봇은 앞으로 못 간다. 감지를 끈 운영자 재출발은 로봇 자체 회피로 지나간다(가정).
        if world and not self.suppress:
            r.hold_until = max(r.hold_until, now + self.DT + 0.05)
        armed = now < self.armed_until
        if not armed:
            self.state, self.stopped, self.on_since, self.clear_since = "unknown", False, None, None
            return
        near = any(math.hypot(r.x - s[0], r.y - s[1]) < self.GOAL_M for s in self.stops)
        seen = world and not self.suppress and not near
        if seen:
            self.clear_since = None
            self.on_since = self.on_since if self.on_since is not None else now
            if now - self.on_since >= self.K_ON:
                self.state = "blocked"
                if not self.stopped:
                    self.stopped, self.episode = True, self.episode + 1
                    if not self.record:          # 기록만(관제 MODE=record) — 판단만 하고 취소하지 않는다
                        self.cancels += 1
                        r.cmd("CANCEL_NAV")
        else:
            self.on_since = None
            if self.state != "clear":
                self.clear_since = self.clear_since if self.clear_since is not None else now
                if now - self.clear_since >= self.T_CLEAR:
                    self.state, self.stopped = "clear", False


class Block:
    """trigger() 가 처음 참이 된 순간부터 sec 초 동안 앞을 막는다(sec=None 이면 계속)."""

    def __init__(self, sim, trigger, sec):
        self.sim, self.trigger, self.sec, self.t0 = sim, trigger, sec, None

    def __call__(self):
        now = self.sim.clock.now()
        if self.t0 is None:
            if not self.trigger():
                return False
            self.t0 = now
        if self.sec is None:
            return self.trigger()          # 자리에 붙은 물체 — 그 구간에 있는 동안만
        return now - self.t0 < self.sec


def _count(sim, kind):
    return sum(1 for _t, k, _m in sim.events if k == kind)


def _guide(sim, block_sec, trigger=None, events=(), max_sec=240, answers=None):
    """안내 세션에서 내과로 이동. events: [(막힘 시작 뒤 초, fn(stage))]. answers: 가짜 Jetson 대답 목록."""
    st = Stage(sim)
    st.arrive_standby()
    trig = trigger or (lambda: sim.robot.y >= 2.0)
    blk = Block(sim, trig, block_sec)
    guard = FakeGuard(sim, blk)
    FakeVoice(sim, answers or [])        # 현행 관제엔 질문이 없어 아무것도 안 한다
    sid = st.operator_start()
    db = st.Session()
    try:
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
    finally:
        db.close()
    pending = sorted(events, key=lambda e: e[0])

    def check():
        while pending and blk.t0 is not None and sim.clock.now() - blk.t0 >= pending[0][0]:
            pending.pop(0)[1](st)
        if pending:
            sim.clock.after(0.1, check)
    check()
    st.drive(until=lambda: not st.sm.is_navigating, max_sec=max_sec)
    return st, sid, guard, blk


def _resume_lead(sim, guard):
    """'다시 출발할게요' 에서 그 뒤 첫 재전송(NAV)까지 초 — 출발 전에 들리는가."""
    t_say = [t for t, x in guard.notice_t if x == RESUME]
    if not t_say:
        return None
    navs = [t for t, c in sim.robot.timeline if c.startswith("NAV") and t >= t_say[-1] - 0.01]
    return round(navs[0] - t_say[-1], 1) if navs else None


def _stop_say_after(sim, guard):
    """CANCEL_NAV(감지 노드 정지)에서 멈춤 멘트까지 초."""
    t_stop = [t for t, c in sim.robot.timeline if c == "CANCEL_NAV"]
    t_say = [t for t, x in guard.notice_t if x == STOP]
    return round(t_say[0] - t_stop[0], 1) if t_stop and t_say else None


def _out(st, sid, guard, blk, **extra):
    from app.navigation import nav_hold
    sim = st.sim
    arrived = [(round(a[1], 1), round(a[2], 1)) for a in sim.robot.arrivals]
    return {"notices": guard.notices, "cancels": guard.cancels, "wired": guard.wired, "arm_log": guard.arm_log[:12],
            "arrived_clinic": tuple(CLINIC) in arrived, "navigating": bool(st.sm.is_navigating),
            "hold_open": nav_hold.get_hold() is not None,
            "blocked_alert": _count(sim, "guide_nav_blocked"), "staff_alert": _count(sim, "guide_obstacle_staff"),
            "cleared_alert": _count(sim, "guide_obstacle_cleared"),
            "resume_lead": _resume_lead(sim, guard), "stop_say_after": _stop_say_after(sim, guard),
            "notes": list(sim.notes), "closed_alert": _count(sim, "guide_obstacle_closed"),
            # 대기 장소·내과 첫 이동을 뺀 다시 보내기 횟수
            "resends": sum(1 for c in sim.robot.cmds if c.startswith("NAV")) - 2,
            "sess": st.session(sid)["Result"],
            "block_t0": None if blk.t0 is None else round(blk.t0 - sim.clock.t0, 1), **extra}


# ── 사람이 막음 ───────────────────────────────────────

@scenario("O1 사람이 3초 서 있다 비킴 → 멈춤 멘트(정지 뒤 1초 안) → '다시 출발할게요' 1초 넘게 뒤 재전송 → 내과 도착",
          env=ON,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["cancels"] == 1
          and r["notices"] == [STOP, RESUME] and r["blocked_alert"] == 0 and r["staff_alert"] == 0
          and r["stop_say_after"] is not None and r["stop_say_after"] <= 1.0
          and r["resume_lead"] is not None and r["resume_lead"] >= 1.0)
def o1(sim):
    return _out(*_guide(sim, 3.0))


@scenario("O2 지나가는 사람 1초 → 멈춤 멘트(멈추면 바로라 나옴) → '다시 출발할게요' 뒤 출발·도착",
          env=ON,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["cancels"] == 1 and r["notices"] == [STOP, RESUME]
          and r["resume_lead"] is not None and r["resume_lead"] >= 1.0)
def o2(sim):
    return _out(*_guide(sim, 1.0))


@scenario("O3 40초 막힘 → 30초 '조금 더 기다리겠습니다' → 풀리자 긴 정지라 '준비됐어요' 질문 → 손님 '네' → '다시 출발하겠습니다'·도착",
          env=ON,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["notices"] == [STOP, WAIT, ASK, RESUME]
          and r["staff_alert"] == 0 and not r["hold_open"] and r["blocked_alert"] == 0)
def o3(sim):
    return _out(*_guide(sim, 40.0, answers=["yes"]))


def _chair(sim):
    """지도에 없는 의자 — 경로 y 2~3m 구간에서만 앞을 막는다(지나가면 끝)."""
    return lambda: 2.0 <= sim.robot.y < 3.0


def _operator(action):
    def go(st):
        from app.navigation import nav_hold
        st.sim.notes.append(f"운영자 {action}: {nav_hold.request(action)}")
    return go


@scenario("O4 지도에 없는 의자(계속 막힘) → 2분 직원 호출 → 150초 운영자 '다시 출발' 은 앞이 막혀 거절 → 그 자리 대기(내과 안 감)",
          env=ON,
          expect=lambda r: _no_err(r) and not r["arrived_clinic"] and r["cancels"] == 1 and r["staff_alert"] == 1
          and r["hold_open"] and r["notices"][:3] == [STOP, WAIT, STAFF] and RESUME not in r["notices"]
          and any("앞이 아직 막혀" in n for n in r["notes"]))
def o4(sim):
    return _out(*_guide(sim, None, trigger=_chair(sim), events=[(150.0, _operator("retry"))]))


@scenario("O5 계속 막힘 → 2분 직원 호출 → 150초 운영자 '안내 종료' → 세션 운영자 종료·내과 안 감(근무 중이라 대기 장소로 복귀 이동)",
          env=ON,
          expect=lambda r: _no_err(r) and not r["arrived_clinic"] and r["sess"] == "aborted_operator"
          and not r["hold_open"] and r["staff_alert"] == 1)
def o5(sim):
    return _out(*_guide(sim, None, events=[(150.0, _operator("abort"))], max_sec=240))


@scenario("O6 20초 막힘 중 통신 6초 두절(감지 노드 3초 뒤 스스로 꺼짐) → 두절 동안 다시 보내지 않음 → 풀린 뒤 1번 출발·도착",
          env=ON,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["cancels"] >= 1
          and r["notices"] == [STOP, RESUME] and r["resends"] == 1)
def o6(sim):
    return _out(*_guide(sim, 20.0, events=[(5.0, lambda st: st.sim.robot.drop_comm(6.0))]))


@scenario("O7 목적지 1m 앞에서 사람 3초 → 감지 안 함(도착은 로봇 자체 회피) → 도착, 정지 없음",
          env=ON,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["cancels"] == 0 and r["notices"] == [])
def o7(sim):
    return _out(*_guide(sim, 3.0, trigger=lambda: sim.robot.y >= 5.0))


# ── 회귀 ──────────────────────────────────────────────

@scenario("O8 기능 끔(OBSTACLE_GUARD_ENABLED 없음) → 지금과 같음: 감지 신호 0, 정지 0, 멘트 0",
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["cancels"] == 0 and r["notices"] == []
          and r["arms"] == 0)
def o8(sim):
    st, sid, guard, blk = _guide(sim, 3.0)
    return _out(st, sid, guard, blk, arms=guard.arms)


@scenario("O9 안내가 아닌 직접 이동에서 사람 3초 → 감지 신호 0, 정지 0 (지금과 같음)",
          env=ON,
          expect=lambda r: _no_err(r) and r["pos"] == [6, 0] and r["cancels"] == 0 and r["arms"] == 0)
def o9(sim):
    guard = FakeGuard(sim, Block(sim, lambda: sim.robot.x >= 2.0, 3.0))
    sim.move([(6, 0)])
    return {"cancels": guard.cancels, "arms": guard.arms, "notices": guard.notices}


@scenario("O10 다지점 안내 묶음 중간에서 5초 막힘 → 남은 점부터 다시 보내 끝점 도착",
          env=ON_MP,
          expect=lambda r: _no_err(r) and r["pos"] == [9, 0] and r["cancels"] == 1
          and r["notices"] == [STOP, RESUME] and r["multi_sent"][-1] < r["multi_sent"][0])
def o10(sim):
    sim.pl._guide_move_active = lambda: True
    sim.pl._scenario_move_active = lambda: True      # 10-29 부터 감지는 안내 시나리오 이동에서만
    guard = FakeGuard(sim, Block(sim, lambda: sim.robot.x >= 4.0, 5.0))
    sim.move([(3, 0, True), (6, 0, True), (9, 0)])
    return {"cancels": guard.cancels, "notices": guard.notices}


# ── 직원 호출 뒤 (10-29 추가: 45초마다 대기 안내 · 10분 대응 없으면 종료) ─────────

END = "앞이 계속 막혀 안내를 마칠게요. 가까운 직원에게 문의해 주세요."


@scenario("O11 3분 막힘 → 2분 직원 호출(45초 반복) → 180초에 풀려도 출발 안 함 → 200초 운영자 '다시 출발' → 출발·도착",
          env=ON, limit_sec=900,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["notices"] == [STOP, WAIT, STAFF, STAFF, RESUME]
          and r["closed_alert"] == 0 and not r["hold_open"] and any("'ok': True" in n for n in r["notes"]))
def o11(sim):
    st, sid, guard, blk = _guide(sim, 180.0, events=[(200.0, _operator("retry"))], max_sec=400)
    return _out(st, sid, guard, blk)


@scenario("O12 계속 막힘·운영자 대응 없음 15분 → 종료하지 않고 그 자리 대기(세션 진행 중·확인창 열림·45초 대기 안내 반복)",
          env=ON, limit_sec=1500,
          expect=lambda r: _no_err(r) and not r["arrived_clinic"] and r["sess"] == "running" and r["hold_open"]
          and r["notices"][:3] == [STOP, WAIT, STAFF] and r["notices"].count(STAFF) >= 15
          and END not in r["notices"] and r["closed_alert"] == 0)
def o12(sim):
    return _out(*_guide(sim, None, max_sec=950))


@scenario("O13 직원 호출 뒤 330초에 풀림 → 출발 안 함 → 360초 운영자 '다시 출발' → 출발·도착",
          env=ON, limit_sec=900,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["closed_alert"] == 0
          and END not in r["notices"] and r["notices"][-1] == RESUME and not r["hold_open"]
          and any("'ok': True" in n for n in r["notes"]))
def o13(sim):
    return _out(*_guide(sim, 330.0, events=[(360.0, _operator("retry"))], max_sec=600))


# ── 안내 시나리오에서만 · 기록 모드 (10-29 추가) ─────────────────────────

REC = {"OBSTACLE_GUARD_MODE": "record"}


@scenario("O14 기록 모드(MODE=record) — 시나리오 이동 중 감지 노드를 '기록만'으로 켬 → 사람 3초: 취소·멘트 없이 지금처럼 도착",
          env=REC,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["records"] > 0 and r["cancels"] == 0
          and r["notices"] == [] and r["episodes"] == 1)
def o14(sim):
    st, sid, guard, blk = _guide(sim, 3.0)
    return _out(st, sid, guard, blk, records=guard.records, episodes=guard.episode)


@scenario("O15 상시 대기 단발 응대(시나리오 없음) 이동에서 사람 3초 — MODE=on 이어도 감지 신호 0 (안내 시나리오에서만)",
          env=ON,
          expect=lambda r: _no_err(r) and r["arms"] == 0 and r["cancels"] == 0 and r["notices"] == [])
def o15(sim):
    import importlib.util as _u
    from harness import REGISTRY as _R
    n = len(_R)
    spec = _u.spec_from_file_location("obstacle_single_base", os.path.join(os.path.dirname(__file__),
                                                                         "standby_single_no_limit.py"))
    mod = _u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    del _R[n:]
    st = mod.SingleStage(sim, limit_min=20)
    st.arrive_standby()
    guard = FakeGuard(sim, Block(sim, lambda: sim.robot.y >= 2.0, 3.0))
    sid = st.call().get("sessionId")
    st.drive(until=lambda: not st.sm.is_navigating or sim.robot.y >= 5.9, max_sec=120)
    return {"arms": guard.arms, "cancels": guard.cancels, "notices": guard.notices, "pos_y": round(sim.robot.y, 1),
            "sid": sid}


# ── 전방 카메라 기록 단계 C1 (10-29) — 카메라 서비스에 켜기 신호 ─────────────

CAM = {**REC, "OBSTACLE_CAMERA_ENABLED": "1"}


class CamTap:
    """obstacle_hold 의 socket 을 가로채 카메라 서비스로 간 신호를 모은다(감지 노드 쪽은 FakeGuard 가 받는다)."""

    def __init__(self):
        import json as _j
        import types as _t
        self.msgs = []
        try:
            from app.navigation import obstacle_hold as oh
        except ImportError:
            return
        tap = self

        class _Sock:
            def __init__(self, *a, **k):
                pass

            def sendto(self, data, addr):
                tap.msgs.append((addr, _j.loads(data.decode())))
                return len(data)

            def settimeout(self, *_):
                pass

            def recvfrom(self, *_):
                raise OSError("tap")

            def close(self):
                pass
        oh.socket = _t.SimpleNamespace(socket=_Sock, AF_INET=2, SOCK_DGRAM=2)


@scenario("O16 카메라 켬(+record) — 시나리오 이동 중 카메라 서비스에 켜기 신호(위치·record·응답 안 기다림), 감지 판정은 그대로",
          env=CAM,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["cam_n"] > 3 and r["cam_pose"] and r["cam_record"]
          and r["cam_noreply"] and r["cam_addr"] == ["vision", 40110] and r["cancels"] == 0 and r["notices"] == [])
def o16(sim):
    tap = CamTap()
    st, sid, guard, blk = _guide(sim, 3.0)
    m = [x for a, x in tap.msgs]
    return _out(st, sid, guard, blk, cam_n=len(m), cam_pose=all(x.get("pose") for x in m) if m else False,
                cam_record=all(x.get("record") for x in m) if m else False,
                cam_noreply=all(x.get("reply") is False for x in m) if m else False,
                cam_addr=list(tap.msgs[0][0]) if tap.msgs else None)


@scenario("O17 카메라 켬 — 안내가 아닌 직접 이동에서는 카메라 신호 0",
          env=CAM,
          expect=lambda r: _no_err(r) and r["pos"] == [6, 0] and r["cam_n"] == 0)
def o17(sim):
    tap = CamTap()
    FakeGuard(sim, Block(sim, lambda: sim.robot.x >= 2.0, 3.0))
    sim.move([(6, 0)])
    return {"cam_n": len(tap.msgs)}


@scenario("O18 카메라 끔(기본) — 시나리오 이동이어도 카메라 신호 0 (지금과 같음)",
          env=REC,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["cam_n"] == 0)
def o18(sim):
    tap = CamTap()
    st, sid, guard, blk = _guide(sim, 3.0)
    return _out(st, sid, guard, blk, cam_n=len(tap.msgs))


# ── 정지 구분(C3, 10-29) — 동작은 같고 알림에 '막은 것' 을 싣는다 ─────────────

def _staff_msg(sim):
    return next((m for _t, k, m in sim.events if k == "guide_obstacle_staff"), "")


def _with_kind(kind, fn):
    FakeGuard.KIND = kind
    try:
        return fn()
    finally:
        FakeGuard.KIND = None


@scenario("O19 의자(구분 object) 계속 막힘 → O4 와 같음(다시 출발 거절·대기), 직원 호출 알림에 '사람 아닌 물체'",
          env=ON,
          expect=lambda r: _no_err(r) and not r["arrived_clinic"] and r["staff_alert"] == 1 and r["hold_open"]
          and r["notices"][:3] == [STOP, WAIT, STAFF] and "막은 것: 사람 아닌 물체" in r["staff_msg"])
def o19(sim):
    out = _with_kind("object", lambda: _out(*_guide(sim, None, trigger=_chair(sim), events=[(150.0, _operator("retry"))])))
    return {**out, "staff_msg": _staff_msg(sim)}


@scenario("O20 옛 감지 노드(구분 없음) 150초 막힘 → 2분 직원 호출 → 160초 운영자 '다시 출발' → 도착, 알림에 '구분 못 함'",
          env=ON,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["notices"][:3] == [STOP, WAIT, STAFF]
          and r["notices"][-1] == RESUME and r["staff_alert"] == 1 and "막은 것: 구분 못 함" in r["staff_msg"])
def o20(sim):
    out = _out(*_guide(sim, 150.0, events=[(160.0, _operator("retry"))]))
    return {**out, "staff_msg": _staff_msg(sim)}


def _resume_msg(sim):
    return next((m for _t, k, m in sim.events if k == "guide_obstacle_resume"), "")


@scenario("O21 사람(구분 person) 3초 막힘 → 재출발 기록에 '막은 것: 사람' (현행: 풀린 뒤 답을 읽어 '구분 None')",
          env=ON,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["notices"] == [STOP, RESUME]
          and "막은 것: 사람" in r["resume_msg"])
def o21(sim):
    out = _with_kind("person", lambda: _out(*_guide(sim, 3.0)))
    return {**out, "resume_msg": _resume_msg(sim)}


@scenario("O22 90초 막힘 → 30초 대기 멘트 1번만·관제 알림 0 → 풀리자 질문 → '네' → 출발·도착",
          env=ON,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["notices"] == [STOP, WAIT, ASK, RESUME]
          and r["staff_alert"] == 0 and r["cleared_alert"] == 0)
def o22(sim):
    return _out(*_guide(sim, 90.0, answers=["yes"]))


@scenario("O23 직원 호출 뒤 막힌 채 운영자 '다시 출발'(130초) → 거절·로봇 그대로 → 200초 풀림 → 210초 다시 누르면 출발·도착",
          env=ON, limit_sec=900,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["notices"][-1] == RESUME
          and sum("앞이 아직 막혀" in n for n in r["notes"]) == 1 and sum("'ok': True" in n for n in r["notes"]) == 1)
def o23(sim):
    return _out(*_guide(sim, 200.0, events=[(130.0, _operator("retry")), (210.0, _operator("retry"))], max_sec=500))


class Flicker:
    """trigger 뒤 sec 초 막고, 첫 '다시 출발할게요' 0.2초 뒤부터 again 초 다시 막는다(출발 대기 중 다시 막히는 장면)."""

    def __init__(self, sim, trigger, sec, again):
        self.sim, self.trigger, self.sec, self.again, self.t0 = sim, trigger, sec, again, None
        self.guard = None

    def __call__(self):
        now = self.sim.clock.now()
        if self.t0 is None:
            if not self.trigger():
                return False
            self.t0 = now
        if now - self.t0 < self.sec:
            return True
        said = [t for t, x in (self.guard.notice_t if self.guard else []) if x == RESUME]
        if not said:
            return False
        d = self.sim.clock.elapsed() - said[0]
        return 0.2 <= d < 0.2 + self.again


@scenario("O24 5초 막힘 → 풀려 '다시 출발할게요' → 0.2초 뒤 다시 막힘(4초, 감지 노드 정지 2번째) → 출발 거둠 → 다시 풀려 멘트 뒤 출발·도착(재전송 1번)",
          env=ON,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["cancels"] == 2 and r["resends"] == 1
          and r["notices"].count(RESUME) == 2)
def o24(sim):
    st = Stage(sim)
    st.arrive_standby()
    blk = Block(sim, lambda: False, None)
    fl = Flicker(sim, lambda: sim.robot.y >= 2.0, 5.0, 4.0)
    guard = FakeGuard(sim, fl)
    fl.guard = guard
    sid = st.operator_start()
    db = st.Session()
    try:
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
    finally:
        db.close()
    st.drive(until=lambda: not st.sm.is_navigating, max_sec=240)
    blk.t0 = fl.t0
    return _out(st, sid, guard, blk)


# ── 시연 대본 19~21 (10-30) ─────────────────────────────

@scenario("O25 시연 설정 · 3초 막힘 → 1.5초 멈춤 멘트 → 풀리자 '준비됐어요' 질문(늘 묻기) → '네' → '다시 출발하겠습니다'·도착",
          env=DEMO,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["notices"] == [STOP, ASK, RESUME]
          and r["resume_lead"] is not None and r["resume_lead"] >= 1.0)
def o25(sim):
    return _out(*_guide(sim, 3.0, answers=["yes"]))


@scenario("O26 시연 설정 · 지나가는 사람 1초 → 멈춤 멘트·질문 없이 다시 출발(대본 비고 '짧은 정지는 멘트 없이')",
          env=DEMO,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["notices"] == [] and r["cancels"] == 1)
def o26(sim):
    return _out(*_guide(sim, 1.0, answers=["yes"]))


@scenario("O27 긴 정지(40초) 풀림 → 질문 무응답 → 한 번 더 질문 → '네' → 출발·도착",
          env=ON,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["notices"] == [STOP, WAIT, ASK, ASK, RESUME]
          and r["staff_alert"] == 0)
def o27(sim):
    return _out(*_guide(sim, 40.0, answers=["none", "yes"]))


@scenario("O28 긴 정지 풀림 → 두 번 다 무응답 → 직원 호출(관제 알림·확인창) → 자동 출발 없음",
          env=ON,
          expect=lambda r: _no_err(r) and not r["arrived_clinic"] and r["notices"][:5] == [STOP, WAIT, ASK, ASK, STAFF]
          and r["staff_alert"] == 1 and r["hold_open"] and r["resends"] == 0)
def o28(sim):
    return _out(*_guide(sim, 40.0, answers=["none", "none"], max_sec=150))


@scenario("O29 긴 정지 풀림 → Jetson 대답이 안 옴(끊김) → 20초 뒤 다시 묻고 또 안 오면 직원 호출",
          env=ON,
          expect=lambda r: _no_err(r) and not r["arrived_clinic"] and r["notices"].count(ASK) == 2
          and r["staff_alert"] == 1 and r["resends"] == 0)
def o29(sim):
    return _out(*_guide(sim, 40.0, answers=[None, None], max_sec=160))


@scenario("O30 시연 설정 · 질문 중 다시 막힘 → 말없이 정지로 → 다시 풀리면 다시 질문 → '네' → 출발·도착",
          env=DEMO,
          expect=lambda r: _no_err(r) and r["arrived_clinic"] and r["notices"].count(ASK) == 2
          and r["notices"][-1] == RESUME and r["notices"].count(STOP) == 1 and r["resends"] == 1)
def o30(sim):
    st = Stage(sim)
    st.arrive_standby()
    fl = Flicker2(sim, lambda: sim.robot.y >= 2.0, 5.0, 4.0)
    guard = FakeGuard(sim, fl)
    fl.guard = guard
    FakeVoice(sim, ["yes", "yes"], delay=8.0)   # 첫 질문의 대답은 다시 막힌 뒤 도착 → 버려져야 한다
    sid = st.operator_start()
    db = st.Session()
    try:
        st.vs.session_move(sid, place_id=2, db=db, _="sim")
    finally:
        db.close()
    st.drive(until=lambda: not st.sm.is_navigating, max_sec=240)
    blk = Block(sim, lambda: False, None)
    blk.t0 = fl.t0
    return _out(st, sid, guard, blk)


class Flicker2(Flicker):
    """첫 '준비됐어요' 질문 1초 뒤부터 again 초 다시 막는다."""

    def __call__(self):
        now = self.sim.clock.now()
        if self.t0 is None:
            if not self.trigger():
                return False
            self.t0 = now
        if now - self.t0 < self.sec:
            return True
        said = [t for t, x in (self.guard.notice_t if self.guard else []) if x == ASK]
        if not said:
            return False
        d = self.sim.clock.elapsed() - said[0]
        return 1.0 <= d < 1.0 + self.again
