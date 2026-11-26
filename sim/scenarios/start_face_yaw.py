"""안내 시작 위치 생략 때 장소 방향 맞춤(2026-11-03 메모) — 현행 vs 변경.

  sim\\run.bat sim\\scenarios\\start_face_yaw.py --cand sim\\out\\cand_faceyaw
메모: "로봇 대기위치 근처에 있을 때 그 위치에서 바로 시작하는 로직 … 그 위치에서 바로 시작하되 yaw 방향은
확인하고 그 방향을 바라본 후 진행하는게 맞아."
현행: 시작 장소(call_move)에서 GUIDE_CALL_NEAR_M(1.0m) 안이면 이동·회전 없이 '이미 근처'로 넘어간다.
변경: 그 자리에서 장소 방향(Yaw)으로 제자리 NAV 하나(20° 이상 차이일 때) → 다 돈 뒤 시작.
무대: guide_speed_start.py Stage(시작 장소 (2,0), 내과 (6,0)). 측정: NAV 수·끝 위치·끝 방향·이동 요청 소요(가상 초).
가짜 로봇은 같은 좌표 NAV 를 받으면 출발 지연 뒤 바로 목표 방향이 된다 — 실제 회전 시간·같은 좌표 NAV 에서 도는지는 실기 확인.
"""
import importlib.util
import math
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("gss_base_fy", os.path.join(os.path.dirname(__file__), "guide_speed_start.py"))
gss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gss)
del REGISTRY[_n:]

PLACE_YAW = math.pi / 2     # 대기 장소가 바라보는 방향(손님 쪽) = 90°


def _set_start_yaw(st, yaw):
    db = st.Session()
    try:
        db.query(st.M.LocationInfo).filter(st.M.LocationInfo.id == gss.START).update({"Yaw": yaw})
        db.commit()
    finally:
        db.close()


def _deg(r):
    return round(math.degrees(r))


def _start(sim, dx=0.0, dy=-0.4, robot_yaw=0.0, place_yaw=PLACE_YAW, then_guide=False, docked=False):
    st = gss.Stage(sim)
    _set_start_yaw(st, place_yaw)
    sim.robot.place(2.0 + dx, dy, robot_yaw)
    if docked:
        sim.robot.charging = True
    sid, first = st.open(start_id=gss.START)
    t0 = sim.clock.elapsed()
    st.move(sid, gss.START)
    took = None
    # 시작 이동 요청이 돌아온 시각 — Stage.move 안에서 session_move 가 끝난 뒤 nav_thread 를 돈다
    took = round(sim.clock.elapsed() - t0, 1)
    out = {"first_step": first, "navs_call": len(st.speeds), "yaw_after": _deg(sim.robot.yaw),
           "pos_after": [round(sim.robot.x, 2), round(sim.robot.y, 2)], "took": took}
    if then_guide:
        sim.robot.charging = False
        st.set_step(sid, "guide_move")
        st.move(sid, gss.CLINIC)
        out["navs_total"] = len(st.speeds)
    return out


def _turned(r):
    return ((r["navs_call"] == 1 and r["yaw_after"] == 90 and r["pos_after"] == [2.0, -0.4]
             and r["errors"] == 0)
            or f"NAV {r['navs_call']}개 방향 {r['yaw_after']}° 위치 {r['pos_after']}")


def _untouched(yaw):
    def f(r):
        return ((r["navs_call"] == 0 and r["yaw_after"] == yaw and r["errors"] == 0)
                or f"NAV {r['navs_call']}개 방향 {r['yaw_after']}°")
    return f


@scenario("F1 [변경] 대기 장소 0.4m 앞, 방향 90° 다름 → 제자리에서 90°로 돌고 시작(위치 그대로)", expect=_turned)
def f1(sim):
    return _start(sim)


@scenario("F2 [변경] 반대 방향(-90°, 180° 차이) → 돌고 시작", expect=_turned)
def f2(sim):
    return _start(sim, robot_yaw=-math.pi / 2)


@scenario("F3 [회귀] 방향 차이 10° → 돌지 않음(NAV 0)", expect=_untouched(80))
def f3(sim):
    return _start(sim, robot_yaw=math.radians(80))


@scenario("F4 [경계] 각도 경계 180°↔-180° 감기(장소 179°, 로봇 -179°) → 2° 차이라 돌지 않음",
          expect=_untouched(-179))
def f4(sim):
    return _start(sim, robot_yaw=math.radians(-179), place_yaw=math.radians(179))


@scenario("F5 [회귀] 멀리(7m) → 지금처럼 시작 장소로 이동(장소 방향으로 도착)",
          expect=lambda r: (r["navs_call"] >= 1 and r["pos_after"] == [2.0, 0.0] and r["errors"] == 0)
          or f"NAV {r['navs_call']}개 위치 {r['pos_after']}")
def f5(sim):
    return _start(sim, dx=7.0, dy=0.0)


@scenario("F6 [경계] 도킹(충전) 중 → 돌지 않음(충전 해제는 첫 이동이 한다)", expect=_untouched(0))
def f6(sim):
    return _start(sim, docked=True)


@scenario("F7 [회귀] 돌고 난 뒤 첫 안내 이동(내과) — 거부 0, 내과 도착",
          expect=lambda r: (r["pos"] == [6, 0] and r["rejects"] == 0 and r["errors"] == 0)
          or f"위치 {r['pos']} 거부 {r['rejects']}")
def f7(sim):
    return _start(sim, then_guide=True)


@scenario("F8 [회귀] 안내 단계(guide_move)에서 목표 0.3m 앞 → 회전하지 않음(시작 장소만)",
          expect=lambda r: (r["navs"] == 0 and r["yaw"] == 0) or f"NAV {r['navs']}개 방향 {r['yaw']}°")
def f8(sim):
    st = gss.Stage(sim)
    _set_start_yaw(st, PLACE_YAW)
    sim.robot.place(2.0, -0.3, 0.0)
    sid, _ = st.open()
    st.set_step(sid, "guide_move")
    st.move(sid, gss.START)
    return {"navs": len(st.speeds), "yaw": _deg(sim.robot.yaw)}


# 운영 설정(관제 컨테이너 환경 변수 11-03 확인): 도착 후 내비 보행 유지 켬·제자리 미리 전환 켬
OPS_ENV = {"NAV_KEEP_GAIT_ON_ARRIVAL": "true", "NAV_PRIME_ON_SESSION_START": "true"}


@scenario("K1 [변경·운영 설정] 0.4m 앞 90° 다름 → 돌고 시작, 첫 안내 이동은 리셋 없이(보행 전환 늘지 않음)",
          env=OPS_ENV,
          expect=lambda r: (r["yaw_after"] == 90 and r["pos"] == [6, 0] and r["rejects"] == 0
                            and r["errors"] == 0)
          or f"방향 {r['yaw_after']}° 위치 {r['pos']} 거부 {r['rejects']}")
def k1(sim):
    return _start(sim, then_guide=True)


@scenario("K2 [회귀·운영 설정] 방향 맞음 → 지금처럼 제자리 미리 전환만, 첫 안내 이동 정상",
          env=OPS_ENV,
          expect=lambda r: (r["yaw_after"] == 90 and r["pos"] == [6, 0] and r["rejects"] == 0
                            and r["errors"] == 0)
          or f"방향 {r['yaw_after']}° 위치 {r['pos']} 거부 {r['rejects']}")
def k2(sim):
    return _start(sim, robot_yaw=PLACE_YAW, then_guide=True)
