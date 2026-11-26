"""위치 급변 오경보·도크 재접속 오주입 수정(2026-11-06).

  sim\\run.bat sim\\scenarios\\jump_fix_1106.py --cand sim\\out\\cand_jumpfix

실기(11-06, 로봇 13·1F 맵 47):
- 10:27:03 진료대기장소로 가는 중 통신 1초 두절. 10:27:18 도착, 10:27:20 '위치 급변 5.54m'
  (이전=(-3.59,0.62), 보고=(-2.26,5.99)). 로봇 측위 기록(nos.pose.log)은 내내 이어졌고, (-2.26,5.99)는
  10:27:03 무렵 좌표다 → 늦게 도착한 UDP 옛 답을 읽은 것(추정, 근거 강함). 운영자 확정 전까지 안 풀림.
- 03:12·08:49 노트북 절전→깨어남. 로봇은 도크 위에서 켜진 채(부팅 번호·측위 PID 그대로)였는데
  '도크 재접속'으로 충전소 위치를 다시 주입.
변경:
- polling: UDP 대체 조회 전 버퍼 비우기, receiver timestamp 가 거꾸로 간 답 버림(3회 연속이면 기준 재설정),
  급변은 튀기 전 자리 기준으로 2번 연속일 때만 미확정, 확인 대기 표본은 반영 안 함,
  age 3초 넘는 표본은 급변 판정 안 함, '위치 급변' 미확정은 원래 자리 0.5m 안 10초면 자동 해제.
- send_move: 급변 확인 대기 중이면 이동 시작이 최대 2.5초 기다린 뒤 판정.
- auto_init_pose: 도크 재접속이라도 부팅 번호 같음·위치 원점 아님이면 주입 안 함.
가짜로 둔 것(실측 아님):
- UDP: 보낸 순간 receiver 의 최신 좌표를 답으로 만들고, 지정한 지연 뒤 수신 버퍼에 넣는다.
- TCP 실패: 지정한 시각 뒤 첫 POSITION 요청 한 번만 None(→ UDP 대체 조회).
- 주행 중 여부는 send_move.is_navigating 을 직접 켠다(실제 주행 없음).
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_here = os.path.dirname(__file__)


def _load(name, alias):
    n = len(REGISTRY)
    spec = importlib.util.spec_from_file_location(alias, os.path.join(_here, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    del REGISTRY[n:]
    return mod


rps = _load("reloc_pose_sync.py", "rps_base_jump1106")
rbl = _load("reboot_lost.py", "rbl_base_jump1106")
RID = rps.RID

A = (-3.59, 0.62, -1.52)    # 진료대기장소(도착 자리)
B = (-2.26, 5.99, 3.12)     # 통신 두절 때 지나던 자리


def _cand(sim) -> bool:
    return hasattr(sim.pl, "jump_suspect_active")


# ── 가짜 UDP(지연 답이 버퍼에 남는다) ──────────────────────────
class _UdpModule:
    AF_INET = 2
    SOCK_DGRAM = 2
    SOCK_STREAM = 1
    SOL_SOCKET = 1
    SO_REUSEADDR = 4
    timeout = TimeoutError

    def __init__(self, stage, delay_fn):
        self.stage, self.delay_fn = stage, delay_fn
        self.sent = 0
        self.stale_reads = 0

    def socket(self, *a, **k):
        mod, st = self, self.stage
        clock = st.clock

        class _Sock:
            def __init__(self):
                self.buf = []
                self.blocking = True

            def settimeout(self, t):
                self.blocking = True

            def setblocking(self, flag):
                self.blocking = bool(flag)

            def setsockopt(self, *a): pass
            def bind(self, *a): pass
            def close(self): pass

            def sendto(self, m, addr):
                mod.sent += 1
                reply = dict(st.latest)
                if st.latest_mono is not None:
                    reply["age"] = round(clock.now() - st.latest_mono, 3)
                reply["_sent_at"] = clock.elapsed()
                clock.after(mod.delay_fn(clock.elapsed()), lambda r=reply: self.buf.append(r))
                return len(m)

            def recvfrom(self, n):
                import json
                if not self.blocking:
                    if not self.buf:
                        raise BlockingIOError("sim: empty")
                else:
                    end = clock.now() + 2.0
                    while not self.buf and clock.now() < end:
                        clock.sleep(0.05)
                    if not self.buf:
                        raise TimeoutError("sim: udp timeout")
                r = self.buf.pop(0)
                if clock.elapsed() - r["_sent_at"] > 2.0:
                    mod.stale_reads += 1
                r = {k: v for k, v in r.items() if k != "_sent_at"}
                return json.dumps(r).encode(), ("sim", 0)

        return _Sock()


def _stage(sim, tcp_fail_after=(), udp_delay=lambda t: 0.05):
    st = rps.Stage(sim, viewer=False)
    st.set_true(*A)
    fails = sorted(tcp_fail_after)
    orig = st._receiver

    def receiver(msg, **k):
        if msg.get("action") == "POSITION" and fails and st.clock.elapsed() >= fails[0]:
            fails.pop(0)
            return None
        return orig(msg, **k)
    sim.sender.request_receiver_tcp = receiver
    st.udp = _UdpModule(st, udp_delay)
    sim.pl.socket = st.udp
    import app.navigation.send_move as nm
    st.nm = nm
    nm.is_navigating = False
    return st


def _nav(st, t_on, t_off):
    st.at(t_on, lambda: setattr(st.nm, "is_navigating", True))
    st.at(t_off, lambda: setattr(st.nm, "is_navigating", False))


def _glide(st, frm, to, t0, sec, steps=8):
    for i in range(steps + 1):
        f = i / steps
        st.at(t0 + sec * f, lambda f=f: st.belief.__setitem__(
            slice(0, 3), [frm[0] + (to[0] - frm[0]) * f, frm[1] + (to[1] - frm[1]) * f, to[2]]))


def _watch(sim, st, out, every=0.5, until=60.0):
    """미확정이 처음 켜진 시각·풀린 시각을 기록한다."""
    out.setdefault("pending_at", None)
    out.setdefault("released_at", None)

    def tick():
        p = sim.rt.is_initpose_pending(RID)
        t = round(st.clock.elapsed(), 1)
        if p and out["pending_at"] is None:
            out["pending_at"] = t
        if not p and out["pending_at"] is not None and out["released_at"] is None:
            out["released_at"] = t
        if t < until:
            st.clock.after(every, tick)
    st.clock.after(every, tick)


def _result(sim, st, out):
    pos = sim.rt.get_position(RID)
    out.update({
        "pending": sim.rt.is_initpose_pending(RID),
        "manual": sum(1 for _, k, _m in sim.events if k == "robot_initpose_manual_needed"),
        "recovered": sum(1 for _, k, _m in sim.events if k == "robot_initpose_recovered"),
        "udp_stale_reads": st.udp.stale_reads,
        "pos": (round(pos.get("x", 0), 2), round(pos.get("y", 0), 2)),
    })
    return out


def _drive_samples(sim, st, sec, period=2.0, override=None):
    """위치 수집 루프 대신 2초마다 실제 _apply_position_sample 을 시계 사건으로 부른다.

    하네스는 한 스레드라, 이동 요청이 '기다리는' 동안 position_thread 가 멈춘다. 그래서 이동 시작 대기
    시나리오는 표본 반영을 사건으로 넣어 두 흐름이 번갈아 돌게 한다. override(t, sample) 로 표본을 바꾼다.
    """
    def tick():
        t = st.clock.elapsed()
        if t > sec:
            return
        alive = st.robot.comm_ok()
        if alive or st.latest_mono is None:
            st._receiver_poll_robot()
        p = dict(st.latest)
        p["age"] = round(st.clock.now() - (st.latest_mono or st.clock.now()), 3)
        if override:
            p = override(t, p)
        if p.get("timestamp"):
            sim.pl._apply_position_sample(RID, p)
        st.clock.after(period, tick)
    st.clock.after(0.05, tick)


def _ask_move_when_jump(sim, st, out, after_jump=None):
    def first_jump_seen():
        hit = sim.rt.is_initpose_pending(RID) or (_cand(sim) and sim.pl.jump_suspect_active(RID))
        if not hit:
            st.clock.after(0.05, first_jump_seen)
            return
        if after_jump:
            after_jump()
        t0 = st.clock.elapsed()
        out["blocked"] = st.nm._initpose_blocked()
        out["wait_s"] = round(st.clock.elapsed() - t0, 2)
    st.at(9.0, first_jump_seen)


def _run_events(sim, st, sec):
    sim.run(lambda: st.clock.sleep(sec), grace=0.0)
    st.alive = False


# ── J: 위치 급변 ─────────────────────────────────────────
@scenario("J1 [재현 11-06 10:27] 주행 중 통신 두절 때 UDP 답이 늦게 옴 → 도착 뒤 다음 UDP 조회가 옛 답을 읽음",
          expect=lambda r: (not r["pending"] and r["manual"] == 0)
          or f"pending={r['pending']} manual={r['manual']} 옛답읽음={r['udp_stale_reads']}")
def j1(sim):
    st = _stage(sim, tcp_fail_after=(4.0, 20.0), udp_delay=lambda t: 5.0 if t < 10 else 0.05)
    st.set_true(*B)
    _nav(st, 1.0, 17.0)
    _glide(st, B, A, 6.0, 8.0)
    out = {}
    _watch(sim, st, out)
    st.run(40.0)
    return _result(sim, st, out)


@scenario("J2 서 있는데 측위 폭주(11-05 16:58 같은 꼴, 1초마다 3~4m) → 둘 다 미확정·알림 1번, 늦어짐은 변경안 약 2초",
          expect=lambda r: (r["pending"] and r["manual"] == 1)
          or f"pending={r['pending']} manual={r['manual']}")
def j2(sim):
    st = _stage(sim)
    pts = [(-5.90, 5.98), (-2.63, 4.64), (1.92, 4.63), (5.18, 5.17), (4.32, 2.98)]
    for i, (x, y) in enumerate(pts):
        st.at(10.0 + i * 1.1, lambda x=x, y=y: st.belief.__setitem__(slice(0, 2), [x, y]))
    out = {}
    _watch(sim, st, out)
    st.run(40.0)
    return _result(sim, st, out)


@scenario("J3 서 있는데 측위가 한 표본만 튀고 바로 제자리 → 변경안은 무시",
          expect=lambda r: (not r["pending"] and r["manual"] == 0)
          or f"pending={r['pending']} manual={r['manual']}")
def j3(sim):
    st = _stage(sim)
    # 위치 조회는 10.05·12.05초 — 10.05초 표본 하나만 튄 값이 되게 한다
    st.at(9.9, lambda: st.belief.__setitem__(slice(0, 2), [B[0], B[1]]))
    st.at(11.0, lambda: st.belief.__setitem__(slice(0, 2), [A[0], A[1]]))
    out = {}
    _watch(sim, st, out)
    st.run(40.0)
    return _result(sim, st, out)


@scenario("J4 튀어서 미확정(6초) → 원래 자리로 돌아와 머묾 → 변경안은 약 10초 뒤 자동 해제",
          expect=lambda r: (not r["pending"] and r["manual"] == 1 and r["recovered"] == 1)
          or f"pending={r['pending']} manual={r['manual']} recovered={r['recovered']} 해제={r['released_at']}")
def j4(sim):
    st = _stage(sim)
    st.at(10.1, lambda: st.belief.__setitem__(slice(0, 2), [B[0], B[1]]))
    st.at(16.1, lambda: st.belief.__setitem__(slice(0, 2), [A[0] + 0.1, A[1] - 0.1]))
    out = {}
    _watch(sim, st, out)
    st.run(45.0)
    return _result(sim, st, out)


@scenario("J5 튀어서 다른 자리에 머묾 → 둘 다 미확정 유지(운영자 확정 필요)",
          expect=lambda r: (r["pending"] and r["recovered"] == 0)
          or f"pending={r['pending']} recovered={r['recovered']}")
def j5(sim):
    st = _stage(sim)
    st.at(10.1, lambda: st.belief.__setitem__(slice(0, 2), [B[0], B[1]]))
    out = {}
    _watch(sim, st, out)
    st.run(45.0)
    return _result(sim, st, out)


@scenario("J6 원래 자리로 돌아왔지만 로봇이 측위 상실(Location=1) 보고 → 자동 해제 안 함",
          expect=lambda r: (r["pending"] and r["recovered"] == 0)
          or f"pending={r['pending']} recovered={r['recovered']}")
def j6(sim):
    st = _stage(sim)
    st.at(10.1, lambda: st.belief.__setitem__(slice(0, 2), [B[0], B[1]]))
    st.at(16.1, lambda: (st.belief.__setitem__(slice(0, 2), [A[0], A[1]]), setattr(st, "location", 1)))
    out = {}
    _watch(sim, st, out)
    st.run(45.0)
    return _result(sim, st, out)


@scenario("J7 실제 폭주 첫 표본 직후 이동 요청 → 변경안은 2초 안쪽 기다렸다 막음(출발 안 함)",
          expect=lambda r: r["blocked"] is True or f"blocked={r['blocked']} 대기={r['wait_s']}s")
def j7(sim):
    st = _stage(sim)
    pts = [(-5.90, 5.98), (-2.63, 4.64), (1.92, 4.63), (5.18, 5.17)]
    for i, (x, y) in enumerate(pts):
        st.at(10.0 + i * 1.1, lambda x=x, y=y: st.belief.__setitem__(slice(0, 2), [x, y]))
    out = {"blocked": None, "wait_s": None}
    _drive_samples(sim, st, 20.0)
    _ask_move_when_jump(sim, st, out)
    _run_events(sim, st, 20.0)
    return out


@scenario("J8 옛 답 한 번(첫 표본 직후) 뒤 이동 요청 → 변경안은 기다렸다 허용(현행은 막힘)",
          expect=lambda r: r["blocked"] is False or f"blocked={r['blocked']} 대기={r['wait_s']}s")
def j8(sim):
    st = _stage(sim)
    out = {"blocked": None, "wait_s": None}
    # 튄 표본을 한 번 흉내 낸다(timestamp 는 새것 — 순서 검사로는 못 거르는 경우)
    shot = {"done": False}

    def override(t, p):
        if not shot["done"] and t >= 10.0:
            shot["done"] = True
            return dict(p, x=B[0], y=B[1])
        return p
    _drive_samples(sim, st, 20.0, override=override)
    _ask_move_when_jump(sim, st, out)
    _run_events(sim, st, 20.0)
    return out


@scenario("J11 급변 첫 표본 뒤 로봇 응답 끊김(확인 표본이 안 옴) 중 이동 요청 → 변경안은 기다린 뒤에도 출발 보류",
          expect=lambda r: r["blocked"] is True or f"blocked={r['blocked']} 대기={r['wait_s']}s")
def j11(sim):
    st = _stage(sim)
    st.at(10.0, lambda: st.belief.__setitem__(slice(0, 2), [B[0], B[1]]))
    out = {"blocked": None, "wait_s": None}
    _drive_samples(sim, st, 25.0)
    # 이후 receiver 는 같은 옛 좌표(age 증가)만 돌려준다
    _ask_move_when_jump(sim, st, out, after_jump=lambda: st.robot.drop_comm(30.0))
    _run_events(sim, st, 25.0)
    return out


@scenario("J9 receiver 시계가 100초 뒤로 맞춰짐 → 변경안은 옛 답 2번 버린 뒤 기준 재설정, 위치 계속 갱신",
          expect=lambda r: (abs(r["pos"][0] - (A[0] + 1.0)) < 0.05 and not r["pending"])
          or f"pos={r['pos']} pending={r['pending']}")
def j9(sim):
    st = _stage(sim)
    orig = sim.sender.request_receiver_tcp

    def receiver(msg, **k):
        p = orig(msg, **k)
        if p and st.clock.elapsed() >= 10.0:
            p = dict(p, timestamp=p["timestamp"] - 100.0)
        return p
    sim.sender.request_receiver_tcp = receiver
    st.at(10.5, lambda: st.belief.__setitem__(slice(0, 2), [A[0] + 1.0, A[1]]))
    out = {}
    st.run(25.0)
    return _result(sim, st, out)


@scenario("J10 회귀: 주행 중에는 급변 감시 꺼짐 그대로(주행 중 큰 이동은 미확정 안 됨)",
          expect=lambda r: (not r["pending"] and r["manual"] == 0)
          or f"pending={r['pending']} manual={r['manual']}")
def j10(sim):
    st = _stage(sim)
    _nav(st, 5.0, 30.0)
    _glide(st, A, B, 6.0, 4.0)
    out = {}
    st.run(40.0)
    return _result(sim, st, out)


# ── D: 도크 재접속 ────────────────────────────────────────
DOCK = (-0.932, 2.374, 1.5720)


class DockStage(rbl.Stage):
    docked = True

    def _sync(self):
        super()._sync()
        rt = self.sim.rt
        with rt._lock:
            e = rt._runtime[rbl.RID]
            e["charge_state"] = {"state": 2 if self.docked else 0}


def _dtrig(n):
    def ok(r):
        return r["triggers"] == n or f"trig={r['triggers']} seeds={r['seeds']} pending={r['pending']}"
    return ok


@scenario("D1 [재현 11-06 03:12] 도크 위, 관제 통신만 40분 끊김(노트북 절전) → 부팅 번호 같음 → 변경안은 주입 안 함",
          env=rbl.ON, expect=_dtrig(0))
def d1(sim):
    st = DockStage(sim, true_pose=DOCK, last_pose=DOCK)
    st.power_cycle(10, 10 + 2400, DOCK, boot_loc=0, new_boot=None)
    return st.go(2600)


@scenario("D2 도크 위에서 실제로 끄고 켬(부팅 번호 바뀜) → 둘 다 주입", env=rbl.ON, expect=_dtrig(1))
def d2(sim):
    st = DockStage(sim, true_pose=DOCK, last_pose=DOCK)
    st.power_cycle(10, 90, (0.05, 0.02, 0.0), boot_loc=1)
    return st.go()


@scenario("D3 도크 위에서 끄고 켰는데 부팅 위치가 원점 아님(10-27 꼴, 부팅 번호 바뀜) → 둘 다 주입",
          env=rbl.ON, expect=_dtrig(1))
def d3(sim):
    st = DockStage(sim, true_pose=DOCK, last_pose=DOCK)
    st.power_cycle(10, 90, (0.57, -0.21, -0.49), boot_loc=1)
    return st.go()


@scenario("D4 도크 위, 구버전 receiver(부팅 번호 없음) 통신 복구 → 지금과 같음(주입)", env=rbl.ON, expect=_dtrig(1))
def d4(sim):
    st = DockStage(sim, true_pose=DOCK, last_pose=DOCK)
    st.boot_id = None
    st.power_cycle(10, 60, DOCK, boot_loc=0, new_boot=None)
    return st.go()


@scenario("D5 도크 위, 부팅 번호 같지만 위치가 원점(측위 재시작) → 둘 다 주입", env=rbl.ON, expect=_dtrig(1))
def d5(sim):
    st = DockStage(sim, true_pose=DOCK, last_pose=DOCK)
    st.power_cycle(10, 60, (0.03, -0.02, 0.0), boot_loc=1, new_boot=None)
    return st.go()
