"""본사 1F/3F 시연 시나리오(robot 10) — 대화 턴 축소 대본 적용.

기준: docs/본사_Demo_시연_관제단계표_261019.md (대본 docs/본사_Demo_시연_수정대본_Ji_261019_개정안.md)
관리 UI 는 단계 흐름(다음 단계·대기 방식·단계 추가)을 바꾸지 못해 DB 를 직접 고친다.

  1F 병원(hospital)      흐름 재구성 — 손잡이·준비 확인, 편의점 분기, 준비 확인 한 번으로 합침, 수납·추가 목적지 합침
                          단계 9개 추가, 4개 비활성(guide_start · turn_confirm · resume_ask · payment_arrive)
  3F 관공서(public_office) 멘트만 바꾼다(흐름 그대로)
  목적지 사전 '3층관공서' 도착 문구({도착안내}) — 1F 시연 마지막 멘트

바꾸기 전 값은 처음 적용할 때 _backup/<이 파일 이름>.json 에 남긴다(Backend/data 는 호스트에 마운트돼 남는다).
다시 실행해도 백업은 처음 것을 유지한다. 되돌리면 백업 값으로 복원하고 추가한 단계는 끈다(지우지 않음).

운영자 단계 자동 진행 시간(AutoAdvanceSec, E9)은 그 칸이 있는 관제에서만 넣는다 — 없으면 건너뛰고 알린다.
편의점 분기 A(방문자 "대기 자리로 안내해 줘")는 E2(운영자 대기 중 음성 출구) 전까지 도달하지 않는다.

실행:     docker exec -e PYTHONPATH=/app control-backend python data/voice_scenario_patches/demo_hq_20261019_robot10.py [--dry-run]
되돌리기:  docker exec -e PYTHONPATH=/app control-backend python data/voice_scenario_patches/demo_hq_20261019_robot10.py --revert
"""
import json
import os
import sys

from app.database import voice_segment_test as seg
from app.database.database import SessionLocal
from app.database.models import LocationInfo, VoiceScenario, VoiceScenarioStep as Step, VoiceSlotValue

ROBOT_ID = 10
NAME = os.path.splitext(os.path.basename(__file__))[0]
BACKUP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backup", f"{NAME}.json")

FIELDS = ("SayText", "WaitFor", "ExpectedIntent", "ActionType", "SlotName", "TargetLocationId",
          "NextStepCode", "OnNoStepCode", "OnTimeoutStepCode", "ResponseTimeoutSec", "RetryCount",
          "IsActive", "AutoAdvanceSec")
HAS_AUTO = hasattr(Step, "AutoAdvanceSec")

# 장소는 id 로 고정하고 이름으로 한 번 더 확인한다 — 장소를 지웠다 다시 만들어
# id 가 다른 곳을 가리키면 이름이 달라 멈춘다. 병원 시나리오는 10-14 부터 맵 47 을 쓴다(2026-10-20 DB 확인).
RECEPTION, SEAT, PAYMENT = (437, "원무과 접수"), (438, "진료대기장소"), (441, "원무과수납")
STORE = (439, "편의점입구")

READY_TRY = dict(ResponseTimeoutSec=30, RetryCount=3)

HOSPITAL_UPDATE = {
    "greet": dict(SayText="안녕하세요. 병원에 오신 것을 환영합니다. 오늘 안내를 도와드릴 로봇입니다.",
                  NextStepCode="handle_guide"),
    "purpose_ask": dict(SayText="오늘은 어떤 진료를 받으러 오셨나요?"),
    "purpose_confirm": dict(SayText="{목적지}로 안내해 드릴까요?", NextStepCode="ticket_move"),
    "guide_start": dict(IsActive=0),
    "ticket_move": dict(SayText="먼저 접수를 위해 원무과로 모시겠습니다. 출발하거나 방향을 바꾸고 멈출 때 "
                                "미리 알려드릴게요. 천천히 출발하겠습니다.", _target=RECEPTION),
    "ticket_wait": dict(SayText="원무과에 도착했습니다. 접수 대기번호를 발급해 드릴게요. 잠시만 기다려 주세요."),
    "ticket_announce": dict(SayText="대기번호는 {대기번호}번입니다. 다시 한번 알려드릴게요. {대기번호}번입니다."),
    "seat_move": dict(SayText="이제 진료를 기다리실 자리로 모시겠습니다. 천천히 출발하겠습니다.",
                      NextStepCode="store_ask", _target=SEAT),
    "seat_wait": dict(SayText="대기 공간에 도착했습니다. 환자분 [현장 확인된 의자 방향]에 의자가 있습니다. "
                              "천천히 앉아 주세요. 진료 순서가 되면 알려드릴게요.",
                      ExpectedIntent="RESUME", NextStepCode="turn_ready", AutoAdvanceSec=20),
    "turn_confirm": dict(IsActive=0),
    "clinic_move": dict(SayText="{목적지} 진료실로 출발하겠습니다. 천천히 이동하겠습니다."),
    "clinic_wait": dict(SayText="진료실 입구입니다. 의료진의 안내에 따라 진료를 받아 주세요.",
                        NextStepCode="payment_ready", AutoAdvanceSec=-1),
    "resume_ask": dict(IsActive=0),
    "payment_move": dict(SayText="출발하겠습니다.", NextStepCode="exit_ask", _target=PAYMENT),
    "payment_arrive": dict(IsActive=0),
    "exit_ask": dict(SayText="수납 창구 앞에 도착했습니다. 수납을 마치시면 더 안내가 필요한 곳을 말씀해 주세요. "
                             "없으시면 출구까지 모셔다 드릴게요.", ResponseTimeoutSec=120, RetryCount=1),
    "extra_move": dict(SayText="{목적지}로 가시는군요. 안내해 드리겠습니다."),
    "finish": dict(SayText="안내를 마치겠습니다. 이용해 주셔서 감사합니다."),
}

# (StepOrder, 단계) — 기존 단계 사이에 끼도록 순서를 고른다
HOSPITAL_INSERT = {
    "handle_guide": (12, dict(SayText="제 몸체 위쪽 가운데에 있는 안내용 손잡이를 잡아 주세요. 천천히 찾으셔도 괜찮습니다.",
                              WaitFor="none", NextStepCode="ready_check")),
    "ready_check": (14, dict(SayText="손잡이를 잡으셨으면 '준비됐어요'라고 말씀해 주세요.",
                             WaitFor="user", ExpectedIntent="YES_NO", NextStepCode="purpose_ask",
                             OnNoStepCode="ready_check", OnTimeoutStepCode="ready_check", **READY_TRY)),
    "store_ask": (82, dict(SayText="진료 대기 공간에 도착했습니다. 환자분 [현장 확인된 의자 방향]에 의자가 있습니다. "
                                   "천천히 앉아 주세요. 진료 순서가 되면 제가 알려드릴게요. "
                                   "기다리시는 동안 편의점에 다녀오시겠어요?",
                           WaitFor="user", ExpectedIntent="YES_NO", NextStepCode="store_move",
                           OnNoStepCode="seat_stay", OnTimeoutStepCode="seat_stay",
                           ResponseTimeoutSec=10, RetryCount=2)),
    "seat_stay": (84, dict(SayText="알겠습니다. 여기서 편하게 기다려 주세요.", WaitFor="operator",
                           ExpectedIntent="RESUME", NextStepCode="turn_ready", AutoAdvanceSec=30)),
    "store_move": (86, dict(SayText="편의점 입구까지 모시겠습니다. 천천히 출발하겠습니다.", WaitFor="arrival",
                            ActionType="navigate", NextStepCode="store_wait", _target=STORE)),
    "store_wait": (88, dict(SayText="편의점 입구에 도착했습니다. 대기 자리로 돌아가실 때는 '대기 자리로 안내해 줘'라고 "
                                    "말씀해 주세요. 진료 순서가 되면 제가 먼저 알려드릴게요.",
                            WaitFor="operator", ExpectedIntent="RESUME", NextStepCode="turn_ready",
                            AutoAdvanceSec=-1)),
    # 분기 A — E2(음성 출구)가 store_wait 에서 이리로 보낸다. 그 전에는 도달하지 않는다.
    "store_return_move": (89, dict(SayText="네, 대기 자리로 모시겠습니다. 천천히 출발하겠습니다.", WaitFor="arrival",
                                   ActionType="navigate", NextStepCode="seat_wait", _target=SEAT)),
    "turn_ready": (95, dict(SayText="환자분, 진료 순서가 되었습니다. {목적지} 진료실로 모실게요. "
                                    "손잡이를 잡으시고 '준비됐어요'라고 말씀해 주세요.",
                            WaitFor="user", ExpectedIntent="YES_NO", NextStepCode="clinic_move",
                            OnNoStepCode="turn_ready", OnTimeoutStepCode="turn_ready", **READY_TRY)),
    "payment_ready": (145, dict(SayText="진료 수고하셨습니다. 수납을 위해 원무과로 모실게요. "
                                        "준비되시면 '준비됐어요'라고 말씀해 주세요.",
                                WaitFor="user", ExpectedIntent="YES_NO", NextStepCode="payment_move",
                                OnNoStepCode="payment_ready", OnTimeoutStepCode="payment_ready", **READY_TRY)),
}

OFFICE_UPDATE = {
    "purpose_ask": dict(SayText="오늘은 어떤 업무를 보러 오셨나요?"),
    "purpose_confirm": dict(SayText="{목적지} 업무가 맞으신가요?"),
    "ticket_move": dict(SayText="{목적지} 신청이시군요. 먼저 안내데스크로 모시겠습니다. 천천히 따라와 주세요."),
    "ticket_wait": dict(SayText="안내데스크에 도착했습니다. 대기번호표를 발급해 드릴게요. 잠시만 기다려 주세요."),
    "form_move": dict(SayText="이제 신청서를 작성하실 곳으로 안내하겠습니다."),
    "form_write": dict(SayText="신청서 작성 장소에 도착했습니다. 작성을 마치시면 '작성 완료'라고 말씀해 주세요."),
    "form_done": dict(SayText="수고하셨습니다. 이제 대기 공간으로 안내하겠습니다."),
    "seat_wait": dict(SayText="대기 공간에 도착했습니다. 앉아서 기다려 주시면 순서가 되었을 때 알려드릴게요.",
                      AutoAdvanceSec=15),
    "turn_confirm": dict(SayText="{대기번호}번 고객님, 순서가 되었습니다. {창구}번 창구로 안내해 드릴까요?"),
    "counter_move": dict(SayText="{창구}번 창구로 모시겠습니다. 천천히 따라와 주세요."),
    "counter_arrive": dict(SayText="{창구}번 창구에 도착했습니다. 담당 직원의 안내에 따라 업무를 진행해 주세요."),
    "lobby_move": dict(SayText="로비로 안내하겠습니다. 천천히 따라와 주세요."),
    "finish": dict(SayText="안내를 마치겠습니다. 이용해 주셔서 감사합니다."),
}

SLOT_ARRIVAL = {"3층관공서": "여기서 잠시 기다려 주세요. 병원 안내는 여기까지입니다. 3층에서 이어서 안내해 드릴게요."}

# 도달해야 하는 경로(시작 → 끝) — 하나라도 끊기면 적용하지 않는다
HOSPITAL_PATHS = [("greet", "finish"), ("greet", "extra_arrive"), ("store_ask", "turn_ready"),
                  ("store_wait", "turn_ready"), ("store_return_move", "turn_ready"),
                  ("turn_ready", "payment_ready")]
OFFICE_PATHS = [("greet", "finish")]


class PatchError(Exception):
    pass


def _scenario(db, code: str) -> VoiceScenario:
    sc = db.query(VoiceScenario).filter(VoiceScenario.RobotId == ROBOT_ID, VoiceScenario.Code == code).first()
    if not sc:
        raise PatchError(f"robot {ROBOT_ID} 에 시나리오 {code} 가 없다")
    return sc


def _place(db, map_id: int, spec: tuple, store_id: int | None) -> int:
    """(id, 이름) 장소를 확인해 id 를 준다. id 가 시나리오 맵에 없거나 이름이 다르면 멈춘다."""
    pid, name = spec
    store = pid is None
    if store:
        if not store_id:
            on_map = [(p.id, p.LacationName) for p in db.query(LocationInfo).filter(
                LocationInfo.MapId == map_id).all() if name in (p.LacationName or "")]
            raise PatchError(f"편의점 장소 id 를 --store-id 로 지정할 것 (맵 {map_id} 의 '{name}' 장소: "
                             f"{on_map or '없음 — 먼저 등록'})")
        pid = store_id
    hit = db.query(LocationInfo).filter(LocationInfo.id == pid).first()
    if not hit or hit.MapId != map_id:
        raise PatchError(f"장소 {pid} 가 시나리오 맵({map_id})에 없다"
                         + (f" — 맵 {hit.MapId} 의 '{hit.LacationName}'" if hit else ""))
    ok = name in (hit.LacationName or "") if store else hit.LacationName == name
    if not ok:
        raise PatchError(f"장소 {pid} 이름이 '{hit.LacationName}' — '{name}' 이어야 한다(장소가 바뀌었는지 확인)")
    return hit.id


def _resolved(db, sc, fields: dict, store_id) -> dict:
    out = {k: v for k, v in fields.items() if not k.startswith("_")}
    if "_target" in fields:
        out["TargetLocationId"] = _place(db, sc.MapId, fields["_target"], store_id)
    if not HAS_AUTO:
        out.pop("AutoAdvanceSec", None)
    return out


def _snapshot(row) -> dict:
    return {k: getattr(row, k) for k in FIELDS if hasattr(row, k)}


def _check(db, sc, paths) -> list[str]:
    """켜진 단계에서 꺼진·없는 단계로 가는 연결, 끊긴 경로, 장소 없는 이동 단계."""
    active = (db.query(Step).filter(Step.ScenarioId == sc.id, Step.IsActive == 1)
              .order_by(Step.StepOrder).all())
    codes = {s.StepCode for s in active}
    problems = [f"{s.StepCode} → {n} (꺼졌거나 없음)" for s in active
                for n in (s.NextStepCode, s.OnNoStepCode, s.OnTimeoutStepCode) if n and n not in codes]
    problems += [f"경로 끊김 {a} → {b}" for a, b in paths if not seg.reachable(active, a, b)]
    problems += [f"{s.StepCode} 이동 장소 없음" for s in active
                 if s.ActionType == "navigate" and not s.SlotName and not s.TargetLocationId]
    return problems


def apply(db, store_id: int | None = None) -> dict:
    hosp, office = _scenario(db, "hospital"), _scenario(db, "public_office")
    first = not os.path.exists(BACKUP)
    backup = {"steps": {}, "inserted": [], "slots": {}} if first else json.load(open(BACKUP, encoding="utf-8"))
    changed = []

    for sc, table in ((hosp, HOSPITAL_UPDATE), (office, OFFICE_UPDATE)):
        rows = {r.StepCode: r for r in db.query(Step).filter(Step.ScenarioId == sc.id).all()}
        missing = set(table) - set(rows)
        if missing:
            raise PatchError(f"{sc.Code} 에 단계가 없다: {sorted(missing)}")
        for code, fields in table.items():
            row = rows[code]
            backup["steps"].setdefault(f"{sc.Code}.{code}", _snapshot(row))
            for k, v in _resolved(db, sc, fields, store_id).items():
                if getattr(row, k) != v:
                    setattr(row, k, v)
                    changed.append(f"{sc.Code}.{code}.{k}")

    rows = {r.StepCode: r for r in db.query(Step).filter(Step.ScenarioId == hosp.id).all()}
    for code, (order, fields) in HOSPITAL_INSERT.items():
        vals = {"WaitFor": "none", "ActionType": "none", "IsActive": 1, **_resolved(db, hosp, fields, store_id)}
        row = rows.get(code)
        if row is None:
            db.add(Step(ScenarioId=hosp.id, StepCode=code, StepOrder=order, **vals))
            changed.append(f"hospital.{code} (추가)")
        else:                                   # 되돌린 뒤 다시 적용 — 꺼 둔 행을 다시 쓴다
            for k, v in vals.items():
                if getattr(row, k) != v:
                    setattr(row, k, v)
                    changed.append(f"hospital.{code}.{k}")
        if code not in backup["inserted"]:
            backup["inserted"].append(code)

    for value, text in SLOT_ARRIVAL.items():
        sl = db.query(VoiceSlotValue).filter(VoiceSlotValue.RobotId == ROBOT_ID,
                                             VoiceSlotValue.Value == value).first()
        if not sl:
            raise PatchError(f"목적지 사전에 '{value}' 가 없다")
        backup["slots"].setdefault(value, sl.ArrivalText)
        if sl.ArrivalText != text:
            sl.ArrivalText = text
            changed.append(f"목적지 {value}.ArrivalText")

    db.flush()
    problems = _check(db, hosp, HOSPITAL_PATHS) + _check(db, office, OFFICE_PATHS)
    if problems:
        raise PatchError("적용 후 검사 실패: " + "; ".join(problems))
    return {"changed": changed, "backup": backup, "first": first}


def revert(db) -> dict:
    if not os.path.exists(BACKUP):
        raise PatchError(f"백업이 없다({BACKUP}) — 적용한 적이 없거나 이미 되돌렸다")
    backup = json.load(open(BACKUP, encoding="utf-8"))
    changed = []
    scs = {"hospital": _scenario(db, "hospital"), "public_office": _scenario(db, "public_office")}
    for key, old in backup["steps"].items():
        code_sc, code = key.split(".", 1)
        row = db.query(Step).filter(Step.ScenarioId == scs[code_sc].id, Step.StepCode == code).first()
        for k, v in old.items():
            if hasattr(row, k) and getattr(row, k) != v:
                setattr(row, k, v)
                changed.append(f"{key}.{k}")
    for code in backup["inserted"]:
        row = db.query(Step).filter(Step.ScenarioId == scs["hospital"].id, Step.StepCode == code).first()
        if row and row.IsActive:
            row.IsActive = 0
            changed.append(f"hospital.{code} (끔)")
    for value, text in backup["slots"].items():
        sl = db.query(VoiceSlotValue).filter(VoiceSlotValue.RobotId == ROBOT_ID,
                                             VoiceSlotValue.Value == value).first()
        if sl and sl.ArrivalText != text:
            sl.ArrivalText = text
            changed.append(f"목적지 {value}.ArrivalText")
    db.flush()
    return {"changed": changed}


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    store_id = int(argv[argv.index("--store-id") + 1]) if "--store-id" in argv else None
    db = SessionLocal()
    try:
        if "--revert" in argv:
            out = revert(db)
        else:
            out = apply(db, store_id)
        print(f"바뀐 항목 {len(out['changed'])}개:")
        for c in out["changed"]:
            print("  ", c)
        if not HAS_AUTO and "--revert" not in argv:
            print("※ 이 관제에는 AutoAdvanceSec(E9) 칸이 없어 자동 진행 시간은 넣지 않았다 — "
                  "E9 배포 뒤 다시 실행할 것")
        if dry:
            db.rollback()
            print("(--dry-run: 저장하지 않음)")
            return 0
        db.commit()
        if "--revert" in argv:
            os.replace(BACKUP, BACKUP + ".reverted")
        elif out["first"]:
            os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
            with open(BACKUP, "w", encoding="utf-8") as f:
                json.dump(out["backup"], f, ensure_ascii=False, indent=1)
            print(f"백업: {BACKUP}")
        return 0
    except PatchError as e:
        db.rollback()
        print(f"[중단] {e} — 아무것도 바꾸지 않았다")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
