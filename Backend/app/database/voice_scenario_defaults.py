"""다단계 안내 시나리오 기본값 — 병원(11개 구간) · 관공서(9개 구간) · 주차장(엘리베이터 안내).

원본 사양: QCS_Docs/09_Reference/Docs/UND_병원_관공서_관제시스템_사전정의데이터_음성멘트_VER01_Ji260916.md
설계:     QCS_Docs/02_Backend/음성-시나리오-단계엔진-설계.md

사양의 한 '구간'(예: 5.6 접수창구 안내)은 발화·대기·이동이 섞여 있어 단계 여러 개로 펴진다.
단계 하나는 [발화] -> [WaitFor] -> [다음 단계] 한 번이다.

멘트의 치환자는 Jetson 단계 엔진이 채운다.
    {목적지}   고객 목적지 — 사용자가 말해 확정된 곳(진료과·민원 종류) — 세션 DestinationName.
               고정 장소 이동(발급기·대기석 …)은 이 값을 바꾸지 않는다(세션 NavTargetName 에 따로 남는다).
    {도착지}   실제로 도착하는 장소 이름 — 목적지와 같은 곳이면 목적지 이름
    {도착안내} 목적지 사전의 도착 문구(목적지와 도착 장소가 다를 때의 안내). 없으면 빈칸
    {대기번호} 운영자가 입력한 대기번호 — 세션 TicketNo
    {창구}     운영자가 입력한 창구 번호 — 세션 CounterNo
'{목적지}로' · '{도착지}로' 는 엔진이 받침에 맞춰 '로/으로' 를 고른다. 다른 조사는 받침에 따라
갈리므로 치환자 바로 뒤에 두지 않는다("{목적지} 앞으로").

운영자 단계(WaitFor=operator)는 ExpectedIntent 로 무엇을 입력받는지 표시한다.
    TICKET  대기번호 입력        COUNTER 창구 번호 입력(순번 도래)
    RESUME  작업 재개(진료 후 재호출 등)
"""

from sqlalchemy.orm import Session

from app.database.models import LocationInfo, RobotInfo, VoiceScenario, VoiceScenarioStep

WAIT_NONE, WAIT_USER, WAIT_OPERATOR, WAIT_ARRIVAL = "none", "user", "operator", "arrival"
OPERATOR_INPUTS = ("TICKET", "COUNTER", "RESUME")
# PURPOSE: 목적지 사전(진료과·민원)에서만 / DESTINATION: 사전 + 운영 맵의 모든 장소
USER_INTENTS = ("PURPOSE", "DESTINATION", "YES_NO", "FORM_COMPLETE")

SLOT_DEST = "목적지"

# 고정 목적지 — 장소 이름에 이 말이 들어간 장소가 시나리오 맵에 **하나뿐이면** 자동으로 연결한다.
# 둘 이상이면 연결하지 않는다(엉뚱한 곳으로 데려가는 것보다 '미연결' 경고가 낫다).
TARGET_HINTS: dict[str, tuple[str, ...]] = {
    "ticket": ("번호표", "발급기", "키오스크"),
    "waiting": ("대기구역", "대기석", "대기 자리", "대기자리"),
    "reception": ("접수창구", "접수"),
    "payment": ("수납",),
    "exit": ("출구",),
    "civil": ("민원창구", "민원 창구"),
    "lobby": ("로비",),
    # 주차장 — 로봇이 서 있는 엘리베이터 앞 대기 장소 / 방문객을 맞는 안내 장소
    "robot_waiting": ("로봇대기",),
    "robot_greeting": ("로봇안내",),
}


def _s(code, say, wait=WAIT_NONE, nxt=None, *, intent=None, action="none", slot=None,
       target=None, on_no=None, on_timeout=None, timeout=10, retry=2) -> dict:
    return dict(StepCode=code, SayText=say, WaitFor=wait, NextStepCode=nxt,
                ExpectedIntent=intent, ActionType=action, SlotName=slot, _target=target,
                OnNoStepCode=on_no, OnTimeoutStepCode=on_timeout,
                ResponseTimeoutSec=timeout, RetryCount=retry)


def _nav_fixed(code, say, target, nxt) -> dict:
    return _s(code, say, WAIT_ARRIVAL, nxt, action="navigate", target=target)


def _nav_slot(code, say, nxt) -> dict:
    return _s(code, say, WAIT_ARRIVAL, nxt, action="navigate", slot=SLOT_DEST)


def _tail(arrive_next: str | None) -> list[dict]:
    """추가 안내 · 종료 — 병원 5.11 / 관공서 6.9 공통.

    arrive_next: 추가 목적지에 도착한 뒤 갈 단계. 병원은 출구 확인에서 이미 추가 안내를 물었고
    그 목적지가 엘리베이터(다른 층)라 도착 멘트로 끝낸다(None). 관공서는 다시 묻는다.
    """
    return [
        _s("finish_ask", "추가로 안내가 필요한 곳이 있으신가요?", WAIT_USER, "extra_dest",
           intent="YES_NO", on_no="finish", on_timeout="finish"),
        _s("extra_dest", "어디로 안내해 드릴까요?", WAIT_USER, "extra_move",
           intent="DESTINATION", on_no="finish", on_timeout="finish"),
        _nav_slot("extra_move", "{목적지}로 안내하겠습니다.", "extra_arrive"),
        # {도착지}: 실제 도착 장소, {도착안내}: 목적지 사전의 도착 문구(목적지와 도착 장소가 다를 때)
        _s("extra_arrive", "{도착지}에 도착했습니다. {도착안내}", nxt=arrive_next),
        _s("finish", "안내를 종료하겠습니다. 이용해 주셔서 감사합니다."),
    ]

HOSPITAL = {
    "Code": "hospital",
    "DisplayName": "병원 안내",
    "SortOrder": 0,
    "FloorHints": ("1층", "1F"),
    # 경유지 방향 안내("잠시 후 왼쪽으로 방향을 바꾸겠습니다") — 보이지 않는 방문객 안내
    "TurnCues": True,
    "steps": [
        # 5.1 입구 호출 — 세션은 운영자가 연다(1M). 호출 위치를 정하면 로봇이 그곳으로 먼저
        # 이동한 뒤(세션 가상 단계 call_move) 이 단계부터 돈다.
        _s("greet", "안녕하세요. 안내를 도와드리겠습니다.", nxt="purpose_ask"),
        # 5.2 진료 목적 확인
        _s("purpose_ask", "어떤 진료를 위해 방문하셨나요?", WAIT_USER, "purpose_confirm",
           intent="PURPOSE"),
        _s("purpose_confirm", "{목적지} 안내가 맞으신가요?", WAIT_USER, "guide_start",
           intent="YES_NO", on_no="purpose_ask"),
        _s("guide_start", "안내를 시작하겠습니다. 제 안내에 따라 천천히 이동해 주세요.",
           nxt="ticket_move"),
        # 5.3 접수 구역 안내
        _nav_fixed("ticket_move", "대기번호 발급 위치로 안내하겠습니다.", "ticket", "ticket_wait"),
        # 5.4 대기번호 발급 — 1M 은 운영자 수동 입력
        _s("ticket_wait", "대기번호 발급 위치에 도착했습니다. 대기번호를 발급해 드리겠습니다. 잠시만 기다려 주세요.",
           WAIT_OPERATOR, "ticket_announce", intent="TICKET"),
        _s("ticket_announce", "환자님의 대기번호는 {대기번호}번입니다.", nxt="seat_move"),
        # 5.5 대기자리 안내
        _nav_fixed("seat_move", "대기 자리로 안내하겠습니다.", "waiting", "seat_wait"),
        # 5.6 순번 도래 — 운영자가 창구 번호를 넣으면 진행
        _s("seat_wait", "대기 위치에 도착했습니다. 순서가 되면 다시 안내해 드리겠습니다.",
           WAIT_OPERATOR, "turn_confirm", intent="COUNTER"),
        _s("turn_confirm", "{대기번호}번 순서가 되었습니다. {창구}번 접수창구로 안내해 드릴까요?",
           WAIT_USER, "counter_move", intent="YES_NO", on_no="finish_ask"),
        _nav_fixed("counter_move", "{창구}번 접수창구로 안내하겠습니다.", "reception", "counter_arrive"),
        _s("counter_arrive",
           "{창구}번 접수창구에 도착했습니다. 접수를 마치시면 '네'라고 말씀해 주세요.",
           WAIT_USER, "clinic_move", intent="YES_NO", on_no="finish_ask",
           on_timeout="finish_ask", timeout=120, retry=1),
        # 5.7 진료실 안내 — 진료 목적의 목적지(진료과 → 진료실 장소)
        _nav_slot("clinic_move", "{목적지} 앞으로 안내하겠습니다.", "clinic_wait"),
        # 5.8 진료 후 재호출 — 진료가 끝나면 운영자가 재개한다
        _s("clinic_wait", "{목적지} 앞에 도착했습니다. 이곳에서 진료를 기다려 주세요.",
           WAIT_OPERATOR, "resume_ask", intent="RESUME"),
        # 5.9 수납 창구
        _s("resume_ask", "진료 보시느라 수고하셨습니다. 수납 창구로 안내해 드릴까요?",
           WAIT_USER, "payment_move", intent="YES_NO", on_no="exit_ask"),
        _nav_fixed("payment_move", "수납 창구로 안내하겠습니다.", "payment", "payment_arrive"),
        _s("payment_arrive", "수납 창구에 도착했습니다.", nxt="exit_ask"),
        # 5.10 병원 출구 — 추가 목적지를 말하면 그곳으로("3층 관공서" → 엘리베이터 입구),
        # 없다·대답이 없으면 출구로. 출구가 기본이라 오래 기다리지 않는다(10초 × 2회).
        _s("exit_ask", "추가로 안내가 필요하신가요? 없으시면 병원 출구로 안내하겠습니다.",
           WAIT_USER, "extra_move", intent="DESTINATION", on_no="exit_move",
           on_timeout="exit_move", timeout=10, retry=1),
        _nav_fixed("exit_move", "출구로 안내하겠습니다.", "exit", "exit_arrive"),
        # 추가 안내는 출구 확인에서 이미 물었다 — 출구에 닿으면 인사하고 끝낸다
        _s("exit_arrive", "병원 출구에 도착했습니다.", nxt="finish"),
        # 5.11 서비스 종료 — 추가 목적지(3층 관공서 → 엘리베이터 입구)는 도착 멘트로 끝낸다
        *_tail(arrive_next=None),
    ],
}

PUBLIC_OFFICE = {
    "Code": "public_office",
    "DisplayName": "관공서 안내",
    "SortOrder": 1,
    "FloorHints": ("3층", "3F"),
    "TurnCues": False,
    "steps": [
        # 6.1 고객 안내 시작
        _s("greet", "안녕하세요. 민원 업무 안내를 도와드리겠습니다.", nxt="purpose_ask"),
        # 6.2 민원 목적 확인
        _s("purpose_ask", "어떤 민원 업무를 위해 방문하셨나요?", WAIT_USER, "purpose_confirm",
           intent="PURPOSE"),
        _s("purpose_confirm", "{목적지} 업무 안내가 맞으신가요?", WAIT_USER, "ticket_move",
           intent="YES_NO", on_no="purpose_ask"),
        # 6.3 대기번호 발급/등록 — 1M 은 운영자 수동 입력
        _nav_fixed("ticket_move", "대기번호 발급 위치로 안내하겠습니다.", "ticket", "ticket_wait"),
        _s("ticket_wait", "대기번호 발급 위치에 도착했습니다. 대기번호를 발급해 드리겠습니다. 잠시만 기다려 주세요.",
           WAIT_OPERATOR, "ticket_announce", intent="TICKET"),
        _s("ticket_announce", "고객님의 대기번호는 {대기번호}번입니다.", nxt="form_move"),
        # 6.4 신청서 위치 안내 — 민원 종류의 목적지(민원 → 신청서 비치 장소)
        _nav_slot("form_move", "{목적지} 신청서가 있는 곳으로 안내하겠습니다.", "form_write"),
        # 6.5 신청서 작성 — "작성 완료"
        _s("form_write",
           "신청서가 있는 곳에 도착했습니다. 작성을 마치시면 '작성 완료'라고 말씀해 주세요.",
           WAIT_USER, "form_done", intent="FORM_COMPLETE", on_timeout="finish_ask",
           timeout=300, retry=3),
        # 6.6 대기자리 안내
        _nav_fixed("form_done", "수고하셨습니다. 대기 자리로 안내하겠습니다.",
                   "waiting", "seat_wait"),
        # 6.7 순번 도래 확인
        _s("seat_wait", "대기 자리에 도착했습니다. 순서가 되면 다시 안내해 드리겠습니다.",
           WAIT_OPERATOR, "turn_confirm", intent="COUNTER"),
        _s("turn_confirm", "{대기번호}번 순서가 되었습니다. {창구}번 창구로 안내해 드릴까요?",
           WAIT_USER, "counter_move", intent="YES_NO", on_no="finish_ask"),
        # 6.8 민원 창구 안내
        _nav_fixed("counter_move", "{창구}번 민원 창구로 안내하겠습니다.", "civil", "counter_arrive"),
        _s("counter_arrive", "{창구}번 민원 창구에 도착했습니다.", nxt="lobby_ask"),
        # 6.9 로비 복귀 / 업무 종료 — 업무를 보는 동안 답이 늦을 수 있어 길게 기다린다
        _s("lobby_ask", "업무가 끝나시면 '로비로 안내해 줘'라고 말씀해 주세요.", WAIT_USER, "lobby_move",
           intent="YES_NO", on_no="finish_ask", on_timeout="finish_ask", timeout=120, retry=1),
        _nav_fixed("lobby_move", "로비로 안내하겠습니다.", "lobby", "lobby_arrive"),
        _s("lobby_arrive", "로비에 도착했습니다.", nxt="finish_ask"),
        *_tail(arrive_next="finish_ask"),
    ],
}

PARKING = {
    "Code": "parking",
    "DisplayName": "주차장 안내",
    "SortOrder": 2,
    "FloorHints": ("2층", "2F"),
    "TurnCues": False,
    "steps": [
        # 1. 안내 장소로 가서(멘트 없는 이동 — 관제 화면은 다음 단계의 '도착 장소'로 합쳐 보여 준다) 인사하며
        #    엘리베이터 안내가 필요한지 묻는다. 아니요·무응답이면 그 자리에서 끝낸다.
        #    멘트 없는 이동은 따라올 사람이 없어 관제가 기본 속도로 보낸다(voice_scenario.session_move).
        _nav_fixed("greet_move", None, "robot_greeting", "greet_ask"),
        _s("greet_ask", "안녕하세요. 엘리베이터로 안내해 드릴까요?", WAIT_USER, "elevator_move",
           intent="YES_NO", on_no="decline_end", on_timeout="decline_end", timeout=10, retry=1),
        # 엘리베이터 앞으로 데려간다. 경유지는 구간(route_info)이 끼우고, 도착 방향은 이동 장소의 yaw 다 —
        # 엘리베이터를 바라보게 하려면 그 방향으로 저장한 장소를 연결한다(층 이동 시드용 로봇 대기 장소와 따로).
        _nav_fixed("elevator_move", "안내를 시작하겠습니다. 제 뒤를 따라와 주세요.",
                   "robot_waiting", "elevator_arrive"),
        _s("elevator_arrive", "엘리베이터 입구에 도착했습니다. 앞쪽 엘리베이터를 이용해 주세요. 감사합니다."),
        _s("decline_end", "알겠습니다. 도움이 필요하시면 다시 불러 주세요."),
    ],
}

DEFAULT_SCENARIOS = (HOSPITAL, PUBLIC_OFFICE, PARKING)
DEFAULT_BY_CODE = {sc["Code"]: sc for sc in DEFAULT_SCENARIOS}


def default_step(code: str, step_code: str) -> dict | None:
    """기본값 한 단계 (관제 UI 의 '기본값으로' 에 쓴다)."""
    sc = DEFAULT_BY_CODE.get(code)
    for st in (sc or {}).get("steps", []):
        if st["StepCode"] == step_code:
            return st
    return None


def map_places(db: Session, map_id: int | None) -> list[LocationInfo]:
    """시나리오 맵의 장소(POI). 맵이 없으면 연결할 후보도 없다."""
    if not map_id:
        return []
    return (db.query(LocationInfo)
            .filter(LocationInfo.MapId == map_id)
            .all())


def _norm(name: str | None) -> str:
    """장소 이름 비교용 — 띄어쓰기·밑줄 차이를 무시한다(2F_로봇_대기장소 ↔ 로봇 대기)."""
    return (name or "").replace(" ", "").replace("_", "")


def _resolve_target(places: list[LocationInfo], key: str | None) -> int | None:
    """힌트에 걸리는 장소가 정확히 하나일 때만 연결한다."""
    if not key:
        return None
    hints = TARGET_HINTS.get(key, ())
    hits = [p for p in places if any(_norm(h) in _norm(p.LacationName) for h in hints)]
    return hits[0].id if len(hits) == 1 else None


def _resolve_floor(db: Session, robot: RobotInfo, hints: tuple[str, ...]) -> int | None:
    from app.database.models import FloorInfo

    q = db.query(FloorInfo)
    if robot.BusinessId:
        q = q.filter(FloorInfo.BusinessId == robot.BusinessId)
    norm = {h.replace(" ", "").upper() for h in hints}
    hits = [f for f in q.all() if (f.FloorName or "").replace(" ", "").upper() in norm]
    return hits[0].id if len(hits) == 1 else None


def _resolve_map(db: Session, robot: RobotInfo, floor_id: int | None) -> int | None:
    """층의 맵. 로봇이 지금 그 층이면 쓰고 있는 맵, 아니면 그 층에 맵이 하나뿐일 때만."""
    from app.database.models import RobotMapInfo

    if not floor_id:
        return None
    if robot.CurrentFloorId == floor_id and robot.CurrentMapId:
        return robot.CurrentMapId
    maps = db.query(RobotMapInfo.id).filter(RobotMapInfo.FloorId == floor_id).all()
    return maps[0].id if len(maps) == 1 else None


def _step_row(scenario_id: int, order: int, st: dict, places: list[LocationInfo]) -> VoiceScenarioStep:
    cols = {k: v for k, v in st.items() if not k.startswith("_")}
    return VoiceScenarioStep(ScenarioId=scenario_id, StepOrder=order,
                             TargetLocationId=_resolve_target(places, st.get("_target")),
                             IsActive=1, **cols)


def apply_defaults(db: Session, robot_id: int, codes: list[str] | None = None,
                   reset: bool = False) -> dict:
    """로봇에 기본 시나리오를 넣는다. 반환: {created: [...], reset: [...]}

    - 없는 시나리오는 만든다.
    - 있는 시나리오는 건드리지 않는다 — 운영자가 고친 멘트를 덮지 않는다.
      `reset=True` 면 단계를 기본값으로 되돌린다(층 · 맵 · 활성 여부는 유지).
    이동 장소는 시나리오 맵의 POI 중에서만 자동 연결한다.
    호출자가 commit 한다.
    """
    robot = db.query(RobotInfo).filter(RobotInfo.id == robot_id).first()
    if not robot:
        raise ValueError(f"로봇 {robot_id} 없음")
    created, was_reset = [], []

    for tpl in DEFAULT_SCENARIOS:
        if codes and tpl["Code"] not in codes:
            continue
        sc = (db.query(VoiceScenario)
              .filter(VoiceScenario.RobotId == robot_id, VoiceScenario.Code == tpl["Code"])
              .first())
        if sc and not reset:
            continue
        if not sc:
            floor_id = _resolve_floor(db, robot, tpl["FloorHints"])
            sc = VoiceScenario(RobotId=robot_id, Code=tpl["Code"], DisplayName=tpl["DisplayName"],
                               SortOrder=tpl["SortOrder"], IsActive=1, FloorId=floor_id,
                               MapId=_resolve_map(db, robot, floor_id),
                               TurnCuesEnabled=1 if tpl.get("TurnCues", True) else 0)
            db.add(sc)
            db.flush()
            created.append(tpl["Code"])
        else:
            db.query(VoiceScenarioStep).filter(VoiceScenarioStep.ScenarioId == sc.id).delete()
            db.flush()
            was_reset.append(tpl["Code"])
        places = map_places(db, sc.MapId)
        for i, st in enumerate(tpl["steps"], start=1):
            db.add(_step_row(sc.id, i * 10, st, places))

    return {"created": created, "reset": was_reset}
