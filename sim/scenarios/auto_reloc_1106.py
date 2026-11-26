"""측위 상실 자동 재측위(11-06) — 미확정·제자리면 관제가 스캔을 지도 전체와 맞춰 위치를 넣는다.

재현: 11-06 13:32~13:34 로봇은 대기장소(-3.64,5.89)에 서 있는데 NOS 는 (-6.7,0.84) 를 보고, 미확정('위치 급변 감지')
2분 넘게 유지 → 운영자가 재조정. 현행은 자동으로 아무것도 하지 않는다.

가짜로 둔 것(시나리오 안):
  - 스캔: 11-06 14:00~14:16 맵 47 에서 실제로 받은 라이다 스캔(_자료/2026-11-06_전역재측위). 그 장의 정답 = 받을 때 로봇 보고 위치.
  - 지도 탐색 계산: 하네스는 numpy·cv2 를 가짜로 바꾸므로, auto_reloc_eval.py 가 변경안 scan_match.global_locate 를
    실제 지도(맵 47)·실제 스캔으로 돌린 결과(sim/out/auto_reloc_fits.json)를 장마다 그대로 돌려준다.
    넣은 뒤 그 자리 점수 = 정답 자세에서 잰 실제 점수(정답 0.4m 밖이면 30%).
  - 위치 넣기: 넣은 곳이 실제 자세에서 1m·30° 안이면 실제 자세로 정합하고 Location=0(park_seed 와 같은 규칙).
    그보다 멀면 넣은 곳에 머문다(Location 은 converge_loc, 669 처럼 0 일 수도).
  - 상태 heartbeat 1초마다 auto_relocalize.check. 스캔 중계 켜기 3초, 스캔 1장 1초.
"""
import json
import math
import os

from harness import scenario

RID = 13
MAP, FLOOR = 47, 1
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DATA = os.path.join(_ROOT, "_자료", "2026-11-06_전역재측위")
_MAPS = os.path.join(_ROOT, "Backend", "static", "maps")
LOST_REASON = "위치 급변 감지"


_FITS_PATH = os.path.join(_ROOT, "sim", "out", "auto_reloc_fits.json")
FITS = json.load(open(_FITS_PATH, encoding="utf-8")) if os.path.exists(_FITS_PATH) else {}


def _scans(name):
    keys = sorted((k for k in FITS if k.startswith(name + "#")), key=lambda k: int(k.split("#")[1]))
    t = FITS[keys[0]]["truth"] if keys else (0.0, 0.0, 0.0)
    return keys, (t[0], t[1], t[2])


STANDBY = _scans("scan_standby.json")      # 로봇대기장소
CLINIC = _scans("scan_spot2.json")         # 진료대기장소
DESK = _scans("scan_spot3.json")           # 원무과 접수(2등 75~78%)
WRONG_REPORT = (-6.7, 0.84, -3.13)         # 11-06 13:32~13:34 NOS 가 보고한 틀린 좌표


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def _fake_locate(f, key):
    r = FITS[key]
    return {"ok": r["ok"], "reason": r["reason"], "fit": r["fit"], "gap": r["gap"] or 0.0, "n": 250}


class Stage:
    def __init__(self, sim, scans, truth, report=WRONG_REPORT, reason=LOST_REASON, report_loc=0,
                 converge_loc=1, docked=False, walking=False):
        import app.robot_control.auto_init_pose as aip
        self.sim, self.aip = sim, aip
        try:
            import app.robot_control.auto_relocalize as ar
        except ImportError:
            ar = None
        sim.rewire_late_modules()
        self.ar = ar
        self.truth = truth
        self.belief = list(report)
        self.location = report_loc
        self.converge_loc = converge_loc
        self.walking = walking
        if not scans:
            self.scan_sets = [[]]
        else:
            self.scan_sets = scans if isinstance(scans[0], list) else [scans]
        self.captures = 0
        self.relays = 0
        self.on_inject = None
        self.sent = []
        self.inject_reply = "ok"
        self.no_converge = False
        sim.robot.charging = docked
        rt = sim.rt
        with rt._lock:
            rt._runtime[RID] = {"robot_id": RID, "robot_name": "SIM", "current_floor_id": FLOOR,
                                "current_map_id": MAP, "position": {}, "last_heartbeat": 1,
                                "charge_state": {"state": 2 if docked else 0}}
        rt.get_position = lambda rid: {"x": self.belief[0], "y": self.belief[1], "yaw": self.belief[2],
                                       "location": self.location, "timestamp": sim.clock.now()}
        rt.set_initpose_pending(RID, True, reason)
        aip._send_init_pose_via_receiver = self._send
        if ar is not None:
            from app.robot_control import scan_match as sm
            ar._relay_on = self._relay_on
            ar._relay_off = lambda: None
            ar._grab = self._grab
            ar._field = lambda rid: (object(), MAP)
            sm.global_locate = _fake_locate
            sm.to_laser = lambda key: key
            sm.score_pose = self._score_at
        self._beat()

    def _relay_on(self):
        self.relays += 1
        self.sim.clock.sleep(3.0)

    def _grab(self, n):
        self.sim.clock.sleep(1.0 * n)
        sets = self.scan_sets[self.captures % len(self.scan_sets)]
        self.captures += 1
        if not sets:
            return []
        return [sets[i % len(sets)] for i in range(n)] if len(self.scan_sets) == 1 else \
            [self.scan_sets[(self.captures - 1 + i) % len(self.scan_sets)][0] for i in range(n)]

    def _score_at(self, f, key, x, y, yaw):
        r = FITS.get(key) or {}
        near = math.hypot(x - self.truth[0], y - self.truth[1]) <= 0.4
        return {"score": (r.get("at_truth") or 0.0) if near else 30.0, "mean_err": 0.0}

    def _send(self, pose):
        self.sent.append((round(pose["PosX"], 2), round(pose["PosY"], 2)))
        if self.on_inject:
            self.on_inject()
        if self.inject_reply != "ok":
            return self.inject_reply
        if self.no_converge:
            self.location = 1
            return "ok"
        seed = (pose["PosX"], pose["PosY"], pose["Yaw"])
        d = math.hypot(seed[0] - self.truth[0], seed[1] - self.truth[1])
        if d <= 1.0 and abs(_wrap(seed[2] - self.truth[2])) <= math.radians(30):
            self.belief, self.location = list(self.truth), 0
        else:
            self.belief, self.location = list(seed), self.converge_loc
        return "ok"

    def _beat(self):
        if self.walking:
            self.belief[0] += 0.8          # 1초에 0.8m 걷는 중
        if self.ar is not None:
            self.ar.check(RID)
        self.sim.clock.after(1.0, self._beat)

    def go(self, sec=90, confirm_at=None):
        if confirm_at is not None:
            self.sim.at(confirm_at, lambda: self.confirm.append(self.aip.confirm_current_position_manual(RID)))
        self.confirm = []
        self.sim.run(lambda: self.sim.clock.sleep(sec), grace=0.0)
        rt = self.sim.rt
        err = math.hypot(self.belief[0] - self.truth[0], self.belief[1] - self.truth[1])
        st = (self.ar._state.get(RID) or {}) if self.ar else {}
        return {
            "pending": rt.is_initpose_pending(RID),
            "reason": rt.get_initpose_reason(RID),
            "injects": list(self.sent),
            "alerts": sum(1 for _, k, _m in self.sim.events if k == "robot_initpose_manual_needed"),
            "captures": self.captures,
            "relays": self.relays,
            "logs": sum(1 for _, k, _m in self.sim.events if k == "robot_initpose_auto_reloc"),
            "tries": st.get("tries", 0),
            "err_m": round(err, 2),
            "confirm": [c[0] for c in self.confirm],
        }


DONE = "위치 확인 필요(자동 재측위 완료 — '완료'로 확정)"
FAIL = "위치 확인 필요(자동 재측위 실패)"


def _exp(injects=None, pending=None, reason=None, alerts=None, err_max=None, tries=None, confirm=None,
         relays=None, logs=None):
    def ok(r):
        good = True
        if relays is not None:
            good &= r["relays"] == relays
        if logs is not None:
            good &= r["logs"] == logs
        if injects is not None:
            good &= len(r["injects"]) == injects
        if pending is not None:
            good &= r["pending"] == pending
        if reason is not None:
            good &= r["reason"] == reason
        if alerts is not None:
            good &= r["alerts"] == alerts
        if err_max is not None:
            good &= r["err_m"] <= err_max
        if tries is not None:
            good &= r["tries"] == tries
        if confirm is not None:
            good &= r["confirm"] == confirm
        return good or (f"injects={r['injects']} pending={r['pending']} reason={r['reason']!r} "
                        f"alerts={r['alerts']} logs={r['logs']} err={r['err_m']} tries={r['tries']} relays={r['relays']} conf={r['confirm']}")
    return ok


@scenario("A1 [재현] 11-06 13:32 대기장소에 서 있는데 (-6.7,0.84) 보고·급변 미확정 → 대기장소로 넣고 '완료' 대기",
          expect=_exp(injects=1, pending=True, reason=DONE, alerts=0, logs=1, err_max=0.05, relays=1))
def a1(sim):
    return Stage(sim, *STANDBY).go()


@scenario("A2 A1 뒤 운영자 '완료' → 확정(자율주행 허용)",
          expect=_exp(injects=1, pending=False, confirm=[True]))
def a2(sim):
    return Stage(sim, *STANDBY).go(sec=90, confirm_at=60)


@scenario("A3 측위 상실 보고(Location=1)·진료대기장소 → 넣고 수렴(Location 0) 뒤 '완료' 대기",
          expect=_exp(injects=1, pending=True, reason=DONE, alerts=0, logs=1, err_max=0.05))
def a3(sim):
    return Stage(sim, *CLINIC, reason="측위 상실(로봇 보고)", report_loc=1).go()


@scenario("A4 원무과 접수(옆 2.2m 가 75~78%) → 실제 스캔 점수 차 17 이라 채택",
          expect=_exp(injects=1, reason=DONE, err_max=0.05))
def a4(sim):
    return Stage(sim, *DESK).go()


@scenario("A5 걷는 중(1초 0.8m) → 시작 안 함", expect=_exp(injects=0, tries=0, reason=LOST_REASON))
def a5(sim):
    return Stage(sim, *STANDBY, walking=True).go(sec=60)


@scenario("A6 스캔 세 장이 다른 자리(대기장소·진료대기·대기장소) → 안 넣음, 60초마다 3번 뒤 5분 쉼",
          expect=_exp(injects=0, relays=3, reason=LOST_REASON))
def a6(sim):
    mixed = [STANDBY[0], CLINIC[0]]           # 장마다 번갈아
    return Stage(sim, mixed, STANDBY[1]).go(sec=240)


@scenario("A7 지도와 안 맞는 스캔(아무 점) → 안 넣음", expect=_exp(injects=0, reason=LOST_REASON))
def a7(sim):
    return Stage(sim, ["junk#0", "junk#1", "junk#2"], STANDBY[1]).go(sec=40)


@scenario("A8 스캔을 못 받음(중계 실패) → 안 넣음, 재시도", expect=_exp(injects=0, reason=LOST_REASON))
def a8(sim):
    return Stage(sim, [], STANDBY[1]).go(sec=40)


@scenario("A9 도크 위 미확정 → 충전소 재주입 몫이라 하지 않음", expect=_exp(injects=0, tries=0))
def a9(sim):
    st = Stage(sim, *STANDBY, docked=True)
    st.aip.check_and_recover_pending = lambda rid: None
    return st.go(sec=40)


@scenario("A10 층 이동 중(사전 맵 전환) → 하지 않음", expect=_exp(injects=0, tries=0))
def a10(sim):
    st = Stage(sim, *STANDBY, reason="층 이동 중(사전 맵 전환)")
    return st.go(sec=40)


@scenario("A11 운영자가 위치 재조정 창을 열어 둠 → 하지 않음", expect=_exp(injects=0, tries=0))
def a11(sim):
    from app.robot_control import relocalize
    relocalize._add_viewer(1)                # 시나리오마다 따로 도는 프로세스라 되돌리지 않는다
    return Stage(sim, *STANDBY).go(sec=40)


@scenario("A12 주행 중 → 하지 않음", expect=_exp(injects=0, tries=0))
def a12(sim):
    import app.navigation.send_move as nav
    nav.is_navigating = True
    return Stage(sim, *STANDBY).go(sec=40)


@scenario("A13 넣었지만 수렴 안 함(Location=1 유지) → '자동 재측위 실패' 알림, 다시 넣지 않음",
          expect=_exp(injects=1, pending=True, reason=FAIL, alerts=1))
def a13(sim):
    st = Stage(sim, *STANDBY)
    st.no_converge = True
    return st.go(sec=150)


@scenario("A14 위치 넣기 전달 실패(timeout) → 실패 알림", expect=_exp(injects=1, reason=FAIL, alerts=1))
def a14(sim):
    st = Stage(sim, *STANDBY)
    st.inject_reply = "timeout"
    return st.go(sec=60)


@scenario("A15 탐색 중 로봇이 스스로 회복(미확정 풀림) → 넣지 않음", expect=_exp(injects=0, pending=False))
def a15(sim):
    st = Stage(sim, *STANDBY)
    sim.at(8, lambda: sim.rt.set_initpose_pending(RID, False, by_localization=True))
    return st.go(sec=40)


@scenario("A16 AUTO_RELOCALIZE_ENABLED=0 → 지금처럼 아무것도 안 함", env={"AUTO_RELOCALIZE_ENABLED": "0"},
          expect=_exp(injects=0, tries=0, reason=LOST_REASON))
def a16(sim):
    return Stage(sim, *STANDBY).go(sec=40)


def _junk_then(sim, walk_at, stand_scans, walk=True):
    st = Stage(sim, ["junk#0", "junk#1", "junk#2"], STANDBY[1])

    def walk_start():
        st.walking = walk
    def walk_end():
        st.walking = False
        st.scan_sets = [stand_scans]
    sim.at(walk_at, walk_start)
    sim.at(walk_at + 6, walk_end)
    return st


@scenario("A17 [11-06 실제 흐름] 지도 밖에 서서 3번 실패 → 걸어서 대기장소로 돌아와 섬 → 바로 다시 탐색해 넣음",
          expect=_exp(injects=1, reason=DONE, err_max=0.05))
def a17(sim):
    return _junk_then(sim, 160, STANDBY[0]).go(sec=230)


@scenario("A18 지도 밖 3번 실패 뒤 보고 좌표가 굳어 움직임을 못 봄 → 5분 쉬고 다시 해서 넣음",
          expect=_exp(injects=1, reason=DONE, err_max=0.05))
def a18(sim):
    return _junk_then(sim, 160, STANDBY[0], walk=False).go(sec=520)


@scenario("A19 측위 상실(Location=1) 25초 뒤 로봇이 스스로 회복 → 손대지 않음(안내 회복 대기 30초 그대로)",
          expect=_exp(injects=0, relays=0, pending=False))
def a19(sim):
    st = Stage(sim, *STANDBY, reason="측위 상실(로봇 보고)", report_loc=1)

    def recover():
        st.location = 0
        sim.rt.set_initpose_pending(RID, False, by_localization=True)
    sim.at(25, recover)
    return st.go(sec=80)


@scenario("A20 측위 상실 사유인데 로봇이 정상(0)을 보고 중 → 곧 저절로 풀리므로 하지 않음",
          expect=_exp(injects=0, relays=0, reason="측위 상실(로봇 보고)"))
def a20(sim):
    return Stage(sim, *STANDBY, reason="측위 상실(로봇 보고)", report_loc=0).go(sec=80)


@scenario("A21 탐색 중 운영자가 재조정 창을 엶 → 넣지 않고 손 뗌", expect=_exp(injects=0, relays=1, reason=LOST_REASON))
def a21(sim):
    from app.robot_control import relocalize
    st = Stage(sim, *STANDBY)
    sim.at(22, lambda: relocalize._add_viewer(1))
    return st.go(sec=80)


@scenario("A22 넣은 직후 운영자가 직접 넣어 확정 → 자동 쪽은 아무것도 덮어쓰지 않음",
          expect=_exp(injects=1, pending=False, alerts=0, logs=0))
def a22(sim):
    st = Stage(sim, *STANDBY)
    st.on_inject = lambda: sim.at(0.5, lambda: sim.rt.set_initpose_pending(RID, False))
    return st.go(sec=80)


@scenario("A23 지도 밖에 10분 서 있음 → 중계 켜기 4번(60초×3 + 5분 뒤 1번)에서 멈춤",
          expect=_exp(injects=0, relays=4, reason=LOST_REASON))
def a23(sim):
    return Stage(sim, ["junk#0", "junk#1", "junk#2"], STANDBY[1]).go(sec=600)
