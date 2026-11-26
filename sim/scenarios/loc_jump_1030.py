"""측위 튐을 관제가 직접 판정(2026-10-30) — 669 측위는 위치가 틀려도 Location=0(정상)을 보낸다.

  sim\\run.bat sim\\scenarios\\loc_jump_1030.py --cand sim\\out\\cand_locjump
실기(10-30, 1F 맵 47):
- 12:55 정지 로봇에 11m 떨어진 위치 주입 → 좌표가 3초 동안 초당 6m 로 내달려 세 번째 자리에 멈춤, 내내 Location=0.
- 12:40 충전소-1 → 엘리베이터입구 주행 중 7m 어긋난 채 걸음, 멈춘 뒤에야 '위치 급변' 감지.
- 11:58 층 전환 시드 주입이 TCP·UDP 모두 실패했는데 옛 좌표로 '완료' 확정.
변경: polling._check_loc_jump(0.5m 이상을 초당 2m 넘게 옮기면 튐, 주입 직후 주입 자리로 가는 것은 제외),
주행 중 2초 안 2번이면 정지·미확정, 확정·수렴은 튐 뒤 5초, 층 이동 뒤 주입 전달 전 확정 거부.
무대: reloc_pose_sync.Stage(가짜 receiver 가 '로봇이 믿는 자세' + Location 을 0.5초마다 돌려줌, 실제 위치 폴링 스레드).
주입은 status.init_pose 대신 '로봇이 믿는 자세'를 바꾸고, 변경안이면 주입 전달 기록(note_initpose_injected)을 남긴다.
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("rps_base_1030", os.path.join(os.path.dirname(__file__), "reloc_pose_sync.py"))
rps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rps)
del REGISTRY[_n:]
RID = rps.RID
TRUE = (0.50, -0.76, -0.41)       # 10-30 실제 위치


def _cand(sim) -> bool:
    return hasattr(sim.rt, "loc_jump_age")


def _inject(sim, st, x, y, yaw):
    st.belief = [x, y, yaw]
    if _cand(sim):
        sim.rt.note_initpose_injected(RID, x, y)


def _runaway(st, to, sec, steps=6):
    """믿는 자세가 sec 초 동안 to 로 내달린다(주입 직후 폭주 재현)."""
    sx, sy = st.belief[0], st.belief[1]
    for i in range(1, steps + 1):
        f = i / steps
        st.at_rel(sec * f, lambda f=f: st.belief.__setitem__(slice(0, 2), [sx + (to[0] - sx) * f,
                                                                         sy + (to[1] - sy) * f]))


def _stage(sim):
    st = rps.Stage(sim)
    st.set_true(*TRUE)
    st.at_rel = lambda dt, fn: st.clock.after(dt, fn)
    import app.robot_control.status  # noqa: F401  화면 위치 API(변경안 loc_jump_age)
    sim.rewire_late_modules()
    return st


def _confirm(out, key):
    from app.robot_control.auto_init_pose import confirm_current_position_manual
    ok, why = confirm_current_position_manual(RID)
    out[key], out[key + "_why"] = ok, why


def _jumps(sim):
    return sim.rt.loc_jumps_within(RID, 999.0) if _cand(sim) else None


@scenario("J1 [재현] 정지 로봇에 11m 틀린 위치 주입 → 3초간 초당 6m 내달림 → 멈춘 1초 뒤 '완료'는 거부",
          expect=lambda r: (r["confirm_1s"] is False and (r["jumps"] or 0) >= 1 and r["confirm_8s"] is True)
          or f"멈춘 1초 뒤 완료={r['confirm_1s']} ({r['confirm_1s_why']}) 튐={r['jumps']} 8초 뒤 완료={r['confirm_8s']}")
def j1(sim):
    st = _stage(sim)
    out = {}
    sim.rt.set_initpose_pending(RID, True, "운영자 재조정")

    def go():
        _inject(sim, st, 5.88, -8.99, 2.19)
        _runaway(st, (2.60, 5.20), 3.0)
    st.at(5.0, go)
    st.at(9.0, lambda: _confirm(out, "confirm_1s"))     # 내달림이 끝나고 1초
    st.at(16.0, lambda: _confirm(out, "confirm_8s"))    # 8초 — 틀린 자리에 가만히 있으면 못 잡는다(한계)
    st.run(17.0)
    out["jumps"] = _jumps(sim)
    out["shown"] = st.live
    return out


@scenario("J2 [회귀] 맞는 위치 주입(좌표가 주입 자리로 한 번에 옮겨 감) → 튐 아님, 2초 뒤 '완료' 통과",
          expect=lambda r: (r["confirm"] is True and not r["jumps"]) or f"완료={r['confirm']} ({r['confirm_why']}) 튐={r['jumps']}")
def j2(sim):
    st = _stage(sim)
    st.belief = [3.0, 2.0, 0.0]                         # 틀린 자리에서 시작
    out = {}
    sim.rt.set_initpose_pending(RID, True, "운영자 재조정")
    st.at(5.0, lambda: _inject(sim, st, *TRUE))
    st.at(7.0, lambda: _confirm(out, "confirm"))
    st.run(8.0)
    out["jumps"] = _jumps(sim)
    return out


def _walk(sim, st, speed, jumps=()):
    """x 방향으로 speed m/s 주행(실제·믿는 자세 같이). jumps=[(t, dx, dy)] 그 순간 믿는 자세만 옮긴다."""
    sim.sm.is_navigating = True
    st.robot.place(0.0, 0.0, 0.0)
    st.belief = [0.0, 0.0, 0.0]
    off = [0.0, 0.0]

    def tick(dt):
        if not sim.sm.is_navigating:
            return
        st.robot.x += speed * dt
        st.belief[0] = st.robot.x + off[0]
        st.belief[1] = st.robot.y + off[1]
    st.clock.add_ticker(tick)
    for t, dx, dy in jumps:
        st.at(t, lambda dx=dx, dy=dy: off.__setitem__(slice(0, 2), [off[0] + dx, off[1] + dy]))


def _nav_out(sim):
    return {"still_nav": bool(sim.sm.is_navigating), "pending": sim.rt.is_initpose_pending(RID),
            "reason": sim.rt.get_initpose_reason(RID), "jumps": _jumps(sim),
            "stop_cmds": sum(1 for c in sim.robot.summary().get("cmds", []) if c in ("STOP", "CANCEL_NAV"))}


@scenario("J3 [재현] 주행 중 측위가 2.5m·3m 연달아 튐(12:40 재현) → 세우고 미확정",
          expect=lambda r: (not r["still_nav"] and r["pending"] and r["stop_cmds"] >= 1)
          or f"주행 중={r['still_nav']} 미확정={r['pending']}({r['reason']}) 정지 명령={r['stop_cmds']} 튐={r['jumps']}")
def j3(sim):
    st = _stage(sim)
    _walk(sim, st, 0.5, jumps=[(8.0, 1.8, 1.7), (8.6, 2.2, 1.0)])
    st.run(14.0)
    return _nav_out(sim)


@scenario("J4 [회귀] 주행 1.2 m/s(최고 속도) 정상 — 튐 0·계속 주행",
          expect=lambda r: (r["still_nav"] and not r["pending"] and not r["jumps"]) or str(r))
def j4(sim):
    st = _stage(sim)
    _walk(sim, st, 1.2)
    st.run(14.0)
    return _nav_out(sim)


@scenario("J5 [경계] 주행 중 한 번만 1m 튐 — 기록만, 주행 계속",
          expect=lambda r: (r["still_nav"] and not r["pending"] and r["jumps"] in (None, 1)) or str(r))
def j5(sim):
    st = _stage(sim)
    _walk(sim, st, 0.5, jumps=[(8.0, 1.0, 0.0)])
    st.run(14.0)
    return _nav_out(sim)


@scenario("J6 [재현] 층 이동 뒤 시드 주입이 로봇에 안 감(11:58) → 옛 좌표 '완료' 거부, 주입 뒤엔 통과",
          expect=lambda r: (r["before_inject"] is False and r["after_inject"] is True)
          or f"주입 전 완료={r['before_inject']} ({r['before_inject_why']}) 주입 뒤={r['after_inject']} ({r['after_inject_why']})")
def j6(sim):
    st = _stage(sim)
    st.belief = [-6.89, 7.94, 0.0]                      # 옛 층 좌표, Location=0
    out = {}
    st.at(2.0, lambda: sim.rt.begin_floor_transit(RID))
    st.at(6.0, lambda: _confirm(out, "before_inject"))
    st.at(8.0, lambda: _inject(sim, st, *TRUE))
    st.at(12.0, lambda: _confirm(out, "after_inject"))
    st.run(13.0)
    return out


@scenario("J7 [회귀] 측위 상실 보고 → 미확정, 회복 → 해제(튐 판정과 별개)",
          expect=lambda r: (r["pending_after_lost"] and not r["pending_after_ok"]) or str(r))
def j7(sim):
    st = _stage(sim)
    out = {}
    st.at(4.0, lambda: setattr(st, "location", 1))
    st.at(24.0, lambda: out.update(pending_after_lost=sim.rt.is_initpose_pending(RID)))
    st.at(24.5, lambda: setattr(st, "location", 0))
    st.at(44.0, lambda: out.update(pending_after_ok=sim.rt.is_initpose_pending(RID)))
    st.run(45.0)
    return out


@scenario("J8 [경계] 재부팅처럼 원점 리셋 튐 뒤 1초 만에 시드 주입 → 주입 2초 뒤 '완료' 통과(주입 전 튐은 무시)",
          expect=lambda r: r["confirm"] is True or f"완료={r['confirm']} ({r['confirm_why']}) 튐={r['jumps']}")
def j8(sim):
    st = _stage(sim)
    out = {}
    sim.rt.set_initpose_pending(RID, True, "전원 on")
    st.at(4.0, lambda: st.belief.__setitem__(slice(0, 3), [0.0, 0.0, 0.0]))   # 측위 원점 리셋
    st.at(5.0, lambda: _inject(sim, st, *TRUE))
    st.at(7.0, lambda: _confirm(out, "confirm"))
    st.run(8.0)
    out["jumps"] = _jumps(sim)
    return out


@scenario("J9 [경계] 주행 중 자동문 상실(Location=1) 앞뒤로 2번 튐 → 세우지 않음, 회복하면 저절로 해제(296 흐름)",
          expect=lambda r: (r["still_nav"] and r["reason_mid"] == "측위 상실(로봇 보고)" and not r["pending"])
          or f"주행 중={r['still_nav']} 중간 사유={r['reason_mid']} 끝 미확정={r['pending']}({r['reason']}) 정지 명령={r['stop_cmds']}")
def j9(sim):
    st = _stage(sim)
    out = {}
    _walk(sim, st, 0.5, jumps=[(8.2, 1.8, 0.8), (8.8, -1.6, -0.6)])
    st.at(7.8, lambda: setattr(st, "location", 1))       # 로봇이 상실 보고 직후 좌표가 튐
    st.at(13.0, lambda: out.update(reason_mid=sim.rt.get_initpose_reason(RID)))
    st.at(15.0, lambda: setattr(st, "location", 0))      # 회복(약 7초)
    st.run(24.0)
    return {**_nav_out(sim), **out}


@scenario("J10 [경계] 이미 미확정(운영자 재조정 중)일 때 주행 중 튐 2번 → 사유를 덮어쓰지 않음",
          expect=lambda r: r["reason"] == "운영자 재조정" or f"사유={r['reason']}")
def j10(sim):
    st = _stage(sim)
    _walk(sim, st, 0.5, jumps=[(8.0, 1.8, 1.7), (8.6, 2.2, 1.0)])
    st.at(5.0, lambda: sim.rt.set_initpose_pending(RID, True, "운영자 재조정"))
    st.run(14.0)
    return _nav_out(sim)


def _nav_reason(sim):
    from app.robot_control.status import get_nav
    return get_nav().get("stop_reason")


@scenario("J11 [변경] 측위 튐 정지 → /robot/nav stop_reason '측위 튐 감지'(음성 설명 멘트용), 새 이동 시작하면 지움",
          expect=lambda r: (r["reason_after_stop"] == "측위 튐 감지" and r["reason_after_new"] is None
                            and r["reason_user_stop"] == "사용자 정지")
          or f"정지 뒤={r['reason_after_stop']} 새 이동 뒤={r['reason_after_new']} 사용자 정지={r['reason_user_stop']}")
def j11(sim):
    st = _stage(sim)
    out = {}
    _walk(sim, st, 0.5, jumps=[(8.0, 1.8, 1.7), (8.6, 2.2, 1.0)])

    def new_move():
        sim.sm.clear_arrival_actions()
        sim.sm.is_navigating = True
        out["reason_after_new"] = _nav_reason(sim) if not sim.sm.is_navigating else None
    st.at(11.0, lambda: out.update(reason_after_stop=_nav_reason(sim)))
    st.at(11.5, new_move)
    st.at(12.0, lambda: sim.sm.stop_navigation_internal("사용자 정지"))
    st.at(12.5, lambda: out.update(reason_user_stop=_nav_reason(sim)))
    st.run(13.0)
    return out


@scenario("J12 [경계] 주행 중 2m 튀었다 0.5초 뒤 제자리로 돌아옴 → 세우지 않음(순간 튐)",
          expect=lambda r: (r["still_nav"] and not r["pending"]) or str(r))
def j12(sim):
    st = _stage(sim)
    _walk(sim, st, 0.5, jumps=[(8.0, 1.6, 1.2), (8.5, -1.6, -1.2)])
    st.run(14.0)
    return _nav_out(sim)


@scenario("J13 [경계] 주행 중 튀었다 1.5초 뒤 다른 곳으로 또 튐(안 돌아옴) → 세움",
          expect=lambda r: (not r["still_nav"] and r["pending"]) or str(r))
def j13(sim):
    st = _stage(sim)
    _walk(sim, st, 0.5, jumps=[(8.0, 1.6, 1.2), (9.5, 1.5, -2.5)])
    st.run(14.0)
    return _nav_out(sim)


def _trace_dir(sim):
    """측위 기록을 sim/out/_loc_trace_test 로 돌린다. 현행(기록 기능 없음)이면 None."""
    try:
        import app.robot_io.loc_trace as lt
    except ImportError:
        return None
    import shutil
    d = os.path.join(os.path.dirname(__file__), "..", "out", "_loc_trace_test")
    shutil.rmtree(d, ignore_errors=True)
    lt.TRACE_DIR = d
    lt._file = None
    lt._last.clear()
    return lt


def _trace_rows(lt):
    import glob
    rows = []
    for path in glob.glob(os.path.join(lt.TRACE_DIR, "*.csv")):
        with open(path, encoding="utf-8-sig") as f:
            rows += [line.rstrip("\r\n").split(",") for line in f][1:]
    return rows


@scenario("J14 [변경] 측위 기록 — 주행 표본(약 0.5초)·튐·정지·주입 줄이 CSV 로 남고, 대기 중 그대로면 10초에 1줄",
          expect=lambda r: (r.get("nav_rows", 0) >= 12 and r.get("jump_rows") == 2 and r.get("stop_rows") == 1
                            and r.get("inject_rows") == 1 and r.get("idle_rows", 99) <= 3)
          or str({k: v for k, v in r.items() if k.endswith("_rows") or k == "sample"}))
def j14(sim):
    lt = _trace_dir(sim)
    st = _stage(sim)
    if lt is None:
        st.run(1.0)
        return {"nav_rows": 0}
    _walk(sim, st, 0.5, jumps=[(8.0, 1.8, 1.7), (8.6, 2.2, 1.0)])
    st.at(12.0, lambda: sim.rt.note_initpose_injected(RID, *TRUE[:2]))
    st.run(32.0)                                   # 8.6초 정지 뒤 약 23초 대기
    rows = _trace_rows(lt)
    kinds = [r[2] for r in rows]
    nav = [r for r in rows if r[2] == "위치" and r[8] == "1"]
    idle = [r for r in rows if r[2] == "위치" and r[8] == "0"]
    return {"nav_rows": len(nav), "jump_rows": sum(1 for r in rows if r[2] == "위치" and r[11]),
            "stop_rows": kinds.count("정지"), "inject_rows": kinds.count("주입"),
            "idle_rows": len([r for r in idle if r[11] == ""]), "sample": rows[:2] + rows[-2:]}


# 10-30 14:06 1F 병원 1회 주행(저속) 실제 측위 기록(backend_logs/loc_trace, 표본 시각 = 기록 시각 - 지연초, 출발 기준 초)
TRACE_1406 = [(0.0, 2.229, 0.154, 1.423), (0.5, 2.233, 0.149, 1.423), (1.0, 2.236, 0.15, 1.423), (1.5, 2.241, 0.146, 1.424), (2.0, 2.231, 0.159, 1.424), (2.5, 2.165, 0.199, 1.647), (3.01, 2.227, 0.239, 2.183), (3.51, 2.327, 0.319, 2.654), (4.01, 2.293, 0.021, 3.025), (4.51, 2.136, 0.201, 3.11), (5.01, 1.852, 0.176, 3.058), (5.52, 1.641, 0.095, 3.0), (6.02, 1.412, 0.069, 2.97), (6.52, 1.177, 0.069, 2.954), (7.03, 0.975, 0.098, 2.95), (7.53, 0.753, 0.173, 2.959), (8.02, 0.509, 0.2, 2.973), (8.53, 0.329, 0.243, 2.982), (9.03, 0.147, 0.305, 2.985), (9.53, -0.131, 0.349, 2.99), (10.04, -0.276, 0.359, 2.992), (11.54, -0.934, 0.492, 2.988), (12.05, -1.185, 0.471, 3.018), (12.54, -1.396, 0.507, 3.033), (13.04, -1.616, 0.537, 3.032), (13.55, -1.835, 0.547, 3.059), (14.05, -2.052, 0.568, 3.083), (14.55, -2.266, 0.569, 3.124), (15.05, -2.504, 0.564, 3.136), (15.55, -2.733, 0.553, -3.105), (16.06, -2.953, 0.587, -3.131), (16.56, -3.068, 0.568, 3.134), (17.57, -3.113, 0.677, 2.587), (18.06, -2.946, 0.674, 2.04), (19.07, -3.217, 0.261, 1.489), (19.57, -3.165, 0.371, 1.538), (20.58, -3.103, 0.736, 1.626), (21.07, -3.075, 0.898, 1.649), (21.57, -3.08, 1.086, 1.656), (22.08, -3.1, 1.262, 1.652), (23.59, -3.159, 1.963, 1.608), (24.08, -3.213, 2.172, 1.594), (25.59, -3.176, 2.797, 1.564), (26.1, -3.162, 3.015, 1.56), (27.61, -3.209, 3.598, 1.561), (28.11, -3.186, 3.795, 1.556), (28.61, -3.192, 3.976, 1.553), (29.11, -3.203, 4.182, 1.548), (29.6, -3.205, 4.393, 1.54), (30.62, -3.182, 4.808, 1.529), (31.11, -3.173, 5.003, 1.524), (31.62, -3.167, 5.211, 1.522), (32.12, -3.151, 5.402, 1.52), (32.62, -3.147, 5.59, 1.52), (33.13, -3.146, 5.753, 1.522), (33.64, -3.15, 5.81, 1.523), (34.64, -3.393, 5.782, 2.359), (35.14, -3.207, 5.814, 2.723), (35.64, -2.954, 6.114, 2.867), (36.65, -3.304, 6.181, 3.011), (37.14, -3.572, 6.16, 3.006), (37.64, -3.737, 6.11, 3.009), (38.15, -3.294, 6.304, 2.646), (38.65, -3.384, 6.126, 2.089), (39.16, -3.2, 6.325, 1.63), (39.66, -3.521, 6.144, 0.999), (40.16, -3.204, 6.75, 0.477), (41.16, -1.948, 6.29, -0.178), (41.66, -1.984, 6.032, -0.12), (42.66, -1.221, 5.312, 0.151), (43.19, -0.686, 5.082, 0.268), (44.18, -0.182, 5.093, 0.788), (44.67, -0.027, 5.116, 0.937), (45.18, 0.198, 5.158, 1.123), (46.18, 0.64, 5.311, 1.578), (46.68, 0.314, 5.413, 1.813), (47.18, 0.627, 5.232, 1.771), (47.68, 0.558, 5.017, 1.835), (48.18, 0.471, 5.413, 1.837), (48.69, 0.414, 5.55, 1.787), (49.19, 0.251, 5.402, 1.808), (49.7, 0.204, 5.447, 1.764), (50.2, 0.527, 5.744, 1.56), (50.7, 0.504, 5.707, 1.694), (51.2, 0.523, 5.957, 1.674)]


def _replay(sim, st, speed):
    """기록 좌표를 그 시각에 '로봇이 믿는 자세'로 넣는다. 주행 중·보낸 속도 명령 speed."""
    sim.sm.is_navigating = True
    sim.sm.waypoints_list = [{"x": 0.0, "y": 0.0, "sent_speed": speed} for _ in range(10)]
    sim.sm.current_wp_index = 1
    st.belief = list(TRACE_1406[0][1:])
    # 기록 표본 사이는 시간에 맞춰 잇는다(관제가 못 받은 사이도 로봇은 움직였다 — 추정). 표본 그대로 두면
    # 0.5초마다 읽는 시뮬에서 1.5초 걸린 이동이 0.5초에 일어난 것처럼 보여 가짜 튐이 생긴다.
    start = st.clock.elapsed() + 2.0

    def tick(dt):
        now = st.clock.elapsed() - start
        for (ta, xa, ya, wa), (tb, xb, yb, wb) in zip(TRACE_1406, TRACE_1406[1:]):
            if ta <= now <= tb:
                f = (now - ta) / (tb - ta) if tb > ta else 1.0
                st.belief[0], st.belief[1], st.belief[2] = xa + (xb - xa) * f, ya + (yb - ya) * f, wb
                return
    st.clock.add_ticker(tick)


@scenario("J15 [재현] 14:06 저속 1회 주행 실제 좌표 재생 — 꺾은 직후 초당 1.05~1.37m 밀림에서 세움(현행은 끝까지 감)",
          expect=lambda r: (not r["still_nav"] and r["pending"] and r["reason"] == "측위 튐 감지") or str(r))
def j15(sim):
    st = _stage(sim)
    _replay(sim, st, "slow")
    st.run(2.0 + TRACE_1406[-1][0] + 1.0)
    return _nav_out(sim)


@scenario("J16 [관찰] 같은 좌표를 보통 속도 명령으로 재생 — 기준 1.8 이라 세우지 않음(보통 걸음 1.0~1.2 와 구분 못 함)")
def j16(sim):
    st = _stage(sim)
    _replay(sim, st, "normal")
    st.run(2.0 + TRACE_1406[-1][0] + 1.0)
    return _nav_out(sim)
