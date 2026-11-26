"""층 이동 뒤 시드 미전달이면 '완료' 거부(2026-10-30) — 11:58 1F 도착 재현.

  sim\\run.bat sim\\scenarios\\floor_seed_1030.py --cand sim\\out\\cand_floorseed
실기: 1F 지도로 바꾼 뒤 위치 재조정 창이 엘리베이터 입구 시드를 보냈으나 TCP·UDP 모두 시간 초과로 로봇에 안 감.
로봇은 Location=0 을 보내 '완료'가 통과했고 옛 층 좌표 (-6.89, 7.94) 로 확정됐다.
변경: runtime.begin_floor_transit 시각 기록, 주입이 로봇에 전달되면(receiver ok) 기록, 층 이동 미확정이면 전달 전 '완료' 거부.
무대: reloc_pose_sync.Stage(가짜 receiver, 실제 위치 폴링). 주입은 실제 status.init_pose — 가짜 receiver 가 INIT_POSE 에
ok/시간 초과를 돌려준다.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location("rps_base_fs", os.path.join(os.path.dirname(__file__), "reloc_pose_sync.py"))
rps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rps)
del REGISTRY[_n:]
RID = rps.RID
LOBBY = (5.422, -10.306, -1.2066)
OLD = (-6.89, 7.94, 0.0)


def _stage(sim, inject_ok):
    st = rps.Stage(sim)
    st.robot.place(*LOBBY)
    st.belief = list(OLD)                       # 옛 층 좌표, Location=0
    st.inject_ok = inject_ok
    base_receiver = st._receiver

    def receiver(msg, **k):
        if msg.get("action") == "INIT_POSE":
            if not st.inject_ok():
                raise TimeoutError("timed out")   # 11:58 TCP 시간 초과
            it = msg["items"]
            st.belief = [it["PosX"], it["PosY"], it["Yaw"]]
            return {"status": "ok", "transport_ok": True, "error_code": 0, "response": {}}
        return base_receiver(msg, **k)
    sim.sender.request_receiver_tcp = receiver
    import app.robot_control.status as status
    status.request_receiver_tcp = receiver if hasattr(status, "request_receiver_tcp") else None
    sim.rewire_late_modules()
    return st, status


def _inject(st, status, out, key, pose):
    try:
        res = status.init_pose(status.InitPoseBody(x=pose[0], y=pose[1], yaw=pose[2], robot_id=RID))
        out[key] = res.get("transport_ok")
    except Exception as e:  # noqa: BLE001
        out[key] = f"예외 {e.__class__.__name__}"


def _confirm(out, key):
    from app.robot_control.auto_init_pose import confirm_current_position_manual
    ok, why = confirm_current_position_manual(RID)
    out[key], out[key + "_why"] = ok, why


@scenario("F1 [재현] 층 이동 뒤 시드 주입이 시간 초과(11:58) → 옛 좌표로 '완료' 거부",
          expect=lambda r: r["confirm"] is False or f"완료={r['confirm']} ({r['confirm_why']}) 주입={r['inject']}")
def f1(sim):
    st, status = _stage(sim, inject_ok=lambda: False)
    out = {}
    st.at(1.0, lambda: sim.rt.begin_floor_transit(RID))
    st.at(3.0, lambda: _inject(st, status, out, "inject", LOBBY))
    st.at(20.0, lambda: _confirm(out, "confirm"))
    st.run(21.0)
    return out


@scenario("F2 [회귀] 시드 실패 뒤 다시 넣기(전달 성공) → '완료' 통과",
          expect=lambda r: (r["confirm_before"] is False and r["confirm_after"] is True)
          or f"전={r['confirm_before']} 뒤={r['confirm_after']} ({r['confirm_after_why']})")
def f2(sim):
    ok = {"v": False}
    st, status = _stage(sim, inject_ok=lambda: ok["v"])
    out = {}
    st.at(1.0, lambda: sim.rt.begin_floor_transit(RID))
    st.at(3.0, lambda: _inject(st, status, out, "inject1", LOBBY))
    st.at(20.0, lambda: _confirm(out, "confirm_before"))
    st.at(21.0, lambda: ok.update(v=True))
    st.at(22.0, lambda: _inject(st, status, out, "inject2", LOBBY))
    st.at(32.0, lambda: _confirm(out, "confirm_after"))
    st.run(33.0)
    return out


@scenario("F3 [회귀] 층 이동 아님(평소 재조정) — 주입 없이 '완료' 그대로 통과",
          expect=lambda r: r["confirm"] is True or f"완료={r['confirm']} ({r['confirm_why']})")
def f3(sim):
    st, status = _stage(sim, inject_ok=lambda: True)
    st.belief = list(LOBBY)
    out = {}
    st.at(1.0, lambda: sim.rt.set_initpose_pending(RID, True, "운영자 재조정"))
    st.at(6.0, lambda: _confirm(out, "confirm"))
    st.run(7.0)
    return out


@scenario("F4 [회귀] 층 이동 뒤 시드 바로 성공 → '완료' 통과",
          expect=lambda r: r["confirm"] is True or f"완료={r['confirm']} ({r['confirm_why']})")
def f4(sim):
    st, status = _stage(sim, inject_ok=lambda: True)
    out = {}
    st.at(1.0, lambda: sim.rt.begin_floor_transit(RID))
    st.at(3.0, lambda: _inject(st, status, out, "inject", LOBBY))
    st.at(14.0, lambda: _confirm(out, "confirm"))
    st.run(15.0)
    return out
