# 사전 시뮬레이션 하네스

관제 Backend 코드를 **가상 시계 + 가짜 로봇** 위에서 실제로 돌려, 수정 전·후 동작을 같은 시나리오로 비교한다.
운영 DB·로봇·receiver 에는 닿지 않는다(임시 SQLite, 실제 소켓 송신 차단, `.env` 미사용).

## 실행

```bat
sim\run.bat sim\scenarios\nav_basic.py                          :: 작업 폴더 Backend 만
sim\run.bat snapshot sim\out\cand_작업명                         :: 변경안 사본(Backend/app) 만들기
sim\run.bat sim\scenarios\내시나리오.py --cand sim\out\cand_작업명   :: 현행 vs 변경 비교
sim\run.bat sim\scenarios\내시나리오.py --base git:HEAD --cand ...  :: 현행을 커밋 기준으로
```

첫 실행 때 `sim\.venv` 를 만든다. 결과 표는 화면에, 전체 결과(명령열·로그 꼬리)는 `sim\out\*.json` 에 남는다.
마지막 변형에서 기대 판정이 하나라도 실패하면 종료 코드 1.

## 시나리오 작성

```python
from harness import scenario

@scenario("도착 후 Regular 복귀", expect=lambda r: r["mode"] == "Regular" and r["errors"] == 0)
def arrive(sim):
    sim.move([(3, 0, True), (6, 3)])          # (x, y[, 통과점])
```

| 도구 | 용도 |
|---|---|
| `sim.move(points, keep_gait=, charge=, stop_at=)` | 웨이포인트 이동을 `nav_thread` 로 끝까지 |
| `sim.run(fn, until=pred)` | 아무 Backend 함수나 가상 시계 위에서 |
| `sim.at(초, fn)` / `sim.wait(초)` | 끼워 넣을 사건 / 시간 흘리기 |
| `sim.robot.drop_comm(초)` / `hold(초)` / `place(x, y)` | 통신 두절, 제자리 대기, 위치 지정 |
| `sim.sm`, `sim.pl`, `sim.rt`, `sim.sender` … | Backend 모듈(속성을 바꿔 상황 설정) |
| `@scenario(..., params={"speed": 0.3}, env={...})` | 로봇 규칙 수치, import 전 환경 변수 |

결과 dict 의 키: `pos gait mode posture charging executing switches rejects races charge_mode0 dropped arrivals cmds sim_sec errors events`.
시나리오 함수가 dict 를 돌려주면 결과에 합쳐진다.

## 재현하는 것 / 못 하는 것

가짜 로봇 규칙은 `harness/robot.py` 머리말에 있다(실기에서 확인한 것만). 벤더 펌웨어 재정렬, 측위 품질,
Wi-Fi 타이밍, 여러 스레드의 실제 동시 실행은 재현하지 않는다. 이런 부분은 보고서에 "실기 확인 필요"로 적는다.
새로 실측한 로봇 동작이 생기면 `robot.py` 규칙과 `RobotParams` 에 반영한다.
