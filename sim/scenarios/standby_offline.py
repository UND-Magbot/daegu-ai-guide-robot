"""상시 대기 근무 시작 때 로봇이 끊겨 있을 때 (현행 vs 변경, 10-23).

현행: 근무 시간이 되어도 로봇이 끊겨 있으면 아무 알림 없이 시작을 미룬다.
변경: 근무마다 한 번 "연결되면 바로 근무를 시작합니다(운영 시간 HH:MM까지)" 알림. 연결되면 다음 틱에 시작.
무대는 standby_map.py 의 MapStage(대기 장소·로봇 모두 1F — 맵 보류 없음). 온라인 여부만 바꾼다.
"""
import importlib.util
import os

from harness import REGISTRY, scenario

_n = len(REGISTRY)
_spec = importlib.util.spec_from_file_location(
    "standby_map_base_offline", os.path.join(os.path.dirname(__file__), "standby_map.py"))
_map = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_map)
del REGISTRY[_n:]
MapStage, _no_err = _map.MapStage, _map._no_err


class OfflineShiftStage(MapStage):
    def __init__(self, sim, online=False):
        super().__init__(sim, place_map=1, robot_map=1)
        self.online = online
        self.lp._is_target_robot_online = lambda sched: self.online

    def offline_holds(self):
        return [m for _t, k, m in self.sim.events if k == "standby_offline_hold"]


@scenario("S1 근무 시작 때 끊김 → 알림 1건(틱 3번), 로봇 안 잡음 (현행: 알림 없음)",
          expect=lambda r: _no_err(r) and not r["started"] and r["phase"] is None
          and len(r["offline"]) == 1 and "연결되면 바로 근무를 시작합니다" in r["offline"][0]
          and f"운영 시간 {r['end']}까지" in r["offline"][0])
def s1(sim):
    st = OfflineShiftStage(sim)
    started = any([st.start_shift(), st.start_shift(), st.start_shift()])
    return st.res(started, offline=st.offline_holds(), end=f"{st._window[1]:%H:%M}")


@scenario("S2 끊김 알림 뒤 연결되면 다음 틱에 근무 시작, 알림은 그대로 1건",
          expect=lambda r: _no_err(r) and r["started"] and r["phase"] is not None and len(r["offline"]) == 1)
def s2(sim):
    st = OfflineShiftStage(sim)
    st.start_shift()
    st.start_shift()
    st.online = True
    started = st.start_shift()
    return st.res(started, offline=st.offline_holds())


@scenario("S3 연결된 로봇은 알림 없이 바로 근무 시작(회귀)",
          expect=lambda r: _no_err(r) and r["started"] and r["phase"] is not None and r["offline"] == [])
def s3(sim):
    st = OfflineShiftStage(sim, online=True)
    return st.res(st.start_shift(), offline=st.offline_holds())
