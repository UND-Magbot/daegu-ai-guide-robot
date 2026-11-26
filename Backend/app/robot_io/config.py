"""로봇 통신 네트워크 설정.

Docker 이미지를 다시 빌드하지 않고 현장 공유기/로봇망 IP를 맞출 수 있도록
환경변수를 우선 사용한다. 미설정 시 기존 운영 기본값으로 동작한다.
"""

import os


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        print(f"[ROBOT CONFIG] {name}={value!r} 값이 정수가 아니어서 기본값 {default} 사용")
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    print(f"[ROBOT CONFIG] {name}={value!r} 값이 bool 이 아니어서 기본값 {default} 사용")
    return default

# 로봇 본체 (ASDU 프로토콜 엔드포인트)
ROBOT_IP = _env_str("ROBOT_IP", "10.21.31.103")
ROBOT_PORT = _env_int("ROBOT_PORT", 30000)

# receiver.py (로봇 NOS에서 동작하는 중계기)
RECEIVER_IP = _env_str("RECEIVER_IP", _env_str("NOS_HOST", "10.21.31.106"))
RECEIVER_PORT = _env_int("RECEIVER_PORT", 40000)
# 신뢰성 명령(NAV) 전용 TCP 채널 포트. WiFi(PC↔NOS) 구간 패킷 유실/순단에
# 강하도록 NAV는 TCP로 보장 전송한다. receiver.py의 동일 상수와 반드시 일치해야 한다.
# STOP·긴급정지·폴링 등 지연 민감/손실 허용 트래픽은 RECEIVER_PORT(UDP)를 그대로 쓴다.
RECEIVER_TCP_PORT = _env_int("RECEIVER_TCP_PORT", 40001)

# PC 측 수신 포트
PC_PORT_POS = _env_int("PC_PORT_POS", 35000)
PC_PORT_STATUS = _env_int("PC_PORT_STATUS", 35001)
PC_PORT_NAV = _env_int("PC_PORT_NAV", 35002)

# 폴링 주기
REQ_INTERVAL_POS = 2.0
REQ_INTERVAL_HB = 1.0

# 기본 초기 pose
INIT_POSE = {"PosX": 3.965, "PosY": -2.395, "PosZ": 0.0, "Yaw": -1.664}

# ── 다층 경로 층 전환 (floor_transition) ──
# True(기본, 실엘베): 사람이 로봇을 엘리베이터에 태워 도착층 EV로비에 내려놓는다.
#   → 로봇은 이미 도착 waypoint 자리에 있으므로, 그 좌표를 seed 로 주입해 수렴하면
#     '도착 자동감지'로 보고 해당 waypoint 를 건너뛴 뒤 다음 목적지로 주행한다.
# False(겹침 매핑 검증): 두 맵이 경계 구간에서 겹치도록 매핑되어, 로봇이 경계를
#   스스로 주행해 건널 수 있다. 출발 EV로비에 선 채로 도착 맵이 로드되므로 로봇은
#   아직 도착 waypoint 자리가 '아니다'.
#   → seed 는 초기 추정일 뿐이고(도착 waypoint 와 실제 위치가 어긋나 있음),
#     scan-match 가 새 맵에서 잡아낸 위치가 안정 정착하면 그 좌표를 채택한다.
#     경계 waypoint 는 건너뛰지 않고 정상 주행 명령으로 전송한다.
# ⚠️ 실엘베에서 False 로 두면 층 이동 후 '안정적으로 틀린' 좌표를 채택할 수 있어 위험하다.
FLOOR_TRANSITION_ASSISTED = _env_bool("FLOOR_TRANSITION_ASSISTED", True)

# ── 전원 자동 on(부팅) 시 init_pose 자동 주입 (ERR-07) ──
AUTO_INIT_POSE_ENABLED = True          # 자동 주입 기능 on/off
AUTO_INIT_POSE_COOLDOWN_SEC = 120      # 한 번 주입 후 재트리거 금지 구간(플래핑 방지)
AUTO_INIT_POSE_SETTLE_SEC = 3.0        # 주입 후 수렴 대기 시간
# 주입 좌표(도킹포인트 ch-1) 대비 허용 오차(수렴 성공 판정, m).
# 실측: 충전 도킹 시 로봇 scan-match 가 등록 도킹포인트에서 ~1.9m 떨어진 위치로 안정 수렴한다
# (등록 좌표 vs 로봇 측위 프레임 편차). 도킹 구역 안이면 정상으로 보고 2m 이내를 확정 처리한다.
# (이보다 더 벗어나면 비정상으로 보고 거부 → 수동 재조정.)
AUTO_INIT_POSE_VERIFY_TOLERANCE_M = 2.0
# 수동 재조정 수렴 검증(verify_and_clear): 고정 대기 후 1회 판정하면 위치 보고 주기(2s)와
# 어긋날 때 수렴했는데도 미확정으로 떨어져 재드래그가 필요했다. 주입 이후 수신한 위치를
# 주기적으로 보고 조건을 만족하는 즉시 확정하고, 최대 대기까지 못 맞추면 실패로 본다.
AUTO_INIT_POSE_VERIFY_FIRST_CHECK_SEC = 1.0   # 주입 직후 첫 판정까지 대기
AUTO_INIT_POSE_VERIFY_POLL_SEC = 0.5          # 재판정 간격
AUTO_INIT_POSE_VERIFY_MAX_WAIT_SEC = 6.0      # 이 시간 안에 수렴 못 하면 미확정
# 준비 대기 재시도: 부팅 직후 로봇은 INIT_POSE 를 timeout(미수락) 시키므로,
# status:ok ACK 가 올 때까지 일정 간격으로 재시도한다(= "받을 준비가 된 순간"에 주입).
AUTO_INIT_POSE_RETRY_INTERVAL_SEC = 5.0   # status:ok 못 받았을 때 재시도 간격
AUTO_INIT_POSE_READY_TIMEOUT_SEC = 90.0   # 이 시간 내 status:ok 못 받으면 escalation(수동 알림)
AUTO_INIT_POSE_MAX_CONVERGE_FAIL = 3      # ok 받았으나 수렴 실패가 N회면 escalation(수동 알림)
# 시드 후 '현재 좌표가 로봇 실위치인지' 신뢰 확인용 안정성 검사.
# 시드 직후 로봇은 idle(정지) 이므로 위치가 안정적이어야 한다. scan-match(라이다↔저장맵)가
# 시드를 인정하면 제자리, 불일치면 위치가 끌려가 움직인다 → EPS 이상 움직이면 좌표 신뢰불가.
# (dock_anchor 는 물리적으로 도킹 위치가 확정이라 이 검사를 건너뛴다.)
AUTO_INIT_POSE_STABILITY_SEC = 4.0        # 시드 후 위치 안정성 관찰 시간
AUTO_INIT_POSE_STABILITY_SAMPLES = 4      # 관찰 샘플 수
AUTO_INIT_POSE_STABILITY_EPS_M = 0.15     # idle 허용 이동량(이상이면 scan-match 불일치 의심)
# 시드(last_status)와 로봇 보고 위치가 다른데도 안정적이면 = off 중 옮겨졌고 로봇이 스스로
# 재측위한 것으로 보고 그 위치를 채택(update)한다. 단 (0,0,0) 부근이면 로봇이 시드를 무시하고
# 리셋값에 머문 것이므로 채택하지 않고 미초기화로 본다(원점 가드).
AUTO_INIT_POSE_ORIGIN_GUARD_M = 0.3       # 이 거리 안(원점 부근)이면 유효 위치로 채택 안 함
# 전원 on 직후 충전 감지가 1~2초 지연될 수 있어, 이 시간 동안 충전 여부를 기다린다.
# 끝까지 비충전이면 자동으로 위치를 못 잡으므로 '미확정 + 운영자 맵 클릭 지정' 으로 넘긴다.
AUTO_INIT_POSE_CHARGE_GRACE_SEC = 15.0
# 미확정 자동 복구: 미확정 상태에서 충전 중이고 보고 위치가 충전소(dock) 좌표로
# 연속 이 횟수만큼(status heartbeat 단위, ≈REQ_INTERVAL_HB 초 간격) 안정 수렴하면
# 미확정을 자동 해제한다. rviz 등 외부 위치 교정 후 자동 반영을 위함.
# (dock 은 접지 진실이라 '안정적으로 틀림' 위험이 없다 — 물리적으로 도킹 위치에 있음.)
AUTO_INIT_POSE_RECOVER_SAMPLES = 5
# 재접속(offline→online) 후 도크 점유(charge_state∈{1,2,5} 또는 is_charging)를 관측하는
# 창(heartbeat tick 수, ≈REQ_INTERVAL_HB 초 간격). 재접속 순간엔 charge_state 가 아직 안
# 들어와 있을 수 있어, 이 창 안에 한 번이라도 도크로 잡히면 '도크 재접속'으로 인정해
# 자동 위치 재조정을 트리거한다.
AUTO_INIT_POSE_DOCK_CHECK_TICKS = 15
# 도크를 못 쓸 때(충전소 고장·배터리 분리 충전) 로봇을 세워 두고 끄는 '주차 장소' 이름(쉼표 구분).
# 도크 밖 전원 on 에서 마지막 위치가 현재 맵의 이 장소 반경 안이고 방향도 30° 안이면
# 마지막 위치 대신 장소의 등록 좌표·방향으로 init_pose 를 넣고 수렴을 확인한다.
# 비우면 끔(현행: 도크 밖 전원 on 은 운영자 위치 재조정).
AUTO_INIT_POSE_PARK_PLACES = frozenset(
    n.strip() for n in _env_str("AUTO_INIT_POSE_PARK_PLACES", "").split(",") if n.strip()
)
AUTO_INIT_POSE_PARK_RADIUS_M = float(_env_str("AUTO_INIT_POSE_PARK_RADIUS_M", "1.0"))

# 측위 상실 자동 재측위(11-06): 미확정·제자리 5초면 관제가 스캔을 지도 전체와 맞춰 위치를 넣는다.
# 넣은 뒤 확정은 운영자 '완료'(위치 재조정 창). 끄면 지금처럼 운영자 재조정만.
AUTO_RELOCALIZE_ENABLED = _env_bool("AUTO_RELOCALIZE_ENABLED", True)

# ── 모터/드라이버 온도 모니터링 (STATUS 응답의 DeviceTemperature 검사) ──
# 운영 환경의 실제 모터 온도 특성에 맞게 조정한다. 단위는 로봇이 내려주는 값
# (일반적으로 °C) 기준이며, 최초 수신 시 thermal 모듈이 원본 구조를 1회 로깅한다.
MOTOR_TEMP_WARN_C = 60.0       # 경고 임계값(°C) — 실시간 stdout 로그
MOTOR_TEMP_CRITICAL_C = 75.0   # 위험 임계값(°C) — stdout + DB 이벤트/알림
MOTOR_TEMP_VALID_MAX_C = 200.0 # 이 값 초과는 비정상 센서값으로 보고 무시(오탐 방지)
MOTOR_TEMP_WARN_LOG_INTERVAL_SEC = 10.0  # 경고 stdout 로그 최소 간격(폴링 스팸 방지)

# ── 통과 지점 연속 주행 ──
# 켜면 통과 지점에서 멈추지 않고 이어 간다: 반경 진입 시 다음 지점 직선이 벽에서 충분히 떨어져 있을
# 때만 넘기고(지름길 검사), 넘길 때 관제 sleep(1) 을 생략하며 NAV 에 continue 를 실어 receiver 가
# 모드 리셋을 생략할 수 있게 한다(receiver NAV_CONTINUE_SKIP_RESET 와 함께 켜야 효과가 크다).
# 끄면 기존과 같다.
NAV_CONTINUOUS_PASS = _env_bool("NAV_CONTINUOUS_PASS", False)
# 지름길이 끝내 막혀 있으면 통과 지점에 이만큼 다가갔을 때 넘긴다(멈추지 않게).
PASS_THROUGH_MIN_RADIUS = 0.3

# ── 다지점 NAV (2026-10-20 실기 확인) ──
# 켜면 통과 지점·노드가 이어지는 구간을 멈출 곳(작업지·문 앞·층 경계·도킹 통로)까지 묶어 receiver
# NAV_MULTI 1건으로 보낸다. 점 사이 전환을 로봇이 스스로 해 모드 리셋 공백(3.1s)이 없고, Wi-Fi 가
# 끊겨도 묶음 끝까지 간다. 점이 1개면 기존 단일 NAV 그대로. 다지점 receiver 를 배포한 뒤 켠다.
NAV_MULTIPOINT_ENABLED = _env_bool("NAV_MULTIPOINT_ENABLED", False)
# 한 묶음 최대 점 수 — 넘으면 나눠 보낸다(묶음 끝에서 한 번 서고, 다음 묶음은 내비 보행 유지로 리셋 없이 출발).
# 본체 basic_server 가 약 1KB 넘는 1003 프레임을 해석하지 않는다(10-27 실측: 4점 976B 수락, 7점 1612B 4회 무시,
# 10월 본체 로그 최대 4점). receiver NAV_MULTI_MAX_POINTS(64)는 형식 검사 상한일 뿐이다.
# 10-29: 중계기가 다지점을 공백 없는 형식·소수 3자리로 보내 5점이 약 845B(976B 안)에 들어간다 → 5점.
# 중계기는 976B 를 넘는 묶음을 거절한다(그 구간만 단일 NAV). 6점 이상은 본체 한도 실기 확인 뒤.
# ⚠ 배포 순서: NOS 중계기(압축 형식) 먼저 — 옛 중계기로 5점을 보내면 1,022B 라 본체가 버린다(sim multi_max4 X4).
NAV_MULTI_MAX_POINTS = 5

# ── 출발 구간 저속 (2026-10-28) ──
# 장소에 서 있다가 새 이동을 시작할 때 첫 구간(로봇 → 첫 점)만 저속으로 보낸다 — 보통 속도로 바로 내달리는
# 출발이 급발진처럼 느껴진다(본체 가속 설정은 명령에 없음). 첫 점이 노드·경유점이면 본체가 어차피 그 점에서
# 감속(약 2.5s, 10-20 실측)하므로 정지는 늘지 않는다. 첫 구간이 이보다 길면 이동 전체가 느려지므로 그대로 둔다.
NAV_SOFT_START = _env_bool("NAV_SOFT_START", True)
# 10-29 사용자 결정: 3 → 4m, 직선 다지점 저속 보정(아래)의 첫 구간에도 같은 한도 — 첫 구간이 길면 이동 전체가
# 저속처럼 보였다(10-29 실기: 수납 5.4m·엘리베이터 3.7m 첫 구간 저속, 14.5m 49초).
# 10-29 15시 사용자 결정: 다시 3m(첫 구간 거리 한도는 직선 다지점에도 그대로 적용).
NAV_SOFT_START_MAX_M = 3.0
# ── 직선 모드 출발·꺾인 뒤 구간 저속 (2026-10-29) ──
# 직선 모드 다지점은 서서 돈 뒤 0.1초 만에 0 → 최고 속도(보통 1.20m/s)로 뛰어 앞바퀴가 들린다(10-29 NOS 계획기 기록,
# 실주행 16회, 그중 2회는 0.5초 뒤 장애물 정지). 저속(0.5) 출발에서는 들림·정지가 없었다.
# 0 보다 크면 직선 다지점 묶음에서 ① 첫 걷는 구간(NAV_SOFT_START_MAX_M 이하일 때, 10-29 오후부터) ② 앞 점에서 이 각도(도) 이상 꺾인 구간을 저속으로 보낸다
# (원래 보통·고속일 때). 0 이면 끔. 단일 이동·자율 모드는 위 출발 구간 저속(3m 이하) 규칙 그대로.
NAV_TURN_SLOW_DEG = float(_env_str("NAV_TURN_SLOW_DEG", "0"))

# ── 안내 도착 후 내비 보행 유지 ──
# 켜면 안내 시나리오 이동이 도착해도 GAIT_BASIC 을 보내지 않고 내비 보행(0x3002)으로 서 있게 한다.
# 다음 이동은 NAV 에 continue 를 실어 receiver 가 모드 리셋(→0x1001→0x3002)을 생략하게 한다.
# 도착·출발마다 생기던 다리 재정렬 2회가 없어진다(시뮬 2026-10-08: 시연 9건 보행 전환 148→62).
# 통과 지점도 걷는 중에 넘기지 않고 도착(작업 완료)을 기다린 뒤 리셋 없이 잇는다(시뮬: 경유지 전환 44→0).
# 로봇이 리셋 없는 NAV 를 받지 않을 수 있어(실행 중 NAV 는 거부함, 10-08 실측) 아래 시간 안에 출발이
# 안 보이면 리셋해서 다시 보내고 그 안내 세션 동안은 끈다. 세션이 끝나면 기본 보행으로 돌린다.
NAV_KEEP_GAIT_ON_ARRIVAL = _env_bool("NAV_KEEP_GAIT_ON_ARRIVAL", False)
# 리셋 없는 출발 실측(10-08 시연 8건) 0.9~4.5s — 4s 는 1건이 넘어 6s
NAV_KEEP_GAIT_START_TIMEOUT_SEC = _env_int("NAV_KEEP_GAIT_START_TIMEOUT_MS", 6000) / 1000.0
# 시간이 지났어도 이만큼 움직였으면(상태 보고만 늦은 것) 출발한 것으로 본다
NAV_KEEP_GAIT_MOVED_M = 0.10
NAV_KEEP_GAIT_TURNED_RAD = 0.17   # ≈10°
# 안내 세션을 제자리에서 시작할 때(시작 위치 생략·현재 위치에서 시작) 현재 위치로 NAV 하나를 보내
# 미리 내비 모드(0x3002)로 바꿔 둔다 — 보행 전환이 첫 출발 대신 세션 시작에 나고, 첫 이동은 리셋 없이 나간다.
# 펌웨어는 바로 앞 목표도 Mode 1 + 0x3002 로 바꾼 뒤 제자리에서 끝낸다(10-12 본체 로그, 전환 후 1.2s 완료).
# 보행 유지(NAV_KEEP_GAIT_ON_ARRIVAL)가 켜져 있어야 동작한다. 시뮬: docs/분석보고/2026-10-12/분석보고_2026-10-12_자료/start_nav_gait_sim.py
NAV_PRIME_ON_SESSION_START = _env_bool("NAV_PRIME_ON_SESSION_START", True)
NAV_PRIME_GAIT_TIMEOUT_SEC = 4.0  # 이 안에 0x3002 가 안 보이면 지금처럼 첫 이동에서 전환
# 전환을 본 뒤 제자리 작업이 끝나기를 기다리는 여유 — 실행 중이면 리셋 없는 첫 NAV 를 거부한다
# (시뮬: 1.5s 면 작업이 늦을 때 거부 → 그 세션 보행 유지 꺼짐, 3s 는 거부 0)
NAV_PRIME_SETTLE_SEC = _env_int("NAV_PRIME_SETTLE_MS", 3000) / 1000.0

# ── 자동 우회(via_route) 맵별 끄기 ──
# 쉼표로 구분한 맵 ID(robot_map_info.id). 이 맵에서는 경유지를 끼우지 않고 목표만 보낸다 — 길은 로봇이 정한다.
# 2026-10-08 관공서 3F(43): 경유지가 드문드문해 직선의 6배 넘게 돌아가는 우회·이동 거부가 생겼다.
# 안정화되면 robot_map_info 컬럼(화면에서 켜고 끄기)으로 옮긴다.
def _env_map_ids(name: str) -> frozenset:
    return frozenset(int(v) for v in _env_str(name, "").replace(" ", "").split(",") if v.isdigit())


AUTO_DETOUR_DISABLED_MAPS = _env_map_ids("AUTO_DETOUR_DISABLED_MAPS")
# ── 구간 우선 길 찾기 ──
# 쉼표로 구분한 맵 ID. 이 맵에서 장소 이동(안내 시나리오 포함)은 맵 관리에서 그린 구간(route_info, 방향 반영)을
# 따라 간다 — 직선이 막혔을 때 출발 장소→목표가 구간으로 이어지면 그 중간 장소를 거쳐 간다.
# 구간이 없거나 직선의 2배를 넘으면 지금 동작(우회 끔이면 목표만, 아니면 자동 우회).
# 2026-10-08 관공서 3F(43)에서 먼저 검증한다.
ROUTE_GRAPH_MAPS = _env_map_ids("ROUTE_GRAPH_MAPS")
# 위 맵 중 직선이 뚫려 있어도 구간을 따를 맵. 2026-10-13 2F 주차장(46): 주차면 사이라 지도상 직선이 비어 있어도
# 차량 통로(경유지)로 돌아가야 한다.
ROUTE_GRAPH_ALWAYS_MAPS = _env_map_ids("ROUTE_GRAPH_ALWAYS_MAPS")
# ── 노드망 경로 (2026-10-19) ──
# 켜면 모든 맵에서 노드망이 필수다: 노드가 있고 충전소(·엘리베이터 로비)가 노드망에 붙은 맵에서만 자율 이동하고,
# 모든 이동은 노드망을 따라간다(구간 우선·자동 우회 대신). 기존 맵을 노드망으로 옮기기 전에는 끈다.
NODE_ROUTING_ENABLED = _env_bool("NODE_ROUTING_ENABLED", False)
# 노드망 다지점 주행 모드 — straight(직선, NavMode 0) | auto(자율, NavMode 1).
# 직선은 노드 띠를 곧게 따라가지만 NOS 계획기가 제자리 회전 뒤 0.1초 만에 최고 속도(보통 1.20m/s)로 뛰어
# 앞바퀴가 들린다(10-29 실주행 16회). 자율은 보통 최고 1.00m/s, 걸으면서 돈다 — 대신 점 사이 길은 로봇이 정한다.
NODE_NAV_MODE = _env_str("NODE_NAV_MODE", "straight").lower()
if NODE_NAV_MODE not in ("straight", "auto"):
    print(f"[ROBOT CONFIG] NODE_NAV_MODE={NODE_NAV_MODE!r} 은 straight|auto 가 아니어서 straight 사용")
    NODE_NAV_MODE = "straight"
# 자율 모드에서 이 각도(도) 이상 꺾는 통과 점은 다음 구간 방향을 줘 서서 돌게 한다(10-29 사용자: 자율은 걸으면서
# 크게 돈다). 들어오는 방향을 주면 걸으며 지나가고, 다음 구간 방향을 주면 점에서 서서 돌고 간다(10-20 실기).
# 0 이면 끔(모든 통과 점 들어오는 방향, 지금 동작). 직선 모드는 어차피 점마다 서서 돌아 쓰지 않는다.
NODE_AUTO_TURN_STOP_DEG = float(_env_str("NODE_AUTO_TURN_STOP_DEG", "0"))
# 자동 간선을 잇는 최대 노드 거리(m). 벽 검사는 via_route 와 같은 여유(0.15m)를 쓴다.
NODE_AUTO_EDGE_MAX_M = float(_env_str("NODE_AUTO_EDGE_MAX_M", "3.0"))
# 노드를 건너뛰어도 되는 띠 폭(m) — 건너뛴 노드가 지름길 직선에서 이 안에 있을 때만 건너뛴다(D3, 10-20).
# 직선 모드는 보낸 점을 곧장 잇는다. 벽 여유만 보고 건너뛰면 넓은 로비를 대각선으로 가로질러 노드망을 벗어난다.
NODE_LANE_TOL_M = float(_env_str("NODE_LANE_TOL_M", "0.4"))
# 측위 복구 직후 위치가 마지막 정상 측위 위치에서 이 거리(m) 넘게 튀었으면 남은 구간을 다시 계산한다
# (같은 목표 재전송이면 튄 위치에서 다음 노드로 곧장 가려다 벽을 넘을 수 있다).
# 10-19 실측(B3, 09-29~10-15 로그): 정지 측위 잡음 최대 0.27m, 주행 중 30초 안 자가 복구 120건 p90 0.43m,
# 1F 상실 버스트 1.06~2.62m·경유지_001 약 1m — 0.5m 면 정상 복구 90% 는 재전송 유지, 문제 사례는 모두 재계산.
NODE_REPLAN_JUMP_M = float(_env_str("NODE_REPLAN_JUMP_M", "0.5"))
# 상실이 이보다 길었으면(초) 거리와 상관없이 다시 계산한다 — 30초 넘는 상실 36건 중 12건이 1m 넘게 달라졌다.
NODE_REPLAN_LOST_SEC = float(_env_str("NODE_REPLAN_LOST_SEC", "30"))
# ── 안내 중 장애물 정지·자동 재개 (2026-10-29) ──
# 켜면 안내 이동 중 NOS 감지 노드(robot_receiver/obstacle_guard.py)에 감지를 켜 두고, 앞이 막혀 멈추면
# 도착·재전송 판정을 쉬었다가 풀리면 같은 목표로 다시 보낸다. 감지 노드가 없는 NOS 에서는 응답이 없어 지금처럼 동작한다.
OBSTACLE_GUARD_ENABLED = _env_bool("OBSTACLE_GUARD_ENABLED", False)
# off | record | on (10-29). 감지는 안내 시나리오 세션의 이동에서만 켠다(단발 응대·사전 주행·작업·직접 이동 제외).
#   record: 감지 노드가 판단·기록만 하고 이동을 취소하지 않는다(실기 확인용) — 관제도 정지 상태를 잡지 않는다
#   on:     막히면 감지 노드가 취소하고 관제가 멘트·재출발·직원 호출을 맡는다
# 비우면 OBSTACLE_GUARD_ENABLED 를 따른다(true → on).
_obstacle_mode = _env_str("OBSTACLE_GUARD_MODE", "").lower()
OBSTACLE_GUARD_MODE = (_obstacle_mode if _obstacle_mode in ("off", "record", "on")
                       else "on" if OBSTACLE_GUARD_ENABLED else "off")
OBSTACLE_GUARD_PORT = _env_int("OBSTACLE_GUARD_PORT", 40100)
# 정지 뒤 이 시간(초) 넘게 막혀 있으면 멈춤 멘트를 한다. 0 = 멈춤을 안 주기에 바로. 1.5초는 감시 주기 1초와 겹쳐
# 2.4초 뒤에 나왔다(10-30 #505).
OBSTACLE_ANNOUNCE_SEC = float(_env_str("OBSTACLE_ANNOUNCE_SEC", "0"))
# 정지 직후 바로 NAV 를 보내면 보행 전환과 겹쳐 제자리에 선다(0xa328) — 동행 확인 재출발과 같은 1.5초를 둔다.
OBSTACLE_SETTLE_SEC = float(_env_str("OBSTACLE_SETTLE_SEC", "1.5"))
# 멈춘 뒤 이 시간(초) 넘게 서 있다 풀리면(긴 정지) 출발 전에 손님에게 '준비됐어요' 를 묻는다(10-30 시연 대본 20).
# 0 = 멈춤 멘트를 한 정지는 늘 묻기(시연: OBSTACLE_ANNOUNCE_SEC 1.5 와 함께), 운영 기본 30.
OBSTACLE_ASK_AFTER_SEC = float(_env_str("OBSTACLE_ASK_AFTER_SEC", "30"))
# 멈춘 뒤 이 시간(초) 동안 풀리지 않으면 직원 호출(관제 알림·확인창) — 그 뒤엔 관제 처리까지 대기(10-30 결정 2분).
OBSTACLE_STAFF_SEC = float(_env_str("OBSTACLE_STAFF_SEC", "120"))
# 직원 호출 뒤 대기 안내를 다시 하는 간격(초) — 동행 이탈과 같은 45초.
OBSTACLE_STAFF_REPEAT_SEC = float(_env_str("OBSTACLE_STAFF_REPEAT_SEC", "45"))
# 전방 카메라 사람 인식(robot_vision/front_person_watch.py, vision 컨테이너, 10-29 C1 기록 단계).
# 켜면 감지 노드에 보내는 켜기 신호를 카메라 서비스에도 보낸다(응답을 기다리지 않는다). 지금은 기록만 — 판정에 쓰지 않는다.
OBSTACLE_CAMERA_ENABLED = _env_bool("OBSTACLE_CAMERA_ENABLED", False)
OBSTACLE_CAMERA_HOST = _env_str("OBSTACLE_CAMERA_HOST", "vision")
OBSTACLE_CAMERA_PORT = _env_int("OBSTACLE_CAMERA_PORT", 40110)
