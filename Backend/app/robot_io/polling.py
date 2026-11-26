"""위치·상태·네비게이션 폴링 스레드.

receiver.py(로봇 NOS에서 동작)에 JSON action을 보내 응답을 받는 방식이다.
ASDU 프로토콜로 로봇에 직접 요청하지 않는다.
"""

import json
import math
import os
import socket
import threading
import time

import app.robot_io.runtime as runtime
from app.user_cache import get_robot_id, get_robot_name, get_robot_business_id
from app.logs.service import log_event
from app.robot_io.config import (
    ROBOT_IP,
    RECEIVER_IP,
    RECEIVER_PORT,
    REQ_INTERVAL_POS,
    REQ_INTERVAL_HB,
    AUTO_INIT_POSE_ORIGIN_GUARD_M,
    NAV_CONTINUOUS_PASS,
    PASS_THROUGH_MIN_RADIUS,
    NAV_KEEP_GAIT_START_TIMEOUT_SEC,
    NAV_KEEP_GAIT_MOVED_M,
    NAV_KEEP_GAIT_TURNED_RAD,
)


# ─────────────────────────────────────────────────────────
# 관측성 · 수명주기 (C-8 / M-9)
# ─────────────────────────────────────────────────────────
# NAV 디버그 로깅 토글(C-8): 주행 중 매 폴링(1s)마다 출력되던 [NAV DEBUG] 로그를
# 환경변수 NAV_DEBUG=1 일 때만 남긴다. 평상시 IO/로그 용량을 줄인다.
NAV_DEBUG = os.environ.get("NAV_DEBUG", "0").strip().lower() in ("1", "true", "yes", "on")


def _navdbg(msg: str) -> None:
    if NAV_DEBUG:
        print(msg)


# 폴링 스레드 종료 신호(M-9): 서버 shutdown 시 set 되어 각 루프가 빠져나간다.
# (데몬 스레드라 프로세스 종료 시 강제 회수되긴 하나, --reload/재구성 시
#  좀비 루프가 남지 않도록 명시적 종료 경로를 둔다.)
_shutdown = threading.Event()
POSITION_JUMP_GUARD_M = 3.0


def stop_polling_threads() -> None:
    """폴링 데몬 스레드 3개에 종료를 요청한다(main shutdown 훅에서 호출)."""
    _shutdown.set()


def _is_navigating() -> bool:
    try:
        import app.navigation.send_move as nav_mod
        return bool(nav_mod.is_navigating)
    except Exception:
        return False


def _is_mapping() -> bool:
    """매핑(맵 작성) 진행 중인지. 매핑 중에는 SLAM 이 새 맵을 원점부터 작성하느라 위치가
    (0,0) 부근으로 리셋되며 큰 점프가 발생한다 — 이는 localization 오인이 아니므로
    위치 급변 가드(→'위치 재조정' 모달)에서 제외한다."""
    try:
        from app.map.mapping_control import mapping_state
        return bool(mapping_state.get("is_running"))
    except Exception:
        return False


POSITION_JUMP_REASON = "위치 급변 감지"
# 급변이 한 번만 보이고 다음 표본에서 제자리면 늦게 도착한 옛 좌표 등으로 보고 무시한다(11-06 10:27 실측:
# 통신 1초 두절 때의 UDP 답이 17초 뒤 읽혀 5.54m 급변 오경보). 두 번 연속일 때만 미확정으로 돌린다.
_jump_suspect: dict[int, dict] = {}   # 확인 대기 중인 급변 — {"base": (x, y)}
# '위치 급변'으로 켠 미확정의 자동 해제: 튀기 전 자리 0.5m 안으로 돌아와 10초 머물면 푼다.
JUMP_RELEASE_RADIUS_M = 0.5
JUMP_RELEASE_SEC = 10.0
_jump_hold: dict[int, dict] = {}      # {"base": (x, y), "near_since": monotonic | None}


def jump_suspect_active(robot_id: int) -> bool:
    """급변 표본 하나를 다음 표본으로 확인하는 중인지(이동 시작이 잠깐 기다린다)."""
    return robot_id in _jump_suspect


def wait_jump_suspect(robot_id: int, timeout: float = 4.5) -> None:
    """급변 확인 대기가 끝날 때까지(최대 timeout) 기다린다 — 그사이 틀린 위치로 출발하지 않게.

    서 있을 때 위치 조회는 2초 간격 + TCP 응답 대기(최대 2초)라 4.5초면 다음 표본이 온다.
    """
    end = time.monotonic() + timeout
    while robot_id in _jump_suspect and time.monotonic() < end:
        time.sleep(0.1)


def _guard_untrusted_position_jump(robot_id: int, pos: dict) -> bool:
    """비주행 중 갑작스러운 큰 위치 점프는 localization 오인으로 보고 DB 저장을 보류한다.

    반환: True 면 이 표본은 확인 대기라 반영하지 않는다(직전 위치 유지).
    """
    if runtime.is_initpose_pending(robot_id) or _is_navigating() or _is_mapping():
        _jump_suspect.pop(robot_id, None)
        return False
    # 수동 확정 직후 유예 구간엔 급변 가드를 쉰다(재조정 수렴 중 좌표 튐을 미확정 재설정으로 오인 방지).
    if runtime.in_manual_confirm_grace(robot_id):
        _jump_suspect.pop(robot_id, None)
        return False
    suspect = _jump_suspect.get(robot_id)
    if suspect:
        # 비교 기준은 튀기 전 자리로 고정한다(튄 좌표끼리 비교하면 계속 튀는 폭주를 놓친다).
        prev = {"x": suspect["base"][0], "y": suspect["base"][1]}
    else:
        prev = runtime.get_position(robot_id) or {}
        prev_ts = prev.get("timestamp", 0) or 0
        if not prev_ts:
            # 재시작 직후 등 라이브 이전 좌표가 없을 때는 마지막 신뢰 위치(DB 복원 포함)를
            # 기준으로 비교한다. 이게 없으면(완전 신규) 비교 불가 → 통과.
            prev = runtime.get_trusted_position(robot_id) or {}
            prev_ts = prev.get("timestamp", 0) or 0
            if not prev_ts:
                return False
    dx = float(pos.get("x", 0.0)) - float(prev.get("x", 0.0))
    dy = float(pos.get("y", 0.0)) - float(prev.get("y", 0.0))
    dist = math.hypot(dx, dy)
    if dist < POSITION_JUMP_GUARD_M:
        if suspect:
            _jump_suspect.pop(robot_id, None)
            print(f"[AUTO-INITPOSE] robot {robot_id} 위치 급변 1회 뒤 제자리 — 일시 튐으로 보고 무시 "
                  f"(튄 값=({suspect['seen'][0]:.2f}, {suspect['seen'][1]:.2f}))")
        return False

    # 목적지가 원점(0,0) 부근이면 운영 중 localization 드리프트가 아니라 SLAM 리셋
    # (전원 재부팅/매핑류)일 가능성이 크다. 재부팅 경로(auto_init_pose)가 별도 처리하므로
    # 여기서 '위치 재조정' 모달을 띄우지 않는다(리셋 상황 오판정 모달 방지 — 사용자 정책).
    if math.hypot(float(pos.get("x", 0.0)), float(pos.get("y", 0.0))) < AUTO_INIT_POSE_ORIGIN_GUARD_M:
        _jump_suspect.pop(robot_id, None)
        print(f"[AUTO-INITPOSE] robot {robot_id} 위치 급변이나 원점 부근 — 리셋 상황으로 보고 모달 생략")
        return False

    seen = (float(pos.get("x", 0.0)), float(pos.get("y", 0.0)))
    if not suspect:
        _jump_suspect[robot_id] = {"base": (float(prev.get("x", 0.0)), float(prev.get("y", 0.0))),
                                   "seen": seen}
        print(f"[AUTO-INITPOSE] robot {robot_id} 위치 급변 1회({dist:.2f}m) — 다음 표본으로 확인")
        return True
    _jump_suspect.pop(robot_id, None)

    detail = (
        f"비주행 중 위치가 {dist:.2f}m 급변하여 자동 위치 확정을 보류합니다. "
        f"이전=({prev.get('x', 0.0):.2f}, {prev.get('y', 0.0):.2f}), "
        f"보고=({pos.get('x', 0.0):.2f}, {pos.get('y', 0.0):.2f})"
    )
    runtime.set_initpose_pending(robot_id, True, POSITION_JUMP_REASON)
    _jump_hold[robot_id] = {"base": suspect["base"], "near_since": None}
    print(f"[AUTO-INITPOSE] robot {robot_id} 위치 급변 감지 — {detail}")
    try:
        log_event(
            "error", "robot_initpose_manual_needed", "로봇 위치 확인 필요",
            error_json=detail,
            robot_id=get_robot_id(), robot_name=get_robot_name(),
            business_id=get_robot_business_id(),
        )
    except Exception as e:
        print(f"[AUTO-INITPOSE] 위치 급변 알림 로그 실패: {e}")
    return False


def _maybe_release_jump_hold(robot_id: int, pos: dict) -> None:
    """'위치 급변'으로 켠 미확정을, 좌표가 튀기 전 자리로 돌아와 머물면 스스로 푼다.

    측위 상실 보고(Location≠0)가 있으면 풀지 않는다. 669 는 틀어져도 0 을 보내므로(10-30) 실제로
    거르는 조건은 '튀기 전 자리 0.5m 안에 10초'다. 다른 사유로 바뀌었거나 풀렸으면 기록만 지운다.
    """
    hold = _jump_hold.get(robot_id)
    if not hold:
        return
    if (not runtime.is_initpose_pending(robot_id)
            or runtime.get_initpose_reason(robot_id) != POSITION_JUMP_REASON):
        _jump_hold.pop(robot_id, None)
        return
    loc = pos.get("location")
    d = math.hypot(float(pos.get("x", 0.0)) - hold["base"][0], float(pos.get("y", 0.0)) - hold["base"][1])
    if d > JUMP_RELEASE_RADIUS_M or (loc is not None and loc != 0):
        hold["near_since"] = None
        return
    now_mono = time.monotonic()
    if hold["near_since"] is None:
        hold["near_since"] = now_mono
        return
    if now_mono - hold["near_since"] < JUMP_RELEASE_SEC:
        return
    _jump_hold.pop(robot_id, None)
    runtime.set_initpose_pending(robot_id, False, by_localization=True)
    where = f"({pos.get('x', 0.0):.2f}, {pos.get('y', 0.0):.2f})"
    print(f"[AUTO-INITPOSE] robot {robot_id} 위치 급변 뒤 원래 자리로 돌아옴 @ {where} — 미확정 해제")
    try:
        log_event(
            "robot", "robot_initpose_recovered", "로봇 위치 초기화 복구",
            error_json=f"위치 급변 뒤 {JUMP_RELEASE_SEC:.0f}초 동안 원래 자리에 머물러 자동 해제했습니다. 위치={where}",
            robot_id=get_robot_id(), robot_name=get_robot_name(),
            business_id=get_robot_business_id(),
        )
    except Exception as e:
        print(f"[AUTO-INITPOSE] 급변 해제 로그 실패: {e}")


# receiver 위치 답의 timestamp(receiver 시계) — 이보다 오래된 답은 늦게 도착한 옛 답이다.
_last_recv_ts: dict[int, float] = {}
_old_reply_streak: dict[int, int] = {}
# 연속으로 이만큼 '옛 답'이면 receiver 시계가 뒤로 맞춰진 것으로 보고 기준을 새로 잡는다.
OLD_REPLY_RESET_COUNT = 3


def _is_old_reply(robot_id: int, pos: dict) -> bool:
    ts = pos.get("timestamp")
    if not isinstance(ts, (int, float)) or ts <= 0:
        return False
    last = _last_recv_ts.get(robot_id)
    if last is not None and ts < last - 1e-3:
        n = _old_reply_streak.get(robot_id, 0) + 1
        _old_reply_streak[robot_id] = n
        if n < OLD_REPLY_RESET_COUNT:
            print(f"[POS] robot {robot_id} 늦게 도착한 옛 위치 답 버림 ({last - ts:.1f}s 전 표본)")
            return True
        print(f"[POS] robot {robot_id} 옛 답 {n}회 연속 — receiver 시계 변경으로 보고 기준 재설정")
    _old_reply_streak[robot_id] = 0
    _last_recv_ts[robot_id] = float(ts)
    return False


# 로봇 측위 상태를 미확정 플래그에 반영하기 전 연속 확인 횟수(위치 폴링 2초 주기 → 약 4초).
# NOS 는 정상↔경고를 짧게 오가므로 한 번의 보고로는 뒤집지 않는다.
LOCALIZATION_CONFIRM_POLLS = 2
LOCALIZATION_LOST_CONFIRM_SEC = 3.0
_loc_streak: dict[int, tuple[bool, int, float]] = {}


def _localization_sync_suppressed(robot_id: int) -> bool:
    """매핑·층 전환 중에는 측위가 의도적으로 재시작되므로 상실 보고를 미확정에 반영하지 않는다."""
    if _is_mapping():
        return True
    try:
        from app.navigation.floor_transition import is_init_suppressed
        return bool(is_init_suppressed(robot_id))
    except Exception:
        return False


def _sync_localization_pending(robot_id: int, location, pos: dict) -> None:
    """로봇이 보고한 측위 상태(Location)를 관제 미확정 플래그 하나로 합친다.

    미확정 하나만 보면 이동 가드·'위치 확인 필요' 알림·지도 마커가 같은 기준으로 움직인다.
    - 상실이 연속 확인되면 미확정으로 돌리고 알림을 낸다(운영자 확정 직후 유예 중이면 기다린다).
    - 정상이 연속 확인되면 '측위 상실' 사유로 켠 미확정만 해제한다.
    """
    if location is None or _localization_sync_suppressed(robot_id):
        return
    lost = location != 0
    now_mono = time.monotonic()
    prev_lost, count, first_mono = _loc_streak.get(robot_id, (lost, 0, now_mono))
    if prev_lost == lost:
        count += 1
    else:
        count = 1
        first_mono = now_mono
    _loc_streak[robot_id] = (lost, count, first_mono)
    if count < LOCALIZATION_CONFIRM_POLLS:
        return
    if lost and now_mono - first_mono < LOCALIZATION_LOST_CONFIRM_SEC:
        return

    pending = runtime.is_initpose_pending(robot_id)
    where = f"({pos.get('x', 0.0):.2f}, {pos.get('y', 0.0):.2f})"
    if lost:
        if pending or runtime.in_manual_confirm_grace(robot_id):
            return
        runtime.set_initpose_pending(robot_id, True, runtime.LOCALIZATION_LOST_REASON)
        print(f"[AUTO-INITPOSE] robot {robot_id} 측위 상실 보고(Location={location}) @ {where} — 미확정 전환")
        try:
            log_event(
                "error", "robot_initpose_manual_needed", "로봇 위치 확인 필요",
                error_json=f"로봇이 측위 상실을 보고했습니다(Location={location}). 보고 위치={where}",
                robot_id=get_robot_id(), robot_name=get_robot_name(),
                business_id=get_robot_business_id(),
            )
        except Exception as e:
            print(f"[AUTO-INITPOSE] 측위 상실 알림 로그 실패: {e}")
    elif pending and runtime.get_initpose_reason(robot_id) == runtime.LOCALIZATION_LOST_REASON:
        runtime.set_initpose_pending(robot_id, False, by_localization=True)
        print(f"[AUTO-INITPOSE] robot {robot_id} 측위 정상 복귀 @ {where} — 미확정 해제")
        try:
            log_event(
                "robot", "robot_initpose_recovered", "로봇 위치 초기화 복구",
                error_json=f"로봇 측위가 정상으로 돌아왔습니다. 위치={where}",
                robot_id=get_robot_id(), robot_name=get_robot_name(),
                business_id=get_robot_business_id(),
            )
        except Exception as e:
            print(f"[AUTO-INITPOSE] 측위 복귀 로그 실패: {e}")


# ─────────────────────────────────────────────────────────
# 위치 스레드
# ─────────────────────────────────────────────────────────
POS_INTERVAL_NAVIGATING = 0.5  # 주행 중 위치 폴링 주기(대기 중은 REQ_INTERVAL_POS)


# receiver 가 로봇 응답을 못 받는 동안에도 마지막 좌표를 돌려준다. 그 경과(age)가 이보다 길면
# 측위 상태 판정에 쓰지 않는다 — 같은 옛 샘플을 '연속 확인'으로 세면 끊긴 채 정상/상실이 굳는다.
POSITION_STALE_SEC = 3.0


def _apply_position_sample(rid: int, pos: dict) -> None:
    """receiver POSITION 응답 하나를 runtime 에 반영한다(TCP·UDP 경로 공통)."""
    age = pos.get("age")
    age = float(age) if isinstance(age, (int, float)) else None
    if _is_old_reply(rid, pos):
        return
    stale = age is not None and age > POSITION_STALE_SEC
    # 오래된 표본(receiver 가 로봇 응답을 못 받는 중)은 급변 판정에 쓰지 않는다.
    if not stale and _guard_untrusted_position_jump(rid, pos):
        return   # 급변 확인 대기 — 다음 표본까지 직전 위치 유지
    runtime.update_position(rid, pos["x"], pos["y"], pos["yaw"],
                            location=pos.get("location"), age=age)
    if stale:
        return
    _maybe_release_jump_hold(rid, pos)
    _sync_localization_pending(rid, pos.get("location"), pos)


def _position_poll_interval() -> float:
    """주행 중·위치 재조정 창이 열린 동안은 빠르게 — 스캔 오버레이가 이 좌표로 그려진다."""
    from app.navigation.send_move import is_nav_active
    from app.robot_control.relocalize import is_relocalize_viewing
    if is_nav_active() or is_relocalize_viewing():
        return POS_INTERVAL_NAVIGATING
    return REQ_INTERVAL_POS


def _drain_udp(sock) -> int:
    """소켓 수신 버퍼에 남은 답을 버린다. 버린 개수를 돌려준다."""
    n = 0
    try:
        sock.setblocking(False)
        while n < 64:
            sock.recvfrom(4096)
            n += 1
    except (BlockingIOError, InterruptedError, OSError, TimeoutError):
        pass
    finally:
        try:
            sock.settimeout(2.0)
        except OSError:
            pass
    if n:
        print(f"[POS] UDP 버퍼에 남은 옛 위치 답 {n}개 버림")
    return n


def position_thread():
    print(f"[LISTEN] 위치 Listener 시작 (via receiver.py {RECEIVER_IP}:{RECEIVER_PORT})")

    # 소켓 재사용(M-6): 매 폴링마다 UDP 소켓을 생성/파기하던 비효율을 제거한다.
    # 타임아웃은 정상 상황이므로 소켓을 유지하고, 그 외 예외에서만 재생성한다.
    sock = None
    while not _shutdown.is_set():
        try:
            if sock is None:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(2.0)

            # 위치는 TCP 신뢰 채널을 우선 사용한다. 기존 UDP는 TCP 장애 시
            # 보조 경로로 남겨 구버전 receiver와 일시적인 TCP 장애를 흡수한다.
            from app.robot_io.sender import request_receiver_tcp
            pos = request_receiver_tcp(
                {"action": "POSITION"},
                connect_timeout=1.0,
                recv_timeout=2.0,
            )
            if not isinstance(pos, dict) or not pos.get("timestamp"):
                raise ConnectionError("receiver TCP POSITION response is unsupported or stale")

            if pos.get("timestamp", 0) > 0:
                rid = runtime.get_robot_id_by_ip(ROBOT_IP)
                if rid is not None:
                    _apply_position_sample(rid, pos)
                    # 미확정(전원 on 직후) 동안 보고 위치 추이를 JSON 캡처(조사용)
                    try:
                        if runtime.is_initpose_pending(rid):
                            from app.robot_control.poweron_capture import capture_poll_if_pending
                            capture_poll_if_pending(rid, pos)
                    except Exception:
                        pass

        except (socket.timeout, TimeoutError, ConnectionError, OSError, ValueError):
            # TCP 신뢰 채널 장애 시 기존 UDP 요청으로 한 번 보조 조회한다.
            try:
                msg = json.dumps({"action": "POSITION"}).encode("utf-8")
                # 앞서 시간 초과로 포기한 답이 늦게 와 쌓여 있으면 이번 답으로 잘못 읽는다(11-06 10:27) — 먼저 비운다.
                _drain_udp(sock)
                sock.sendto(msg, (RECEIVER_IP, RECEIVER_PORT))
                data, _ = sock.recvfrom(4096)
                pos = json.loads(data.decode("utf-8"))
                if pos.get("timestamp", 0) > 0:
                    rid = runtime.get_robot_id_by_ip(ROBOT_IP)
                    if rid is not None:
                        _apply_position_sample(rid, pos)
            except socket.timeout:
                pass
            except Exception as fallback_error:
                print("[ERR POS TCP/UDP FALLBACK]", fallback_error)
        except Exception as e:
            print("[ERR POS]", e)
            log_event("error", "position_recv_error", "로봇 위치 수신 실패",
                      error_json=str(e),
                      robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
            # 소켓이 손상됐을 수 있으니 닫고 다음 루프에서 재생성한다.
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
                sock = None

        # 주행 중에는 0.5s — 통과 지점 반경 진입을 늦게 알면 로봇이 이미 감속해 멈춘다
        time.sleep(_position_poll_interval())

    if sock is not None:
        try:
            sock.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────
# 상태 스레드
# ─────────────────────────────────────────────────────────
_was_online: dict[int, bool] = {}  # robot_id → 이전 온라인 상태
_status_source_stale: dict[int, bool] = {}  # robot_id → receiver STATUS source timestamp stale 상태


def _robot_is_online() -> bool:
    """상태 스레드가 하트비트로 판정한 현재 로봇 온라인 여부."""
    rid = runtime.get_robot_id_by_ip(ROBOT_IP)
    if rid is None:
        return False
    return bool(_was_online.get(rid, False))

# 시간 기반 임계값 — 마지막 성공 시각으로부터 경과한 시간으로 판정
UNSTABLE_ALARM_AFTER_SEC = 15.0   # 15초 이상 끊김 → 불안정 알람 (1회)
OFFLINE_AFTER_SEC = 35.0          # 35초 이상 끊김 → 오프라인 확정 (runtime ERROR_MAX_AGE와 일치)

# STATUS UDP 재시도 — 혼잡 무선에서 패킷 1~2개 유실로 즉시 실패 처리되어
# Offline로 깜빡이는 것을 막기 위해 한 폴링 사이클 안에서 짧게 여러 번 시도한다.
STATUS_RETRY_ATTEMPTS = 3
STATUS_RETRY_TIMEOUT = 0.7
# 3층 Wi-Fi 복귀 직후 UDP 왕복 지연이 길어지는 구간의 임시 완화값.
# 이 값은 통신 단절을 정상으로 오인하지 않도록 하는 임시 조치이며,
# 최종적으로는 STATUS를 TCP 신뢰 채널로 전환한다.
STATUS_SOCKET_TIMEOUT = 3.0

# receiver 캐시(basic_status)가 이보다 오래되면 로봇 heartbeat 실패로 간주
STATUS_SOURCE_STALE_AFTER_SEC = 8.0


def _try_status_once(sock: socket.socket) -> dict | None:
    """STATUS 요청 1회 시도(소켓 재사용, M-6). 성공 시 응답 dict, 타임아웃 시 None.

    타임아웃 외 예외는 소켓 손상 가능성이 있으므로 호출자가 재생성하도록 전파한다.
    """
    # 상태값은 TCP receiver(40001)에 먼저 요청한다. TCP 장애나 구버전
    # receiver일 때만 기존 UDP(40000)를 보조 경로로 사용한다.
    try:
        from app.robot_io.sender import request_receiver_tcp
        tcp_resp = request_receiver_tcp(
            {"action": "STATUS"},
            connect_timeout=1.0,
            recv_timeout=2.0,
        )
        if isinstance(tcp_resp, dict):
            return tcp_resp
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
        pass

    msg = json.dumps({"action": "STATUS"}).encode("utf-8")
    sock.sendto(msg, (RECEIVER_IP, RECEIVER_PORT))
    try:
        data, addr = sock.recvfrom(8192)
        return json.loads(data.decode("utf-8"))
    except socket.timeout:
        return None


def _source_timestamp(payload: dict | None) -> float:
    if not payload:
        return 0.0
    try:
        return float(payload.get("timestamp") or 0.0)
    except (TypeError, ValueError):
        return 0.0


BOOT_GRACE_SEC = 30.0  # 백엔드 시작 후 이 시간 동안은 불안정 알람 억제


def status_thread():
    """receiver.py 경유로 배터리 상태 폴링 + 온라인/오프라인 전환 로그.

    시간 기반 판정: 마지막 성공 시각 기준 경과 시간으로 불안정/오프라인 알람.
    - UNSTABLE_ALARM_AFTER_SEC 이상 끊김 → 불안정 알람 (1회만)
    - OFFLINE_AFTER_SEC 이상 끊김 → 오프라인 확정
    """
    print(f"[LISTEN] 상태 Listener 시작 (via receiver.py {RECEIVER_IP}:{RECEIVER_PORT})")

    boot_time = time.time()            # 스레드 시작(≈백엔드 시작) 시각
    last_success_time = time.time()    # 마지막 성공 수신 시각
    unstable_alarm_fired = False       # 이 끊김 구간에서 불안정 알람 이미 발생했는가
    ever_succeeded = False             # 한 번이라도 STATUS 수신 성공했는가
    status_sock = None                 # 재사용 소켓 (M-6)

    while not _shutdown.is_set():
        try:
            if status_sock is None:
                status_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                status_sock.settimeout(STATUS_SOCKET_TIMEOUT)
            resp = _try_status_once(status_sock)
        except Exception as e:
            # 소켓 손상(타임아웃 외 예외) → 닫고 재생성, 이번 사이클은 실패로 처리
            print("[ERR STATUS]", e)
            if status_sock is not None:
                try:
                    status_sock.close()
                except Exception:
                    pass
                status_sock = None
            resp = None

        success = False
        if resp is not None:
            battery = resp.get("BatteryStatus", {}) or {}
            charge_state = resp.get("ChargeStatus")
            device_temp = resp.get("DeviceTemperature", {})
            basic_status = resp.get("BasicStatus", {}) or {}
            motion_info = resp.get("MotionInfo", {}) or {}
            abnormal_status = resp.get("AbnormalStatus", []) or []
            # 응답이 도달했으면 last_heartbeat는 반드시 갱신한다
            # (battery가 아직 비어있을 수 있지만 네트워크는 살아있음).
            rid = runtime.get_robot_id_by_ip(ROBOT_IP)
            if rid is not None:
                basic_ts = _source_timestamp(basic_status)
                source_stale = (
                    basic_ts > 0
                    and (time.time() - basic_ts) >= STATUS_SOURCE_STALE_AFTER_SEC
                )
                if source_stale:
                    if not _status_source_stale.get(rid, False):
                        _status_source_stale[rid] = True
                        print(
                            f"[AUTO-INITPOSE] robot {rid} STATUS source stale "
                            f"({time.time() - basic_ts:.1f}s) — 다음 fresh STATUS를 전원 on edge로 준비"
                        )
                        try:
                            from app.robot_control.auto_init_pose import mark_offline
                            mark_offline(rid)
                        except Exception as e:
                            print(f"[AUTO-INITPOSE] stale mark_offline 실패: {e}")
                    resp = None
                else:
                    if _status_source_stale.pop(rid, False):
                        print(f"[AUTO-INITPOSE] robot {rid} STATUS source fresh 복귀")

            if resp is not None and rid is not None:
                runtime.update_status(
                    rid, battery, time.time(),
                    charge_state=charge_state,
                    device_temp=device_temp,
                    basic_status=basic_status,
                    gait=motion_info.get("gait"),
                    abnormal_status=abnormal_status,
                )
                success = True
                last_success_time = time.time()
                ever_succeeded = True
                unstable_alarm_fired = False  # 성공 시 알람 플래그 리셋

                # 관절 모터 과열 감지 + 위험 시 보호 동작(충전소 도킹 후 SIT)
                try:
                    from app.robot_control.motor_thermal import check_motor_overheat
                    check_motor_overheat(rid, device_temp)
                except Exception as e:
                    print(f"[WARN] 모터 과열 체크 실패: {e}")

                # 오프라인 → 온라인 전환 감지
                if not _was_online.get(rid, False):
                    _was_online[rid] = True
                    log_event("robot", "robot_online", "로봇 온라인",
                              robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())

                # 배터리 임계치 도달 시 자동 충전 복귀 트리거 (정책은 auto_return 모듈 참조)
                try:
                    from app.robot_control.auto_return import check_battery_and_return
                    check_battery_and_return(rid)
                except Exception as e:
                    print(f"[AUTO-RETURN] check 실패: {e}")

                # 전원 자동 on(부팅) 상승에지 시 init_pose 자동 주입 트리거 (ERR-07)
                try:
                    from app.robot_control.auto_init_pose import check_and_init_pose
                    check_and_init_pose(rid)
                except Exception as e:
                    print(f"[AUTO-INITPOSE] check 실패: {e}")

                # 미확정 자동 복구: 충전 중 보고 위치가 충전소(dock) 좌표로 안정 수렴하면
                # 미확정 해제(rviz 등 외부 교정 후 자동 반영). 비충전은 운영자 버튼으로 해제.
                try:
                    from app.robot_control.auto_init_pose import check_and_recover_pending
                    check_and_recover_pending(rid)
                except Exception as e:
                    print(f"[AUTO-INITPOSE] recover check 실패: {e}")

                # 도크 밖 미확정: 제자리면 스캔을 지도 전체와 맞춰 위치를 찾아 넣는다(11-06)
                try:
                    from app.robot_control.auto_relocalize import check as auto_reloc_check
                    auto_reloc_check(rid)
                except Exception as e:
                    print(f"[AUTO-RELOC] check 실패: {e}")

                # 모터 과열 감지는 motor_thermal(check_motor_overheat) 단일 경로 사용.
                # 구 thermal.check_and_log_thermal 은 임계 충돌로 배선 제거.

        if not success:
            elapsed = time.time() - last_success_time

            # 10초 이상 끊김 → 불안정 알람 (이 끊김 구간에서 1회만).
            # 단, 아래 경우에는 알람을 띄우지 않는다:
            #   1) 로봇이 마지막으로 알려진 상태가 '켜짐(Sleep=0)'이 아닌 경우
            #   2) 백엔드 시작 후 grace period(30초) 이내인 경우
            #   3) 한 번도 STATUS 수신에 성공한 적 없는 경우 (초기 연결 중)
            in_grace = (time.time() - boot_time) < BOOT_GRACE_SEC
            if (elapsed >= UNSTABLE_ALARM_AFTER_SEC
                    and not unstable_alarm_fired
                    and not in_grace
                    and ever_succeeded):
                unstable_alarm_fired = True
                rid = runtime.get_robot_id_by_ip(ROBOT_IP)
                if rid is not None:
                    # 로봇이 켜진 상태였을 때만 알람 발화
                    with runtime._lock:
                        entry = runtime._runtime.get(rid)
                    last_basic = (entry or {}).get("basic_status") or {}
                    last_sleep = last_basic.get("Sleep")
                    if last_sleep == 0:
                        log_event("error", "robot_connection_error", "로봇 통신 연결 불안정",
                                  error_json=f"{elapsed:.1f}초 간 응답 없음",
                                  robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())

            # 12초 이상 끊김 → 오프라인 확정
            if elapsed >= OFFLINE_AFTER_SEC:
                rid = runtime.get_robot_id_by_ip(ROBOT_IP)
                if rid is not None and _was_online.get(rid, False):
                    _was_online[rid] = False
                    log_event("robot", "robot_offline", "로봇 오프라인",
                              robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
                    # 다음 온라인 복귀를 전원 on 상승에지로 잡기 위해 powered 내림 (ERR-07)
                    try:
                        from app.robot_control.auto_init_pose import mark_offline
                        mark_offline(rid)
                    except Exception as e:
                        print(f"[AUTO-INITPOSE] mark_offline 실패: {e}")

        time.sleep(REQ_INTERVAL_HB)

    if status_sock is not None:
        try:
            status_sock.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────
# 네비게이션 스레드
# ─────────────────────────────────────────────────────────
ARRIVAL_COOLDOWN = 1.5
NAV_POLL_INTERVAL = 1.0       # 자율주행 중 폴링 주기 (도착/hang 감지 민감도 유지)
NAV_IDLE_POLL_INTERVAL = 5.0  # 비주행(대기) 시 폴링 주기 — 대기 로봇 상시 트래픽 완화 (C-1)
NAV_RETRY_TIMEOUT = 30.0   # 전송 후 N초 내 이동 시작 안 하면 재전송
# 멈춤(hang) 판정 — 보낸 뒤 흐른 시간이 아니라 실제로 움직이지 않은 시간으로 본다(10-22).
# 보낸 시각 기준이면 오래 걸리는 정상 이동도 60초마다 리셋 재전송돼 섰다 다시 출발했고,
# 4번째에는 가는 중인데 도착으로 처리됐다.
NAV_STALL_SEC = float(os.getenv("NAV_STALL_SEC", "60"))
NAV_STALL_MOVE_M = 0.2    # 이만큼 움직이면 진행 중으로 본다(측위 떨림보다 크게)
NAV_MAX_RETRIES = 3        # 최대 재전송 횟수 (ever_moved=False / 255 일시정지 / hang 합산)
NAV_START_DETECT_TIMEOUT = 15.0  # status=0 상태에서 보행/NAV 시작을 기다리는 시간
ARRIVAL_CONFIRM_COUNT = 3  # status==0 연속 N회 확인 후 도착 판정 (오판 방지)
NEAR_SKIP_DISTANCE = 0.5   # 목표 웨이포인트까지 이 거리(m) 이내면 이미 도착으로 간주
# 안내 이동(keep_gait 표시된 마지막 지점)에서 로봇이 NAV 를 받고도 전혀 움직이지 않았을 때(status 0 유지)
# 이 거리 안이면 도착으로 본다. 로봇은 가까운 목표 NAV 를 무시한다(09-28: 0.6m) — 안내 쪽은 근처 판정을
# 0.5m 로 줄여(voice_scenario.GUIDE_NEAR_M) 0.5~1.0m 목표도 보내 보므로, 받지 않으면 재전송 한도까지
# 서 있다 실패하지 않게 예전 판정 반경(1.0m)으로 받아 준다.
GUIDE_NO_START_NEAR_M = float(os.getenv("GUIDE_NO_START_NEAR_M", "1.0"))
# 상태(status==0)로 도착을 판정해도 목표에서 이보다 멀면 도착이 아니다(로봇이 NAV 를 받고 안 움직인 경우).
# 로봇의 도착 허용 오차·장애물 앞 정지를 감안해 넉넉히 둔다.
ARRIVAL_MAX_DISTANCE = float(os.getenv("ARRIVAL_MAX_DISTANCE", "1.5"))
# 통과 지점(pass_through, 자동 삽입 경유지): 멈추기를 기다리지 않고 이 반경 안에 들어오면 다음 지점을
# 보낸다. 위치는 최대 ~1초 늦으므로(receiver 0.5s + 관제 0.5s 폴링) 로봇이 감속하기 전에 잡히게 넉넉히 둔다.
PASS_THROUGH_RADIUS = float(os.getenv("PASS_THROUGH_RADIUS", "1.0"))
PASS_THROUGH_POLL_INTERVAL = 0.5  # 통과 지점으로 가는 동안의 NAV 폴링 주기
RECEIVER_UNRESPONSIVE_THRESHOLD = 30  # NAV_STATUS 연속 N회 타임아웃 시 receiver 응답 불능 로그(1회)
RECEIVER_LOST_RESEND_SEC = 8.0   # 통신 두절 후 복구되면, 이 시간 이상 끊겼던 경우 현재 WP 자동 재전송
RECEIVER_LOST_STOP_SEC = 60.0    # 통신 두절이 이 시간 이상 지속되면 자율주행 안전 정지 + 사용자 알림


def _moved_since(start: dict) -> bool:
    """보낸 뒤 위치나 방향이 바뀌었나 — NAV 상태 보고가 늦어도 실제로는 출발했을 수 있다."""
    rid = runtime.get_robot_id_by_ip(ROBOT_IP)
    if not rid or not start or not start.get("timestamp"):
        return False
    pos = runtime.get_position(rid)
    moved = math.hypot(pos["x"] - start["x"], pos["y"] - start["y"])
    turned = abs((pos["yaw"] - start["yaw"] + math.pi) % (2 * math.pi) - math.pi)
    return moved >= NAV_KEEP_GAIT_MOVED_M or turned >= NAV_KEEP_GAIT_TURNED_RAD


def _guide_move_active() -> bool:
    """지금 주행이 안내 이동인가 — 다단계 안내 세션 중이거나 상시 대기 단발 응대가 목적지로 가는 중."""
    try:
        from app.scheduler import standby_session as sess_mod
        sess = sess_mod.get_session()
        if sess and sess.get("phase") == "guiding":
            return True
        from app.database.routes.voice_scenario import scenario_busy
        return scenario_busy(get_robot_name())
    except Exception:  # noqa: BLE001
        return False


def _scenario_move_active() -> bool:
    """지금 주행이 안내 시나리오 세션의 이동인가 — 장애물 감지는 여기서만 켠다(10-29 결정, 단발 응대 제외)."""
    try:
        from app.database.routes.voice_scenario import scenario_busy
        return scenario_busy(get_robot_name())
    except Exception:  # noqa: BLE001
        return False


def _stall_paused() -> bool:
    """일부러 서 있는 구간 — 멈춤으로 세지 않는다(경유지 도착 확인 대기·층 전환)."""
    try:
        from app.navigation.arrival_gate import gate_status
        if gate_status().get("active"):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.navigation.floor_transition import is_transitioning
        if is_transitioning(get_robot_id()):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


# NAV_STATUS 통신 상태 — 멈춘 이동 처리(nav_hold)가 끊긴 동안 요청을 받지 않게 본다
_nav_comm_lost = False


def nav_comm_ok() -> bool:
    """receiver(NOS)와 주행 상태를 주고받고 있는가. 끊겼으면 다시 시도·중단 명령이 로봇에 닿지 않는다."""
    return not _nav_comm_lost


def _comm_lost_stop(stop_navigation_internal) -> None:
    """통신 두절 안전 정지. 상시 대기 근무 중이면 근무는 남기고 '자리 비움(통신 두절)' — 복구되면 대기 장소로."""
    from app.scheduler import loop as lp
    if lp.get_standby_state() is None:
        stop_navigation_internal("통신 두절 안전 정지")
        return
    stop_navigation_internal("통신 두절 안전 정지", keep_schedule=True)
    lp.on_standby_comm_lost()


def _set_comm_lost(lost: bool) -> None:
    global _nav_comm_lost
    _nav_comm_lost = lost


def _halt_target() -> bool:
    """못 가면 멈춰야 하는 주행(안내 사전 주행) — 지금처럼 '못 감' 표시 후 끝낸다."""
    from app.navigation.send_move import heading_wp_no, waypoints_list
    no = heading_wp_no()
    return 0 < no <= len(waypoints_list) and bool(waypoints_list[no - 1].get("halt_if_unreached"))


def _open_nav_hold(stalled_sec: float, pos, status, why: str) -> None:
    """지점에 못 갔다 — 건너뛰지 않고 그 자리에서 운영자 선택을 기다린다(P2)."""
    from app.navigation import nav_hold
    from app.navigation.send_move import heading_wp_no, waypoints_list
    no = heading_wp_no()
    name = waypoints_list[no - 1].get("name", f"WP{no}") if 0 < no <= len(waypoints_list) else f"WP{no}"
    kind = nav_hold.move_kind()
    if not nav_hold.open_hold(kind, no, name, pos, why):
        return
    where = f"({pos[0]:.1f}, {pos[1]:.1f})" if pos else "알 수 없음"
    print(f"[WARN] 이동 멈춤 — {name} 도착 못 함 ({why}), 종류={kind}, 운영자 선택 대기")
    log_event("error", "nav_unreached_hold",
              f"{get_robot_name()} '{name}' 지점에 도착하지 못해 멈췄습니다 — 운영자 확인 필요",
              detail=f"kind={kind}, WP{no}/{len(waypoints_list)}, 위치={where}, status={status}, {why}",
              robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())


def _end_held_move(h: dict, by_operator: bool) -> None:
    """멈춘 이동을 끝낸다 — 운영자 '중단' 또는 응답 없음(NAV_HOLD_TIMEOUT_SEC)."""
    from app.navigation.send_move import stop_navigation_internal
    from app.scheduler import loop as lp
    from app.scheduler import standby_session as sess_mod
    kind, name = h["kind"], h["wpName"]
    who = "운영자 중단" if by_operator else "응답 없음 — 자동 처리"
    reason = f"{name} 도착 못 함 — {who}"
    log_event("schedule", "nav_hold_action" if by_operator else "nav_hold_timeout",
              f"{get_robot_name()} 멈춘 이동 처리: {reason}", detail=f"kind={kind}",
              robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())

    def go_charge():
        try:
            from app.robot_control.charge import _return_to_charge_internal
            _return_to_charge_internal(cancel_running=False)
        except Exception as e:  # noqa: BLE001
            print(f"[ERR] 멈춘 이동 처리 후 충전소 복귀 실패: {e}")

    if kind == "work":
        if by_operator:
            stop_navigation_internal(reason)                 # 회차 = 취소
        else:
            lp.abort_active_run(reason)                      # 회차 = 중단, 다음 회차는 그대로
            stop_navigation_internal(reason, keep_schedule=True)
        go_charge()
    elif kind == "manual":
        stop_navigation_internal(reason, keep_schedule=True)
    elif kind == "standby":
        if by_operator:
            stop_navigation_internal(reason)                 # 근무 중단
            go_charge()
        else:
            if sess_mod.is_active():
                sess_mod.abort(sess_mod.RESULT_COMPLETED, f"대기 장소 복귀 못 함 — {name}")
            stop_navigation_internal(reason, keep_schedule=True)
            lp._set_away("return_failed")                    # 근무는 유지, 운영 시간 끝나면 지금 규칙대로
    elif kind == "guide":
        _end_guide(reason)


def _end_guide(reason: str, result: str = "aborted_operator", closed_by: str = "operator") -> None:
    """운영자 '안내 종료'(또는 장애물 앞 대응 없음) — 근무(상시 대기)는 남기고 안내만 닫는다."""
    from app.navigation.send_move import stop_navigation_internal
    from app.scheduler import loop as lp
    from app.scheduler import standby_session as sess_mod
    stop_navigation_internal(reason, keep_schedule=True)
    if sess_mod.is_active():
        sess_mod.abort(sess_mod.RESULT_ABORTED_OPERATOR, reason)
        state = lp.get_standby_state()
        if state:
            lp._return_to_post(state)
        return
    from app.database.database import SessionLocal
    from app.database.routes import voice_scenario as vs
    db = SessionLocal()
    try:
        s = vs._active_session(db, get_robot_name())
        if s:
            vs.close(db, s, result, closed_by, reason)
    finally:
        db.close()


def nav_thread():
    # 지연 import — 순환 참조 회피
    from app.navigation.send_move import (
        navigation_send_next, navigation_resend_current,
        is_nav_active, get_current_target, get_nav_sent_time, check_and_clear_reset_flag,
        mark_pass_through_handoff,
        keep_gait_probe, clear_keep_gait_probe, keep_gait_start_failed, keep_gait_probe_timeout,
        keep_gait_at_pass_through, mark_pass_through_kept, on_multi_progress, multi_active,
        multi_at_last,
    )
    from app.scheduler.loop import (
        on_navigation_complete, on_navigation_error, get_active_schedule_id,
    )
    from app.navigation import obstacle_hold

    last_status = None
    ever_moved = False      # 현재 WP에서 이동(!=0)을 한 번이라도 감지했는지
    zero_count = 0          # status==0 연속 카운트
    pause_since = 0         # 255 연속 시작 시간
    last_stand_sent = 0     # 마지막 STAND 전송 시간
    retry_count = 0
    stall_pos = None        # 멈춤 판정 기준 위치 — 여기서 NAV_STALL_MOVE_M 이상 움직이면 갱신
    stall_since = 0.0       # stall_pos 를 잡은 시각
    guide_blocked = False   # 안내 이동 막힘을 이번 주행에서 이미 알렸는가
    consecutive_timeouts = 0  # NAV_STATUS UDP 타임아웃 연속 횟수 (연결 끊김 감지)
    comm_lost_since = 0.0     # 통신 두절(타임아웃/송신실패) 시작 시각 (0=정상 수신 중)
    recv_lost_alerted = False  # 통신 두절 안전 정지 알림을 이번 두절 구간에서 이미 발생시켰는가
    receiver_alert_fired = False  # receiver 응답 불능 알림을 이번 두절 구간에서 이미 발생시켰는가
    # 통신 두절로 안전 정지했다. 그때 보낸 CANCEL_NAV·GAIT_BASIC 은 두절 중이라 로봇에 닿지 않았을 수
    # 있다 — 로봇은 마지막 목표까지 가서 내비 보행으로 남는다. 복구되면 한 번 더 보낸다.
    comm_stop_followup = False

    print(f"[LISTEN] 네비 Listener 시작 (via receiver.py {RECEIVER_IP}:{RECEIVER_PORT})")

    while not _shutdown.is_set():
        arrived = False
        comm_failed = False   # 이번 사이클에서 NAV_STATUS 통신(송신/수신)이 실패했는가

        # ── 리셋 신호 감지 (새 주행 시작 / 정지 / 다음 WP 전송 시) ──
        reset, is_full = check_and_clear_reset_flag()
        if reset:
            last_status = None
            ever_moved = False
            zero_count = 0
            pause_since = 0
            last_stand_sent = 0
            stall_pos = None
            stall_since = 0.0
            if is_full:
                retry_count = 0
                guide_blocked = False
                from app.navigation import nav_hold as _nh
                _nh.close_hold()
                obstacle_hold.reset()
                # 새 주행/정지 시 통신 두절 추적도 초기화 (다음 두절 때 다시 알림 가능하도록)
                comm_lost_since = 0.0
                recv_lost_alerted = False
            print(f"[NAV] 상태 리셋 (last_status=None, full={is_full})")

        # ── 상태 기반 도착 감지 ──
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.5)
            msg = json.dumps({"action": "NAV_STATUS"}).encode("utf-8")
            sock.sendto(msg, (RECEIVER_IP, RECEIVER_PORT))

            data, addr = sock.recvfrom(4096)
            nav = json.loads(data.decode("utf-8"))

            # 다지점 주행이면 로봇이 보고한 점 번호로 지난 점을 표시한다(구버전 receiver 는 value 없음).
            # 아래 통신 복구 재전송보다 먼저 — 두절 동안 지난 점을 반영해야 남은 점만 다시 보낸다.
            multi_running = is_nav_active() and multi_active() and nav.get("status") not in (0, None)
            if multi_running:
                on_multi_progress(nav.get("value"))

            if comm_stop_followup:
                comm_stop_followup = False
                if not is_nav_active():
                    _resend_stop_after_comm_loss()

            # 연결 복구 직후: 끊김 구간의 stale 상태를 도착 판정에 쓰지 않도록 카운터 리셋
            if comm_lost_since > 0 and is_nav_active():
                lost_dur = time.time() - comm_lost_since
                print(f"[NAV] 통신 복구 — 도착 판정 상태 리셋 (두절 {lost_dur:.0f}s 후)")
                ever_moved = False
                zero_count = 0
                last_status = None
                # 두절이 길었으면 그 사이 로봇이 명령을 잃었을 수 있으므로 현재 WP 재전송하여 주행 재개.
                # 다지점 묶음을 아직 실행 중이면 로봇은 두절과 상관없이 가고 있다 — 재전송(리셋)하면 세우기만 한다.
                if multi_running:
                    print(f"[NAV] 통신 복구 — 다지점 실행 중이라 재전송 안 함 (두절 {lost_dur:.0f}s)")
                elif lost_dur >= RECEIVER_LOST_RESEND_SEC and retry_count < NAV_MAX_RETRIES:

                    retry_count += 1
                    print(f"[NAV] 통신 복구 후 현재 WP 재전송 ({retry_count}/{NAV_MAX_RETRIES})")
                    try:
                        navigation_resend_current()
                    except Exception as e:
                        print(f"[ERR] 복구 재전송 실패: {e}")
            consecutive_timeouts = 0
            comm_lost_since = 0.0
            recv_lost_alerted = False
            _set_comm_lost(False)

            # NAV_STATUS 정상 수신 복귀 — 앞서 올린 응답 불능 error를 해제 이벤트로 닫는다.
            if receiver_alert_fired:
                receiver_alert_fired = False
                log_event("robot", "receiver_recovered", "NOS receiver 응답 복구",
                          detail=f"NOS({RECEIVER_IP}:{RECEIVER_PORT}) NAV_STATUS 정상 수신",
                          robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())

            status = nav.get("status")
            nav_age = nav.get("age")  # receiver가 자기 monotonic 시계로 계산한 경과시간(초)

            # stale 데이터 무시 (5초 이상 오래된 데이터)
            # age는 receiver(로봇 NOS) 한 머신 안에서만 계산되므로 서버-로봇 시계 오프셋과 무관하다.
            # (이전 구현은 서버 time.time()에서 로봇 timestamp를 빼서, 두 시계가 어긋나면
            #  멀쩡한 데이터를 전부 stale로 폐기하는 버그가 있었다.)
            # 구버전 receiver(age 미전송, age=None)면 잘못된 cross-clock 비교 대신 필터를 건너뛴다.
            stale = nav_age is not None and nav_age > 5.0

            # 안내 중 장애물 정지 — NOS 감지 노드가 이동을 취소했다. 서 있는 동안 도착·재전송·멈춤 판정을 쉬고,
            # 풀리면 같은 목표를 다시 보낸다(재전송이 판정 상태를 리셋한다).
            # stale 검사보다 먼저 돈다 — 장애물로 선 동안 로봇 상태가 갱신되지 않아 stale 로 건너뛰면 감지 신호가
            # 끊겨 감지 노드가 꺼지고, 그사이 받은 손님 대답을 처리하지 못했다(2026-11-02 세션 521, 21초).
            ob = obstacle_hold.cycle(is_nav_active(), _scenario_move_active())
            if ob == obstacle_hold.RESUME:
                # 서 있는 동안 내비 보행 그대로다 — 리셋 없이 잇는다(리셋하면 보행이 풀렸다 다시 잡힘, 10-30 15:10)
                navigation_resend_current(continuing=True)
            elif ob == obstacle_hold.ABORT:
                from app.navigation import nav_hold as _ob_nh
                _end_held_move(_ob_nh.close_hold(), by_operator=True)
            if ob:
                if not stale:
                    last_status = status
                stall_pos = None
                time.sleep(NAV_POLL_INTERVAL)
                continue

            if stale:
                if is_nav_active():
                    _navdbg(f"[NAV DEBUG] stale 데이터 무시 (age={nav_age:.1f}s)")
                time.sleep(NAV_POLL_INTERVAL)
                continue

            if is_nav_active():
                sent_time = get_nav_sent_time()
                cooldown_ok = sent_time > 0 and (time.time() - sent_time) > ARRIVAL_COOLDOWN
                elapsed = time.time() - sent_time if sent_time > 0 else 0
                _navdbg(f"[NAV DEBUG] status={status}, last={last_status}, cooldown={cooldown_ok}, moved={ever_moved}, zero={zero_count}/{ARRIVAL_CONFIRM_COUNT}, elapsed={elapsed:.1f}s")

            if status is not None:
                if last_status != status:
                    print(f"[SYNC] NAV 상태 변화: {last_status} → {status}")

                # 이동 감지 (한 번이라도 non-zero면 기록)
                # 단, 255(pause) → 0 전환은 "이동 완료 후 정지"가 아니라 "중도 포기 정지"일
                # 가능성이 커서 도착 카운터(zero_count)와 ever_moved를 리셋한다.
                # 실제 도착이면 로봇 위치가 목표 근처일 것이므로 아래 near-skip 로직이
                # 잡아준다. 멀면 재전송 경로로 빠져 stuck을 복구한다.
                if status != 0:
                    ever_moved = True
                    zero_count = 0
                    probe = keep_gait_probe()
                    if probe:
                        clear_keep_gait_probe(started_after=time.time() - probe[0])
                else:
                    if last_status == 255:
                        print("[NAV] 255 → 0 전환 — 도착 판정 리셋 (중도 정지 가능성)")
                        ever_moved = False
                        zero_count = 0
                    else:
                        zero_count += 1

                sent_time = get_nav_sent_time()
                cooldown_ok = sent_time > 0 and (time.time() - sent_time) > ARRIVAL_COOLDOWN

                # 통과 지점 보행 유지: 걷는 중에 넘기면 로봇이 거부하고, 리셋해서 넘기면 기본 보행으로 바뀐다.
                # 그래서 통과 지점에 도착(작업 완료, status 0)하면 한 번만 확인하고 바로 리셋 없이 다음 지점을 보낸다.
                pass_keep = is_nav_active() and keep_gait_at_pass_through()
                if pass_keep and ever_moved and status == 0 and cooldown_ok:
                    pass_dist = _pass_through_distance(get_current_target())
                    if pass_dist is not None and pass_dist < PASS_THROUGH_RADIUS:
                        arrived = True
                        mark_pass_through_kept()
                        print(f"➡ 통과 지점 도착 (거리={pass_dist:.2f}m) — 보행 유지, 리셋 없이 다음 WP 전송")

                # 경유지 방향 안내 — 다가가는 경유지에 1~2m 앞에서 미리 알린다(send_move.check_approach_cue)
                if is_nav_active():
                    try:
                        from app.navigation.send_move import check_approach_cue
                        check_approach_cue()
                    except Exception as e:  # noqa: BLE001 — 안내 때문에 도착 판정이 멈추면 안 된다
                        print(f"[CUE] 방향 안내 확인 실패: {e}")

                # 도착 판정: 이동한 적 있고, 연속 N회 status==0 확인 + 실제로 목표 근처
                if (not arrived and ever_moved and zero_count >= ARRIVAL_CONFIRM_COUNT
                        and is_nav_active() and cooldown_ok):
                    far = _target_far_distance(get_current_target())
                    if far is not None:
                        # 로봇이 NAV 를 받고 상태만 잠깐 바꿨다 0 으로 돌아온 경우 — 제자리다.
                        # 도착으로 보면 안내가 "도착했습니다" 멘트를 한다(2026-10-01 실측: 호출→접수
                        # 2.1m 를 안 움직였는데 13초 만에 도착 처리). 이동 미감지로 돌려 재전송시킨다.
                        print(f"[WARN] NAV 상태는 정지인데 목표와 {far:.2f}m — 도착 아님, 이동 미감지로 처리")
                        ever_moved = False
                    else:
                        arrived = True
                        if pass_keep:
                            mark_pass_through_kept()
                    if arrived:
                        print(f"🎉 NAV 도착! (상태 기반: 연속 {zero_count}회 status==0 확인)")
                if arrived:
                    from app.navigation.send_move import current_wp_index, waypoints_list
                    wp_name = waypoints_list[current_wp_index - 1].get("name", f"WP{current_wp_index}") if current_wp_index > 0 else f"WP{current_wp_index}"
                    log_event("schedule", "nav_arrival",
                              f"{wp_name} 도착 ({current_wp_index}/{len(waypoints_list)})",
                              robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())

                # 통과 지점: 멈추기를 기다리지 않고 반경 안에 들어오면 다음 지점으로 넘긴다.
                # 출발한 뒤(ever_moved)에만 본다 — 출발 전부터 반경 안이면 위의 정지 판정이 처리한다.
                # 보행 유지 중이면 넘기지 않는다(위에서 도착을 기다려 잇는다).
                if not arrived and not pass_keep and ever_moved and is_nav_active() and cooldown_ok:
                    pass_target = get_current_target()
                    pass_dist = _pass_through_distance(pass_target)
                    if pass_dist is not None and _pass_through_handoff_ok(pass_target, pass_dist):
                        arrived = True
                        mark_pass_through_handoff()
                        print(f"➡ 통과 지점 진입 (거리={pass_dist:.2f}m < {PASS_THROUGH_RADIUS}m) — 멈추지 않고 다음 WP 전송")

                # 이동 미감지: status=0이 지속될 때 처리
                sent_time = get_nav_sent_time()
                elapsed = time.time() - sent_time if sent_time > 0 else 0
                if (not arrived and not ever_moved
                        and zero_count >= ARRIVAL_CONFIRM_COUNT
                        and is_nav_active() and cooldown_ok):
                    # 이미 목표 근처에 있으면 바로 도착 처리 (재전송 불필요)
                    target = get_current_target()
                    # 다지점 묶음의 끝이 출발점과 같으면(한 바퀴 돌아오는 경로) 출발 전부터 끝점 근처다
                    # — 묶음은 끝점 거리로 '이미 도착' 을 판단하지 않는다. 단 로봇이 마지막 점으로 가는 중이라고
                    # 보고한 뒤에는 본다: 끝점 도착을 통신 두절(1s) 중에 놓치면 서 있는 로봇에 같은 점을 거듭 보내다
                    # 재전송 한도를 넘겼다(10-27 12:44·15:00·16:01).
                    if target and (not multi_active() or multi_at_last()):
                        rid = runtime.get_robot_id_by_ip(ROBOT_IP)
                        pos = runtime.get_position(rid) if rid else {"x": 0, "y": 0}
                        dx = pos["x"] - target["x"]
                        dy = pos["y"] - target["y"]
                        dist = (dx**2 + dy**2) ** 0.5
                        near_r = (max(NEAR_SKIP_DISTANCE, GUIDE_NO_START_NEAR_M)
                                  if target.get("keep_gait") else NEAR_SKIP_DISTANCE)
                        if dist < near_r:
                            arrived = True
                            print(f"[OK] NAV 이미 목표 근처 (거리={dist:.2f}m < {near_r}m) — 다음 WP로 진행"
                                  + (" · 가까운 안내 목표라 로봇이 출발하지 않음" if dist >= NEAR_SKIP_DISTANCE else ""))

                    # 목표와 멀면 재전송 (보행/NAV 시작을 15초 대기 후)
                    if not arrived and elapsed >= NAV_START_DETECT_TIMEOUT:
                        if retry_count < NAV_MAX_RETRIES:
                            retry_count += 1
                            print(f"[WARN] NAV 이동 미감지 — 재전송 ({retry_count}/{NAV_MAX_RETRIES})")
                            try:
                                navigation_resend_current()
                            except Exception as e:
                                print(f"[ERR] 재전송 실패: {e}")
                        elif _guide_move_active() or _halt_target():
                            # 안내·사전 주행은 '못 감' 으로 끝낸다 — 안내 엔진이 도착 멘트 대신 실패로 처리한다
                            arrived = True
                            print(f"[WARN] NAV 이동 미감지 — 재전송 한도 초과, 다음 WP로 진행")
                            from app.navigation.send_move import mark_current_wp_unreached, heading_wp_no, waypoints_list
                            h_no = heading_wp_no()
                            h_name = (waypoints_list[h_no - 1].get("name", f"WP{h_no}")
                                      if 0 < h_no <= len(waypoints_list) else f"WP{h_no}")
                            mark_current_wp_unreached("이동 미감지 — 재전송 한도 초과",
                                                      through_end=_guide_move_active())
                            # 안내는 실패로 끝난다(음성 엔진 '이동이 중단됐어요') — 운영자 화면에도 알린다
                            log_event("error", "nav_hang_skip",
                                      f"{get_robot_name()} 안내 이동 — '{h_name}' 로 출발하지 못해 안내를 멈췄습니다",
                                      detail=f"WP{h_no}/{len(waypoints_list)}, 다시 보내기 {NAV_MAX_RETRIES}번 실패",
                                      robot_id=get_robot_id(), robot_name=get_robot_name(),
                                      business_id=get_robot_business_id())
                        else:
                            _open_nav_hold(elapsed, None, status, "출발하지 않음 · 다시 보내기 3번 실패")

                # 내비 보행 유지 출발: 리셋 없이 보낸 NAV 를 로봇이 받지 않았을 수 있다(실행 중 NAV 거부 펌웨어).
                # 15초(재전송 대기)까지 두면 안내 이동마다 멈추므로 짧게 보고, 리셋 재전송 + 이번 세션은 끈다.
                probe = keep_gait_probe()
                if (probe and not arrived and not ever_moved and is_nav_active()
                        and time.time() - probe[0] >= keep_gait_probe_timeout()):
                    if _moved_since(probe[1]):
                        clear_keep_gait_probe(started_after=time.time() - probe[0])
                    else:
                        keep_gait_start_failed(time.time() - probe[0])
                        retry_count += 1
                        try:
                            navigation_resend_current()
                        except Exception as e:
                            print(f"[ERR] 재전송 실패: {e}")

                # 충전 중 여부 확인
                _rid_for_charge = runtime.get_robot_id_by_ip(ROBOT_IP)
                _is_charging = runtime.is_charging(_rid_for_charge) if _rid_for_charge else False

                # 일시 정지(255) 추적 + 앉기 방지
                if status == 255:
                    if pause_since == 0:
                        pause_since = time.time()
                    # 5초마다 STAND 전송하여 앉기 방지 (충전 중이면 스킵)
                    if is_nav_active() and not _is_charging and (time.time() - last_stand_sent) >= 5.0:
                        last_stand_sent = time.time()
                        try:
                            from app.robot_io.sender import send_to_robot
                            send_to_robot("STAND")
                        except Exception as e:
                            print(f"[ERR] STAND 전송 실패: {e}")
                    elif _is_charging:
                        print(f"[NAV] 충전 중 — STAND 전송 스킵")
                else:
                    pause_since = 0

                # 일시 정지(255): 연속 10초 이상 지속 시 현재 WP 재전송 (충전 중이면 스킵)
                if (not arrived and status == 255 and pause_since > 0
                        and is_nav_active() and cooldown_ok
                        and not _is_charging
                        and (time.time() - pause_since) >= 10.0
                        and retry_count < NAV_MAX_RETRIES):
                    retry_count += 1
                    pause_since = time.time()  # 재전송 후 타이머 리셋
                    print(f"[WARN] NAV 일시정지(255) 연속 10초 지속 — 재전송 ({retry_count}/{NAV_MAX_RETRIES})")
                    try:
                        navigation_resend_current()
                    except Exception as e:
                        print(f"[ERR] 재전송 실패: {e}")

                # 재전송: 전송 후 N초 지났는데 이동을 한 번도 안 했으면 명령 재전송 (충전 중이면 스킵)
                if (is_nav_active() and not ever_moved and not arrived
                        and sent_time > 0
                        and not _is_charging
                        and (time.time() - sent_time) > NAV_RETRY_TIMEOUT
                        and retry_count < NAV_MAX_RETRIES):
                    retry_count += 1
                    print(f"[WARN] NAV 재전송 시도 ({retry_count}/{NAV_MAX_RETRIES}) — {NAV_RETRY_TIMEOUT}초 내 이동 미감지")
                    try:
                        navigation_resend_current()
                    except Exception as e:
                        print(f"[ERR] 재전송 실패: {e}")

                # 멈춤(hang) 감지 — 이동을 시작한 뒤 NAV_STALL_SEC 동안 NAV_STALL_MOVE_M 도 못 움직였다.
                # 오래 걸려도 움직이고 있으면 건드리지 않는다. 경유지 확인 대기·층 전환처럼 일부러 서는 구간은 세지 않는다.
                stalled_sec = 0.0
                if is_nav_active() and ever_moved and not arrived and not _is_charging:
                    now_s = time.time()
                    rid_s = runtime.get_robot_id_by_ip(ROBOT_IP)
                    pos_s = runtime.get_position(rid_s) if rid_s else None
                    if pos_s and (stall_pos is None or math.hypot(pos_s["x"] - stall_pos[0],
                                                                  pos_s["y"] - stall_pos[1]) >= NAV_STALL_MOVE_M):
                        if guide_blocked:
                            guide_blocked = False
                            log_event("schedule", "guide_nav_unblocked", "안내 이동 재개 — 막힘이 풀렸습니다",
                                      robot_id=get_robot_id(), robot_name=get_robot_name(),
                                      business_id=get_robot_business_id())
                        from app.navigation import nav_hold
                        if stall_pos is not None and nav_hold.close_hold() and not _guide_move_active():
                            # 멈춤 중 로봇이 스스로 다시 감(막힘 풀림) — 원래 주행을 잇는다
                            retry_count = 0
                            log_event("schedule", "nav_hold_resumed", "이동 재개 — 막힘이 풀렸습니다",
                                      robot_id=get_robot_id(), robot_name=get_robot_name(),
                                      business_id=get_robot_business_id())
                        stall_pos = (pos_s["x"], pos_s["y"])
                        stall_since = now_s
                    elif _stall_paused():
                        stall_since = now_s
                    if stall_since:
                        stalled_sec = now_s - stall_since

                # 운영자 선택·응답 없음 처리 — 주행 상태는 이 스레드에서만 바꾼다
                from app.navigation import nav_hold
                hold_req = nav_hold.take_request()
                if hold_req and nav_hold.get_hold():
                    h = nav_hold.get_hold()
                    if hold_req == "retry":
                        nav_hold.close_hold()
                        guide_blocked = False
                        retry_count = 0
                        stall_since = time.time()
                        log_event("schedule", "nav_hold_action", f"{get_robot_name()} 멈춘 이동 처리: {h['wpName']} 다시 시도",
                                  robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
                        try:
                            navigation_resend_current()
                        except Exception as e:
                            print(f"[ERR] 다시 시도 재전송 실패: {e}")
                    elif hold_req == "skip":
                        nav_hold.close_hold()
                        from app.navigation.send_move import mark_current_wp_skipped
                        mark_current_wp_skipped(f"{h['wpName']} 건너뜀(운영자)")
                        log_event("schedule", "nav_hold_action", f"{get_robot_name()} 멈춘 이동 처리: {h['wpName']} 건너뛰고 계속",
                                  robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
                        arrived = True
                    elif hold_req == "abort":
                        nav_hold.close_hold()
                        _end_held_move(h, by_operator=True)
                elif nav_hold.timed_out():
                    _end_held_move(nav_hold.close_hold(), by_operator=False)

                if (is_nav_active() and ever_moved and not arrived
                        and not _is_charging
                        and cooldown_ok
                        and not nav_hold.get_hold()
                        and stalled_sec >= NAV_STALL_SEC):
                    from app.navigation.send_move import current_wp_index as h_idx, waypoints_list as h_list
                    if _guide_move_active():
                        # 안내 중에는 다시 보내지도, 도착으로 넘기지도 않는다 — 방문객 앞에서 섰다 가거나
                        # 가지 않은 곳을 '도착' 이라고 하게 된다. 풀리면 이어 가고, 안 풀리면 운영자가 정한다.
                        if not guide_blocked:
                            guide_blocked = True
                            from app.navigation import nav_hold
                            from app.navigation.send_move import heading_wp_no as _hw
                            _t = get_current_target() or {}
                            nav_hold.open_hold("guide", _hw(), _t.get("name") or "목적지", stall_pos, "60초 제자리")
                            target = get_current_target() or {}
                            where = f"({stall_pos[0]:.1f}, {stall_pos[1]:.1f})" if stall_pos else "알 수 없음"
                            print(f"[WARN] 안내 이동 막힘 — {int(stalled_sec)}초 제자리, 목표={target.get('name')}")
                            log_event("schedule", "guide_nav_blocked",
                                      f"{get_robot_name()} 안내 이동이 막혔습니다 — {target.get('name') or '목적지'} 가는 중 "
                                      f"{int(stalled_sec)}초째 제자리",
                                      detail=f"WP{h_idx}/{len(h_list)}, 위치={where}, status={status}",
                                      robot_id=get_robot_id(), robot_name=get_robot_name(),
                                      business_id=get_robot_business_id())
                    elif retry_count < NAV_MAX_RETRIES:
                        retry_count += 1
                        print(f"[WARN] NAV hang 감지 ({int(stalled_sec)}s 제자리, status={status}) — 현재 WP 재전송 ({retry_count}/{NAV_MAX_RETRIES})")
                        log_event("error", "nav_hang_retry",
                                  f"NAV hang 감지 — 현재 WP 재전송 ({retry_count}/{NAV_MAX_RETRIES})",
                                  detail=f"WP{h_idx}/{len(h_list)}, status={status}, 제자리={int(stalled_sec)}s",
                                  robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
                        stall_since = time.time()
                        try:
                            navigation_resend_current()
                        except Exception as e:
                            print(f"[ERR] hang 재전송 실패: {e}")
                    elif _halt_target():
                        # 안내 사전 주행 — 못 간 지점에서 주행을 끝낸다(send_move 가 halt_if_unreached 로 멈춤)
                        arrived = True
                        from app.navigation.send_move import mark_current_wp_unreached
                        mark_current_wp_unreached("NAV hang — 재전송 한도 초과")
                        log_event("error", "nav_hang_skip",
                                  "이동 지점에 도착하지 못했습니다",
                                  detail=f"WP{h_idx}/{len(h_list)}, 제자리={int(stalled_sec)}s",
                                  robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
                    else:
                        _open_nav_hold(stalled_sec, stall_pos, status, "60초 제자리 · 다시 보내기 3번 실패")

                rid = runtime.get_robot_id_by_ip(ROBOT_IP)
                if rid is not None:
                    runtime.update_nav(rid, False, status, time.time())
                last_status = status

        except socket.timeout:
            comm_failed = True
            consecutive_timeouts += 1
            if is_nav_active():
                _navdbg(f"[NAV DEBUG] NAV_STATUS 응답 타임아웃 ({consecutive_timeouts}회)")
            # NOS receiver 응답 불능 감지 — 이번 두절 구간에서 1회만 기록.
            # 로봇이 온라인(하트비트 정상)인데 NAV_STATUS만 계속 타임아웃되는 경우에만 발화한다.
            # 로봇 자체가 꺼져 있으면 robot_offline이 이미 그 사실을 알리므로, 여기서 중복으로
            # error를 올리면 로봇이 꺼진 채 백엔드를 재시작할 때마다 오탐이 남는다.
            if (consecutive_timeouts >= RECEIVER_UNRESPONSIVE_THRESHOLD
                    and not receiver_alert_fired
                    and _robot_is_online()):
                receiver_alert_fired = True
                log_event("error", "receiver_unresponsive",
                          f"NOS receiver 응답 불능 의심 (NAV_STATUS {consecutive_timeouts}회 연속 타임아웃)",
                          detail=f"NOS({RECEIVER_IP}:{RECEIVER_PORT}) receiver.py 점검 필요",
                          robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
        except Exception as e:
            comm_failed = True
            print("[ERR NAV]", e)
            from app.navigation.send_move import current_wp_index as err_wp_idx, waypoints_list as err_wp_list
            err_route = " → ".join(wp.get("name", f"WP{i+1}") for i, wp in enumerate(err_wp_list)) if err_wp_list else ""
            err_detail = f"중단 지점: WP{err_wp_idx}/{len(err_wp_list)}"
            if err_route:
                err_detail += f"\n경로: {err_route}"
            log_event("error", "nav_error", "네비게이션 오류 발생",
                      detail=err_detail, error_json=str(e),
                      robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
            # 실패 지점을 남긴다 — 로그로만 흘리면 화면이 "어디서 멈췄는지"를 못 그린다.
            if err_wp_idx > 0:
                from app.navigation.send_move import set_nav_error
                set_nav_error(err_wp_idx, "주행 중 통신 오류로 중단되었습니다")
        finally:
            if sock:
                try: sock.close()
                except: pass

        # ── 통신 두절 지속 감지 (수신 성공/실패와 무관하게 경과 시간 기반) ──
        # 정상 수신 분기의 hang/재전송 로직은 "응답을 받아야만" 동작하므로,
        # receiver/네트워크 두절로 응답 자체가 끊기면 도착 판정이 영원히 불가능해
        # 자율주행이 무한 대기 상태로 멈춘다. 이를 경과 시간으로 직접 판정해 복구/정지한다.
        if comm_failed:
            if comm_lost_since == 0.0:
                comm_lost_since = time.time()
                _set_comm_lost(True)
            lost_dur = time.time() - comm_lost_since
            if (is_nav_active() and not recv_lost_alerted
                    and lost_dur >= RECEIVER_LOST_STOP_SEC):
                recv_lost_alerted = True
                from app.navigation.send_move import (
                    current_wp_index as lost_wp_idx, waypoints_list as lost_wp_list,
                    stop_navigation_internal,
                )
                wp_total = len(lost_wp_list)
                print(f"[ALARM] 로봇 통신 두절 {lost_dur:.0f}s 지속 — 자율주행 안전 정지 (WP{lost_wp_idx}/{wp_total})")
                # 사용자 화면 알림 발생 (nav_comm_lost → ALERT_TRIGGER_RULES 등록됨)
                # 근무 중이면 loop 가 근무용 알림(작업 종류 — 오프라인에도 억제되지 않음)을 보낸다. 중복이라 뺀다
                from app.scheduler.loop import get_standby_state as _standby_state
                if _standby_state() is None:
                    log_event("error", "nav_comm_lost",
                              "로봇 통신 두절로 자율주행이 정지되었습니다",
                              detail=f"NOS({RECEIVER_IP}:{RECEIVER_PORT}) 무응답 {lost_dur:.0f}s 지속\n"
                                     f"중단 지점: WP{lost_wp_idx}/{wp_total} — 통신 복구 후 작업을 다시 시작하세요.",
                              robot_id=get_robot_id(), robot_name=get_robot_name(),
                              business_id=get_robot_business_id())
                # 무한 대기 방지: 주행 상태를 정리하고 로봇에 정지/취소 전송
                try:
                    _comm_lost_stop(stop_navigation_internal)
                except Exception as e:
                    print(f"[ERR] 통신 두절 안전 정지 실패: {e}")
                comm_stop_followup = True

        if arrived and is_nav_active():
            rid = runtime.get_robot_id_by_ip(ROBOT_IP)
            if rid is not None:
                runtime.update_nav(rid, True, last_status, time.time())
            try:
                navigation_send_next()
                # 네비게이션이 완료되었으면 스케줄러 콜백 호출.
                # **응대 세션도 이 콜백으로 다음 단계를 정한다** — 작업 없이 받은
                # 음성 호출은 활성 스케줄이 없어서, 스케줄만 보면 도착이 아무에게도
                # 안 잡힌다(로봇이 목적지에 선 채로 안내 멘트도 없이 멈춘다).
                if not is_nav_active():
                    from app.scheduler import standby_session as sess_mod
                    if get_active_schedule_id() is not None or sess_mod.is_active():
                        on_navigation_complete()
                    # 작업 완료 후 충전소 자동 복귀 (스케줄러 / 원격 startpath)
                    import app.navigation.send_move as nav_mod
                    if getattr(nav_mod, "auto_return_to_charge", False):
                        nav_mod.auto_return_to_charge = False
                        try:
                            from app.robot_control.charge import _return_to_charge_internal
                            result = _return_to_charge_internal(cancel_running=False)
                            if not result.get("ok"):
                                print(f"[AUTO-CHARGE] 복귀 스킵: {result.get('msg')}")
                        except Exception as e:
                            print(f"[AUTO-CHARGE ERR] 자동 충전소 복귀 실패: {e}")
            except Exception as e:
                print(f"[ERR] navigation_send_next 실패: {e}")
                from app.navigation.send_move import current_wp_index, waypoints_list
                err_route2 = " → ".join(wp.get("name", f"WP{j+1}") for j, wp in enumerate(waypoints_list)) if waypoints_list else ""
                err_detail2 = f"중단 지점: WP{current_wp_index}/{len(waypoints_list)}"
                if err_route2:
                    err_detail2 += f"\n경로: {err_route2}"
                log_event("error", "nav_error", "다음 웨이포인트 이동 실패",
                          detail=err_detail2, error_json=str(e),
                          robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())
                if get_active_schedule_id() is not None:
                    on_navigation_error(str(e))

        # 적응형 폴링 주기 (C-1): 자율주행 중에는 1s로 민감하게, 대기 중에는 5s로
        # 완화하여 대기 로봇에 대한 상시 NAV_STATUS 트래픽을 줄인다.
        # 주행 시작 시 다음 사이클부터 즉시 1s로 복귀하므로 도착 감지에 영향 없다.
        # 통과 지점으로 가는 중이면 0.5s — 1s 간격이면 반경 진입을 늦게 알아 로봇이 이미 감속한다.
        if not is_nav_active():
            time.sleep(NAV_IDLE_POLL_INTERVAL)
        elif (get_current_target() or {}).get("pass_through"):
            time.sleep(PASS_THROUGH_POLL_INTERVAL)
        else:
            time.sleep(NAV_POLL_INTERVAL)


def _resend_stop_after_comm_loss() -> None:
    """통신 두절 안전 정지 후 복구 — 두절 중 유실됐을 수 있는 취소·기본 보행 복귀를 다시 보낸다."""
    from app.robot_io.sender import send_to_robot
    from app.navigation.send_move import _restore_basic_gait

    try:
        send_to_robot("CANCEL_NAV")
    except Exception as e:
        print(f"[ERR] 통신 복구 후 정지 재전송 실패: {e}")
        return
    _restore_basic_gait("통신 복구")
    print("[NAV] 통신 복구 — 두절 중 정지 명령 재전송 (CANCEL_NAV, 기본 보행·Regular)")
    log_event("robot", "nav_comm_stop_resent",
              "통신 복구 — 안전 정지 명령(내비 취소·기본 보행) 재전송",
              robot_id=get_robot_id(), robot_name=get_robot_name(), business_id=get_robot_business_id())


def _distance_to(target: dict | None) -> float | None:
    """목표까지 남은 거리(m). 목표가 없거나 위치를 믿을 수 없으면 None."""
    if not target:
        return None
    rid = runtime.get_robot_id_by_ip(ROBOT_IP)
    if rid is None:
        return None
    pos = runtime.get_position(rid)
    if not pos.get("timestamp") or pos.get("initpose_pending"):
        return None
    return math.hypot(pos["x"] - target["x"], pos["y"] - target["y"])


def _pass_through_distance(target: dict | None) -> float | None:
    """통과 지점까지 남은 거리(m). 통과 지점이 아니거나 위치를 모르면 None."""
    if not target or not target.get("pass_through"):
        return None
    return _distance_to(target)


def _pass_through_handoff_ok(target: dict, dist: float) -> bool:
    """통과 지점에서 지금 다음 지점으로 넘겨도 되는가.

    연속 주행을 켜면 로봇은 넘긴 그 자리에서 다음 지점으로 곧장 틀어 간다. 그 직선이 벽에 붙으면
    통과 지점에 더 다가갈 때까지 넘기지 않는다. 끝내 막혀 있으면 PASS_THROUGH_MIN_RADIUS 에서 넘긴다.
    """
    if dist >= PASS_THROUGH_RADIUS:
        return False
    # 노드(노드망 경로)는 문 틈·모퉁이 바로 앞에 놓인다 — 리셋해서 넘겨도 넘긴 자리에서 다음 노드로 틀어 가므로
    # 연속 주행이 꺼져 있어도 지름길 검사를 한다(10-19 시뮬: 복도 노드 0.4m 앞에서 넘겨 문 틈 끝을 질러감).
    if (not NAV_CONTINUOUS_PASS and not target.get("node")) or dist < PASS_THROUGH_MIN_RADIUS:
        return True
    from app.navigation.send_move import get_next_target
    from app.navigation.via_route import shortcut_is_clear

    nxt = get_next_target()
    rid = runtime.get_robot_id_by_ip(ROBOT_IP)
    if not nxt or rid is None:
        return True
    pos = runtime.get_position(rid)
    return shortcut_is_clear(target.get("map_id"), (pos["x"], pos["y"]), (nxt["x"], nxt["y"]))


def _target_far_distance(target: dict | None) -> float | None:
    """'정지' 상태인데 목표에서 ARRIVAL_MAX_DISTANCE 보다 멀면 그 거리, 아니면 None.

    위치를 모르면 None — 판단할 수 없으므로 기존처럼 상태만으로 도착 처리한다.
    """
    dist = _distance_to(target)
    return dist if dist is not None and dist > ARRIVAL_MAX_DISTANCE else None


# ─────────────────────────────────────────────────────────
# 런처
# ─────────────────────────────────────────────────────────
def start_polling_threads() -> None:
    """위치/상태/네비 폴링 데몬 스레드 3개를 기동한다."""
    threading.Thread(target=position_thread, daemon=True).start()
    threading.Thread(target=status_thread, daemon=True).start()
    threading.Thread(target=nav_thread, daemon=True).start()
