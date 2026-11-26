"""층 이동 뒤에는 주입이 수렴해도 자동 확정하지 않고 운영자 '완료'를 기다린다(2026-11-05).

  sim\run.bat sim\scenarios\floor_confirm.py --cand sim\out\cand_floor_confirm
배경: 주입 뒤 로봇이 넣은 위치 근처로 수렴하면 verify_and_clear 가 미확정을 바로 풀었다(운영자 확인 없음).
층 도착 시드는 방향을 스캔으로 정하게 됐지만(11-05), 틀렸을 때 사람이 걸러 줄 단계를 남긴다.
무대: floor_seed_1030 과 같다(가짜 receiver + 실제 위치 폴링, 실제 status.init_pose·confirm_current_position_manual).
가짜로 둔 것: 수렴 판정(_await_converged) 결과를 '수렴함'으로 — 하네스에서는 주입이 한 사건 안에서 위치 표본을
기다려 수렴을 못 본다. 실기는 층 도착 시드 뒤 1~3초에 '수동 init_pose 수렴 성공 — 미초기화 해제'가 남았다
(10-29 16:17·16:54, 11-02 09:31·10:10·13:47, 11-03 16:51 관제 로그).
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("fs1030_base_fc", os.path.join(os.path.dirname(__file__), "floor_seed_1030.py"))
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)
del REGISTRY[_n:]
RID, LOBBY = fs.RID, fs.LOBBY


def _converge_ok(sim, st):
    # 로봇이 믿는 위치를 시드 0.2m 옆에서 시작 — 옛 층 좌표(22m)면 '위치 급변' 가드가 다시 미확정으로 돌려
    # 자동 해제 여부가 가려진다(첫 실행에서 확인).
    st.belief = [LOBBY[0] + 0.2, LOBBY[1], LOBBY[2]]
    import app.robot_control.auto_init_pose as aip
    aip._await_converged = lambda robot_id, pose, max_wait_sec: (True, "수렴(시나리오 가정)")


def _inject(st, status, out, key, pose):
    try:
        res = status.init_pose(status.InitPoseBody(x=pose[0], y=pose[1], yaw=pose[2], robot_id=RID))
        out[key] = res.get("transport_ok")
        out[key + "_conv"] = res.get("converged")
        out[key + "_why"] = res.get("reason") or res.get("msg")
    except Exception as e:  # noqa: BLE001
        out[key] = f"예외 {e.__class__.__name__}: {e}"


def _pending(sim, out, key):
    out[key] = sim.rt.is_initpose_pending(RID)


@scenario("C1 [변경] 층 이동 뒤 시드 주입·수렴 → 운영자 '완료' 전까지 미확정 유지(자율주행 막힘)",
          expect=lambda r: r.get("inject") is True and r.get("pending_after_inject") is True
          or f"주입={r.get('inject')} 주입 뒤 미확정={r.get('pending_after_inject')}")
def c1(sim):
    st, status = fs._stage(sim, inject_ok=lambda: True)
    _converge_ok(sim, st)
    out = {}
    st.at(1.0, lambda: sim.rt.begin_floor_transit(RID))
    st.at(3.0, lambda: _inject(st, status, out, "inject", LOBBY))
    st.at(14.0, lambda: _pending(sim, out, "pending_after_inject"))
    st.run(15.0)
    return out


@scenario("C2 [변경] C1 뒤 운영자 '완료' → 확정(미확정 해제)",
          expect=lambda r: r.get("confirm") is True and r.get("pending_after_confirm") is False
          or f"완료={r.get('confirm')} ({r.get('confirm_why')}) 뒤 미확정={r.get('pending_after_confirm')}")
def c2(sim):
    st, status = fs._stage(sim, inject_ok=lambda: True)
    _converge_ok(sim, st)
    out = {}
    st.at(1.0, lambda: sim.rt.begin_floor_transit(RID))
    st.at(3.0, lambda: _inject(st, status, out, "inject", LOBBY))
    st.at(14.0, lambda: fs._confirm(out, "confirm"))
    st.at(14.5, lambda: _pending(sim, out, "pending_after_confirm"))
    st.run(15.0)
    return out


@scenario("C3 [회귀] 평소 재조정(층 이동 아님) 주입·수렴 → 지금처럼 자동 확정",
          expect=lambda r: r.get("inject") is True and r.get("pending_after_inject") is False
          or f"주입={r.get('inject')} 주입 뒤 미확정={r.get('pending_after_inject')}")
def c3(sim):
    st, status = fs._stage(sim, inject_ok=lambda: True)
    _converge_ok(sim, st)
    out = {}
    st.at(1.0, lambda: sim.rt.set_initpose_pending(RID, True, "운영자 재조정"))
    st.at(3.0, lambda: _inject(st, status, out, "inject", LOBBY))
    st.at(14.0, lambda: _pending(sim, out, "pending_after_inject"))
    st.run(15.0)
    return out


@scenario("C4 [회귀] 측위 상실 중 주입·수렴 → 지금처럼 자동 확정",
          expect=lambda r: r.get("inject") is True and r.get("pending_after_inject") is False
          or f"주입={r.get('inject')} 주입 뒤 미확정={r.get('pending_after_inject')}")
def c4(sim):
    st, status = fs._stage(sim, inject_ok=lambda: True)
    _converge_ok(sim, st)
    out = {}
    st.at(1.0, lambda: sim.rt.set_initpose_pending(RID, True, sim.rt.LOCALIZATION_LOST_REASON))
    st.at(3.0, lambda: _inject(st, status, out, "inject", LOBBY))
    st.at(14.0, lambda: _pending(sim, out, "pending_after_inject"))
    st.run(15.0)
    return out
