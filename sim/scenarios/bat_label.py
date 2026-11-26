"""근무 칩 배터리 문구 — 새 배터리 정책(10-22: 기준 이하 3회면 즉시 중단)에 맞춘 정리 (현행 vs 변경).

새 정책에서는 안내 중단 '요청'(AbortRequest=battery)을 걸지 않고 바로 닫는다.
그래서 '배터리 위급 — 안내 중단 예정'은 예전 요청이 남은 세션에서만 나오고, 그때도 이미 닫히는 중이다.
충전소로 가는 동안의 칩 문구에는 사유(배터리 부족)를 넣는다.
"""
from harness import scenario


def _label(sim, state, single_busy=False, guide=None):
    sim.rewire_late_modules()
    import app.database.routes.standby as st
    return st._phase_label(state, single_busy, guide)


def _away_label(sim, reason):
    sim.rewire_late_modules()
    import app.scheduler.loop as lp
    return lp.AWAY_LABELS.get(reason)


LEFTOVER = {"id": 1, "stepCode": "clinic_move", "destination": "내과", "abortRequest": "battery"}


@scenario("L1 예전 중단 요청이 남은 안내 → '안내 중' (현행: '배터리 위급 — 안내 중단 예정')",
          expect=lambda r: r["label"] == "안내 중")
def l1(sim):
    return {"label": _label(sim, {"phase": "serving"}, guide=LEFTOVER)}


@scenario("L2 보통 안내 중 → '안내 중' (회귀)",
          expect=lambda r: r["label"] == "안내 중")
def l2(sim):
    return {"label": _label(sim, {"phase": "serving"}, guide={**LEFTOVER, "abortRequest": None})}


@scenario("L3 배터리 부족으로 충전소 가는 중 → 칩에 사유(배터리 부족)",
          expect=lambda r: r["label"].startswith("배터리 부족") and "충전소로 이동 중" in r["label"])
def l3(sim):
    return {"label": _label(sim, {"phase": "away", "away_label": _away_label(sim, "charge_going")})}


@scenario("L4 나머지 단계 문구 그대로 (회귀)",
          expect=lambda r: {k: r.get(k) for k in ("standby", "returning", "moving", "busy", "charge")} == {
              "standby": "대기 중(응대 가능)", "returning": "대기 장소로 복귀 중",
              "moving": "대기 장소로 이동 중", "busy": "응대 중",
              "charge": "충전 중 — 80%가 되면 대기 장소로 복귀",
          })
def l4(sim):
    return {
        "standby": _label(sim, {"phase": "standby"}),
        "returning": _label(sim, {"phase": "returning"}),
        "moving": _label(sim, {"phase": "moving"}),
        "busy": _label(sim, {"phase": "serving"}, single_busy=True),
        "charge": _label(sim, {"phase": "away", "away_label": _away_label(sim, "charge")}),
    }
