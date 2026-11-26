"""진행 멘트 — 시연 동선(맵 47 병원 안내) 실제 경로 관찰(2026-10-29).

  sim\\run.bat sim\\scenarios\\progress_cue_demo.py --cand sim\\out\\cand_progress_cue
경로는 운영 DB 장소 + 관제 노드망(plan_leg)이 10-29 에 계산한 경유 좌표를 그대로 넣었다(조회만).
출발점은 앞 단계 도착 장소 좌표(실제로는 멈춘 자리). 속도 조건 2가지(VARIANTS)로 어느 구간에서 몇 초에 무엇이 들리는지 본다.
판정 없는 관찰용. 진행 멘트 시각 다음 안내까지 간격(gap)이 5초 이상이어야 말이 끝난다.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("progress_cue_base",
                                               os.path.join(os.path.dirname(__file__), "progress_cue.py"))
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)
del REGISTRY[_n:]

# 단계, 출발, 경유(노드망), 목적지, 진행 멘트
LEGS = [
    ("ticket_move", (-3.7, 6.05), [], (0.18, 6.02), "원무과 방향으로 이동하고 있습니다."),
    ("seat_move", (0.18, 6.02), [(-3.14, 5.9)], (-3.31, 1.12), "진료 대기 공간으로 이동하고 있습니다."),
    ("store_move", (-3.31, 1.12), [(-3.14, 0.46), (-0.97, 0.46)], (-1.18, -2.0), "편의점 방향으로 이동하고 있습니다."),
    ("clinic_move", (-3.31, 1.12), [(-3.14, 0.46)], (2.27, 0.38), "정형외과 진료실로 이동하고 있습니다."),
    ("payment_move", (2.27, 0.38), [(-3.14, 0.46), (-3.14, 5.9)], (0.52, 6.27), "수납 창구 방향으로 이동하고 있습니다."),
    ("extra_move", (0.52, 6.27), [(-3.14, 5.9), (-3.14, 0.46), (-0.97, 0.46), (-0.99, -2.73), (3.47, -3.77),
                                  (3.8, -7.04)], (5.42, -10.31), "엘리베이터 방향으로 이동하고 있습니다."),
]
# 이름: (시나리오 안내 속도, 가짜 로봇 실제 속도 m/s)
#  현장: 10-29 14:05~14:10 실기 — 저속 보정(NAV_TURN_SLOW_DEG)으로 저속 명령, 실제 평균 약 0.33 m/s
#  보통: 저속 보정을 끄면 보통 명령, 실제 1.0~1.2 m/s(사용자 확인)
VARIANTS = {
    "현장 저속명령·0.33m/s": ("slow", 0.33),
    "보통명령·1.1m/s": ("normal", 1.1),
}


def _run(sim, leg, variant):
    step, start, vias, goal, text = leg
    guide_speed, mps = VARIANTS[variant]
    st = pc._stage(sim, vias=vias, progress=text, robot_at=start)
    sim.robot.p.speed = mps
    db = st.Session()
    try:
        p = db.query(st.M.LocationInfo).filter(st.M.LocationInfo.id == 2).first()
        p.LocationX, p.LocationY = goal
        db.query(st.M.VoiceScenario).filter(st.M.VoiceScenario.id == 1).first().GuideSpeed = guide_speed
        db.commit()
    finally:
        db.close()
    r = pc._guide_move(st, sim, step="ticket_move")
    def tag(h):
        if h == text:
            return "★진행"
        if h.startswith(text):
            return "★진행+" + _short(h[len(text):].strip())
        return _short(h)
    return {"heard": [f"{t:.1f}s {tag(h)}" for h, t in zip(r["heard"], r["t"])],
            "min_gap": r["min_gap"], "pos": r["pos"]}


def _short(h):
    return {"잠시 후 왼쪽으로 방향을 바꾸겠습니다.": "왼쪽", "잠시 후 오른쪽으로 방향을 바꾸겠습니다.": "오른쪽",
            "잠시 후 뒤로 방향을 바꾸겠습니다.": "뒤로", "곧 도착합니다.": "곧도착"}.get(h, h[:12])


def _make(leg, variant):
    @scenario(f"[{variant}] {leg[0]}", limit_sec=300)
    def _s(sim):
        return _run(sim, leg, variant)
    return _s


for _v in VARIANTS:
    for _leg in LEGS:
        _make(_leg, _v)
