"""robot 10 목적지 사전: '3층관공서' 항목 추가 + 관공서 계열 동의어를 엘리베이터 항목에서 이동.

2026-10-02. 로컬 Whisper 어휘 힌트에는 목적지 '이름'만 들어가 '관공서'가 빠져 있었고,
'아침부터 공서', '공소' 처럼 깨져 추가 목적지 인식에 실패했다. 이름이 '3층관공서'인 항목을
두면 힌트에 들어간다. 이름은 붙여 쓴다 — 띄어 쓰면 무음 힌트 에코 필터(is_hint_echo)가 못 거른다.
유사 이름 차단(같은 이름이 다른 장소로 데려가는 사고 방지)에 걸리지 않도록 관공서 계열
동의어는 엘리베이터 항목에서 옮긴다. 두 항목 모두 같은 장소(맵 42 '엘리베이터 입구')로 간다.

실행:   docker exec -e PYTHONPATH=/app control-backend python data/voice_scenario_patches/slot_gwangongseo_robot10.py
되돌리기: 같은 명령 끝에 --revert
"""
import sys

from app.database.database import SessionLocal
from app.database.models import VoiceSlotLocation, VoiceSlotValue
from app.database.routes.voice import SIMILARITY_BLOCK, SLOT_NAME, _check_similar_names
from app.database.voice_slot_links import replace_links, validate_location_ids

ROBOT_ID = 10
ELEVATOR_SLOT_ID = 17
NEW_VALUE = "3층관공서"
ELEVATOR_LOCATION_ID = 388          # 맵 42 '엘리베이터 입구'
OLD_ELEVATOR_SYNONYMS = ("3층 관공서,엘리베이터,층 이동,층,3층,다른 층,관공서,관공소,관공석,삼층,"
                         "엘레베이터,엘리베타,엘레베타,삼층,층")
NEW_ELEVATOR_SYNONYMS = "엘리베이터,층 이동,층,3층,다른 층,삼층,엘레베이터,엘리베타,엘레베타"
NEW_SYNONYMS = "3층 관공서,삼층 관공서,관공서,관공소,관공석,공서,공소"

db = SessionLocal()
elevator = db.get(VoiceSlotValue, ELEVATOR_SLOT_ID)
assert elevator and elevator.RobotId == ROBOT_ID and elevator.Value == "엘리베이터 입구"
existing = db.query(VoiceSlotValue).filter(
    VoiceSlotValue.RobotId == ROBOT_ID, VoiceSlotValue.Value == NEW_VALUE).first()


def show(label: str) -> None:
    rows = db.query(VoiceSlotValue).filter(VoiceSlotValue.RobotId == ROBOT_ID).all()
    print(label)
    for s in rows:
        links = [lk.LocationId for lk in
                 db.query(VoiceSlotLocation).filter(VoiceSlotLocation.SlotValueId == s.id)]
        print(f"  #{s.id} {s.Value!r} 장소={links} 동의어={s.Synonyms} 도착안내={s.ArrivalText}")


show("변경 전")
if "--revert" in sys.argv:
    if existing:
        db.delete(existing)
    elevator.Synonyms = OLD_ELEVATOR_SYNONYMS
    db.commit()
else:
    assert not existing, f"'{NEW_VALUE}' 항목이 이미 있다(#{existing.id}) — 이미 적용됨"
    elevator.Synonyms = NEW_ELEVATOR_SYNONYMS
    db.flush()
    conflict = _check_similar_names(db, ROBOT_ID, NEW_VALUE)
    print("유사 이름 검사:", conflict)
    if conflict and conflict["score"] >= SIMILARITY_BLOCK:
        db.rollback()
        sys.exit("유사 이름 차단 — 적용하지 않음")
    locations = validate_location_ids(db, [ELEVATOR_LOCATION_ID])
    slot = VoiceSlotValue(RobotId=ROBOT_ID, SlotName=SLOT_NAME, Value=NEW_VALUE,
                          Synonyms=NEW_SYNONYMS, ArrivalText=elevator.ArrivalText)
    db.add(slot)
    db.flush()
    replace_links(db, slot, locations)
    db.commit()
show("변경 후")
