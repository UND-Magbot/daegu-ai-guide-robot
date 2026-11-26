"""병원 시나리오(robot 10, scenario 1): 순번 도래 후 접수창구(11·12)를 건너뛰고 진료실(13)로.

2026-10-02 요청. 관리 UI 는 단계 흐름(다음 단계·대기 방식)을 바꾸지 못하게 막혀 있어 DB 를 직접 고친다.
  10 turn_confirm  "진료실로 안내할까요?" 질문 유지, '네' → clinic_move('아니요'는 기존대로 finish_ask)
  11 counter_move / 12 counter_arrive  비활성(IsActive=0, 삭제 안 함 — UI 에서 다시 켤 수 있음)
  13 clinic_move   멘트 "진료실로 안내하겠습니다."
11·12 를 끄기만 하면 Jetson 엔진이 없는 단계(counter_move)를 만나 aborted_bad_step 으로 끝난다.

실행:   docker exec control-backend python data/voice_scenario_patches/hospital_skip_counter_robot10.py
되돌리기: 같은 명령 끝에 --revert
"""
import json
import sys

from app.database import voice_segment_test as seg
from app.database.database import SessionLocal
from app.database.models import VoiceScenarioStep as Step

SCENARIO_ID = 1
KEYS = ("SayText", "WaitFor", "ExpectedIntent", "NextStepCode", "OnNoStepCode",
        "OnTimeoutStepCode", "IsActive")
NEW = {
    "turn_confirm": {"SayText": "{대기번호}번 순서가 되었습니다. 진료실로 안내해 드릴까요?",
                     "WaitFor": "user", "ExpectedIntent": "YES_NO", "NextStepCode": "clinic_move",
                     "OnNoStepCode": "finish_ask", "OnTimeoutStepCode": None},
    "counter_move": {"IsActive": 0},
    "counter_arrive": {"IsActive": 0},
    "clinic_move": {"SayText": "진료실로 안내하겠습니다."},
}
# 바꾸기 전 값(2026-10-02 DB 확인) — --revert 용
OLD = {
    "turn_confirm": {"SayText": "{대기번호}번 순서가 되었습니다. {창구}번 접수창구로 안내해 드릴까요?",
                     "WaitFor": "user", "ExpectedIntent": "YES_NO", "NextStepCode": "counter_move",
                     "OnNoStepCode": "finish_ask", "OnTimeoutStepCode": None},
    "counter_move": {"IsActive": 1},
    "counter_arrive": {"IsActive": 1},
    "clinic_move": {"SayText": "{목적지} 앞으로 안내하겠습니다."},
}

db = SessionLocal()
rows = {r.StepCode: r for r in db.query(Step).filter(
    Step.ScenarioId == SCENARIO_ID, Step.StepCode.in_(list(NEW))).all()}
assert set(rows) == set(NEW), f"단계를 못 찾음: {set(NEW) - set(rows)}"


def dump(label: str) -> None:
    print(label, json.dumps({c: {k: getattr(r, k) for k in KEYS} for c, r in rows.items()},
                            ensure_ascii=False, indent=1))


dump("변경 전")
for code, fields in (OLD if "--revert" in sys.argv else NEW).items():
    for k, v in fields.items():
        setattr(rows[code], k, v)
db.commit()
dump("변경 후")

active = (db.query(Step).filter(Step.ScenarioId == SCENARIO_ID, Step.IsActive == 1)
          .order_by(Step.StepOrder).all())
codes = {s.StepCode for s in active}
dangling = [(s.StepCode, n) for s in active
            for n in (s.NextStepCode, s.OnNoStepCode, s.OnTimeoutStepCode) if n and n not in codes]
print("켜진 단계에서 꺼진/없는 단계를 가리키는 연결:", dangling or "없음")
print("seat_wait → clinic_wait 이어짐:", seg.reachable(active, "seat_wait", "clinic_wait"))
print("greet → finish 이어짐:", seg.reachable(active, "greet", "finish"))
