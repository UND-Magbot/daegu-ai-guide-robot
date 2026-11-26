"""하네스 기본 점검 — 현재 관제 이동 경로가 가짜 로봇 위에서 끝까지 도는지 본다.

새 시뮬을 짤 때 이 파일을 복사해 시작한다. 하네스를 고친 뒤에도 이 파일로 회귀를 확인한다.
"""
from harness import scenario

ROUTE = [(3, 0, True), (3, 3, True), (6, 3)]   # 통과점 2개 + 목적지


@scenario("일반 이동 도착(통과점 2·목적지 1)",
          expect=lambda r: r["arrivals"] >= 1 and r["pos"] == [6, 3] and r["errors"] == 0)
def plain(sim):
    sim.move(ROUTE)


@scenario("이동 중 사용자 정지(출발 6s 뒤)",
          expect=lambda r: not r["executing"] and r["pos"] != [6, 3] and r["errors"] == 0)
def user_stop(sim):
    sim.move(ROUTE, stop_at=6.0)


@scenario("주행 중 통신 두절 8s",
          expect=lambda r: r["errors"] == 0 or "두절 중 오류 로그")
def comm_drop(sim):
    sim.at(5.0, lambda: sim.robot.drop_comm(8.0))
    sim.move(ROUTE)


@scenario("충전 도착(도킹)",
          expect=lambda r: r["charging"] and r["errors"] == 0)
def charge_arrival(sim):
    sim.move([(2, 0)], charge=True)


@scenario("도착 직후 0.2s 만에 다음 이동",
          expect=lambda r: r["pos"] == [9, 0] and r["races"] == 0 and r["errors"] == 0)
def back_to_back(sim):
    sim.move(ROUTE)
    sim.wait(0.2)
    sim.move([(6, 0, True), (9, 0)])
