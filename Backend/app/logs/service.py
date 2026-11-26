import time
import threading
import queue
from datetime import datetime

from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from app.database.models import LogDataInfo, Alert
from app.database.database import SessionLocal

# ── 중복 로그 억제 (쿨다운) ──
_cooldown_lock = threading.Lock()
_last_logged: dict[str, float] = {}       # action → 마지막 기록 시각
_suppressed_counts: dict[str, int] = {}   # action → 억제된 횟수
LOG_COOLDOWN_SEC = 30                     # 같은 action 반복 억제 시간(초)
# 쿨다운 예외: 매번 기록되어야 하는 action (알림 Detail에 누적)
# robot_error_code/resolved는 runtime의 차집합 감지가 이미 코드별 중복을 막으므로,
# action 단위 쿨다운으로 서로 다른 코드가 잘못 합쳐져 누락되지 않도록 제외한다(설계서 §11).
_COOLDOWN_EXEMPT = {
    "nav_arrival", "nav_loop", "nav_complete", "nav_error",
    "robot_error_code", "robot_error_resolved",
    # 모터 과열은 관절별 상태머신이 전이 시에만 발화하므로 action 쿨다운 제외(관절별 누락 방지)
    "motor_overheat_warning", "motor_overheat_danger", "motor_overheat_resolved",
    # 스케줄 실패는 회차마다 대상·사유가 다르다. action 쿨다운에 걸리면 30초 안에
    # 발생한 두 번째 회차의 실패가 조용히 사라져 "조용한 실패 금지"가 깨진다.
    "schedule_missed", "schedule_skip_offline", "schedule_gate_timeout", "schedule_battery_low",
    "schedule_wait_offline", "standby_offline_hold", "schedule_robot_missing",
    # 배터리 부족 충전 후 이어 가기 확인 — 요청마다 확인창이 하나씩 떠야 한다.
    # 끝맺음 로그는 서버 재시작 때 '사라진 요청'을 가리는 근거라 쿨다운에 묻히면 안 된다.
    "battery_resume_ask", "battery_resume_confirm", "battery_resume_accepted", "battery_resume_ended",
    "battery_resume_dropped", "battery_resume_done", "battery_resume_lost",
    "route_resume_after_charge", "standby_resume_after_charge",
    "schedule_ttl_exceeded", "schedule_return_failed", "schedule_state_error",
    # 응대는 몇 분 간격으로 연속 발생한다. 쿨다운에 걸리면 세션 로그가 통째로 빈다.
    "standby_session_start", "standby_session_end", "standby_session_timeout",
    "standby_dest_unknown", "standby_session_extended",
    # 안내 동행 이탈은 세션마다 한 번만 보낸다(voice_scenario.session_follow). 쿨다운에 묻히면 안 된다.
    "guide_follow_lost",
    # 안내 중 발화 실패도 세션마다 한 번만 보낸다(voice_scenario.session_tts_failed).
    "guide_tts_failed",
    # 관제가 스스로 닫은 안내 — 세션마다 한 번이다
    "guide_follow_lost_closed", "guide_engine_offline",
    # 안내 이동 막힘·도착 못 함은 세션·지점마다 한 번이다
    "guide_nav_blocked", "guide_nav_unblocked", "nav_hang_skip",
    "nav_unreached_hold", "nav_hold_resumed", "nav_hold_action", "nav_hold_timeout",
    # 안내 중 장애물 정지·재개·직원 호출은 정지마다 한 번이다(navigation/obstacle_hold)
    "guide_obstacle_stop", "guide_obstacle_resume", "guide_obstacle_staff", "guide_obstacle_retry_blocked",
    "standby_comm_lost", "standby_comm_lost_long", "standby_comm_restored",
}

# ── 큐 기반 단일 Writer ──
_log_queue: queue.Queue = queue.Queue(maxsize=500)

# 로그 Action → 알림 자동 생성 규칙
ALERT_TRIGGER_RULES = {
    # Action: (alert Type, alert Status)
    "robot_battery_low":       ("Robot", "error"),
    "robot_connection_error":  ("Robot", "error"),
    "rtsp_error":              ("Robot", "error"),
    "nav_error":               ("Schedule", "error"),
    "guide_nav_blocked":       ("Schedule", "error"),  # 안내 이동 중 60초 제자리 — 운영자 확인
    "guide_obstacle_staff":    ("Schedule", "error"),  # 안내 중 장애물 2분 — 직원 호출(관제 처리까지 대기)
    "nav_hang_skip":           ("Schedule", "error"),  # 이동 지점 도착 못 하고 건너뜀
    "nav_unreached_hold":      ("Schedule", "error"),  # 지점에 못 가 멈춤 — 운영자 선택 대기
    "nav_hold_timeout":        ("Schedule", "error"),  # 응답 없어 자동 처리
    # 근무 중 통신 두절 — 작업 종류라야 로봇 오프라인에도 알림이 억제되지 않는다
    "standby_comm_lost":       ("Schedule", "error"),
    "standby_comm_lost_long":  ("Schedule", "error"),
    "standby_comm_restored":   ("Schedule", "event"),
    "nav_comm_lost":           ("Robot", "error"),  # 통신 두절로 자율주행 안전 정지 (사용자 알림)
    "remote_send_error":       ("Robot", "error"),
    "robot_error_code":        ("Robot", "error"),
    "motor_overheat_warning":  ("Robot", "error"),  # 관절 모터 과열 경고 (75°C+)
    "motor_overheat_danger":   ("Robot", "error"),  # 관절 모터 과열 위험 (85°C+) → 보호 동작
    "thermal_temp_high":       ("Robot", "event"),  # 열화상에서 고온 객체 감지 (45°C+)
    "robot_initpose_manual_needed": ("Robot", "error"),  # 자동 위치 초기화 실패 → 수동 재조정 필요 (확인창)
    "robot_initpose_no_chargestation": ("Robot", "error"),  # 충전 중인데 충전소 미등록 (설정 미비)
    # 스케줄 실행 보장 (PRD-Schedule-Execution §4.5)
    # 전부 Schedule 타입이다 — Robot 타입은 로봇 오프라인 시 알림이 억제되는데,
    # 스케줄 실패의 상당수가 오프라인이 원인이라 정작 알려야 할 때 묻힌다.
    "schedule_missed":         ("Schedule", "error"),  # 허용 지연 초과로 회차 미실행
    "schedule_skip_offline":   ("Schedule", "error"),  # 로봇 오프라인으로 회차 스킵
    "schedule_wait_offline":   ("Schedule", "error"),  # 실행 시각에 오프라인 — 허용 지연까지 대기
    "schedule_gate_timeout":   ("Schedule", "error"),  # 확인 무응답 절대 상한 도달
    "schedule_ttl_exceeded":   ("Schedule", "error"),  # 회차 TTL 초과
    "schedule_return_failed":  ("Schedule", "error"),  # 복귀 실패 — 개입 필요
    "schedule_state_error":    ("Schedule", "error"),  # 작업 상태 갱신 DB 실패
    "schedule_auto_paused":    ("Schedule", "error"),  # 연속 실패로 자동 일시정지
    "schedule_interrupted":    ("Schedule", "error"),  # 재기동 등으로 진행 중 작업이 끊김 → 사람이 재개해야 다시 돈다
    # 상시 대기 응대 (§4.5) — 세션 실패도 조용히 사라지면 안 된다
    "standby_session_timeout": ("Schedule", "event"),  # 응대 시간 초과 (S5b·S5c)
    "standby_dest_unknown":    ("Schedule", "event"),  # 안내 불가 목적지 요청 (S2)
    # 다단계 안내 — 방문자가 따라오지 않아 로봇이 멈춰 운영자를 기다린다(사람이 가 봐야 한다)
    "guide_follow_lost":       ("Schedule", "error"),
    # 다단계 안내 — 로봇 스피커 발화 실패. 방문자가 안내·질문을 못 듣는다
    "guide_tts_failed":        ("Schedule", "error"),
    # 다단계 안내 — 아무도 끝내지 않아 관제가 닫았다(동행 이탈 방치·Jetson 음성 엔진 끊김)
    "guide_follow_lost_closed": ("Schedule", "error"),
    "guide_engine_offline":    ("Schedule", "error"),
    # 다단계 안내 반복(시연 전 테스트) — 배터리가 로봇 최소 기준 이하라 다음 회차를 열지 않았다
    "guide_loop_battery_low":  ("Schedule", "error"),
    "guide_battery_aborted":   ("Schedule", "error"),  # 배터리 부족으로 안내 중단 — 방문객이 남은 곳 포함
    # 상시 대기 — 대기 장소가 다른 층이라 시작 보류(사람이 로봇을 옮겨야 한다)
    "standby_map_mismatch":    ("Schedule", "error"),
    # 상시 대기 — 근무 시작 때 로봇이 끊겨 있어 보류(연결되면 다음 틱에 시작)
    "standby_offline_hold":    ("Schedule", "error"),
    "schedule_robot_missing":  ("Schedule", "error"),
    # 배터리 최소 기준 — 층 전환·자동문 통과가 끝나길 기다림 / 충전소로 갈 수 없음(사람이 가 봐야 한다)
    "battery_low_deferred":    ("Robot", "error"),
    "schedule_battery_low":    ("Schedule", "error"),  # 배터리 부족으로 회차를 끝냄/시작 못 함
    # 충전 후 이어 가기 — 관제 확인창(BatteryResumeAlertWatcher)이 이 알림을 보고 뜬다
    "battery_resume_ask":      ("Robot", "event"),
    "battery_resume_confirm":  ("Robot", "error"),   # 응답 전엔 출발하지 않는다
    "battery_resume_lost":     ("Robot", "error"),   # 서버 재시작으로 이어 가기가 사라짐 — 사람이 다시 시켜야 한다
    "battery_low_no_station":  ("Robot", "error"),
    "standby_away":            ("Schedule", "event"),  # 근무 중 대기 장소로 돌아오지 못함(다른 층 등)
}

# 로그 Action → 알림 제목 매핑
ACTION_TITLES = {
    # Robot
    "robot_error_code":        "로봇 에러",
    "robot_error_resolved":    "로봇 에러 해소",
    "motor_overheat_warning":  "모터 과열 경고",
    "motor_overheat_danger":   "모터 과열 위험",
    "motor_overheat_resolved": "모터 과열 해소",
    "robot_battery_low":       "배터리 부족",
    "robot_connection_error":  "로봇 연결 오류",
    "robot_charging_start":    "충전 시작",
    "robot_charging_complete": "충전 완료",
    "rtsp_error":              "카메라 오류",
    "remote_send_error":       "원격 제어 오류",
    "thermal_temp_high":       "고온 감지",
    "nav_poll_timeout":        "네비게이션 폴링 타임아웃",
    "position_recv_error":     "위치 수신 오류",
    "db_operation_error":      "데이터베이스 오류",
    "robot_initpose_manual_needed": "위치 초기화 필요",
    "robot_initpose_recovered":     "위치 초기화 복구",
    "robot_initpose_no_chargestation": "충전소 미등록",
    "robot_initpose_auto_reloc":    "자동 재측위",
    # Schedule
    "nav_start":               "네비게이션 시작",
    "nav_arrival":             "웨이포인트 도착",
    "nav_complete":            "네비게이션 완료",
    "nav_error":               "네비게이션 오류",
    "guide_nav_blocked":       "안내 이동 막힘",
    "guide_nav_unblocked":     "안내 이동 재개",
    "guide_obstacle_stop":     "안내 이동 — 장애물 정지",
    "guide_obstacle_resume":   "안내 이동 — 장애물 풀려 재개",
    "guide_obstacle_staff":    "안내 이동 — 장애물 직원 호출",
    "guide_obstacle_closed":   "안내 이동 — 장애물 대응 없어 안내 종료",
    "nav_hang_skip":           "이동 지점 도착 못 함",
    "nav_unreached_hold":      "이동 지점 도착 못 함",
    "nav_hold_resumed":        "이동 재개",
    "nav_hold_action":         "멈춘 이동 처리",
    "nav_hold_timeout":        "응답 없음 — 자동 처리",
    "standby_comm_lost":       "상시 대기 — 통신 두절, 자리 비움",
    "standby_comm_lost_long":  "상시 대기 — 통신 두절 오래 지속",
    "standby_comm_restored":   "상시 대기 — 통신 복구",
    "nav_comm_lost":           "로봇 통신 두절 — 자율주행 정지",
    "nav_loop":                "네비게이션 반복",
    "place_move_start":        "장소 이동",
    "path_move_start":         "경로 이동",
    "schedule_cancel":         "작업 취소",
    "schedule_missed":         "작업 미실행",
    "schedule_skip_offline":   "로봇 오프라인으로 스킵",
    "schedule_wait_offline":   "로봇 오프라인 — 연결 대기",
    "schedule_gate_timeout":   "확인 무응답으로 중단",
    "schedule_ttl_exceeded":   "작업 시간 초과",
    "schedule_return_failed":  "복귀 실패 — 개입 필요",
    "schedule_state_error":    "작업 상태 갱신 실패",
    "schedule_auto_paused":    "연속 실패로 자동 일시정지",
    "standby_start":           "상시 대기 시작",
    "standby_ready":           "대기 장소 도착 — 응대 가능",
    "standby_end":             "상시 대기 종료",
    "standby_session_start":   "응대 시작",
    "standby_session_end":     "응대 종료",
    "standby_session_extended": "응대 연장",
    "standby_session_timeout": "응대 시간 초과",
    "standby_dest_unknown":    "안내 불가 목적지 요청",
    "guide_follow_lost":       "안내 동행 이탈 — 운영자 확인 필요",
    "guide_tts_failed":        "안내 음성 출력 실패 — 운영자 확인 필요",
    "guide_follow_lost_closed": "동행 이탈 방치 — 안내 자동 종료",
    "guide_engine_offline":    "음성 엔진 끊김 — 안내 자동 종료",
    "guide_loop_battery_low":  "안내 반복 중지 — 배터리 부족",
    "guide_battery_aborted":   "배터리 부족 — 안내 중단, 충전소 이동",
    "standby_map_mismatch":    "상시 대기 시작 보류 — 대기 장소가 다른 층",
    "standby_offline_hold":    "상시 대기 시작 보류 — 로봇 연결 안 됨",
    "schedule_robot_missing":  "작업 실행 불가 — 등록되지 않은 로봇",
    "battery_low_deferred":    "배터리 부족 — 층 전환·자동문 통과 뒤 충전소 이동",
    "schedule_battery_low":    "배터리 부족 — 작업 회차 종료/미시작",
    "battery_low_no_station":  "배터리 부족 — 충전소로 갈 수 없음",
    "standby_resume_after_charge": "충전 완료 — 대기 장소로 복귀",
    "route_resume_after_charge": "충전 완료 — 경로 반복 이어 가기",
    "battery_resume_ask":      "배터리 부족 — 충전 후 이어 갈까요?",
    "battery_resume_confirm":  "충전 완료 — 이어 갈지 확인 필요",
    "battery_resume_lost":     "서버 재시작 — 충전 후 이어 가기 취소됨",
    "standby_away":            "상시 대기 — 대기 장소로 돌아오지 못함",
    "robot_online":            "로봇 온라인",
    "robot_offline":           "로봇 오프라인",
    # System
    "system_startup":          "서버 시작",
}


class LogService:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        category: str,
        action: str,
        message: str,
        detail: str = None,
        robot_id: int = None,
        robot_name: str = None,
        error_json: str = None,
        business_id: int = None,
    ) -> LogDataInfo:
        log = LogDataInfo(
            Category=category,
            Action=action,
            Message=message,
            Detail=detail,
            RobotId=robot_id,
            RobotName=robot_name,
            BusinessId=business_id,
        )
        self.db.add(log)
        self.db.flush()

        # 알림 자동 생성 규칙 확인
        rule = ALERT_TRIGGER_RULES.get(action)
        if rule:
            alert_type, alert_status = rule
            # Robot 타입 알림: 대상 로봇이 현재 오프라인(네트워크 확정 Offline)
            # 이면 알림을 만들지 않는다. 로그는 그대로 남김.
            # (오프라인 중 연결오류/재연결 노이즈를 화면에 띄우지 않기 위함 — 의도된 동작.
            #  fault 코드(robot_error_code)는 로봇이 온라인일 때만 수신되므로 이 게이트에
            #  걸리지 않고 항상 알림이 생성된다.)
            should_create_alert = True
            if alert_type == "Robot" and robot_id is not None:
                try:
                    import app.robot_io.runtime as rt_mod
                    with rt_mod._lock:
                        entry = rt_mod._runtime.get(robot_id)
                    if entry is not None:
                        last_hb = entry.get("last_heartbeat", 0)
                        if rt_mod._derive_network(last_hb) == "Offline":
                            should_create_alert = False
                except Exception:
                    pass
            if should_create_alert:
                alert_title = ACTION_TITLES.get(action, message)
                # 로봇 fault 코드·모터 과열은 제네릭 제목 대신 구체 메시지(부위/관절+온도)를
                # 제목으로 노출 → 토스트/드롭다운에서도 어디가 문제인지 한눈에(설계서 §10-A).
                if action in ("robot_error_code", "motor_overheat_warning", "motor_overheat_danger"):
                    alert_title = message
                alert = Alert(
                    Type=alert_type,
                    Status=alert_status,
                    Content=alert_title,
                    Detail=message,
                    ErrorJson=error_json or detail,
                    RobotName=robot_name,
                    LogId=log.id,
                    BusinessId=business_id,
                )
                self.db.add(alert)

        self.db.commit()
        self.db.refresh(log)
        return log

    def get_list(
        self,
        category: str = None,
        search: str = None,
        start_date: str = None,
        end_date: str = None,
        page: int = 1,
        size: int = 20,
        business_id: int = None,
    ):
        query = self.db.query(LogDataInfo)

        # 로봇 온라인/오프라인 로그는 목록에서 제외 (통계용으로만 사용)
        query = query.filter(
            LogDataInfo.Action.notin_(["robot_online", "robot_offline"])
        )

        if business_id is not None:
            query = query.filter(LogDataInfo.BusinessId == business_id)

        if category:
            query = query.filter(LogDataInfo.Category == category)
        if search:
            query = query.filter(LogDataInfo.Message.contains(search))
        if start_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            query = query.filter(LogDataInfo.CreatedAt >= start_dt)
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            query = query.filter(LogDataInfo.CreatedAt <= end_dt)

        total = query.count()
        items = (
            query.order_by(desc(LogDataInfo.CreatedAt))
            .offset((page - 1) * size)
            .limit(size)
            .all()
        )

        # 가장 이른 로그 날짜 조회
        min_dt = self.db.query(func.min(LogDataInfo.CreatedAt)).scalar()
        earliest = min_dt.strftime("%Y-%m-%d") if min_dt else None

        return {"items": items, "total": total, "page": page, "size": size, "earliest_date": earliest}


# ── 큐 Writer 스레드 (세션 1개 재사용) ──
def _log_writer():
    """큐에서 로그를 꺼내 단일 세션으로 DB에 기록하는 전용 스레드"""
    db = None
    fail_count = 0

    while True:
        try:
            item = _log_queue.get()  # 블로킹 대기

            # 세션이 없거나 이전에 실패했으면 (재)생성
            if db is None:
                try:
                    db = SessionLocal()
                    fail_count = 0
                except Exception as e:
                    print(f"[ERR log_writer] DB 세션 생성 실패: {e}")
                    fail_count += 1
                    time.sleep(min(fail_count * 2, 30))
                    continue

            try:
                LogService(db).create(**item)
            except Exception as e:
                print(f"[ERR log_writer] {e}")
                try:
                    db.rollback()
                except Exception:
                    pass
                # 세션이 깨졌으면 폐기 후 다음 루프에서 재생성
                try:
                    db.close()
                except Exception:
                    pass
                db = None

        except Exception as e:
            print(f"[ERR log_writer] unexpected: {e}")


# Writer 스레드 시작 (데몬 — 메인 프로세스 종료 시 같이 종료)
threading.Thread(target=_log_writer, daemon=True, name="log-writer").start()


def log_event(
    category: str,
    action: str,
    message: str,
    detail: str = None,
    robot_id: int = None,
    robot_name: str = None,
    error_json: str = None,
    business_id: int = None,
):
    """스레드에서 안전하게 로그를 큐에 추가 (동일 action 30초 쿨다운)"""
    now = time.time()

    if action not in _COOLDOWN_EXEMPT:
        with _cooldown_lock:
            last = _last_logged.get(action, 0)
            if now - last < LOG_COOLDOWN_SEC:
                _suppressed_counts[action] = _suppressed_counts.get(action, 0) + 1
                return
            suppressed = _suppressed_counts.pop(action, 0)
            _last_logged[action] = now

        if suppressed > 0:
            message = f"{message} (이전 {suppressed}건 동일 로그 생략됨)"

    item = dict(
        category=category,
        action=action,
        message=message,
        detail=detail,
        robot_id=robot_id,
        robot_name=robot_name,
        error_json=error_json,
        business_id=business_id,
    )

    try:
        _log_queue.put_nowait(item)
    except queue.Full:
        print("[WARN log_event] 로그 큐 가득참 — 드롭")
