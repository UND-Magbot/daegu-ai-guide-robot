from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import math
import os
import threading
import time
import app.robot_io.runtime as runtime
from app.robot_io.sender import send_nav_to_robot
from app.database.database import SessionLocal, get_db
from app.database.models import LocationInfo, WayInfo, UserInfo, RobotInfo
from app.logs.service import log_event
from app.user_cache import get_robot_id, get_robot_name, get_robot_business_id
from app.navigation.place_lookup import find_place_by_name
from app.auth.dependencies import get_current_user, require_permission
from app.cluster.deps import require_robot_available


move = APIRouter(prefix="/nav")

# 위치 미초기화(자동 init_pose 수렴 실패) 동안 자율주행을 막을 때 쓰는 안내 메시지.
INITPOSE_BLOCK_MSG = (
    "로봇 위치 초기화가 필요합니다. '위치 재조정'으로 위치를 초기화한 후 다시 시도하세요."
)


# 로봇이 측위 상실을 보고해 미확정이 된 경우. 이때 NAV 를 보내면 로봇은 거부(0xa313)하고 5초 뒤
# 스스로 엎드리며, 재전송이 겹치면 basic_server 가 재시작된다(2026-09-29 실측).
LOCALIZATION_LOST_BLOCK_MSG = (
    "로봇이 위치를 잃었습니다(측위 상실). '위치 재조정'에서 로봇 실제 위치를 드래그해 넣은 뒤 다시 시도하세요."
)
FLOOR_TRANSIT_BLOCK_MSG = (
    "층 이동 중입니다. 도착층에서 '위치 재조정'으로 위치를 맞춘 뒤 다시 시도하세요."
)


def _initpose_blocked() -> bool:
    """현재 로봇 위치가 미초기화(신뢰불가) 상태라 자율주행을 보류해야 하는지.

    로봇의 측위 상실 보고도 polling 이 미확정으로 합쳐 두므로 이 플래그 하나만 본다.
    """
    try:
        rid = get_robot_id()
        # 서 있을 때 급변 표본 하나를 확인 중이면 결론(최대 약 2초)을 기다린다 — 그사이 출발하면
        # 주행 중엔 급변 감시가 꺼져 틀린 위치로 계속 갈 수 있다.
        from app.robot_io.polling import jump_suspect_active, wait_jump_suspect
        if jump_suspect_active(rid):
            print("[NAV] 위치 급변 확인 중 — 다음 위치 표본까지 출발 대기")
            wait_jump_suspect(rid)
            if jump_suspect_active(rid):
                print("[NAV] 위치 급변 확인이 끝나지 않아 출발 보류")
                return True
        return runtime.is_initpose_pending(rid)
    except Exception:
        return False


def _initpose_block_msg() -> str:
    """보류 사유에 맞는 안내 — 측위 상실과 일반 미확정을 구분해 알린다."""
    try:
        if runtime.get_initpose_reason(get_robot_id()) == runtime.LOCALIZATION_LOST_REASON:
            return LOCALIZATION_LOST_BLOCK_MSG
        if runtime.get_initpose_reason(get_robot_id()) == runtime.FLOOR_TRANSIT_REASON:
            return FLOOR_TRANSIT_BLOCK_MSG
    except Exception:
        pass
    return INITPOSE_BLOCK_MSG

current_wp_index = 0
waypoints_list = []
is_navigating = False
nav_sent_time = 0
# 마지막 NAV 송신 시각. nav_sent_time 은 정지할 때 0 으로 지워지므로 Regular 복귀 재전송 판단용으로 따로 둔다.
_last_nav_dispatch = 0.0
nav_loop_remaining = 0
nav_loop_total = 0
nav_loop_count = 0          # 현재 진행 중인 반복 회차 (1부터 시작)
nav_loop_infinite = False   # 무한 반복 모드 (작업 중지 전까지 계속)
charge_on_arrival = False  # 도착 후 자동 충전 플래그 (도킹 포인트 도착 시 start_charge 호출)
auto_return_to_charge = False  # 작업 완료 후 충전소로 자동 복귀 플래그
                                # startpath(원격 반복 포함) / 스케줄러만 True.
sit_on_arrival = False  # 도착 후 SIT 플래그 (모터 과열 보호 — 도킹 포인트 도착 시 SIT, 충전 안 함)
overheat_return_pending = False  # 모터 과열 보호: 현재 WP 도착 시 충전소 도킹 루트로 전환 예약
                                # pathmove / placemove / return-to-charge 자체는 False.

# nav_thread 상태 리셋 신호 (새 주행/정지 시 nav_thread가 감지)
_nav_reset_flag = False
_nav_full_reset_flag = False  # True면 retry_count도 리셋
# 새 주행·정지마다 1씩 오른다. 오래 걸리는 백그라운드 절차(언도킹 등)가 기다리는 사이
# 사용자가 다른 이동을 시작했는지 판단하는 데 쓴다 — 바뀌었으면 그 절차는 주행을 덮어쓰지 않는다.
_nav_epoch = 0

# 층 전환이 이미 끝난 경계 waypoint 의 인덱스.
# 겹침 매핑(FLOOR_TRANSITION_ASSISTED=False)에서는 전환 후 이 waypoint 로 실제 주행해야 하는데,
# 그대로 navigation_send_next 를 부르면 같은 map_id 경계를 다시 감지해 재전환된다.
# 전송 직전 이 인덱스와 일치하면 경계 판정을 1회 건너뛴다.
_boundary_done_index = None

# 통과 지점을 '이동 중에' 넘긴 waypoint 인덱스(nav_thread 가 반경 진입으로 넘길 때 남긴다).
# 다음 전송이 이 인덱스와 맞으면 로봇은 아직 걷는 중이므로 대기 없이 continue NAV 를 보낸다.
# 재전송·정지 후 출발처럼 로봇이 서 있을 수 있는 전송에는 붙지 않는다.
_pass_handoff_index = None

# 다지점으로 보낸 묶음 — 웨이포인트 [start, end) 와 로봇이 보고한 지금 향하는 점 번호 value(1부터).
# 보낸 뒤 current_wp_index 는 end 다(도착 판정은 묶음 끝 하나만 한다). 중간 점 진행은 value 로 본다.
_multi: dict | None = None
# receiver 가 다지점을 거부했다(구버전) — 백엔드를 다시 띄울 때까지 단일 NAV 만 보낸다(묶음마다 거부 왕복 방지)
_multi_unsupported = False

# 경유지 방향 안내(turn_cues). 장소 이동을 시작할 때 계산해 그 주행(waypoints_list 객체)에 묶어 두고,
# 로봇이 그 경유지에 NAV_CUE_LEAD_M 안으로 다가오면(polling 이 매 폴링 check_approach_cue) _nav_cue 로
# 내보낸다. 위치를 못 받아 미리 못 냈으면 그 경유지를 지날 때(다음 웨이포인트 전송) 낸다.
# 음성 엔진이 /robot/nav 의 cue.seq 가 바뀌면 그 문구를 말한다.
# 처음에는 통과 순간에 냈는데, 로봇이 이미 꺾기 시작한 뒤라 늦었다(2026-10-11 실기) — 1~2m 앞에서 알린다.
# 위치는 약 1초 늦고(receiver·관제 폴링) 음성 엔진도 1초마다 보므로, 2m 에서 내면 저속 안내 중
# 실제로는 약 1~1.5m 앞에서 들린다.
NAV_CUE_LEAD_M = float(os.getenv("NAV_CUE_LEAD_M", "2.0"))
# 거리만으로 정하면 빨리 걸을수록 늦게 들린다(2026-10-27 로그: 경유지 0.1~0.8m 앞에서 낸 경우 다수).
# 경유지 도착 NAV_CUE_LEAD_SEC 초 전에 들리도록, 지금 속도 × (그 초 + 전달 지연) 앞에서 낸다(최소 NAV_CUE_LEAD_M).
# 전달 지연 = 위치 약 1초 늦음 + 관제 1초 간격 확인(평균 0.5초) + 음성 엔진 1초마다 확인 + 재생 시작.
# NAV_CUE_LEAD_SEC 가 0 이면 예전처럼 거리로만 낸다.
NAV_CUE_LEAD_SEC = float(os.getenv("NAV_CUE_LEAD_SEC", "3.0"))
NAV_CUE_DELAY_SEC = float(os.getenv("NAV_CUE_DELAY_SEC", "2.5"))
CUE_SPEED_MAX = 1.5      # m/s — 위치가 튀어 생긴 속도는 버린다
# 안내 시점에 쓰는 속도 하한 — 그 구간에 보낸 속도 명령으로 본 실제 걸음(m/s). 잰 속도는 저속→보통으로 바뀐 직후
# 늦게 따라오므로(반씩 섞음) 보통 구간에서 방향·도착 안내가 늦을 수 있다(10-29 오후, 출발 저속 4m 한도로 보통 구간이 늘어남).
# 저속 0.35 = 10-29 실기 평균 약 0.3 m/s(저속 명령). 보통 0.5 = 10-30 3F 관공서 실기(자율 모드, 보통 명령) 구간 평균
# 0.35~0.46 m/s 의 위 — 처음엔 1.0(사용자 확인 최고 1.0~1.2)으로 잡았다가 "곧 도착합니다"가 도착 11~16초 전에 나왔다(정정).
# 짧은 구간은 최고 속도에 닿지 못한다. 고속은 기록 없음(추정 0.8).
CUE_SPEED_FLOOR = {"slow": 0.35, "normal": 0.5, None: 0.5, "fast": 0.8}
# "곧 도착합니다" 를 로봇이 멈추기 몇 초 전에 낼지(+ 전달 지연 NAV_CUE_DELAY_SEC). 최소 turn_cues.ARRIVE_SOON_M(2.5m).
# 처음 4.0(=6.5초 앞)이었다가 10-30 16:44 3F 로비에서 4.3m 앞 → 멈춤 8.6초·도착 판정 10.1초로 '곧 도착' 뒤가 길게 비어
# 2.5(=5초 앞)로 줄였다 — Jetson 이 받는 데 약 1초라 멈추기 약 4초 전에 들린다. 관제 도착 판정은 멈춘 뒤 약 2초 더 걸린다.
ARRIVE_SOON_LEAD_SEC = 2.5
# 경로가 이보다 짧으면 "곧 도착합니다" 를 내지 않는다 — 출발하자마자 나와 몇 초 뒤 도착 멘트와 붙어 들렸다
# (10-30 3F 관공서, 안내창구 → 종합민원실 3.5m: 곧 도착 뒤 약 3초 만에 도착 멘트). 진행 멘트 PROGRESS_MIN_M 과 같은 기준.
ARRIVE_SOON_MIN_ROUTE_M = 5.0
# 출발할 때 진행 방향과 지금 방향이 이 각도(도) 이상 다르면 제자리에서 먼저 돌고 출발한다(묶음 첫 점 = 지금 위치).
# 목표 하나로 바로 보내면 걸으면서 돌아 호를 그리며 출발했다(2026-10-28 3F 안내창구 → 종합민원실, 90°). 0 이면 끔.
NAV_DEPART_TURN_DEG = float(os.getenv("NAV_DEPART_TURN_DEG", "45"))
DEPART_TURN_MIN_M = 0.3   # 다음 점이 이보다 가까우면 방향을 정할 수 없다 — 그대로 보낸다
# 잰 속도는 경유지에서 다음 점을 보내는 동안(요청→출발 약 1.6초) 떨어졌다가 늦게 회복한다 — 그 사이 "곧 도착"
# 거리를 짧게 잡아 빠른 구간에서 도착 3초 전에야 나왔다(10-30 시뮬). 최근 이 초 동안의 최고값도 함께 본다.
CUE_PEAK_HOLD_SEC = 10.0
_cue_speed: dict = {"t": None, "x": 0.0, "y": 0.0, "v": 0.0, "hist": []}   # hist: 최근 (시각, 잰 속도)
_route_cues: dict[int, str] = {}
_route_cues_for: list | None = None
_cue_emitted: set[int] = set()
_nav_cue: dict = {"seq": 0, "text": None}
_last_cue_at: float | None = None
# 이동 중 진행 멘트("편의점 방향으로 이동하고 있습니다.", 본사 시연 대본 개정안 E4, 2026-10-29).
# 안내 단계 이동에 문구가 있고 경로가 PROGRESS_MIN_M 이상이면, 걷기 시작하고 PROGRESS_DELAY_SEC 뒤부터 한 번 낸다.
# 다른 안내(방향·접근)와 붙이지 않고 따로 낸다 — 앞 안내를 낸 지 PROGRESS_GAP_SEC 지났고, 다음 안내까지
# PROGRESS_FREE_SEC 이상 남은 '조금 긴 구간'에서만. 목적지 가까이까지 그런 구간이 없으면 생략한다.
# (10-29 실기: 짧은 구간에서 방향 안내 앞에 붙였더니 긴 문장을 말하는 동안 "곧 도착합니다"가 7.7초 밀려 도착 4초 전에 나왔다.)
# 방향 안내와 같은 cue 로 내보낸다 — 음성 엔진은 cue.seq 가 바뀌면 말한다(엔진 변경 없음).
# 5m 면 3F 안내데스크(4.8m, 걷기 약 9초)가 빠졌다(10-30). 4m 미만(약 8초 이하)은 도착 멘트와 붙어 생략.
PROGRESS_MIN_M = 4.0
PROGRESS_DELAY_SEC = 2.0
PROGRESS_GAP_SEC = 5.0     # 앞 안내(약 3초)를 다 말한 뒤
PROGRESS_FREE_SEC = 5.0    # 진행 멘트(약 3초, 미리 합성) + 전달 지연(1~2초)
# 다음 안내까지 시간은 방향·도착 안내와 같은 속도(_cue_v: 잰 속도·최근 최고값·보낸 속도 하한)로 잰다.
# 예전에는 명목값(보통 1.2 m/s)으로 짧게 봤는데, 3F 처럼 실제 0.35~0.5 m/s 인 곳에서는 틈이 늘 없다고 봐 진행 멘트가 빠졌다(10-30).
PROGRESS_DEPART_M = 0.5   # 출발점에서 이만큼 움직이면 걷기 시작으로 본다(충전 해제·제자리 회전 시간은 빼고 잰다)
# 진행 멘트 뒤 이 시간 안에 '곧 도착합니다'가 나올 차례면 내지 않는다 — 진행 멘트(약 3.5초) + 전달·합성 지연(1~2초).
# 도착 멘트("진료실 입구입니다…")가 곧 나오므로 겹치는 쪽은 '곧 도착'을 뺀다(2026-11-02 사용자 결정).
SOON_AFTER_PROGRESS_SEC = 6.0
_progress: dict = {"text": None, "for": None, "start": None, "departed_at": None, "done": True, "said_at": None}

def is_nav_active():
    return is_navigating


def mark_pass_through_handoff() -> None:
    global _pass_handoff_index
    _pass_handoff_index = current_wp_index


# ── 안내 도착 후 내비 보행 유지 (config.NAV_KEEP_GAIT_ON_ARRIVAL) ──
_NAV_GAIT = 0x3002
_gait_kept = False          # 직전 도착에서 GAIT_BASIC 을 생략했다(로봇은 내비 보행으로 서 있다)
_keep_gait_off = False      # 이번 안내 세션에서 리셋 생략 출발이 실패했다 — 세션이 끝날 때까지 끈다
# 리셋 생략으로 보낸 출발 NAV 의 (보낸 시각, 그때 위치). polling 이 짧은 시간 안에 출발을 확인한다.
_keep_gait_probe: tuple[float, dict] | None = None
# 운영자가 화면에서 켜고 끈 값. None 이면 .env(NAV_KEEP_GAIT_ON_ARRIVAL). 재시작하면 .env 값으로 돌아간다.
_keep_gait_override: bool | None = None


def keep_gait_enabled() -> bool:
    from app.robot_io.config import NAV_KEEP_GAIT_ON_ARRIVAL
    return NAV_KEEP_GAIT_ON_ARRIVAL if _keep_gait_override is None else _keep_gait_override


def set_keep_gait_enabled(enabled: bool) -> None:
    """실행 중 켜고 끈다(재배포 없이). 끌 때 내비 보행으로 서 있으면 바로 기본 보행으로 돌린다."""
    global _keep_gait_override, _gait_kept
    _keep_gait_override = enabled
    print(f"[NAV] 도착 후 내비 보행 유지 {'켬' if enabled else '끔'}(운영자)")
    if not enabled and _gait_kept and not is_navigating:
        _gait_kept = False
        _restore_basic_gait("보행 유지 끔")


def keep_gait_state() -> dict:
    from app.robot_io.config import NAV_KEEP_GAIT_ON_ARRIVAL
    return {"enabled": keep_gait_enabled(), "envDefault": NAV_KEEP_GAIT_ON_ARRIVAL,
            "sessionOff": _keep_gait_off, "gaitKept": _gait_kept}


_keep_gait_session_seq = 0  # 세션 시작·종료마다 바뀐다 — 미리 전환하는 동안 세션이 끝났는지 본다


def begin_keep_gait_session() -> None:
    """안내 세션 시작 — 직전 세션의 실패 기록을 지운다."""
    global _keep_gait_off, _keep_gait_session_seq
    _keep_gait_off = False
    _keep_gait_session_seq += 1


def end_keep_gait_session() -> None:
    """안내 세션 종료 — 내비 보행으로 서 있으면 기본 보행으로 돌린다. 주행 중이면 건드리지 않는다."""
    global _gait_kept, _keep_gait_off, _keep_gait_session_seq
    _keep_gait_off = False
    _keep_gait_session_seq += 1
    if _gait_kept and not is_navigating:
        _gait_kept = False
        _restore_basic_gait("안내 세션 종료")


def release_kept_gait(reason: str) -> None:
    """내비 보행으로 세워 뒀으면 기본 보행·Regular 로 돌린다(안내 일시 정지 등 오래 서 있을 때).

    세션 종료와 달리 이번 세션의 보행 유지 실패 기록(_keep_gait_off)은 그대로 둔다. 다음 이동은
    _gait_kept 가 없으니 출발 리셋과 함께 나간다.
    """
    global _gait_kept
    if _gait_kept and not is_navigating:
        _gait_kept = False
        _restore_basic_gait(reason)


def prime_nav_gait_in_place(reason: str) -> str:
    """안내를 제자리에서 시작할 때 현재 위치로 NAV 하나를 보내 내비 모드(0x3002)로 미리 바꾼다.

    보행 전환(다리 재정렬)이 첫 출발 대신 세션 시작에 나고, 첫 이동은 리셋 없이 나간다(_gait_kept).
    주행 흐름(도착 판정)을 쓰지 않는다 — 정지 확인을 여러 번 거쳐 6~8s 걸린다(시뮬). 0x3002 를 본 뒤
    제자리 작업이 끝나기를 NAV_PRIME_SETTLE_SEC 기다린다(실행 중이면 리셋 없는 NAV 를 거부한다).
    전환이 안 보이거나 그사이 주행·세션 종료가 끼면 _gait_kept 를 남기지 않는다 — 첫 이동은 지금처럼 리셋 출발.
    최대 NAV_PRIME_GAIT_TIMEOUT_SEC + NAV_PRIME_SETTLE_SEC 동안 막는다. 반환은 로그용 결과.
    """
    global _gait_kept, _last_nav_dispatch
    from app.robot_io.config import (NAV_PRIME_GAIT_TIMEOUT_SEC, NAV_PRIME_ON_SESSION_START,
                                     NAV_PRIME_SETTLE_SEC)
    if not NAV_PRIME_ON_SESSION_START:
        return "skip:끔"
    if not keep_gait_enabled() or _keep_gait_off:
        return "skip:보행 유지 꺼짐"
    if is_navigating:
        return "skip:주행 중"
    rid = get_robot_id()
    if not rid:
        return "skip:로봇 없음"
    if runtime.is_docked(rid):
        return "skip:도킹 중"
    pos = runtime.get_position(rid)
    if not pos.get("timestamp") or pos.get("initpose_pending"):
        return "skip:위치 미확정"
    if runtime.get_gait(rid) == _NAV_GAIT:
        _gait_kept = True
        return "already"

    seq = _keep_gait_session_seq
    from app.robot_io.sender import send_nav_to_robot as _send_nav
    print(f"[NAV] 제자리 내비 모드 전환 ({reason}) — 현재 위치 ({pos['x']:.2f}, {pos['y']:.2f})")
    _send_nav(1, pos["x"], pos["y"], pos["yaw"], map_id=runtime.get_current_map_id(rid))
    # 직전 세션 종료가 걸어 둔 Regular 복귀 재전송(_retry_mode_regular)이 전환을 되돌리지 않게 한다
    t0 = _last_nav_dispatch = time.time()
    switched = False
    while time.time() - t0 < NAV_PRIME_GAIT_TIMEOUT_SEC:
        time.sleep(0.25)
        if runtime.get_gait(rid) == _NAV_GAIT:
            switched = True
            break
    if not switched:
        print(f"[NAV] 제자리 내비 모드 전환 미확인({NAV_PRIME_GAIT_TIMEOUT_SEC:.0f}s) — 첫 이동에서 전환")
        return "timeout"
    time.sleep(NAV_PRIME_SETTLE_SEC)
    if is_navigating:
        return "superseded:주행 시작"
    if seq != _keep_gait_session_seq:
        _restore_basic_gait("내비 모드 전환 중 안내 종료")
        return "superseded:세션 종료"
    _gait_kept = True
    print(f"[NAV] 제자리 내비 모드 전환 완료 ({time.time() - t0:.1f}s) — 첫 이동은 리셋 없이 출발")
    return "primed"


# 안내 시작 위치를 생략할 때(이미 대기 위치 근처) 그 장소의 방향과 지금 방향이 이 각도(도) 이상 다르면
# 제자리에서 장소 방향으로 돌고 시작한다(11-03 메모: 그 자리에서 시작하되 방향은 맞추고 진행). 0 이면 끔.
GUIDE_START_FACE_DEG = float(os.getenv("GUIDE_START_FACE_DEG", "20"))
FACE_DONE_DEG = 10.0        # 이 안으로 들어오면 다 돈 것으로 본다
# 회전 대기 한도 — 보행 전환(리셋 약 3.1초) + 반 바퀴 회전. 넘어도 그대로 시작한다(이동 요청이 Jetson
# MOVE_TIMEOUT 25초 안에 끝나야 한다: 기립 대기 + 이 값 + NAV_PRIME_SETTLE_SEC).
FACE_TIMEOUT_SEC = 8.0


def _yaw_off_deg(pos: dict, yaw: float) -> float:
    d = (float(yaw) - float(pos.get("yaw") or 0.0) + math.pi) % (2 * math.pi) - math.pi
    return abs(math.degrees(d))


def face_yaw_in_place(yaw: float | None, reason: str) -> str:
    """지금 위치에서 yaw 방향으로 제자리 회전한다(현재 위치 + 장소 방향 NAV 하나).

    주행 흐름(도착 판정)을 쓰지 않는다 — 같은 좌표 목표는 폴링이 '이미 근처'로 넘겨 회전이 끊겼다(10-19).
    NAV 가 내비 모드(0x3002)로 바꾸므로 이어지는 prime_nav_gait_in_place 는 'already' 로 끝난다.
    다 돈 뒤(또는 한도 뒤) 제자리 작업이 끝나기를 NAV_PRIME_SETTLE_SEC 기다린다 — 실행 중이면 첫 이동의
    리셋 없는 NAV 를 로봇이 거부한다. 보행 유지가 꺼져 있으면 기본 보행으로 되돌린다(지금처럼 첫 이동에서 전환).
    반환은 로그용 결과.
    """
    global _last_nav_dispatch
    from app.robot_io.config import NAV_PRIME_SETTLE_SEC
    if GUIDE_START_FACE_DEG <= 0 or yaw is None:
        return "skip:끔"
    if is_navigating:
        return "skip:주행 중"
    rid = get_robot_id()
    if not rid:
        return "skip:로봇 없음"
    if runtime.is_docked(rid):
        return "skip:도킹 중"
    pos = runtime.get_position(rid)
    if not pos.get("timestamp") or pos.get("initpose_pending"):
        return "skip:위치 미확정"
    off = _yaw_off_deg(pos, yaw)
    if off < GUIDE_START_FACE_DEG:
        return f"aligned:{off:.0f}°"

    seq = _keep_gait_session_seq
    from app.robot_io.sender import send_nav_to_robot as _send_nav
    print(f"[NAV] 제자리 방향 맞춤 ({reason}) — {off:.0f}° 차이, 위치 ({pos['x']:.2f}, {pos['y']:.2f})")
    _send_nav(1, pos["x"], pos["y"], float(yaw), map_id=runtime.get_current_map_id(rid))
    t0 = _last_nav_dispatch = time.time()
    done = False
    while time.time() - t0 < FACE_TIMEOUT_SEC:
        time.sleep(0.25)
        if is_navigating:
            return "superseded:주행 시작"
        if _yaw_off_deg(runtime.get_position(rid), yaw) < FACE_DONE_DEG:
            done = True
            break
    if not done:
        print(f"[NAV] 제자리 방향 맞춤 미확인({FACE_TIMEOUT_SEC:.0f}s) — 그대로 시작")
    time.sleep(NAV_PRIME_SETTLE_SEC)
    if is_navigating:
        return "superseded:주행 시작"
    if seq != _keep_gait_session_seq:
        _restore_basic_gait("방향 맞춤 중 안내 종료")
        return "superseded:세션 종료"
    if not keep_gait_enabled() or _keep_gait_off:
        _restore_basic_gait("방향 맞춤 뒤 보행 유지 꺼짐")
    print(f"[NAV] 제자리 방향 맞춤 {'완료' if done else '미확인'} ({time.time() - t0:.1f}s)")
    return "turned" if done else "timeout"


def _arrival_keeps_gait() -> bool:
    """이번 도착에서 GAIT_BASIC 을 생략할까 — 안내 이동이고, 기능이 켜져 있고, 이번 세션에 실패한 적이 없다."""
    return (keep_gait_enabled() and not _keep_gait_off
            and bool(waypoints_list) and bool(waypoints_list[-1].get("keep_gait")))


def keep_gait_at_pass_through() -> bool:
    """지금 가는 통과 지점에서 보행을 유지할까 — 걷는 중 넘기지 않고 도착(작업 완료)을 기다린 뒤 리셋 없이 잇는다.

    펌웨어는 실행 중인 작업에 새 NAV 를 거부하지만 작업이 끝난 뒤의 NAV 는 리셋 없이 받는다(10-08 실측 8/8).
    통과 연속 주행(NAV_CONTINUOUS_PASS)을 켠 경우는 그쪽 방식을 따른다.
    """
    from app.robot_io.config import NAV_CONTINUOUS_PASS
    target = get_current_target()
    # 점 수 한도로 나뉜 묶음 끝(split_end)도 같다 — 걷는 중 넘기면 로봇이 이어가기 NAV 를 거부한다
    return (not NAV_CONTINUOUS_PASS and bool(target) and bool(target.get("pass_through"))
            and (_arrival_keeps_gait() or bool(target.get("split_end"))))


def mark_pass_through_kept() -> None:
    """통과 지점에 도착했다 — 다음 지점을 리셋 없이 보내도록 표시한다."""
    global _gait_kept
    _gait_kept = True


def _take_kept_gait() -> bool:
    """다음 웨이포인트를 리셋 없이 보낼 수 있나 — 보행을 유지해 뒀고 로봇도 내비 보행이라고 보고한다."""
    global _gait_kept
    kept, _gait_kept = _gait_kept, False
    if not kept or _keep_gait_off or not keep_gait_enabled():
        return False
    rid = get_robot_id()
    return bool(rid) and runtime.get_gait(rid) == _NAV_GAIT


def departs_without_reset() -> bool:
    """다음 출발이 리셋·준비 대기 없이 바로 나가나 — 보행을 유지해 서 있다(_take_kept_gait 와 같은 조건, 값은 안 바꾼다).

    이때는 이동 요청에서 실제 출발까지 약 1초 빠르다(준비 대기 time.sleep(1)·보행 전환이 없다).
    음성 엔진이 출발 멘트에 맞춰 이동을 미리 요청하는 시간(MOVE_LEAD)을 이 값으로 고른다.
    """
    if not _gait_kept or _keep_gait_off or not keep_gait_enabled():
        return False
    rid = get_robot_id()
    return bool(rid) and runtime.get_gait(rid) == _NAV_GAIT


def get_nav_cue() -> dict:
    """마지막 방향 안내 {seq, text}. seq 가 바뀌면 새 안내다."""
    return dict(_nav_cue)


def _set_route_cues(start, wps: list, offset: int) -> None:
    """이번 장소 이동의 경유지 방향 안내를 계산해 둔다. 출발 위치를 모르면 안내하지 않는다."""
    global _route_cues, _route_cues_for, _cue_emitted
    from app.navigation.turn_cues import build_cues
    cues = build_cues(start, wps, NAV_CUE_LEAD_M) if start else {}
    _route_cues = {k + offset: v for k, v in cues.items()}
    if start and wps and len(wps) in cues:
        from app.navigation.turn_cues import path_length
        total = path_length([start] + [(float(w["x"]), float(w["y"])) for w in wps])
        if total < ARRIVE_SOON_MIN_ROUTE_M:
            _route_cues.pop(len(wps) + offset)
            print(f"[CUE] 곧 도착 생략 — 경로 {total:.1f}m < {ARRIVE_SOON_MIN_ROUTE_M:.0f}m")
    _route_cues_for = waypoints_list
    _cue_emitted = set()
    _cue_speed.update(t=None, v=0.0, hist=[])
    if _route_cues:
        print(f"[CUE] 경유지 방향 안내: " + ", ".join(f"WP{k} {v}" for k, v in sorted(_route_cues.items())))


def _emit_cue(no: int, why: str) -> None:
    """웨이포인트 no 의 방향 안내를 한 번만 내보낸다."""
    global _nav_cue
    if _route_cues_for is not waypoints_list or no in _cue_emitted:
        return
    text = _route_cues.get(no)
    if text:
        _cue_emitted.add(no)
        _push_cue(text)
        print(f"[CUE] WP{no} {why} — {text}")


def _push_cue(text: str) -> None:
    global _nav_cue, _last_cue_at
    _nav_cue = {"seq": _nav_cue["seq"] + 1, "text": text}
    _last_cue_at = time.time()


def _set_progress(text: str | None, start) -> None:
    """이번 장소 이동의 진행 멘트를 세운다. 문구나 출발 위치가 없으면 내지 않는다."""
    text = (text or "").strip() or None
    _progress.update(text=text, start=start, departed_at=None, done=not (text and start), said_at=None,
                     **{"for": waypoints_list})


def _route_remaining_m(pos: dict, no: int, upto: int) -> float:
    """지금 위치에서 웨이포인트 no 를 거쳐 upto 번까지 경로 길이(m). 번호는 1부터."""
    pts = [(pos["x"], pos["y"])] + [(float(w["x"]), float(w["y"])) for w in waypoints_list[no - 1:upto]]
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


def _soon_share_m(last: int) -> float:
    """남은 길에서 '곧 도착합니다' 몫(m). 이번 이동에 곧 도착이 없으면(노드 없는 한 점 이동·짧은 구간 생략·이미 냄) 0.

    없는 곧 도착 몫을 빼서 5~6m 직선에도 진행 멘트가 빠졌다(10-30 progress_gap_short: 직선 6m 0개).
    """
    planned = _route_cues_for is waypoints_list and last in _route_cues and last not in _cue_emitted
    return _arrive_soon_lead_m(last) if planned else 0.0


def _check_progress_cue(pos: dict) -> None:
    """진행 멘트를 낼 때인지 본다(check_approach_cue 가 매 폴링 부른다)."""
    p = _progress
    if p["done"] or p["for"] is not waypoints_list:
        return
    now = time.time()
    if p["departed_at"] is None:
        if math.hypot(pos["x"] - p["start"][0], pos["y"] - p["start"][1]) >= PROGRESS_DEPART_M:
            p["departed_at"] = now
            from app.navigation.turn_cues import path_length
            total = path_length([p["start"]] + [(float(w["x"]), float(w["y"])) for w in waypoints_list])
            if total < PROGRESS_MIN_M:
                p["done"] = True
                print(f"[CUE] 진행 멘트 생략 — 경로 {total:.1f}m < {PROGRESS_MIN_M:.0f}m")
        return
    if now - p["departed_at"] < PROGRESS_DELAY_SEC:
        return
    no = heading_wp_no()
    last = len(waypoints_list)
    if not (0 < no <= last):
        return
    if _last_cue_at is not None and now - _last_cue_at < PROGRESS_GAP_SEC:
        return
    # 남은 시간은 방향·도착 안내와 같은 속도로 잰다 — 고정 1.2 m/s 로 잡고 '곧 도착' 몫을 v×6.5초(약 6m)로 크게 빼면
    # 3F 8~13m 구간에 틈이 하나도 안 생겼다(10-30 장애물 세션 시뮬 progress_gap_short).
    v = max(_cue_v(no), 0.1)
    nxt = None                     # 아직 안 낸 다음 안내 번호
    if _route_cues_for is waypoints_list:
        nxt = next((k for k in sorted(_route_cues) if k >= no and k not in _cue_emitted), None)
    if nxt is not None and nxt != last:
        if (_route_remaining_m(pos, no, nxt) - _turn_cue_lead_m(nxt)) / v < PROGRESS_FREE_SEC:
            return                 # 곧 방향 안내 — 그 뒤 구간을 기다린다
    else:
        left = _route_remaining_m(pos, no, last)
        if left / v < PROGRESS_FREE_SEC:
            p["done"] = True       # 남은 건 도착뿐 — 진행 멘트만 넣어도 시간이 없다
            print("[CUE] 진행 멘트 생략 — 도착까지 여유 있는 구간이 없음")
            return
        if (left - _soon_share_m(last)) / v < PROGRESS_FREE_SEC:
            # 진행 멘트와 '곧 도착'이 둘 다 들어갈 만큼은 아니다 — '곧 도착'을 빼고 진행 멘트를 남긴다(2026-11-02 사용자 결정).
            # 도착 멘트("진료실 입구입니다…")가 곧 나온다. 예전에는 반대로 진행 멘트를 뺐다.
            _cue_emitted.add(last)
            print("[CUE] 곧 도착 생략 — 진행 멘트를 남긴다(둘 다 들어갈 시간 없음)")
    p["done"] = True
    p["said_at"] = now
    _push_cue(p["text"])
    print(f"[CUE] 진행 멘트 — {p['text']}")


def check_approach_cue() -> None:
    """지금 가는 웨이포인트에 안내가 있고 그 거리 안으로 들어왔으면 낸다(polling 이 부른다).

    경유지는 도착 NAV_CUE_LEAD_SEC 초 전(최소 NAV_CUE_LEAD_M 앞)에 꺾는 방향을, 목적지(마지막)는 도착 ARRIVE_SOON_LEAD_SEC 초 전
    (최소 turn_cues.ARRIVE_SOON_M 앞)에 "곧 도착합니다" 를 낸다. 속도는 구간에 보낸 속도 하한을 함께 본다(_cue_v). 목적지 안내는 위치로만 낸다 — 위치를 모르면 말하지 않는다(도착 순간에
    "곧 도착" 이 나오면 안 되므로 통과 때 대신 내지도 않는다).
    """
    progress = not _progress["done"] and _progress["for"] is waypoints_list
    if not is_navigating or _route_cues_for is not waypoints_list or not (_route_cues or progress):
        return
    rid = get_robot_id()
    pos = runtime.get_position(rid) if rid else {}
    if not pos or not pos.get("timestamp") or pos.get("initpose_pending"):
        return
    _track_cue_speed(pos)          # 안내가 없는 구간에서도 속도는 계속 잰다
    if progress:
        _check_progress_cue(pos)
    no = heading_wp_no()           # 지금 가는 웨이포인트 번호(1부터)
    if no in _cue_emitted or no not in _route_cues or not (0 < no <= len(waypoints_list)):
        return
    wp = waypoints_list[no - 1]
    dist = math.hypot(pos["x"] - wp["x"], pos["y"] - wp["y"])
    lead = _arrive_soon_lead_m(no) if no == len(waypoints_list) else _turn_cue_lead_m(no)
    if dist <= lead:
        said = _progress.get("said_at") if _progress["for"] is waypoints_list else None
        if no == len(waypoints_list) and said is not None and time.time() - said < SOON_AFTER_PROGRESS_SEC:
            # 진행 멘트를 말하는 중 — '곧 도착'은 그 뒤로 밀려 도착 직전에야 들렸다(11-02 #519, 5.7초 밀려 도착 0.4초 전)
            _cue_emitted.add(no)
            print(f"[CUE] WP{no} 곧 도착 생략 — 진행 멘트 {time.time() - said:.1f}초 뒤")
            return
        _emit_cue(no, f"{dist:.1f}m 앞")


def _track_cue_speed(pos: dict) -> None:
    """위치 변화로 지금 속도를 잰다(0.3초 이상 간격, 반씩 섞어 부드럽게)."""
    now = time.time()
    t0 = _cue_speed["t"]
    if t0 is not None and now - t0 < 0.3:
        return
    if t0 is not None:
        v = math.hypot(pos["x"] - _cue_speed["x"], pos["y"] - _cue_speed["y"]) / (now - t0)
        if v <= CUE_SPEED_MAX:
            _cue_speed["v"] = 0.5 * _cue_speed["v"] + 0.5 * v
            hist = _cue_speed["hist"]
            hist.append((now, _cue_speed["v"]))
            while hist and now - hist[0][0] > CUE_PEAK_HOLD_SEC:
                hist.pop(0)
    _cue_speed.update(t=now, x=pos["x"], y=pos["y"])


# '곧 도착'은 최고값을 짧게만 본다 — 경유지에서 잠깐 서는 동안(약 1.6초)의 떨어짐만 메우고, 끝 구간에서
# 3초 넘게 느려졌으면 느린 걸음으로 잡는다(10-30 16:44 3F 로비, 앞서 빨랐던 0.66 m/s 로 잡아 8.6초 전에 나왔다).
ARRIVE_PEAK_HOLD_SEC = 3.0


def _measured_v(hold: float = CUE_PEAK_HOLD_SEC) -> float:
    """잰 속도 — 지금 값과 최근 hold 초 안 최고값 중 큰 값."""
    now = time.time()
    held = max((v for t, v in _cue_speed["hist"] if now - t <= hold), default=0.0)
    return max(_cue_speed["v"], held)


def _cue_v(no: int | None, peak: bool = True) -> float:
    """안내 시점에 쓸 속도 — 잰 속도와 웨이포인트 no 로 가는 구간에 보낸 속도의 하한(CUE_SPEED_FLOOR) 중 큰 값.

    peak=False 면 최근 최고 속도를 짧게(ARRIVE_PEAK_HOLD_SEC)만 본다 — 목적지 앞에서는 줄어든 걸음으로 잡아야 한다.
    """
    measured = _measured_v() if peak else _measured_v(ARRIVE_PEAK_HOLD_SEC)
    if no is None or not (0 < no <= len(waypoints_list)):
        return measured
    wp = waypoints_list[no - 1]
    return max(measured, CUE_SPEED_FLOOR.get(wp.get("sent_speed", wp.get("speed")), 0.5))


def _turn_cue_lead_m(no: int | None = None) -> float:
    """경유지 방향 안내를 낼 거리 — 속도 × (NAV_CUE_LEAD_SEC + 전달 지연), 최소 NAV_CUE_LEAD_M."""
    if NAV_CUE_LEAD_SEC <= 0:
        return NAV_CUE_LEAD_M
    return max(NAV_CUE_LEAD_M, _cue_v(no) * (NAV_CUE_LEAD_SEC + NAV_CUE_DELAY_SEC))


def _arrive_soon_lead_m(no: int) -> float:
    """목적지 "곧 도착합니다" 를 낼 거리 — 속도 × (ARRIVE_SOON_LEAD_SEC + 전달 지연), 최소 ARRIVE_SOON_M."""
    from app.navigation.turn_cues import ARRIVE_SOON_M
    return max(ARRIVE_SOON_M, _cue_v(no, peak=False) * (ARRIVE_SOON_LEAD_SEC + NAV_CUE_DELAY_SEC))


def _announce_passed(passed_no: int) -> None:
    """웨이포인트 passed_no(1부터)를 지났다 — 미리 못 낸 방향 안내가 있으면 지금 낸다."""
    _emit_cue(passed_no, "통과(미리 못 냄)")


def heading_wp_no() -> int:
    """지금 향하는 웨이포인트 번호(1부터). 다지점 묶음 안이면 로봇이 보고한 점."""
    m = _multi
    if m and current_wp_index == m["end"]:
        return m["start"] + m["value"]
    return current_wp_index


def on_multi_progress(value) -> None:
    """polling: 다지점 주행 중 로봇 1007 의 점 번호(1부터). 지난 점의 방향 안내를 내고 hang 시계를 다시 잡는다.

    hang 판정(NAV_HANG_TIMEOUT)은 보낸 시각부터 재므로, 긴 묶음이 점을 지나는 동안에는 새로 잡아 준다.
    """
    global nav_sent_time
    m = _multi
    if not m or not is_navigating or current_wp_index != m["end"]:
        return
    try:
        v = int(value)
    except (TypeError, ValueError):
        return
    v = max(1, min(v, m["end"] - m["start"]))
    if v <= m["value"]:
        return
    for no in range(m["start"] + m["value"], m["start"] + v):
        _announce_passed(no)
    m["value"] = v
    nav_sent_time = time.time()
    print(f"[NAV] 다지점 진행 — {m['start'] + v}/{len(waypoints_list)} 로 가는 중")


def multi_active() -> bool:
    """지금 로봇이 다지점 묶음을 가고 있는가(묶음 끝이 현재 목표)."""
    return bool(_multi) and is_navigating and current_wp_index == _multi["end"]


def multi_at_last() -> bool:
    """다지점 묶음의 마지막 점으로 가는 중이라고 로봇이 보고했는가(1007 진행 번호가 끝 점)."""
    m = _multi
    return multi_active() and m["start"] + m["value"] >= m["end"]


def _single_only(wp: dict) -> bool:
    """늘 단일 NAV 로 보내는 지점 — 도킹 통로·위급 직행·출도킹 고정 통로."""
    return bool(wp.get("charge_chain") or wp.get("no_route") or wp.get("dock_outbound"))


def _passes(wp: dict) -> bool:
    """다음 점과 한 묶음으로 이어 갈 수 있는 지점 — 멈추지 않는 통과 지점·노드.
    문 간선 노드(door_id)에서는 묶음을 끊는다 — 문 앞에서 선다(D5)."""
    return (bool(wp.get("pass_through") or wp.get("node") or wp.get("multi_pass")) and not wp.get("door_id")
            and not wp.get("stop_action") and not wp.get("wait_seconds"))


def _bundle_end(i: int) -> int:
    """웨이포인트 i 부터 한 번에 보낼 묶음의 끝(포함 안 함). 다지점을 안 쓰면 i + 1."""
    from app.robot_io import config
    from app.robot_io.config import NAV_MULTIPOINT_ENABLED, NAV_MULTI_MAX_POINTS
    node_on = getattr(config, "NODE_ROUTING_ENABLED", False)   # 노드망 반영 전 작업 폴더에도 들어가게
    first = waypoints_list[i]
    if not NAV_MULTIPOINT_ENABLED or _multi_unsupported or _single_only(first):
        return i + 1
    j = i + 1
    while j < len(waypoints_list) and j - i < NAV_MULTI_MAX_POINTS:
        wp = waypoints_list[j]
        if not _passes(waypoints_list[j - 1]) or _single_only(wp) or wp.get("map_id") != first.get("map_id"):
            break
        # 노드망이면 아직 노드를 펼치지 않은 다음 목표를 앞 지점 자리에서 미리 펼쳐 같은 묶음으로 잇는다.
        # 펼칠 수 없으면(경로 없음 등) 여기서 끊는다 — 그 목표는 다음 전송에서 펼치며 오류를 알린다.
        if node_on and not (wp.get("node") or wp.get("routed")) and not _expand_ahead(j):
            break
        j += 1
    if j - i >= NAV_MULTI_MAX_POINTS and j < len(waypoints_list) and _passes(waypoints_list[j - 1]):
        j = _split_at_turn(i, j)
    return j


def _split_at_turn(i: int, j: int) -> int:
    """점 수 한도로 끊어야 할 묶음 [i, j) 의 끝을 서서 도는 꺾는 점으로 당긴다.

    묶음 끝에서는 로봇이 반드시 선다(끝점은 작업점, 실행 중 새 NAV 는 거부). 자율 모드는 NODE_AUTO_TURN_STOP_DEG 이상 꺾는
    점에서 어차피 서서 돌므로 거기서 끊으면 직진 중간에 서는 일이 없다(10-29 사용자: 1회 주행이 경유지마다 멈췄다 감).
    그런 점이 없거나 직선 모드면 그대로 둔다(직선 모드는 점마다 서서 돈다).
    """
    from app.robot_io import config
    from app.robot_io.config import NAV_MULTI_MAX_POINTS
    stop_deg = getattr(config, "NODE_AUTO_TURN_STOP_DEG", 0)
    if getattr(config, "NODE_NAV_MODE", "straight") != "auto" or stop_deg <= 0:
        return j
    for e in range(j, i + 1, -1):            # 끝(e-1)이 꺾는 점인 가장 긴 묶음, 최소 2점
        k = e - 1
        if k + 1 >= len(waypoints_list) or k < 1:
            continue
        a, b, c = waypoints_list[k - 1], waypoints_list[k], waypoints_list[k + 1]
        if _turn_deg((a["x"], a["y"]), (b["x"], b["y"]), (c["x"], c["y"])) >= stop_deg:
            if e != j:
                b["split_turn"] = True      # 한도 때문에 끊은 곳 — 다음 묶음은 이어가기(split_end)로
                print(f"[NAV] 다지점 묶음을 꺾는 점 WP{e}에서 끊음(한도 {NAV_MULTI_MAX_POINTS}점, {j - i}→{e - i}점)")
            return e
    return j


_LEG_KEYS = ("prerun_leg", "halt_if_unreached", "speed")


def _inherit_leg_flags(ins: list[dict], goal: dict) -> None:
    """끼운 노드에 목표의 구간 표시를 물려 준다 — 사전 주행은 노드에서 못 가도 그 구간에서 멈추고 실패 구간을 안다."""
    for w in ins:
        for key in _LEG_KEYS:
            if key in goal and key not in w:
                w[key] = goal[key]


def _expand_ahead(j: int) -> bool:
    """묶음에 넣을 다음 목표 j 앞에 노드를 미리 끼운다(출발점 = 바로 앞 지점). 끼웠거나 필요 없으면 True."""
    wp = waypoints_list[j]
    if wp.get("no_route"):
        return True
    map_id = wp.get("map_id")
    prev = waypoints_list[j - 1]
    if map_id is None or prev.get("map_id", map_id) != map_id:
        return False
    from app.navigation import node_route
    db = SessionLocal()
    try:
        plan = node_route.plan_leg(db, int(map_id), (float(prev["x"]), float(prev["y"])), wp)
    finally:
        db.close()
    if plan.error:
        return False
    wp["routed"] = True
    if plan.waypoints:
        _inherit_leg_flags(plan.waypoints, wp)
        waypoints_list[j:j] = plan.waypoints
        if _route_cues_for is waypoints_list:
            # 방향 안내는 웨이포인트 번호에 걸려 있다 — 끼운 뒤로 남은 구간을 다시 계산한다
            _set_route_cues(_cue_start(), waypoints_list[current_wp_index:], current_wp_index)
    print(f"[NODE] (미리) {wp.get('name', '목표')}: "
          + (" → ".join(w["name"] for w in plan.waypoints) if plan.waypoints else "노드 없이 바로"))
    return True


def _cue_start():
    """방향 안내 계산의 출발점 — 지금 보낼 지점의 바로 앞 지점, 없으면 로봇 위치."""
    if current_wp_index > 0:
        p = waypoints_list[current_wp_index - 1]
        return (float(p["x"]), float(p["y"]))
    rid = _resolve_nav_robot_id()
    pos = runtime.get_position(rid) if rid else {}
    return (float(pos["x"]), float(pos["y"])) if pos and pos.get("timestamp") else None


# 직선 다지점에서 출발·꺾인 뒤 구간을 저속으로 바꿔도 되는 원래 속도 — 저속은 그대로, 고속은 계단이 더 커서 함께 낮춘다
_SLOWABLE_SPEEDS = (None, "normal", "fast")


def _straight_slow_on() -> bool:
    """직선 모드 다지점 저속 보정(NAV_TURN_SLOW_DEG)이 켜져 있는가."""
    from app.robot_io import config
    return (getattr(config, "NODE_ROUTING_ENABLED", False) and getattr(config, "NODE_NAV_MODE", "straight") == "straight"
            and getattr(config, "NAV_TURN_SLOW_DEG", 0) > 0)


def _turn_deg(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    """a→b 로 들어와 b→c 로 나갈 때 꺾이는 각도(도). 길이가 0 이면 0."""
    if math.dist(a, b) < 1e-6 or math.dist(b, c) < 1e-6:
        return 0.0
    h1 = math.atan2(b[1] - a[1], b[0] - a[0])
    h2 = math.atan2(c[1] - b[1], c[0] - b[0])
    return abs(math.degrees((h2 - h1 + math.pi) % (2 * math.pi) - math.pi))


def _soft_start_speed(straight_bundle: bool = False) -> str | None:
    """새 이동의 첫 구간을 저속으로 보낼지 — 보낼 속도("slow") 또는 None(원래 속도 그대로).

    서 있다가 출발하는 첫 전송(current_wp_index 0)만, 원래 속도가 보통이고 첫 구간이 짧을 때만.
    straight_bundle: 직선 다지점 저속 보정이 켜진 묶음이면 고속도 저속으로(거리 한도는 같다 — 10-29 오후까지는 거리 무관).
    출발 제자리 회전 점(지금 위치)은 건너뛰고 실제로 걸어가는 첫 구간을 본다.
    """
    from app.robot_io import config
    if not getattr(config, "NAV_SOFT_START", False) or current_wp_index != 0:
        return None
    wp = next((w for w in waypoints_list if not w.get("fixed_yaw")), None)
    if wp is None or wp.get("speed") not in (_SLOWABLE_SPEEDS if straight_bundle else (None, "normal")):
        return None
    rid = _resolve_nav_robot_id()
    pos = runtime.get_position(rid) if rid else {}
    if not pos or not pos.get("timestamp"):
        return None
    leg = math.dist((float(pos["x"]), float(pos["y"])), (float(wp["x"]), float(wp["y"])))
    if leg > config.NAV_SOFT_START_MAX_M:
        return None
    print(f"[NAV] 출발 구간 저속 — 첫 점까지 {leg:.1f}m")
    return "slow"


def _short_restart_leg(prev: tuple[float, float], wp: dict) -> bool:
    """서 있다 다시 출발하는 구간이 출발 저속 거리(NAV_SOFT_START_MAX_M) 이하인가 — NAV_SOFT_START 가 꺼져 있으면 False."""
    from app.robot_io import config
    if not getattr(config, "NAV_SOFT_START", False):
        return False
    return math.dist(prev, (float(wp["x"]), float(wp["y"]))) <= config.NAV_SOFT_START_MAX_M


def _send_bundle(i: int, end: int, continuing: bool = False, first_speed: str | None = None) -> bool:
    """웨이포인트 [i, end) 를 다지점 1건으로 보낸다. receiver 가 거부·무응답이면 False.

    first_speed: 첫 구간에만 쓸 속도(출발 구간 저속) — 출발 회전 점이 앞에 있으면 그다음 점까지.
    """
    from app.robot_io import config
    from app.robot_io.sender import send_nav_multi_to_robot
    rid = _resolve_nav_robot_id()
    pos = runtime.get_position(rid) if rid else {}
    prev = (pos["x"], pos["y"]) if pos and pos.get("timestamp") else None
    # 노드망이면 NODE_NAV_MODE(기본 직선 — 노드 띠 안), 아니면 지금처럼 자율
    mode = getattr(config, "NODE_NAV_MODE", "straight") if getattr(config, "NODE_ROUTING_ENABLED", False) else "auto"
    points = []
    soft = bool(first_speed)
    turn_slow = mode == "straight" and _straight_slow_on()
    stop_deg = getattr(config, "NODE_AUTO_TURN_STOP_DEG", 0.0)
    # 묶음 첫 구간이 꺾이는지 보려면 앞 점으로 들어온 방향이 필요하다 — 앞 점의 앞 점, 없으면 앞 점 방향의 뒤쪽
    came = None
    if i > 1:
        came = (waypoints_list[i - 2]["x"], waypoints_list[i - 2]["y"])
    elif i == 1:
        w = waypoints_list[0]
        came = (w["x"] - math.cos(w["yaw"]), w["y"] - math.sin(w["yaw"]))
    for k in range(i, end):
        wp = waypoints_list[k]
        last = k == end - 1
        # 경유점 방향은 들어오는 방향 — 다음 구간 방향을 주면 점에서 서서 돌고 간다(10-20 실기)
        # 출발 회전 점은 지금 위치라 들어오는 방향이 없다 — 정한 방향(첫 진행 방향)으로 돈다
        fixed = last or prev is None or wp.get("fixed_yaw")
        yaw = wp["yaw"] if fixed else math.atan2(wp["y"] - prev[1], wp["x"] - prev[0])
        # 자율 모드에서 크게 꺾는 점은 다음 구간 방향 — 걸으며 크게 돌지 않고 점에서 서서 돈다(NODE_AUTO_TURN_STOP_DEG)
        if (not fixed and mode == "auto" and stop_deg > 0 and k + 1 < len(waypoints_list)
                and _turn_deg(prev, (wp["x"], wp["y"]), (waypoints_list[k + 1]["x"], waypoints_list[k + 1]["y"])) >= stop_deg):
            nxt = waypoints_list[k + 1]
            yaw = math.atan2(nxt["y"] - wp["y"], nxt["x"] - wp["x"])
            wp["turn_stop"] = True
        speed = first_speed if soft else wp.get("speed")
        if not wp.get("fixed_yaw"):
            soft = False
        # 자율 모드: 앞 점에서 서서 돈(turn_stop) 뒤 다시 출발하는 짧은 구간도 출발 저속.
        # 출발 저속은 이동의 첫 구간만 봐서, 6m 를 달려와 N363 에서 서서 돈 뒤 1.3m 를 보통 속도로 나갔다(11-03 본체 로그)
        if (mode == "auto" and speed in (None, "normal") and prev is not None
                and k > i and waypoints_list[k - 1].get("turn_stop") and _short_restart_leg(prev, wp)):
            speed = "slow"
        # 앞 점에서 크게 꺾이면 본체가 서서 돈 뒤 바로 최고 속도로 뛴다 — 이 구간은 저속
        if (turn_slow and prev is not None and came is not None and speed in _SLOWABLE_SPEEDS
                and _turn_deg(came, prev, (wp["x"], wp["y"])) >= config.NAV_TURN_SLOW_DEG):
            speed = "slow"
        wp["sent_speed"] = speed       # 진행 멘트가 다음 안내까지 시간을 잴 때 쓴다(_check_progress_cue)
        points.append({"x": wp["x"], "y": wp["y"], "yaw": yaw, "speed": speed, "mode": mode})
        came = prev
        prev = (wp["x"], wp["y"])
    return send_nav_multi_to_robot(points, map_id=waypoints_list[i].get("map_id"), continuing=continuing)


# 장애물 재출발(이어가기)의 출발 확인 — 10-30 관제 로그의 리셋 없는 출발 0.8~3.1초 위에 여유 약 1초.
# 도착 뒤 보행 유지 출발(NAV_KEEP_GAIT_START_TIMEOUT 6초)보다 짧게 본다 — "다시 출발할게요" 뒤 서 있는 시간이라.
RESUME_START_TIMEOUT_SEC = 4.0
_probe_kind = "keep"          # 지금 감시가 무엇의 출발 확인인가: keep(도착 뒤 보행 유지) | resume(장애물 재출발)
# 이동 취소 뒤 이어가기 NAV 를 로봇이 받지 않았다 — 관제를 다시 켤 때까지 장애물 재출발은 바로 리셋해서 보낸다.
# (도착 뒤 보행 유지와는 따로 본다 — 그쪽은 취소가 없어 받는다.)
_resume_cont_off = False


def keep_gait_probe_timeout() -> float:
    """지금 출발 확인 감시의 대기 시간(초)."""
    from app.robot_io.config import NAV_KEEP_GAIT_START_TIMEOUT_SEC
    return RESUME_START_TIMEOUT_SEC if _probe_kind == "resume" else NAV_KEEP_GAIT_START_TIMEOUT_SEC


def _new_keep_gait_probe(sent_at: float) -> tuple[float, dict]:
    rid = get_robot_id()
    return sent_at, (runtime.get_position(rid) if rid else {})


def keep_gait_probe() -> tuple[float, dict] | None:
    return _keep_gait_probe


def clear_keep_gait_probe(started_after: float | None = None) -> None:
    """리셋 생략 출발을 확인했다(또는 감시를 그만둔다)."""
    global _keep_gait_probe, _probe_kind
    if _keep_gait_probe and started_after is not None:
        what = "장애물 재출발(리셋 없이)" if _probe_kind == "resume" else "내비 보행 유지"
        print(f"[NAV] {what} 출발 확인 — {started_after:.1f}s")
    _keep_gait_probe = None
    _probe_kind = "keep"


def keep_gait_start_failed(waited: float) -> None:
    """리셋 생략 출발이 보이지 않았다 — 호출한 쪽이 리셋 재전송한다.

    도착 뒤 보행 유지 출발이면 이번 안내 세션 동안 끈다. 장애물 재출발이면 이어가기만 끄고(다음 재출발은 바로 리셋)
    보행 유지는 그대로 둔다 — 취소 뒤에 받지 않는 것이지 도착 뒤 출발과는 다르다.
    """
    global _keep_gait_off, _keep_gait_probe, _probe_kind, _resume_cont_off
    if _probe_kind == "resume":
        _keep_gait_probe = None
        _probe_kind = "keep"
        _resume_cont_off = True
        print(f"[WARN] 장애물 재출발(리셋 없이) 출발 미확인({waited:.1f}s) — 리셋 후 재전송, 다음 재출발부터 바로 리셋")
        log_event("schedule", "resume_cont_fallback",
                  f"장애물 정지 뒤 리셋 없는 재출발 미확인({waited:.1f}s) — 리셋해 다시 보냄, 이후 재출발은 리셋 방식",
                  robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
        return
    _keep_gait_off = True
    _keep_gait_probe = None
    print(f"[WARN] 내비 보행 유지 출발 미확인({waited:.1f}s) — 리셋 후 재전송, 이번 안내 세션은 기존 방식")
    log_event("schedule", "keep_gait_fallback",
              f"도착 후 보행 유지 출발 미확인({waited:.1f}s) — 이번 안내 세션은 기본 보행 복귀 방식",
              robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())


def get_next_target():
    """지금 향하는 지점 다음 지점. 없으면 None."""
    if not is_navigating or current_wp_index >= len(waypoints_list):
        return None
    return waypoints_list[current_wp_index]


# receiver 는 NAV 를 받은 뒤 5초(MODE_REGULAR_NAV_GUARD_MS) 안에 그 NAV 가 끝났는지 모르면 MODE_REGULAR 를 버린다.
# 이미 목표 0.5m 안이라 실행 보고 없이 3~4초 만에 도착 처리된 경우가 그렇다 — 가드가 풀린 뒤 한 번 더 보낸다.
MODE_REGULAR_RETRY_SEC = 5.5


def _retry_mode_regular(dispatch: float, reason: str) -> None:
    """미뤄 둔 Regular 복귀 — 그 사이 상황이 바뀌었으면 보내지 않는다(다음 종료 때 다시 나간다)."""
    if is_navigating or _last_nav_dispatch != dispatch or _gait_kept:
        return
    rid = _resolve_nav_robot_id()
    if rid and runtime.is_docked(rid):
        return
    try:
        from app.robot_io.sender import send_to_robot
        send_to_robot("MODE_REGULAR")
        print(f"[NAV] Regular 모드 복귀 재전송 ({reason})")
    except Exception as e:
        print(f"[WARN] Regular 모드 복귀 재전송 실패: {e}")


def _restore_basic_gait(reason: str = "") -> None:
    """Request the robot's standard gait and Regular mode after navigation ends.

    CANCEL_NAV/STOP halt movement but do not switch the robot out of its
    navigation gait.  GAIT_BASIC is therefore part of every terminal path.
    GAIT_BASIC(/GAIT) 는 보행만 바꾸고 제어 모드는 Navigation 으로 남긴다(컨트롤러 표시) — MODE_REGULAR(Mode 0)로
    모드까지 되돌린다. GAIT_BASIC 은 MODE_REGULAR 를 모르는 receiver 를 위해 함께 보낸다(이미 0x1001 이면 전환 없음).
    도킹 중(도크로 이동·충전)에는 펌웨어 충전 순서를 건드리지 않도록 보내지 않는다.
    """
    suffix = f" ({reason})" if reason else ""
    rid = _resolve_nav_robot_id()
    if rid and runtime.is_docked(rid):
        print(f"[NAV] 도킹 중 — 기본 보행·Regular 복귀 생략{suffix}")
        return
    try:
        from app.robot_io.sender import send_to_robot
        send_to_robot("GAIT_BASIC")
        send_to_robot("MODE_REGULAR")
        print(f"[NAV] 기본 보행·Regular 모드 복귀 요청{suffix}")
        since_nav = time.time() - _last_nav_dispatch
        if since_nav < MODE_REGULAR_RETRY_SEC:
            timer = threading.Timer(MODE_REGULAR_RETRY_SEC - since_nav, _retry_mode_regular,
                                    args=(_last_nav_dispatch, reason or "네비게이션 종료"))
            timer.daemon = True
            timer.start()
    except Exception as e:
        # Do not turn a completed stop into an exception, but keep it visible.
        print(f"[WARN] 기본 보행 모드 복귀 전송 실패: {e}")

def get_current_target():
    if not is_navigating or current_wp_index <= 0:
        return None
    idx = current_wp_index - 1
    if idx < len(waypoints_list):
        return waypoints_list[idx]
    return None

def get_nav_sent_time():
    return nav_sent_time


# 마지막으로 주행이 실패한 지점. 로그로만 흘려보내면 화면이 "어디서 멈췄는지"를 못 그린다.
# {"index": 1-based, "message": str} 또는 None. 새 주행이 시작되면 지워진다.
_last_nav_error = None


def set_nav_error(index: int, message: str) -> None:
    """주행 실패 지점을 남긴다 (진행 표시의 error 마커용)."""
    global _last_nav_error
    _last_nav_error = {"index": int(index), "message": (message or "")[:200]}


def clear_nav_error() -> None:
    global _last_nav_error
    _last_nav_error = None


def mark_current_wp_unreached(reason: str, through_end: bool = False) -> None:
    """지금 향하던 웨이포인트를 '못 감' 으로 표시한다(재전송 한도 초과로 건너뛸 때).

    순찰은 다음 WP 로 계속 가지만, 마지막 WP 를 건너뛰고 끝난 주행을 '도착' 으로 보면
    안내(음성 단계 엔진)가 제자리에서 도착 멘트를 한다 — /robot/nav 의 arrived 가 이 표시를 본다.
    """
    no = heading_wp_no()
    if 0 < no <= len(waypoints_list):
        # 다지점 묶음이면 향하던 점부터 묶음 끝까지 못 간 것이다 — 첫 점만 표시하면 마지막 점이 '도착' 으로
        # 남아 안내가 제자리에서 도착 멘트를 한다(10-27 실기: 7점 묶음 미출발 → 수납 앞에서 도착 멘트)
        m = _multi
        last = m["end"] if m and current_wp_index == m["end"] else no
        if through_end:
            # 안내는 한 곳이라도 못 가면 거기서 끝낸다 — 묶음이 나뉘어 있어도 다음 묶음으로 가지 않는다
            last = len(waypoints_list)
        for k in range(no - 1, min(last, len(waypoints_list))):
            waypoints_list[k]["unreached"] = True
            waypoints_list[k]["unreached_reason"] = reason
            if through_end:
                waypoints_list[k]["halt_if_unreached"] = True
        set_nav_error(no, reason)


def mark_current_wp_skipped(reason: str) -> None:
    """운영자가 [건너뛰고 계속] — 못 간 지점으로 표시하고, 그 지점의 도착 안내·확인은 하지 않는다."""
    no = heading_wp_no()
    mark_current_wp_unreached(reason)
    if 0 < no <= len(waypoints_list):
        waypoints_list[no - 1]["skipped"] = True


def skipped_summary() -> str | None:
    """회차 사유용 — '건너뛴 지점 2: 복도 끝, 비상구 앞'."""
    names = [w.get("name", "") for w in waypoints_list if w.get("skipped")]
    return f"건너뛴 지점 {len(names)}: {', '.join(names)}" if names else None


def last_wp_unreached() -> bool:
    return bool(waypoints_list) and bool(waypoints_list[-1].get("unreached"))


def last_nav_error_message() -> str | None:
    return (_last_nav_error or {}).get("message")


def mark_arrived_in_place(name: str, x: float, y: float, yaw: float) -> None:
    """이미 목표 근처라 움직이지 않고 '도착한 주행' 으로 남긴다.

    NAV 를 보내도 로봇은 가까운 목표에는 움직이지 않아 '이동 미감지' 로 실패 처리된다.
    /robot/nav 의 arrived 가 참이 되도록 끝까지 간 1개짜리 주행 상태를 만든다.
    """
    global waypoints_list, current_wp_index, is_navigating
    waypoints_list = [{"x": x, "y": y, "yaw": yaw, "name": name}]
    current_wp_index = 1
    is_navigating = False
    clear_nav_error()


def get_nav_progress() -> dict:
    """주행 진행 상태 — 로봇 상세 '현재 작업'의 경유지 표시가 쓴다.

    상태 매핑을 여기서 한 번만 한다. 화면이 인덱스로 다시 계산하면
    '몇 번째부터 current 인가' 규칙이 두 벌이 된다.

    주의: 이 값들은 **모듈 전역 1칸**이라 로봇을 구분하지 않는다.
    응답에 robotName 을 실어 화면이 "지금 열어 본 로봇인가"를 가드한다
    (다로봇 런타임이 들어오면 robot_id 파라미터로 바뀐다 — T-11).
    """
    err = _last_nav_error
    steps = []
    heading = heading_wp_no()
    for i, wp in enumerate(waypoints_list):
        no = i + 1
        if err and err["index"] == no:
            status = "error"
        elif no < heading:
            status = "completed"
        elif no == heading:
            status = "current"
        else:
            status = "pending"
        steps.append({"name": wp.get("name") or f"WP{no}", "status": status,
                      "x": wp.get("x"), "y": wp.get("y"), "mapId": wp.get("map_id"),
                      "node": bool(wp.get("node"))})

    # 지도 표시용 — 지금 로봇 위치(출발점 자리)와 남은 거리, 지금 보낸 다지점 묶음(번호 1부터, 끝 포함)
    rid = get_robot_id()
    pos = runtime.get_position(rid) if rid else {}
    start = {"x": pos["x"], "y": pos["y"]} if pos and pos.get("timestamp") else None
    remain = None
    if start and is_navigating and 0 < heading <= len(waypoints_list):
        pts = [(start["x"], start["y"])] + [(w["x"], w["y"]) for w in waypoints_list[heading - 1:]]
        remain = round(sum(math.dist(a, b) for a, b in zip(pts, pts[1:])), 1)
    bundle = ({"from": _multi["start"] + 1, "to": _multi["end"]} if _multi and is_navigating else None)

    return {
        "robotName": get_robot_name(),
        "isNavigating": is_navigating,
        "currentIndex": heading,
        "total": len(waypoints_list),
        "waypoints": steps,
        "start": start,
        "remainM": remain,
        "bundle": bundle,
        "loop": {
            "count": nav_loop_count,
            "total": nav_loop_total,
            "infinite": nav_loop_infinite,
        },
        "error": err,
    }

def check_and_clear_reset_flag():
    """nav_thread에서 호출: 리셋 신호가 있으면 (True, is_full) 반환 후 클리어"""
    global _nav_reset_flag, _nav_full_reset_flag
    if _nav_reset_flag:
        is_full = _nav_full_reset_flag
        _nav_reset_flag = False
        _nav_full_reset_flag = False
        return True, is_full
    return False, False

def nav_epoch() -> int:
    """지금까지 시작·정지된 주행 횟수 — 값이 바뀌었으면 그 사이 다른 주행 명령이 있었다."""
    return _nav_epoch


def clear_arrival_actions() -> None:
    """새 작업 이동 시작 — 앞선 주행이 남긴 '도착 후 SIT·충전' 표시를 지운다.

    도착 처리는 표시만 보고 실행한다. 언도킹 재정렬(sit_on_arrival) 직후 안내 이동이 들어오면
    안내 목적지에서 SIT 했다(2026-10-14 13:11 실기). 과열 보호 예약(overheat_return_pending)은
    다음 도착에서 충전소로 돌리는 보호 동작이라 지우지 않는다.
    """
    global sit_on_arrival, charge_on_arrival
    sit_on_arrival = False
    charge_on_arrival = False


UNDOCK_SIT_MAX_DIST_M = 0.5   # 언도킹 재정렬이 도킹 포인트에서 이보다 멀리 끝나면 앉지 않는다


def _undock_sit_skip_reason(target: dict | None) -> tuple[str, bool] | None:
    """언도킹 재정렬 도착 SIT 를 건너뛸 (사유, 기본 보행 복귀 여부). 과열 보호 SIT 는 대상이 아니다(None).

    엉뚱한 자리에서 끝났으면 그 자리에 앉지 않고 기본 보행으로 세워 둔다. 안내 세션이 이미 진행 중이면
    곧 다음 이동이 나가므로 앉았다 일어서지 않고 내비 보행도 유지한다
    (10-14~15 언도킹 8회 중 7회가 SIT 뒤 안내 시작 STAND).
    """
    if not target or not target.get("undock_realign"):
        return None
    # 안내 세션을 먼저 본다 — 위치가 어긋났어도 곧 안내 이동이 나가므로 기본 보행으로 돌리지 않는다.
    try:
        from app.database.routes.voice_scenario import scenario_busy
        if scenario_busy(get_robot_name()):
            return "안내 세션 진행 중", False
    except Exception as e:
        print(f"[WARN] 언도킹 SIT 전 안내 세션 확인 실패: {e}")
    rid = get_robot_id()
    pos = runtime.get_position(rid) if rid else {}
    if pos.get("x") is not None and pos.get("y") is not None:
        dist = math.hypot(pos["x"] - target["x"], pos["y"] - target["y"])
        if dist > UNDOCK_SIT_MAX_DIST_M:
            return f"도킹 포인트에서 {dist:.2f}m 떨어짐", True
    return None


def _signal_nav_reset(full=False):
    global _nav_reset_flag, _nav_full_reset_flag, nav_sent_time, _nav_epoch
    _nav_epoch += 1
    _nav_reset_flag = True
    _nav_full_reset_flag = full
    # 새 주행이 시작되거나 정지하면 지난 실패 지점은 의미가 없다.
    # 안 지우면 다음 경로의 엉뚱한 지점에 ✕ 가 붙는다.
    clear_nav_error()
    # 이전 작업의 stale nav_sent_time이 남아 있으면 nav_thread가
    # elapsed > NAV_RETRY_TIMEOUT으로 오판해 새 명령 송신 직후 즉시 재전송이 발동된다.
    # 새 명령은 곧 navigation_send_next/resend_current에서 nav_sent_time을 다시 갱신하므로
    # 그 사이 폴링 사이클에서 재전송이 트리거되지 않도록 0으로 비워둔다.
    nav_sent_time = 0


NAV_OWNER = "nav"


def _claim_nav(robot_id: int, working: bool) -> None:
    """주행 시작/종료에 맞춰 로봇 소유권을 잡고 놓는다.

    이 한 곳이 자율주행·장소이동·경로이동 세 경로를 모두 덮는다.
    획득 실패(다른 백엔드 사용 중)는 여기서 막지 않는다 — 진입 시점의
    require_robot_available 이 이미 걸렀고, 주행 도중에 되돌릴 수는 없다.
    """
    if robot_id is None:
        return
    try:
        from app.robot_control import control_lock as cl
        if working:
            cl.acquire_system(robot_id, NAV_OWNER, label="자율 주행")
        else:
            cl.release_system(robot_id, NAV_OWNER)
    except Exception as e:  # noqa: BLE001 - 소유권 처리 실패가 주행을 막지 않는다
        print(f"[NAV] 소유권 처리 실패(무시): {str(e)[:80]}")


def _update_is_working(robot_id: int, working: bool):
    """robot_last_status.IsWorking을 DB에 업데이트 + 로봇 소유권 획득/반납."""
    _claim_nav(robot_id, working)
    try:
        db = SessionLocal()
        try:
            from app.database.models import RobotLastStatus
            row = db.query(RobotLastStatus).filter(
                RobotLastStatus.RobotId == robot_id
            ).first()
            if row:
                row.IsWorking = 1 if working else 0
                db.commit()
        finally:
            db.close()
    except Exception as e:
        print(f"[ERR] IsWorking 업데이트 실패: {e}")


def reconcile_is_working_on_boot() -> None:
    """부팅 시 DB IsWorking 고착 정정.

    비정상 종료(통신 두절/강제 재시작)로 정상 정지 경로를 타지 못하면
    robot_last_status.IsWorking 이 1로 남아, 재시작 후 _check_navigating 이
    DB 값을 읽어 로봇을 영원히 '작업중'으로 오판한다(실시간 상태 갱신과 무관하게
    UI 가 작업중에 고착). 부팅 시 이 백엔드가 제어하는 로봇에 한해 실제 로봇의
    NAV_STATUS 를 1회 확인하여, 물리적으로 비주행(status==0)인데 DB IsWorking==1
    이면 0 으로 정정한다.

    - 자기 로봇만 대상으로 하고 실로봇 상태로 확인하므로 멀티서버 의미
      (다른 서버가 시작한 작업 = DB IsWorking)를 깨지 않는다.
    - receiver 무응답/타임아웃 시에는 확인 불가이므로 그대로 둔다(보수적).
    """
    import json as _json
    import socket as _socket
    from app.robot_io.config import RECEIVER_IP, RECEIVER_PORT
    from app.database.models import RobotLastStatus

    robot_id = get_robot_id()
    if not robot_id:
        return
    try:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        try:
            sock.sendto(_json.dumps({"action": "NAV_STATUS"}).encode("utf-8"),
                        (RECEIVER_IP, RECEIVER_PORT))
            nav = _json.loads(sock.recvfrom(4096)[0].decode("utf-8"))
        finally:
            sock.close()
    except Exception as e:
        print(f"[BOOT-RECONCILE] 실로봇 NAV_STATUS 확인 불가 — IsWorking 정정 보류: {e}")
        return

    if nav.get("status") != 0:
        return  # 실제로 주행 중 — 정정하지 않음

    db = SessionLocal()
    try:
        row = db.query(RobotLastStatus).filter(RobotLastStatus.RobotId == robot_id).first()
        if row and row.IsWorking:
            row.IsWorking = 0
            db.commit()
            print(f"[BOOT-RECONCILE] robot_id={robot_id} 실로봇 비주행 확인 — 고착된 IsWorking=1 → 0 정정")
            log_event("robot", "robot_work_reconciled",
                      "재시작 후 작업중 상태 정정 (실제 비주행 확인)",
                      detail="비정상 종료로 남은 IsWorking=1 을 실로봇 NAV_STATUS=0 확인 후 해제",
                      robot_id=robot_id, robot_name=get_robot_name(), business_id=get_robot_business_id())
    except Exception as e:
        print(f"[BOOT-RECONCILE] IsWorking 정정 실패: {e}")
    finally:
        db.close()

def stop_navigation_internal(reason: str = "", keep_schedule: bool = False) -> bool:
    """인증 의존성 없이 호출 가능한 내부 정지 로직.

    nav_thread(폴링 스레드)의 통신 두절 자동 정지 등 서버 내부에서
    사용자 요청 없이 주행을 안전 정지해야 할 때 사용한다.
    반환값: 정지 시점에 실제로 주행 중이었는지 여부.
    """
    global is_navigating, current_wp_index, nav_loop_remaining, nav_loop_total, nav_loop_count, nav_loop_infinite, charge_on_arrival, auto_return_to_charge, sit_on_arrival, overheat_return_pending, _boundary_done_index, _pass_handoff_index, _gait_kept, _keep_gait_probe, _multi
    was_active = is_navigating
    is_navigating = False
    current_wp_index = 0
    _multi = None
    _boundary_done_index = None
    _pass_handoff_index = None
    _gait_kept = False          # 아래에서 GAIT_BASIC 을 보낸다
    _keep_gait_probe = None
    nav_loop_remaining = 0
    nav_loop_total = 0
    nav_loop_count = 0
    nav_loop_infinite = False
    charge_on_arrival = False
    auto_return_to_charge = False
    sit_on_arrival = False
    overheat_return_pending = False
    _signal_nav_reset(full=True)
    _update_is_working(get_robot_id(), False)

    # 진행 중인 층 전환 취소 — 정지 후에도 워커가 살아 있으면 타임아웃까지 폴링하다
    # 뒤늦게 stop_navigation_internal 을 다시 부르고 CANCEL_NAV 를 중복 송신한다.
    try:
        from app.navigation.floor_transition import cancel_transition
        rid = _resolve_nav_robot_id()
        if rid and cancel_transition(rid):
            print(f"🛗 층 전환 취소 요청 (정지: {reason or '사용자 요청'})")
    except Exception as e:
        print(f"[WARN] 층 전환 취소 실패: {e}")

    # 자동문 통과 표시 정리 — 문 앞 대기 중 정지면 게이트가 스스로 빠져나오고, 통과 중 정지면 표시만 남는다
    try:
        from app.navigation.door_gate import finish_crossing
        finish_crossing(_resolve_nav_robot_id())
    except Exception as e:
        print(f"[WARN] 자동문 통과 표시 정리 실패: {e}")

    # 로봇에 즉시 정지 명령 + 네비게이션 취소
    try:
        from app.robot_io.sender import send_to_robot
        send_to_robot("CANCEL_NAV")
        send_to_robot("STOP")
        _restore_basic_gait(reason or "네비게이션 종료")
    except Exception as e:
        print(f"[WARN] STOP/CANCEL_NAV 전송 실패: {e}")

    # 진행 중인 스케줄 취소. keep_schedule 이면 주행만 세운다 — 상시 대기 중 안내를 시작하며
    # 대기 장소 이동을 멈출 때 근무(회차)까지 취소되면 안 된다.
    try:
        from app.scheduler.loop import cancel_active_schedule, get_active_schedule_id
        if not keep_schedule and get_active_schedule_id() is not None:
            cancel_active_schedule(reason or "작업 정지")
    except Exception as e:
        print(f"[WARN] 스케줄 취소 실패: {e}")

    try:
        from app.recording.manager import stop_all_recording
        stop_all_recording(get_robot_id())
    except Exception as e:
        print(f"[WARN] 녹화 정지 실패: {e}")

    print(f"🛑 NAV STOP (was_active={was_active}, reason={reason or '사용자 요청'})")
    return was_active


@move.get("/progress")
def nav_progress(current_user: UserInfo = Depends(get_current_user)):
    """현재 주행의 경유지 진행 상태. 로봇 상세 '현재 작업'이 5초 주기로 읽는다."""
    return get_nav_progress()


@move.get("/hold")
def nav_hold_state(current_user: UserInfo = Depends(get_current_user)):
    """멈춘 이동(막힘·도착 못 함) — 로봇 상세·확인창이 읽는다. 없으면 hold=null."""
    from app.navigation import nav_hold
    return {"robotName": get_robot_name(), "hold": nav_hold.snapshot()}


class NavHoldActionReq(BaseModel):
    action: str        # retry | skip | abort


@move.post("/hold-action")
def nav_hold_action(req: NavHoldActionReq, current_user: UserInfo = Depends(get_current_user)):
    """멈춘 이동에 대한 운영자 선택. 실제 처리는 주행 감시 스레드가 다음 주기(1초 안)에 한다."""
    from app.navigation import nav_hold
    res = nav_hold.request(req.action)
    if not res["ok"]:
        raise HTTPException(status_code=409, detail=res["message"])
    return res


@move.get("/arrival-gate")
def arrival_gate_state(current_user: UserInfo = Depends(get_current_user)):
    """도착 확인 게이트 상태 — 관제가 "지금 누가 확인을 기다리는가"를 알아야 누를 수 있다.

    이 조회가 없어서 confirm='button'/'voice' 는 설정만 가능하고 실제로는
    600초 하드캡 뒤 작업이 중단되는 옵션이었다(M-06).
    """
    from app.navigation.arrival_gate import gate_status
    st = gate_status()
    if st.get("active"):
        st["robotName"] = get_robot_name()
    return st


@move.post("/stopmove")
def stop_navigation(current_user: UserInfo = Depends(get_current_user)):
    was_active = stop_navigation_internal("사용자 정지")
    return {
        "status": "ok",
        "was_active": was_active,
        "msg": "작업이 중지되었습니다." if was_active else "진행 중인 작업이 없습니다.",
    }

def navigation_resend_current(continuing: bool = False):
    """현재 웨이포인트를 재전송 (로봇이 명령을 무시했을 때).

    continuing: 로봇이 내비 보행으로 서 있다고 보고 모드 리셋 없이 잇는다(receiver 가 보행을 다시 확인하고,
    내비 보행이 아니면 그대로 리셋한다). 장애물 정지가 풀린 출발만 쓴다 — 리셋하면 꺾는 점 앞에서 보행이 풀렸다가
    다시 잡혔다(10-30 15:10 1F N243). 응답 없음·멈춤 감지 재전송은 일부러 리셋하므로 기본값(False)을 쓴다.
    """
    global nav_sent_time, _last_nav_dispatch, _keep_gait_probe, _probe_kind

    if not is_navigating or current_wp_index <= 0:
        return
    if continuing and _resume_cont_off:
        continuing = False          # 앞서 취소 뒤 이어가기를 받지 않았다 — 기다리지 않고 리셋해서 보낸다

    # 층 전환 중에는 재전송 금지 — waypoints_list[idx-1] 은 이전 층 좌표라
    # 새로 활성화된 맵 프레임에 전송하면 엉뚱한 목표로 주행한다.
    from app.navigation.floor_transition import is_transitioning
    rid = _resolve_nav_robot_id()
    if rid and is_transitioning(rid):
        print("🔁 NAV 재전송 스킵 — 층 전환 진행 중")
        return

    idx = current_wp_index  # 이미 +1 된 상태
    wp = waypoints_list[idx - 1]

    # 다지점 묶음이면 지금 향하던 점부터 남은 점을 다시 보낸다(처음부터 보내면 지난 점으로 되돌아간다)
    m = _multi
    if m and idx == m["end"]:
        k = m["start"] + m["value"] - 1
        if m["end"] - k > 1:
            print(f"🔁 NAV 재전송(다지점): {k + 1}~{m['end']} / {len(waypoints_list)}")
            _signal_nav_reset()
            if _send_bundle(k, m["end"], continuing=continuing):
                m["start"], m["value"] = k, 1
                nav_sent_time = _last_nav_dispatch = time.time()
                if continuing:
                    # 취소 뒤 리셋 없는 NAV 를 로봇이 받는지 실기 미확인 — 안 떠나면 출발 확인 감시가 리셋 재전송한다
                    _keep_gait_probe, _probe_kind = _new_keep_gait_probe(nav_sent_time), "resume"
                return
            print("[NAV] 다지점 재전송 실패 — 묶음 끝 단일 재전송")

    print(f"🔁 NAV 재전송: {idx} / {len(waypoints_list)}")
    _signal_nav_reset()
    from app.robot_io.sender import send_nav_to_robot
    send_nav_to_robot(
        idx, wp["x"], wp["y"], wp["yaw"],
        speed=wp.get("speed"), map_id=wp.get("map_id"), continuing=continuing,
    )
    nav_sent_time = _last_nav_dispatch = time.time()
    if continuing:
        _keep_gait_probe, _probe_kind = _new_keep_gait_probe(nav_sent_time), "resume"

def _resolve_nav_robot_id():
    """네비 대상 로봇 id 해석. get_robot_id() 캐시가 비어 있으면(백그라운드 nav_thread 등)
    ROBOT_IP(주행 대상)로 역조회 → DB 첫 로봇 순으로 폴백한다.
    (층 전환 감지가 get_robot_id() 캐시 유무에 좌우되지 않도록.)"""
    try:
        rid = get_robot_id()
        if rid:
            return rid
    except Exception:
        pass
    from app.robot_io.config import ROBOT_IP
    try:
        rid = runtime.get_robot_id_by_ip(ROBOT_IP)
        if rid:
            return rid
    except Exception:
        pass
    # runtime 미populated 대비: DB에서 ROBOT_IP 매칭 로봇(결정적). 못 찾으면 None(전환 안 함).
    try:
        from app.database.models import RobotInfo
        _db = SessionLocal()
        try:
            r = _db.query(RobotInfo).filter(RobotInfo.RobotIP == ROBOT_IP).first()
            return r.id if r else None
        finally:
            _db.close()
    except Exception:
        return None


def resume_after_transition(robot_id: int | None = None, skip_boundary: bool = True):
    """층 전환 완료 후 floor_transition 워커가 호출.

    skip_boundary=True (실엘베/assisted): 사람이 로봇을 도착층 EV로비에 내려놨으므로
        경계 waypoint 자리에 이미 있다 → 건너뛰고 다음 waypoint 부터 주행.
    skip_boundary=False (겹침 매핑): 로봇은 아직 출발 EV로비에 서 있다 → 경계 waypoint 로
        실제 주행해야 한다. 같은 경계를 재감지해 재전환하지 않도록 1회 면제 표시를 남긴다.

    robot_id 를 주면 전환 진행 플래그 해제 → 인덱스 조정 → 재개를 한 번에 처리한다.
    워커가 먼저 플래그를 내리고 이 함수를 부르면, 그 사이 nav_thread 가 끼어들어
    같은 경계를 재감지해 층 전환을 다시 시작할 수 있다(레이스).
    """
    global current_wp_index, _boundary_done_index
    if robot_id:
        from app.navigation.floor_transition import clear_transition
        clear_transition(robot_id)
    if not is_navigating:
        return
    if skip_boundary:
        current_wp_index += 1  # 경계 waypoint(seed 지점) 스킵
    else:
        _boundary_done_index = current_wp_index  # 경계 판정 1회 면제 후 정상 전송
    navigation_send_next()


# ── 노드망 경로 (2026-10-19) ─────────────────────────────
# 켜져 있으면(NODE_ROUTING_ENABLED) 목표를 보내기 직전에 '로봇 위치 → 목표' 구간을 노드 통과 지점으로 펼친다.
# 끼운 지점은 node=True, 펼친 목표는 routed=True, 펼치지 않을 목표(충전 통로·위급 직행 등)는 no_route=True.
def _node_route_fail(msg: str) -> bool:
    global is_navigating
    is_navigating = False
    set_nav_error(current_wp_index + 1, msg)
    _update_is_working(get_robot_id(), False)
    print(f"[NODE] 이동 중단 — {msg}")
    log_event("error", "node_route_failed", msg,
              robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
    return False


def _depart_turn(pos: dict, xy: tuple[float, float], nxt: dict, map_id) -> dict | None:
    """출발 제자리 회전 점 — 지금 방향과 첫 진행 방향이 NAV_DEPART_TURN_DEG 이상 다를 때만.

    다지점 묶음의 첫 점으로만 쓴다(경유점은 0.05m 안까지 들어가 서서 방향을 맞춘다, 10-20 실기).
    단일 NAV 로 보내면 지금 위치라 바로 지난 것으로 볼 수 있어 다지점을 못 쓸 때는 넣지 않는다.
    """
    from app.robot_io.config import NAV_MULTIPOINT_ENABLED
    if NAV_DEPART_TURN_DEG <= 0 or not NAV_MULTIPOINT_ENABLED or _multi_unsupported:
        return None
    if math.dist(xy, (nxt["x"], nxt["y"])) < DEPART_TURN_MIN_M:
        return None
    head = math.atan2(nxt["y"] - xy[1], nxt["x"] - xy[0])
    signed = (head - float(pos.get("yaw") or 0.0) + math.pi) % (2 * math.pi) - math.pi   # + 왼쪽
    diff = abs(signed)
    if math.degrees(diff) < NAV_DEPART_TURN_DEG:
        return None
    return {"x": xy[0], "y": xy[1], "yaw": head, "name": "출발 회전", "map_id": map_id,
            "floor_id": nxt.get("floor_id"), "node": True, "multi_pass": True, "fixed_yaw": True,
            "turn_deg": round(math.degrees(diff)), "turn_signed": round(math.degrees(signed))}


def depart_turn_preview(db, pos: dict, place) -> str | None:
    """그 장소로 지금 출발하면 제자리에서 먼저 도는가 — 돈다면 방향 안내 문장("왼쪽으로 방향을 바꾸겠습니다.").

    음성 엔진이 이동 전 확인 때 받아 출발 멘트 뒤, 회전 직전에 말한다(11-02 메모: 말없이 돌아 손잡이 잡은 방문자가 놀람).
    실제 출발(_expand_node_leg)과 같은 계산 — 출도킹 통로로 나가면 회전 점이 없다. 노드망을 안 쓰면 회전 점도 없다.
    """
    from app.robot_io.config import NODE_ROUTING_ENABLED
    if not NODE_ROUTING_ENABLED or place is None or place.MapId is None:
        return None
    if pos.get("initpose_pending") or not pos.get("timestamp"):
        return None
    from app.navigation import node_route
    from app.navigation.turn_cues import depart_turn_phrase
    robot_xy = (float(pos["x"]), float(pos["y"]))
    goal = {"x": place.LocationX, "y": place.LocationY, "name": place.LacationName,
            "map_id": place.MapId, "floor_id": place.FloorId}
    prefix, outbound_start = node_route.outbound_prefix(db, int(place.MapId), robot_xy, goal)
    if prefix:
        return None
    plan = node_route.plan_leg(db, int(place.MapId), outbound_start or robot_xy, goal)
    if plan.error:
        return None
    turn = _depart_turn(pos, robot_xy, (plan.waypoints or [goal])[0], place.MapId)
    return depart_turn_phrase(turn["turn_signed"]) if turn else None


def _expand_node_leg() -> bool:
    """지금 보낼 목표 앞에 노드 통과 지점을 끼운다. 경로를 못 만들면 이동을 멈추고 False."""
    from app.robot_io.config import NODE_ROUTING_ENABLED
    if not NODE_ROUTING_ENABLED:
        return True
    wp = waypoints_list[current_wp_index]
    if wp.get("node") or wp.get("routed") or wp.get("no_route"):
        return True
    rid = _resolve_nav_robot_id()
    pos = runtime.get_position(rid) if rid else {}
    map_id = wp.get("map_id")
    if map_id is None and rid:
        map_id = runtime.get_current_map_id(rid)
    if pos.get("initpose_pending") or not pos.get("timestamp"):
        # 충전 복귀도 같다 — 노드망 없이 보내지 않는다(10-23). 위치 재조정 뒤 다시 보낸다
        return _node_route_fail("로봇 위치가 확정되지 않아 노드 경로를 계산할 수 없습니다.")
    if map_id is None:
        return _node_route_fail("로봇의 현재 맵을 알 수 없어 노드 경로를 계산할 수 없습니다.")

    from app.navigation import node_route
    robot_xy = (float(pos["x"]), float(pos["y"]))
    start = robot_xy
    # 직전 웨이포인트 근처면 그 좌표에서 출발한 것으로 본다 — 통과 지점을 걷는 중에 넘겨도 같은 길이 나오게
    if current_wp_index > 0:
        prev = waypoints_list[current_wp_index - 1]
        if prev.get("map_id", map_id) == map_id and math.dist(robot_xy, (prev["x"], prev["y"])) < 1.5:
            start = (float(prev["x"]), float(prev["y"]))
    db = SessionLocal()
    try:
        prefix, outbound_start = node_route.outbound_prefix(db, int(map_id), robot_xy, wp)
        plan = node_route.plan_leg(db, int(map_id), outbound_start or start, wp)
    finally:
        db.close()
    if plan.error:
        return _node_route_fail(plan.error)
    wp["routed"] = True
    ins = prefix + plan.waypoints
    # 서 있다가 출발할 때만(앞 지점을 걸어서 지나가는 중이면 이미 그쪽을 보고 있다). 출도킹 통로는 정해진 길이다
    standing = current_wp_index == 0 or not _passes(waypoints_list[current_wp_index - 1])
    turn = None if prefix or not standing else _depart_turn(pos, robot_xy, (ins or [wp])[0], map_id)
    if turn:
        ins = [turn] + ins
    if ins:
        _inherit_leg_flags(ins, wp)
        waypoints_list[current_wp_index:current_wp_index] = ins
        if _route_cues_for is waypoints_list:
            # 방향 안내는 웨이포인트 번호에 걸려 있다 — 끼운 뒤로 남은 구간을 다시 계산한다
            _set_route_cues(start, waypoints_list[current_wp_index:], current_wp_index)
    print(f"[NODE] {wp.get('name', '목표')}: "
          + (" → ".join(w["name"] for w in ins) if ins else "노드 없이 바로")
          + (" (노드망 밖 → 진입)" if plan.entry else "")
          + (f" (출발 제자리 회전 {turn['turn_deg']}°)" if turn else "")
          + f" — 노드 경로 {'-'.join(node_route.label(n) for n in plan.node_path)}")
    return True


def _reset_node_legs() -> None:
    """반복 회차를 다시 시작할 때 — 지난 회차에 끼운 노드를 걷어 내고 목표를 다시 펼치게 한다."""
    waypoints_list[:] = [w for w in waypoints_list if not w.get("node")]
    for w in waypoints_list:
        w.pop("routed", None)


def replan_after_relocalization(robot_id: int, before: dict | None, after: dict | None,
                                info: dict | None = None) -> None:
    """측위가 복구됐는데 위치를 믿기 어려우면 지금 구간을 현재 위치에서 다시 계산한다.

    장애물 정지 후 재개는 같은 목표 재전송이 원칙이지만, 위치가 튄 뒤 재전송하면 튄 자리에서 다음 노드로
    곧장 가려다 벽을 넘을 수 있다. 다시 계산하는 경우(B3 실측 10-19):
      - 마지막 정상 측위 위치에서 NODE_REPLAN_JUMP_M 넘게 달라짐
      - 상실이 NODE_REPLAN_LOST_SEC 넘게 이어짐
      - 상실 중 맵이 바뀌었거나 위치를 새로 심어 풀림(운영자 확정·자동 init_pose)
    아니면 아무것도 하지 않는다(로봇은 가던 목표로 간다).
    """
    global current_wp_index
    from app.robot_io.config import NODE_REPLAN_JUMP_M, NODE_REPLAN_LOST_SEC, NODE_ROUTING_ENABLED
    if not (NODE_ROUTING_ENABLED and is_navigating and after):
        return
    info = info or {}
    jump = math.hypot(after["x"] - before["x"], after["y"] - before["y"]) if before else math.inf
    why = (f"위치 {jump:.2f}m 튐" if jump > NODE_REPLAN_JUMP_M
           else f"상실 {info['lost_sec']:.0f}s" if info.get("lost_sec", 0) > NODE_REPLAN_LOST_SEC
           else "상실 중 맵 바뀜" if info.get("map_changed")
           else "위치 재설정으로 확정" if info.get("reset")
           else "")
    if not why:
        print(f"[NODE] 측위 복구 — 위치 차이 {jump:.2f}m ≤ {NODE_REPLAN_JUMP_M}m, 가던 목표 유지")
        return
    from app.navigation.floor_transition import is_transitioning
    if is_transitioning(robot_id):
        return
    i = current_wp_index - 1                 # 지금 가고 있는(보낸) 웨이포인트
    if not (0 <= i < len(waypoints_list)):
        return
    j = i
    while j < len(waypoints_list) and waypoints_list[j].get("node"):
        j += 1
    if j >= len(waypoints_list) or waypoints_list[j].get("no_route") or waypoints_list[i].get("no_route"):
        return                               # 고정 통로·위급 직행은 그대로 둔다
    goal = waypoints_list[j]
    del waypoints_list[i:j]
    goal.pop("routed", None)
    current_wp_index = i
    msg = f"측위 복구({why}) — '{goal.get('name', '목표')}'까지 현재 위치에서 다시 계산"
    print(f"[NODE] {msg}")
    log_event("schedule", "node_route_replan", msg,
              robot_id=robot_id, robot_name=get_robot_name(), business_id=get_robot_business_id())
    navigation_send_next()


runtime.add_recovery_listener(replan_after_relocalization)


def node_entry_check(db: Session, places: list) -> str | None:
    """노드망 경로가 켜져 있을 때 이동을 받기 전 확인 — 거절 사유 문자열, 괜찮으면 None.

    로봇이 있는 맵과 목적지 맵이 이동 가능(노드망 완성)해야 하고, 목적지 장소가 노드망에 붙어 있어야 한다.
    """
    from app.robot_io.config import NODE_ROUTING_ENABLED
    if not NODE_ROUTING_ENABLED:
        return None
    from app.navigation import node_route
    rid = _resolve_nav_robot_id()
    map_ids = [runtime.get_current_map_id(rid) if rid else None] + [p.MapId for p in places]
    for mid in dict.fromkeys(m for m in map_ids if m is not None):
        ok, msg = node_route.check_ready(db, mid)
        if not ok:
            return msg
    graphs: dict = {}
    for p in places:
        if p.MapId is None or p.LocationX is None:
            continue
        g = graphs.setdefault(p.MapId, node_route.load_graph(db, int(p.MapId)))
        if node_route.attach_any(g, (float(p.LocationX), float(p.LocationY))) is None:
            return f"'{p.LacationName}'은(는) 노드망에 연결되지 않은 장소라 갈 수 없습니다."
    return None


def navigation_send_next():
    global current_wp_index, waypoints_list, is_navigating, nav_sent_time, _last_nav_dispatch, nav_loop_remaining, nav_loop_total, nav_loop_count, nav_loop_infinite, charge_on_arrival, sit_on_arrival, overheat_return_pending, _boundary_done_index, _pass_handoff_index, _gait_kept, _keep_gait_probe, _multi, _multi_unsupported, _probe_kind

    # 통과 지점 이어가기 표시는 이번 전송에만 쓴다 — 중간에 반환해도 다음 주행으로 새지 않게 먼저 거둔다
    handoff_index, _pass_handoff_index = _pass_handoff_index, None
    _keep_gait_probe = None
    _multi = None

    if not is_navigating:
        return

    # 못 간 지점에서 멈춰야 하는 주행(안내 사전 주행) — 순찰처럼 다음 지점으로 넘어가지 않는다
    # 다지점 묶음이면 못 간 점이 묶음 중간일 수 있다 — 보낸 범위 전체를 본다
    failed_wp = next((w for w in waypoints_list[:current_wp_index]
                      if w.get("unreached") and w.get("halt_if_unreached")), None)
    if failed_wp:
        stop_navigation_internal(f"'{failed_wp.get('name', '')}' 도착 실패", keep_schedule=True)
        return

    # 직전 웨이포인트 도착 후: 도착 상호작용 게이트(발화·확인) 또는 고정 대기
    # (첫 출발은 stop_action=None, wait=0 이라 자동 skip)
    # 운영자가 건너뛴 지점은 가지 않았다 — 그 지점의 도착 안내·확인·대기를 막힌 자리에서 하지 않는다
    if 0 < current_wp_index <= len(waypoints_list) and not waypoints_list[current_wp_index - 1].get("skipped"):
        prev_wp = waypoints_list[current_wp_index - 1]
        wait_s = int(prev_wp.get("wait_seconds", 0) or 0)
        sa = prev_wp.get("stop_action")
        wp_name = prev_wp.get("name", f"WP{current_wp_index}")
        confirm = (sa or {}).get("confirm") or "none"

        if sa and confirm != "none":
            # 발화 → 확인 대기(사람 우선) → 확인/타임아웃. 정지 감지 시 중단.
            from app.navigation.arrival_gate import run_gate
            log_event("schedule", "nav_arrival_gate",
                      f"{wp_name} 도착 — 확인 대기({confirm})",
                      robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
            result = run_gate(sa, wait_s, get_robot_id(), is_nav_active, wp_name)
            if result == "abort":
                print(f"⏸ 도착 게이트 중단 — 이동 정지 ({wp_name})")
                stop_navigation_internal("도착 확인 게이트 중단")
                return
        else:
            # 발화만(확인 없음) → 발화 후 기존 고정 대기
            if sa:
                # 사본이 아니라 현재 문구로 말한다 — resolve_announce 주석 참조
                from app.navigation.arrival_gate import resolve_announce, speak_only
                ann = resolve_announce(sa)
                if ann:
                    speak_only(get_robot_id(), ann)
            if wait_s > 0:
                print(f"⏸ 도착 후 대기: {wait_s}초 ({wp_name})")
                log_event("schedule", "nav_wait",
                          f"{wp_name} 도착 후 대기 {wait_s}초",
                          robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
                for _ in range(wait_s):
                    if not is_navigating:
                        print("⏸ 대기 중 정지 감지 — wait 중단")
                        return
                    time.sleep(1)

    # 자동문: 방금 닿은 점이 문 간선 노드다. 다음 점이 같은 문의 반대편 노드면 문 앞에서 통과 게이트를 돌리고,
    # 반대편 노드에 닿은 것이면(통과 끝) 남은 통과 표시를 거둔다.
    if 0 < current_wp_index <= len(waypoints_list) and waypoints_list[current_wp_index - 1].get("door_id") is not None:
        from app.navigation import door_gate
        door_entry = waypoints_list[current_wp_index - 1]
        door_next = waypoints_list[current_wp_index] if current_wp_index < len(waypoints_list) else None
        door_rid = get_robot_id()
        door_gate.finish_crossing(door_rid)
        door_id = door_gate.crossing_door(door_entry, door_next)
        if door_id is not None and door_gate.run_door_gate(door_id, door_entry, door_rid, is_nav_active) != "pass":
            if is_navigating:
                stop_navigation_internal("자동문 통과 중단")
            return

    # 모터 과열 보호: 현재 웨이포인트 도착 후 충전소 도킹 루트로 전환.
    # 작업(포인트 이동)은 여기 도달 시점에 이미 끝난 상태이므로 현재 포인트까지는 수행된다.
    # 도킹 포인트 도착 시 충전이 아니라 SIT 한다(sit_on_arrival).
    if overheat_return_pending:
        overheat_return_pending = False
        try:
            from app.robot_control.charge import build_overheat_return_route
            route = build_overheat_return_route()
        except Exception as e:
            route = None
            print(f"[ERR] 과열 보호 루트 생성 실패: {e}")
        if route:
            waypoints_list = route
            current_wp_index = 0
            charge_on_arrival = False
            sit_on_arrival = True
            nav_loop_infinite = False
            nav_loop_remaining = 0
            nav_loop_total = 0
            print("🌡️ 모터 과열 보호: 충전소 도킹 루트로 전환 (도착 후 SIT)")
            log_event("schedule", "motor_overheat_return",
                      "모터 과열 보호 — 충전소 도킹 루트로 전환(도착 후 SIT)",
                      robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
        else:
            # 현재 층에 도킹 루트 없음 → 제자리 SIT로 부하 제거
            is_navigating = False
            current_wp_index = 0
            try:
                from app.robot_io.sender import send_to_robot
                send_to_robot("SIT")
            except Exception as e:
                print(f"[ERR] SIT 전송 실패: {e}")
            print("🌡️ 모터 과열 보호: 도킹 루트 없음 — 제자리 SIT")
            _update_is_working(get_robot_id(), False)
            return

    if current_wp_index >= len(waypoints_list):
        if nav_loop_infinite:
            nav_loop_count += 1
            current_wp_index = 0
            _reset_node_legs()
            print(f"[SYNC] 무한 반복 — {nav_loop_count}회차 시작")
            log_event("schedule", "nav_loop",
                      f"무한 반복 — {nav_loop_count}회차 시작",
                      robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
        elif nav_loop_remaining > 0:
            nav_loop_remaining -= 1
            nav_loop_count += 1
            current_wp_index = 0
            _reset_node_legs()
            print(f"[SYNC] 반복 시작 (남은 횟수: {nav_loop_remaining + 1})")
            log_event("schedule", "nav_loop",
                      f"반복 시작 (남은 횟수: {nav_loop_remaining + 1})",
                      robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
        else:
            is_navigating = False
            nav_loop_total = 0
            nav_loop_infinite = False
            _update_is_working(get_robot_id(), False)

            try:
                from app.recording.manager import stop_all_recording
                stop_all_recording(get_robot_id())
            except Exception as e:
                print(f"[WARN] 녹화 정지 실패: {e}")

            # 도킹 포인트 도착 후 자동 충전
            if charge_on_arrival:
                charge_on_arrival = False

                # 도킹 위치 정확도 진단 (localization drift 의심 시 추적)
                dock_diag_msg = ""
                try:
                    import math
                    from app.robot_io import runtime
                    rid = get_robot_id()
                    if rid and waypoints_list:
                        dock_target = waypoints_list[-1]
                        pos = runtime.get_position(rid)
                        dx = pos["x"] - dock_target["x"]
                        dy = pos["y"] - dock_target["y"]
                        dist = (dx ** 2 + dy ** 2) ** 0.5
                        target_yaw = float(dock_target.get("yaw", 0.0) or 0.0)
                        dyaw = pos["yaw"] - target_yaw
                        while dyaw > math.pi:
                            dyaw -= 2 * math.pi
                        while dyaw <= -math.pi:
                            dyaw += 2 * math.pi
                        dock_diag_msg = (
                            f"위치 차이 {dist * 100:.1f}cm "
                            f"(dx={dx * 100:+.1f}cm, dy={dy * 100:+.1f}cm, "
                            f"dyaw={math.degrees(dyaw):+.1f}°)"
                        )
                        print(
                            f"🔋 [DOCK DIAG] {dock_diag_msg} | "
                            f"actual=({pos['x']:.3f},{pos['y']:.3f},yaw={pos['yaw']:.3f}) "
                            f"target=({dock_target['x']:.3f},{dock_target['y']:.3f},yaw={target_yaw:.3f})"
                        )
                except Exception as diag_err:
                    print(f"[WARN] 도킹 위치 진단 실패: {diag_err}")

                print("🔋 도킹 포인트 도착 완료 — 충전소 이동 명령 전송")
                arrival_msg = (
                    f"도킹 포인트 도착 완료, 충전 명령 전송 ({dock_diag_msg})"
                    if dock_diag_msg else "도킹 포인트 도착 완료, 충전 명령 전송"
                )
                log_event("schedule", "dock_arrival", arrival_msg,
                          robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
                try:
                    import threading
                    from app.robot_control.charge import start_charge_with_retry
                    # 별도 스레드: Charge=1 송신 후 충전(state 2/5) 확인, 실패 시 재시도.
                    # (nav 스레드 블로킹 방지)
                    threading.Thread(
                        target=start_charge_with_retry, daemon=True, name="charge_retry"
                    ).start()
                except Exception as e:
                    print(f"[ERR] 자동 충전 명령 실패: {e}")
            elif sit_on_arrival and (skip := _undock_sit_skip_reason(waypoints_list[-1] if waypoints_list else None)):
                sit_on_arrival = False
                reason, restore_gait = skip
                print(f"🪑 언도킹 재정렬 도착 — SIT 생략({reason})")
                log_event("robot", "undock_sit_skipped", f"언도킹 재정렬 도착 — SIT 생략: {reason}",
                          robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
                if restore_gait:
                    _restore_basic_gait("언도킹 재정렬 위치 어긋남")
            elif sit_on_arrival:
                sit_on_arrival = False
                print("🌡️ 도킹 포인트 도착 — SIT (모터 냉각, 충전 안 함)")
                log_event("schedule", "motor_overheat_sit",
                          "모터 과열 보호 — 도킹 포인트 도착, SIT(충전 안 함, 모터 냉각)",
                          robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
                try:
                    from app.robot_io.sender import send_to_robot
                    send_to_robot("SIT")
                except Exception as e:
                    print(f"[ERR] SIT 전송 실패: {e}")
            else:
                print("🎉 모든 웨이포인트 이동 완료!")
                log_event("schedule", "nav_complete", "모든 웨이포인트 이동 완료",
                          robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
                if _arrival_keeps_gait():
                    _gait_kept = True
                    print("[NAV] 안내 도착 — 내비 보행 유지(기본 보행 복귀 생략)")
                else:
                    _restore_basic_gait("작업 완료")

            return

    wp = waypoints_list[current_wp_index]
    idx = current_wp_index + 1

    # ── 다층 경로: 층 전환 자동 처리 (waypoint 시퀀스 기반 — robot_id/runtime 비의존) ──
    # 직전 주행 waypoint와 이 waypoint의 map_id가 다르면 = 층 경계 → 전송하지 않고 전환에 위임.
    # (assisted 이동 + EV로비 scan-match 수렴 = 도착 자동감지 → 워커가 resume_after_transition 호출)
    from app.navigation.floor_transition import start_floor_transition, is_transitioning
    rid_ft = _resolve_nav_robot_id()
    if rid_ft and is_transitioning(rid_ft):
        return  # 전환 진행 중 — 전송 보류(완료 후 워커가 재개)
    # 겹침 매핑: 방금 전환을 마친 경계 waypoint 는 로봇이 실제로 주행해 가야 하므로
    # 경계 판정을 1회 면제한다(면제하지 않으면 같은 map_id 경계를 재감지해 재전환).
    boundary_exempt = _boundary_done_index == current_wp_index
    _boundary_done_index = None

    wp_map_id = wp.get("map_id")
    if wp_map_id is not None and current_wp_index > 0 and rid_ft and not boundary_exempt:
        prev_map = waypoints_list[current_wp_index - 1].get("map_id")
        if prev_map is not None and prev_map != wp_map_id:
            if start_floor_transition(rid_ft, wp):
                # nav_thread 상태 리셋(nav_sent_time=0) — 전환 중 로봇은 계속 정지(status==0)이므로
                # 리셋하지 않으면 ever_moved=True 가 남아 매 폴링마다 도착이 재판정되고,
                # nav_arrival 로그·navigation_send_next 호출이 초당 1회씩 반복된다.
                # sent_time=0 이면 cooldown_ok=False 라 도착/재전송/hang 판정이 모두 비활성화된다.
                _signal_nav_reset(full=True)
                print(f"🛗 층 전환: WP {idx} '{wp.get('name','')}' (map {prev_map}→{wp_map_id}) — EV로비 도착 시 자동 재개")
                log_event("schedule", "floor_transition_start",
                          f"층 전환 대기: '{wp.get('name','')}'(map {prev_map}→{wp_map_id}) — EV로비 도착 자동감지",
                          robot_id=rid_ft, robot_name=get_robot_name(), business_id=get_robot_business_id())
                return  # 전환 시작 — 이번엔 전송 안 함

    if not _expand_node_leg():
        return
    wp = waypoints_list[current_wp_index]
    idx = current_wp_index + 1

    x = wp["x"]
    y = wp["y"]
    yaw = wp["yaw"]

    # 통과 지점을 걷는 중에 넘긴 전송인가 — 로봇이 서 있지 않으므로 준비 대기 없이 이어 보낸다
    from app.robot_io.config import NAV_CONTINUOUS_PASS
    continuing = NAV_CONTINUOUS_PASS and handoff_index is not None and handoff_index == current_wp_index
    # 직전 안내 도착·통과 지점 도착에서 내비 보행을 유지했으면 리셋 없이 보낸다(로봇은 내비 보행으로 서 있다)
    from_kept_gait = _take_kept_gait()
    # 점 수 한도로 나뉜 묶음의 끝에 서 있다 — 로봇은 제 작업을 마친 내비 보행이라 리셋 없이 잇는다
    # (안내가 아니어도. receiver 가 내비 보행인지 다시 보고, 6초 안에 안 떠나면 리셋 재전송).
    split_handoff = (current_wp_index > 0
                     and bool(waypoints_list[current_wp_index - 1].get("split_end")))
    from_kept_gait = from_kept_gait or split_handoff
    continuing = continuing or from_kept_gait

    # 다음 웨이포인트 전송 전 nav_thread 상태 리셋
    # → last_status=None으로 초기화되어 새 상태 전환을 감지할 수 있음
    _signal_nav_reset(full=True)

    label = " (내비 보행 유지 출발)" if from_kept_gait else (" (통과 이어가기)" if continuing else "")
    print(f"➡ NAV 이동 시작: {idx} / {len(waypoints_list)}{label}")
    if not continuing:
        time.sleep(1)  # 로봇 네비게이션 준비 대기

    # sleep 중 사용자가 정지했을 수 있으므로 송신 직전 재확인
    if not is_navigating:
        print(f"⏸ NAV 이동 취소됨 (WP {idx}) — sleep 중 정지 감지")
        return

    # 멈출 곳까지 이어지는 통과 지점·노드가 있으면 다지점 1건으로 보낸다(receiver 가 첫 점 앞에서만 리셋).
    # receiver 가 다지점을 모르면(구버전) 거부 응답이 오고, 그때는 아래 단일 NAV 로 보낸다.
    end = _bundle_end(current_wp_index)
    first_speed = _soft_start_speed(straight_bundle=end - current_wp_index > 1 and _straight_slow_on())
    if end - current_wp_index > 1:
        if _send_bundle(current_wp_index, end, continuing=continuing, first_speed=first_speed):
            print(f"➡ 다지점 {end - current_wp_index}점: {idx}~{end} / {len(waypoints_list)}"
                  + (" (이어가기)" if continuing else ""))
            from app.robot_io.config import NAV_MULTI_MAX_POINTS
            if (end < len(waypoints_list) and _passes(waypoints_list[end - 1])
                    and (end - current_wp_index >= NAV_MULTI_MAX_POINTS or waypoints_list[end - 1].get("split_turn"))):
                waypoints_list[end - 1]["split_end"] = True   # 한도로 끊긴 곳 — 다음 묶음은 이어가기
            if idx > 1:
                _announce_passed(idx - 1)
            _multi = {"start": current_wp_index, "end": end, "value": 1}
            current_wp_index = end
            nav_sent_time = _last_nav_dispatch = time.time()
            if from_kept_gait:
                _keep_gait_probe, _probe_kind = _new_keep_gait_probe(nav_sent_time), "keep"
            return
        # 다지점은 끄지 않는다 — 통신 순단 한 번(10-27 15:17 무응답)으로 끄면 남은 주행이 모두 단일 NAV 가 되어
        # 점마다 모드 리셋·기본 보행 전환이 생긴다. 이 구간만 단일로 보내고 다음 묶음은 다시 다지점으로 시도한다.
        print(f"[NAV] 다지점 전송 실패 — WP{idx} 이 구간만 단일 NAV 로 보냄(다음 묶음은 다시 다지점)")
        log_event("error", "nav_multi_unsupported",
                  "다지점 묶음을 보내지 못해 이 구간은 경유지마다 따로 보냅니다(통신·receiver 확인)",
                  robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())

    from app.robot_io.sender import send_nav_to_robot
    wp["sent_speed"] = first_speed or wp.get("speed")
    send_nav_to_robot(
        idx, x, y, yaw,
        speed=first_speed or wp.get("speed"), map_id=wp.get("map_id"),
        continuing=continuing,
    )

    if idx > 1:
        _announce_passed(idx - 1)
    current_wp_index += 1
    nav_sent_time = _last_nav_dispatch = time.time()
    if from_kept_gait:
        _keep_gait_probe, _probe_kind = _new_keep_gait_probe(nav_sent_time), "keep"

@move.post("/startpath")
def start_path_navigation(way_name: str, loop: int = 1, auto_charge: bool = True, current_user: UserInfo = Depends(require_robot_available("robot-list"))):
    """DB 경로(WayInfo)를 읽어 네비게이션 시작.

    auto_charge: 작업 완료 후 충전소 자동 복귀 여부 (기본 True).
                 원격 화면 체크박스로 끌 수 있음(반복 테스트 등).
    """
    from app.robot_control.charge import release_charge_for_work

    global current_wp_index, waypoints_list, is_navigating, nav_loop_remaining, nav_loop_total, nav_loop_count, nav_loop_infinite, auto_return_to_charge

    # 안전 가드: 위치 미초기화 동안 자율주행 보류 (엉뚱한 원점 기준 주행 방지)
    if _initpose_blocked():
        return {"status": "error", "msg": _initpose_block_msg()}

    db = SessionLocal()
    try:
        path = db.query(WayInfo).filter(WayInfo.WayName == way_name).first()
        if not path:
            return {"status": "error", "msg": f"경로 '{way_name}'을(를) 찾을 수 없습니다."}

        place_names = [n.strip() for n in path.WayPoints.split(" - ")]
        from app.navigation.waypoints import parse_wait_seconds, parse_stop_actions, build_waypoints_from_places
        wait_list = parse_wait_seconds(path.WaitSeconds, len(place_names))
        stop_actions = parse_stop_actions(path.StopActions, len(place_names))
        # 장소명 → LocationInfo 조회. 다층 경로 전환 감지를 위해 build_waypoints_from_places 로
        # waypoint(map_id/floor_id 포함)를 생성한다(pathmove 와 동일 경로). 미해석 장소는 에러.
        places = []
        for name in place_names:
            place = find_place_by_name(db, name, get_robot_business_id())
            if not place:
                return {"status": "error", "msg": f"경로 '{way_name}'의 장소 '{name}'을(를) 찾을 수 없습니다."}
            places.append(place)
        waypoints = build_waypoints_from_places(places, wait_list, stop_actions)

        if not waypoints:
            return {"status": "error", "msg": f"경로 '{way_name}'에 유효한 장소가 없습니다."}
        refused = node_entry_check(db, places)
        if refused:
            return {"status": "error", "msg": refused}
    finally:
        db.close()

    # 충전 중이면 먼저 해제(도크 이탈)하고 첫 지점으로 바로 출발한다
    release_charge_for_work()

    waypoints_list = waypoints
    current_wp_index = 0
    is_navigating = True
    clear_arrival_actions()
    # loop <= 0 이면 무한 반복 (작업 중지 전까지 계속)
    nav_loop_infinite = loop <= 0
    nav_loop_remaining = 0 if nav_loop_infinite else max(0, loop - 1)
    nav_loop_total = 0 if nav_loop_infinite else loop
    nav_loop_count = 1
    loop_label = "무한 반복" if nav_loop_infinite else f"{loop}회"
    auto_return_to_charge = auto_charge  # 원격 제어 실행 완료 후 충전소 자동 복귀 (체크박스로 토글)
    _signal_nav_reset(full=True)
    _update_is_working(get_robot_id(), True)

    route_detail = " → ".join(wp["name"] for wp in waypoints_list)
    print(f"🚗 NAV START (경로: {way_name}) — {len(waypoints_list)}개 웨이포인트, 반복: {loop_label}")
    log_event("schedule", "nav_start",
              f"경로 주행 시작: {way_name} ({len(waypoints_list)}개 웨이포인트, {loop_label})",
              detail=f"경로: {route_detail}",
              robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())

    try:
        from app.recording.manager import start_auto_recording
        rid = get_robot_id()
        if not rid:
            db2 = SessionLocal()
            try:
                robot = db2.query(RobotInfo).order_by(RobotInfo.id.asc()).first()
                rid = robot.id if robot else None
            finally:
                db2.close()
        if rid:
            start_auto_recording(rid)
    except Exception as e:
        print(f"[WARN] 자동 녹화 시작 실패: {e}")

    navigation_send_next()
    return {"status": "ok", "msg": f"경로 '{way_name}' 주행 시작 ({loop_label})", "way_name": way_name}


@move.post("/placemove/{place_id}")
def move_to_place(place_id: int, db: Session = Depends(get_db), current_user: UserInfo = Depends(require_robot_available("robot-list"))):
    return start_place_move(place_id, db)


def start_place_move(place_id: int, db: Session, speed: str | None = None,
                     keep_gait: bool = False, arrive_name: str | None = None,
                     progress_text: str | None = None) -> dict:
    """단일 장소 이동. speed 는 목적지 웨이포인트에만 싣는다("slow" = 안내 저속).

    keep_gait: 안내 시나리오 이동 — 도착 후 내비 보행을 유지할 수 있다(NAV_KEEP_GAIT_ON_ARRIVAL).
    arrive_name: 도착 접근 안내에 넣을 부르는 이름("곧 원무과에 도착합니다") — 안내 단계 이동만.
    progress_text: 이동 중 진행 멘트("편의점 방향으로 이동하고 있습니다.") — 안내 단계 이동만.
    """
    from app.robot_control.charge import release_charge_for_work

    # 안전 가드: 위치 미초기화 동안 자율주행 보류
    if _initpose_blocked():
        return {"status": "error", "msg": _initpose_block_msg()}

    place = db.query(LocationInfo).filter(LocationInfo.id == place_id).first()

    if not place:
        return {"status": "error", "msg": "장소를 찾을 수 없습니다."}

    x = place.LocationX
    y = place.LocationY
    yaw = place.Yaw or 0.0

    refused = node_entry_check(db, [place])
    if refused:
        return {"status": "error", "msg": refused}

    # 충전 중이면 먼저 해제(도크 이탈)한다 — 출발점은 해제 뒤 현재 위치(도킹 포인트)
    release_charge_for_work()

    # 직선이 벽을 가로지르면 같은 맵의 경유지 장소로 자동 우회한다. 출발점은 현재 위치
    # — 위치를 믿을 수 없으면 None(판단 없이 바로 이동). 노드망 경로가 켜져 있으면 navigation_send_next 가 펼친다.
    from app.navigation.via_route import plan_via_waypoints
    from app.robot_io.config import NODE_ROUTING_ENABLED
    pos = runtime.get_position(get_robot_id()) if get_robot_id() else {}
    start = ((pos["x"], pos["y"])
             if pos.get("timestamp") and not pos.get("initpose_pending") else None)
    via_wps, via_reason = ([], "node") if NODE_ROUTING_ENABLED else plan_via_waypoints(db, place, start)
    if via_wps is None:
        return {
            "status": "error",
            "msg": f"'{place.LacationName}'까지 맵상 안전 우회 경로를 찾지 못했습니다({via_reason}).",
        }

    work_wps = via_wps + [{
        "x": x, "y": y, "yaw": yaw, "name": place.LacationName,
        "map_id": place.MapId, "floor_id": place.FloorId,
    }]
    if speed:
        # 안내 저속은 경유 구간에도 — 사람이 따라오는 동안 경유지에서 갑자기 빨라지면 안 된다
        for wp in work_wps:
            wp["speed"] = speed
    if keep_gait:
        work_wps[-1]["keep_gait"] = True
    if arrive_name:
        work_wps[-1]["arrive_name"] = arrive_name

    # 단일 장소 이동도 네비게이션 흐름으로 관리 (도착 감지 + IsWorking 연동)
    begin_route(work_wps, start, progress_text=progress_text)

    print(f"🚗 장소 이동: {place.LacationName} → x={x}, y={y}, yaw={yaw}, speed={speed or '기본'}")
    log_event("schedule", "place_move_start", f"장소 이동: {place.LacationName}",
              robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())

    navigation_send_next()
    return {"status": "ok", "msg": f"{place.LacationName}(으)로 이동 명령 전송 완료"}


def begin_route(work_wps: list, cue_start=None, progress_text: str | None = None) -> None:
    """미리 짠 웨이포인트로 새 주행 상태를 세운다. 첫 전송(navigation_send_next)은 부르는 쪽이 한다.

    장소 이동과 안내 사전 주행이 같이 쓴다. 충전소 자동 복귀는 하지 않는다.
    cue_start: 경유지 방향 안내를 계산할 출발 위치. None 이면 안내하지 않는다(멘트 없는 주행).
    progress_text: 이동 중 진행 멘트. None 이면 내지 않는다.
    """
    global current_wp_index, waypoints_list, is_navigating, auto_return_to_charge
    waypoints_list = work_wps
    _set_route_cues(cue_start, work_wps, 0)
    _set_progress(progress_text, cue_start)
    current_wp_index = 0
    is_navigating = True
    clear_arrival_actions()
    auto_return_to_charge = False
    _signal_nav_reset(full=True)
    _update_is_working(get_robot_id(), True)


@move.post("/pathmove/{path_id}")
def move_along_path(path_id: int, db: Session = Depends(get_db), current_user: UserInfo = Depends(require_robot_available("robot-list"))):
    from app.robot_control.charge import release_charge_for_work

    global current_wp_index, waypoints_list, is_navigating, auto_return_to_charge

    # 안전 가드: 위치 미초기화 동안 자율주행 보류
    if _initpose_blocked():
        return {"status": "error", "msg": _initpose_block_msg()}

    path = db.query(WayInfo).filter(WayInfo.id == path_id).first()
    if not path:
        return {"status": "error", "msg": "경로를 찾을 수 없습니다."}

    place_names = [name.strip() for name in path.WayPoints.split(" - ")]
    if len(place_names) < 2:
        return {"status": "error", "msg": "경로에 장소가 2개 이상 필요합니다."}

    # 장소명으로 좌표 조회
    places = []
    for name in place_names:
        place = find_place_by_name(db, name, get_robot_business_id())
        if not place:
            return {"status": "error", "msg": f"장소 '{name}'을(를) 찾을 수 없습니다."}
        places.append(place)

    # 웨이포인트 목록 생성 (yaw = 다음 포인트 방향, 마지막은 저장된 yaw)
    from app.navigation.waypoints import build_waypoints_from_places, parse_wait_seconds, parse_stop_actions
    wait_list = parse_wait_seconds(path.WaitSeconds, len(places))
    stop_actions = parse_stop_actions(path.StopActions, len(places))
    waypoints = build_waypoints_from_places(places, wait_list, stop_actions)

    refused = node_entry_check(db, places)
    if refused:
        return {"status": "error", "msg": refused}

    # 같은 맵의 각 구간을 점유맵으로 검사하고, 벽을 가로지르면 안전 waypoint를 삽입한다.
    # 노드망 경로가 켜져 있으면 navigation_send_next 가 구간마다 노드망으로 펼친다.
    from app.navigation.via_route import plan_via_waypoints
    from app.robot_io.config import NODE_ROUTING_ENABLED
    if NODE_ROUTING_ENABLED:
        plan_via_waypoints = lambda _db, _place, _start: ([], "node")  # noqa: E731
    safe_waypoints = []
    rid = get_robot_id()
    current_map_id = runtime.get_current_map_id(rid) if rid else None
    current_pos = runtime.get_position(rid) if rid else {}
    start = None
    if (current_pos.get("timestamp") and not current_pos.get("initpose_pending")
            and current_map_id is not None):
        start = (current_pos.get("x"), current_pos.get("y"))
    if start is None:
        return {
            "status": "error",
            "msg": "현재 위치와 활성 맵을 확인할 수 없어 안전 경로를 계산하지 못했습니다.",
        }

    for place, waypoint in zip(places, waypoints):
        if start is not None and current_map_id == place.MapId:
            via, reason = plan_via_waypoints(db, place, start)
            if via is None:
                return {
                    "status": "error",
                    "msg": f"'{place.LacationName}'까지 맵상 안전 우회 경로를 찾지 못했습니다({reason}).",
                }
            safe_waypoints.extend(via)
        safe_waypoints.append(waypoint)
        start = (place.LocationX, place.LocationY)
        current_map_id = place.MapId
    waypoints = safe_waypoints

    # 충전 중이면 먼저 해제(도크 이탈)하고 첫 지점으로 바로 출발한다
    release_charge_for_work()

    # 기존 웨이포인트 순차 이동 시스템 활용
    waypoints_list = waypoints
    current_wp_index = 0
    is_navigating = True
    clear_arrival_actions()
    auto_return_to_charge = False  # 경로 이동 버튼은 충전소 복귀 안 함
    _signal_nav_reset(full=True)
    _update_is_working(get_robot_id(), True)

    print(f"🛤 경로 이동 시작: {path.WayName} — 총 {len(waypoints)}개 포인트")
    for i, wp in enumerate(waypoints):
        print(f"  [{i+1}] {wp.get('name', f'WP{i + 1}')} → x={wp['x']}, y={wp['y']}, yaw={wp['yaw']}")
    route_names = " → ".join(place_names)
    log_event("schedule", "path_move_start",
              f"경로 이동 시작: {path.WayName} ({len(waypoints)}개 포인트)",
              detail=f"경로: {route_names}",
              robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())

    try:
        from app.recording.manager import start_auto_recording
        rid = get_robot_id()
        if not rid:
            _db = SessionLocal()
            try:
                _robot = _db.query(RobotInfo).order_by(RobotInfo.id.asc()).first()
                rid = _robot.id if _robot else None
            finally:
                _db.close()
        if rid:
            start_auto_recording(rid)
    except Exception as e:
        print(f"[WARN] 자동 녹화 시작 실패: {e}")

    navigation_send_next()
    return {"status": "ok", "msg": f"경로 '{path.WayName}' 이동 시작 ({len(waypoints)}개 포인트)"}
