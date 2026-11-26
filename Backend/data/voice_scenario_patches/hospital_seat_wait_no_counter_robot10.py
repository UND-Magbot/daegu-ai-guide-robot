"""병원 시나리오(robot 10, scenario 1): 대기 자리(seat_wait)의 창구 번호 입력을 없앤다.

2026-10-02 요청. 접수창구 단계(11·12)를 끈 뒤로 창구 번호는 어디에도 쓰이지 않는다.
순번 도래 신호는 여전히 운영자가 줘야 하므로 운영자 대기는 두고, 입력 종류만 COUNTER → RESUME.
관제 화면은 입력칸 없이 '안내 재개' 버튼만 보여 주고, Jetson 엔진은 단계가 넘어가는 것으로 진행한다.

실행:   docker exec -e PYTHONPATH=/app control-backend python data/voice_scenario_patches/hospital_seat_wait_no_counter_robot10.py
되돌리기: 같은 명령 끝에 --revert
"""
import sys

from app.database import voice_segment_test as seg
from app.database.database import SessionLocal
from app.database.models import VoiceScenarioStep as Step

SCENARIO_ID = 1
STEP_CODE = "seat_wait"
NEW, OLD = "RESUME", "COUNTER"

db = SessionLocal()
st = db.query(Step).filter(Step.ScenarioId == SCENARIO_ID, Step.StepCode == STEP_CODE).first()
assert st and st.WaitFor == "operator", "seat_wait 가 운영자 대기 단계가 아니다"

print(f"변경 전: {st.StepCode} wait={st.WaitFor} intent={st.ExpectedIntent} next={st.NextStepCode} "
      f"멘트={st.SayText!r}")
st.ExpectedIntent = OLD if "--revert" in sys.argv else NEW
db.commit()
print(f"변경 후: {st.StepCode} wait={st.WaitFor} intent={st.ExpectedIntent} next={st.NextStepCode}")

active = (db.query(Step).filter(Step.ScenarioId == SCENARIO_ID, Step.IsActive == 1)
          .order_by(Step.StepOrder).all())
print("greet → finish 이어짐:", seg.reachable(active, "greet", "finish"))
print("seat_wait → clinic_wait 미리 넣어야 할 값:", seg.required_inputs(active, "seat_wait", "clinic_wait"))
