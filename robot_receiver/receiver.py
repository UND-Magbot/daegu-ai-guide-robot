#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import socket
import json
import math
import os
import struct
import time
import threading
from concurrent.futures import ThreadPoolExecutor


def _env_str(name, default):
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        print(f"[ROBOT CONFIG] {name}={value!r} 값이 정수가 아니어서 기본값 {default} 사용")
        return default


ROBOT_IP = _env_str("ROBOT_IP", "10.21.31.103")
ROBOT_PORT = _env_int("ROBOT_PORT", 30000)
# 로봇 본체 ASDU TCP 포트 (hop ② 신뢰 전송용; UDP 30000과 별도 채널).
# NAV 명령을 WiFi 구간 유실 없이 보장 전송하기 위해 사용한다.
ROBOT_TCP_PORT = _env_int("ROBOT_TCP_PORT", 30001)

# PC가 UDP 명령 받을 포트
SERVER_UDP_PORT = _env_int("RECEIVER_PORT", _env_int("SERVER_UDP_PORT", 40000))
# PC가 보내는 신뢰성 명령(NAV) TCP 수신 포트 (hop ①).
# Backend app/robot_io/config.py 의 RECEIVER_TCP_PORT 와 반드시 일치해야 한다.
SERVER_TCP_PORT = _env_int("RECEIVER_TCP_PORT", _env_int("SERVER_TCP_PORT", 40001))

# relay_map.py 로컬 통신 포트
MAPPING_LOCAL_PORT = 50000
mapping_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
mapping_sock.settimeout(1.0)

# relay_charge.py 로컬 통신 포트
CHARGE_LOCAL_PORT = _env_int("CHARGE_LOCAL_PORT", 50001)

# 로봇 위치(1007/2) 폴링 주기(ms). 관제가 경유지 통과(반경 진입 즉시 다음 지점)를 판정하므로
# 2초면 늦다 — 로봇이 1~2m 를 가는 사이에 경유지를 지나쳐 감속·정지한다.
POS_POLL_SEC = _env_int("POS_POLL_MS", 500) / 1000.0

# relay_motion.py 로컬 통신 포트
MOTION_RELAY_PORT = 50003       # receiver → relay_motion (보행/상태 전환 명령 송신)
MOTION_INFO_LOCAL_PORT = 50002  # relay_motion → receiver (현재 gait/state 수신)
motion_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# 로봇이 PC에서 오는 명령 수신용 소켓
server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server_sock.bind(("0.0.0.0", SERVER_UDP_PORT))
server_sock.settimeout(0.1)

# 로봇 → 로봇컨트롤로 명령 송신용 소켓
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(0.1)
sock.bind(("", 30001))


# ============================================================
# 위치 폴링 (추가)
# ============================================================
latest_position = {"x": 0.0, "y": 0.0, "yaw": 0.0, "timestamp": 0}
latest_nav_status = {"status": None, "timestamp": 0, "mono": 0.0}
latest_battery = {}
latest_device_temp = {}
latest_charge_state = {"state": 0, "error_code": 0, "timestamp": 0}  # /CHARGE_STATUS
latest_basic_status = {}  # Type=1002, Command=6 (Obtain Basic Status) — Sleep 값 등 포함
latest_basic_status_lock = threading.Lock()
latest_basic_status_received_at = 0.0
latest_motion_info = {}   # relay_motion.py(/MOTION_INFO) → {"gait":..., "state":...}
latest_abnormal_status = []  # Type=1002, Command=3 (Abnormal Status) — 활성 에러 객체 배열(ErrorList)

def nav_poll_loop():
    global latest_nav_status
    nav_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    nav_sock.settimeout(1.0)

    print("📡 네비 상태 폴링 시작")

    while True:
        try:
            asdu = {
                "PatrolDevice": {
                    "Type": 1007,
                    "Command": 1,
                    "Time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "Items": {}
                }
            }
            asdu_bytes = json.dumps(asdu).encode("utf-8")
            asdu_len = len(asdu_bytes)
            header = struct.pack(
                "<4BHHB7B",
                0xEB, 0x91, 0xEB, 0x90,
                asdu_len, 1, 0x01,
                *(0x00,) * 7
            )
            nav_sock.sendto(header + asdu_bytes, (ROBOT_IP, ROBOT_PORT))

            data, addr = nav_sock.recvfrom(4096)
            try:
                msg = json.loads(data.decode())
            except:
                msg = json.loads(data[16:].decode())

            pd = msg.get("PatrolDevice", {})
            if pd.get("Type") == 1007 and pd.get("Command") == 1:
                items = pd.get("Items", {})
                status = items.get("Status", items.get("State"))
                if status is not None:
                    latest_nav_status = {
                        "status": status,
                        "timestamp": time.time(),
                        # 갱신 시점을 monotonic으로 기록 → NAV_STATUS 응답 시 age 계산에 사용.
                        # 백엔드와 시계가 어긋나도 age는 로봇 NOS 한 머신 안에서만 계산된다.
                        "mono": time.monotonic(),
                        # 다지점 작업이면 지금 향하는 점 번호(1부터) — 관제가 경유점 통과를 이것으로 본다
                        "value": items.get("Value"),
                        "error_code": items.get("ErrorCode"),
                    }
                    _track_nav_progress(status)
        except socket.timeout:
            pass
        except Exception as e:
            print("[NAV POLL ERR]", e)

        time.sleep(1)


def battery_poll_loop():
    """heartbeat(Type=100, Command=100) 전송 → 로봇이 push하는 패킷 중
    - Type=1002, Command=5 에서 BatteryStatus 추출
    - Type=1002, Command=6 (Obtain Basic Status)에서 Sleep / PowerManagement 추출
    - Type=1002, Command=3 (Abnormal Status)에서 활성 에러 목록(ErrorList) 추출
    - /CHARGE_STATUS 패킷에서 충전 상태 추출
    """
    global latest_battery, latest_charge_state, latest_device_temp, latest_basic_status
    global latest_basic_status_received_at
    global latest_abnormal_status
    bat_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    bat_sock.settimeout(1.0)

    print("📡 배터리 폴링 시작 (heartbeat 방식)")

    while True:
        try:
            # heartbeat 전송
            asdu = {
                "PatrolDevice": {
                    "Type": 100,
                    "Command": 100,
                    "Time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "Items": {}
                }
            }
            asdu_bytes = json.dumps(asdu).encode("utf-8")
            asdu_len = len(asdu_bytes)
            header = struct.pack(
                "<4BHHB7B",
                0xEB, 0x91, 0xEB, 0x90,
                asdu_len, 1, 0x01,
                *(0x00,) * 7
            )
            bat_sock.sendto(header + asdu_bytes, (ROBOT_IP, ROBOT_PORT))

            # 로봇이 여러 패킷을 push하므로 2초간 연속 수신
            deadline = time.time() + 2.0
            while time.time() < deadline:
                bat_sock.settimeout(max(0.1, deadline - time.time()))
                try:
                    data, addr = bat_sock.recvfrom(8192)
                    try:
                        msg = json.loads(data.decode())
                    except:
                        msg = json.loads(data[16:].decode())

                    pd = msg.get("PatrolDevice", {})
                    ptype = pd.get("Type")
                    pcmd = pd.get("Command")
                    items = pd.get("Items", {})

                    if ptype == 1002 and pcmd == 5:
                        battery = items.get("BatteryStatus", {})
                        if battery:
                            latest_battery = battery
                            print(f"✅ [BAT] 업데이트: {list(battery.keys())}")
                        else:
                            print(f"⚠️ [BAT] BatteryStatus 키 없음. Items={items}")

                        device_temp = items.get("DeviceTemperature", {})
                        if device_temp:
                            latest_device_temp = device_temp

                    elif ptype == 1002 and pcmd == 6:
                        # Obtain Basic Status — items.BasicStatus에 중첩돼 있음
                        # - Sleep: 0=On, 1/2=Sleep/Off
                        # - PowerManagement: 0=regular dual battery, 1=single battery
                        # - MotionState: 1=Stand, 4=Sit
                        basic = items.get("BasicStatus", {}) or {}
                        with latest_basic_status_lock:
                            latest_basic_status = {
                                "Sleep": basic.get("Sleep"),
                                "PowerManagement": basic.get("PowerManagement"),
                                "MotionState": basic.get("MotionState"),
                                "timestamp": time.time(),
                            }
                            latest_basic_status_received_at = time.time()
                        print(f"[BASIC STATUS] 갱신: Sleep={basic.get('Sleep')} PowerManagement={basic.get('PowerManagement')} MotionState={basic.get('MotionState')}")

                    elif ptype == 1002 and pcmd == 3:
                        # Obtain Abnormal Status — 활성 에러 객체 배열.
                        # 빈 배열/누락 = 정상(또는 해소됨). receiver는 최신값 보관·릴레이만
                        # 담당하고, 변화 감지/로깅은 백엔드 runtime에서 처리한다.
                        latest_abnormal_status = items.get("ErrorList", []) or []
                        # 669 는 정상일 때도 [{'errorCode': 0, 'component': 0}] 를 1분에 약 98번 보낸다 — 0 이 아닌 것만 찍는다(11-05)
                        real_errors = [e for e in latest_abnormal_status
                                       if not (isinstance(e, dict) and e.get("errorCode") == 0)]
                        if real_errors:
                            print(f"⚠️ [ABNORMAL] 활성 에러 {len(real_errors)}건: {real_errors}")

                except socket.timeout:
                    break

        except socket.timeout:
            pass
        except Exception as e:
            print("[BAT POLL ERR]", e)

        time.sleep(3)


def basic_status_poll_loop():
    """BasicStatus(1002/6)만 별도 주기로 수집해 캐시 신선도를 보장한다.

    배터리 heartbeat 수신 루프와 소켓을 분리한다. 로봇이 여러 ASDU 응답을
    burst로 보내거나 다른 polling 응답이 섞여도 BasicStatus 캐시가 오래된
    값으로 남지 않도록 한다.
    """
    global latest_basic_status, latest_basic_status_received_at
    print("📡 BasicStatus 전용 polling 시작 (5초)")
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.6)
        try:
            asdu = json.dumps({
                "PatrolDevice": {
                    "Type": 100,
                    "Command": 100,
                    "Time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "Items": {},
                }
            }).encode("utf-8")
            header = struct.pack(
                "<4BHHB7B", 0xEB, 0x91, 0xEB, 0x90,
                len(asdu), 1, 0x01, *(0x00,) * 7
            )
            sock.sendto(header + asdu, (ROBOT_IP, ROBOT_PORT))
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    data, _ = sock.recvfrom(16384)
                except socket.timeout:
                    continue
                raw = data[16:] if len(data) >= 16 else data
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except Exception:
                    continue
                pd = msg.get("PatrolDevice", {})
                if pd.get("Type") != 1002 or pd.get("Command") != 6:
                    continue
                basic = (pd.get("Items") or {}).get("BasicStatus") or {}
                now = time.time()
                with latest_basic_status_lock:
                    latest_basic_status = {
                          "Sleep": basic.get("Sleep"),
                          "PowerManagement": basic.get("PowerManagement"),
                          "MotionState": basic.get("MotionState"),
                          "timestamp": now,
                      }
                    latest_basic_status_received_at = now
                print(f"[BASIC STATUS 전용] 갱신: Sleep={basic.get('Sleep')} PowerManagement={basic.get('PowerManagement')} MotionState={basic.get('MotionState')}")
                break
        except Exception as exc:
            print(f"[BASIC STATUS POLL ERR] {exc}")
        finally:
            sock.close()
        time.sleep(5.0)


def charge_status_listener():
    """relay_charge.py → receiver: /CHARGE_STATUS 데이터 수신"""
    global latest_charge_state
    ch_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ch_sock.bind(("127.0.0.1", CHARGE_LOCAL_PORT))
    ch_sock.settimeout(2.0)
    print(f"🔋 충전 상태 수신 대기 (127.0.0.1:{CHARGE_LOCAL_PORT})")

    while True:
        try:
            data, addr = ch_sock.recvfrom(1024)
            msg = json.loads(data.decode("utf-8"))
            state = msg.get("state", 0)
            error_code = msg.get("error_code", 0)
            latest_charge_state = {
                "state": state,
                "error_code": error_code,
                "timestamp": time.time()
            }
            print(f"🔋 [CHARGE] state={state}, error_code={error_code}")
        except socket.timeout:
            continue
        except Exception as e:
            print(f"[CHARGE LISTEN ERR] {e}")


def motion_info_listener():
    """relay_motion.py → receiver: /MOTION_INFO(gait/state) 수신 → latest_motion_info 갱신."""
    global latest_motion_info
    mi_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mi_sock.bind(("127.0.0.1", MOTION_INFO_LOCAL_PORT))
    mi_sock.settimeout(2.0)
    print(f"🦿 모션 상태 수신 대기 (127.0.0.1:{MOTION_INFO_LOCAL_PORT})")

    while True:
        try:
            data, addr = mi_sock.recvfrom(1024)
            latest_motion_info = json.loads(data.decode("utf-8"))
        except socket.timeout:
            continue
        except Exception as e:
            print(f"[MOTION INFO LISTEN ERR] {e}")


def position_poll_loop():
    global latest_position
    pos_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pos_sock.settimeout(1.0)

    print("📡 위치 폴링 시작")

    while True:
        try:
            asdu = {
                "PatrolDevice": {
                    "Type": 1007,
                    "Command": 2,
                    "Time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "Items": {}
                }
            }
            asdu_bytes = json.dumps(asdu).encode("utf-8")
            asdu_len = len(asdu_bytes)
            header = struct.pack(
                "<4BHHB7B",
                0xEB, 0x91, 0xEB, 0x90,
                asdu_len, 1, 0x01,
                *(0x00,) * 7
            )
            pos_sock.sendto(header + asdu_bytes, (ROBOT_IP, ROBOT_PORT))

            data, addr = pos_sock.recvfrom(4096)
            try:
                msg = json.loads(data.decode())
            except:
                msg = json.loads(data[16:].decode())

            pd = msg.get("PatrolDevice", {})
            if pd.get("Type") == 1007 and pd.get("Command") == 2:
                items = pd.get("Items", {})
                latest_position = {
                    "x": items.get("PosX", 0.0),
                    "y": items.get("PosY", 0.0),
                    "yaw": items.get("Yaw", 0.0),
                    # 측위 상태 — 0=정상, 그 외=위치 상실/이상(2026-09-29 실측: NOS 상실 중 1).
                    # 좌표는 상실 중에도 마지막 값으로 계속 오므로, 좌표만 보고 믿으면 안 된다.
                    "location": items.get("Location"),
                    "timestamp": time.time(),
                    # 로봇 응답 시각(monotonic). 응답이 끊겨도 위 좌표는 남으므로, 보낼 때 age 로 바꿔 싣는다.
                    "mono": time.monotonic(),
                }
        except socket.timeout:
            pass
        except Exception as e:
            print("[POS POLL ERR]", e)

        time.sleep(POS_POLL_SEC)


def _position_payload():
    """POSITION 응답 — 마지막 로봇 응답 뒤 경과(age, 초)를 싣는다(NAV_STATUS 와 같은 방식).

    로봇 응답이 끊겨도 latest_position 은 마지막 값을 유지하므로, 관제가 이 age 로 오래된 좌표를 가린다.
    시계 오프셋과 무관하도록 monotonic 으로 잰다. 아직 받은 적이 없으면 age=None.
    """
    payload = dict(latest_position)
    mono = payload.pop("mono", 0.0)
    payload["age"] = round(time.monotonic() - mono, 3) if mono > 0 else None
    return payload


# ============================================================
# 공통 ASDU 송신 함수
# ============================================================
def send_asdu(type_id, command_id, items=None, idx=1):
    if items is None:
        items = {}

    asdu = {
        "PatrolDevice": {
            "Type": type_id,
            "Command": command_id,
            "Time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "Items": items
        }
    }

    asdu_bytes = json.dumps(asdu).encode("utf-8")
    asdu_len = len(asdu_bytes)

    header = struct.pack(
        "<4BHHB7B",
        0xEB, 0x91, 0xEB, 0x90,
        asdu_len,
        idx,
        0x01,
        *(0x00,) * 7
    )

    packet = header + asdu_bytes
    sock.sendto(packet, (ROBOT_IP, ROBOT_PORT))
    print("[SEND]", asdu)


# ============================================================
# 제어 모드 변경
# ============================================================
def set_control_mode(mode=0):
    send_asdu(1101, 5, {"Mode": mode})
    print(f"[MODE] → {mode}")
    time.sleep(0.1)


def wait_stand_ready():
    print("⏳ 로봇이 서기 동작 완료할 때까지 대기...")
    time.sleep(1.8)
    set_control_mode(0)
    print("✅ 로봇 이동 가능 상태 (Mode=0)")

# ============================================================
# 기본 동작 함수
# ============================================================
current_speed = 0.5
turn_speed = 0.5

def stand_robot():
    set_control_mode(0)
    send_asdu(2, 22, {"MotionParam": 1})
    print("▶ STAND")
    wait_stand_ready()

def sit_robot():
    set_control_mode(0)
    send_asdu(2, 22, {"MotionParam": 4})
    print("▶ SIT")
    time.sleep(1.0)
    set_control_mode(0)


def move_forward():
    set_control_mode(0)
    send_asdu(2, 21, {"X": current_speed, "Y": 0, "Z": 0, "Roll": 0, "Pitch": 0, "Yaw": 0})
    print("▶ FORWARD")

def move_backward():
    set_control_mode(0)
    send_asdu(2, 21, {"X": -current_speed, "Y": 0,"Z": 0, "Roll": 0, "Pitch": 0, "Yaw": 0})
    print("▶ BACKWARD")

def move_left():
    set_control_mode(0)
    send_asdu(2, 21, {"X": 0.000000, "Y": current_speed, "Z": 0.000000, "Roll": 0.000000, "Pitch": 0.000000, "Yaw": 0.000000})
    print("▶ LEFT")

def move_right():
    set_control_mode(0)
    send_asdu(2, 21, {"X": 0.000000, "Y": -current_speed, "Z": 0.000000, "Roll": 0.000000, "Pitch": 0.000000, "Yaw": 0.000000})
    print("▶ RIGHT")

def turn_left():
    set_control_mode(0)
    send_asdu(2, 21, {"X": 0.1, "Y": 0, "Z": 0, "Roll": 0, "Pitch": 0, "Yaw": turn_speed})
    print("▶ TURN LEFT")

def turn_right():
    set_control_mode(0)
    send_asdu(2, 21, {"X": 0.1, "Y": 0, "Z": 0, "Roll": 0, "Pitch": 0, "Yaw": -turn_speed})
    print("▶ TURN RIGHT")

def stop_robot():
    send_asdu(2, 21, {"X": 0, "Y": 0, "Yaw": 0})
    print("▶ STOP")

def set_slow():
    global current_speed, turn_speed
    current_speed = 0.3
    turn_speed = 0.3
    print("▶ SPEED = SLOW")

def set_normal():
    global current_speed, turn_speed
    current_speed = 0.6
    turn_speed = 0.6
    print("▶ SPEED = NORMAL")

def set_fast():
    global current_speed, turn_speed
    current_speed = 1.0
    turn_speed = 1.0
    print("▶ SPEED = FAST")

def set_wake():
    send_asdu(1101, 6, {"Sleep": False, "Auto": False, "Time": 30})

def cancel_nav():
    """현재 네비게이션 취소 (Type=1004, Command=1)"""
    send_asdu(1004, 1, {})
    print("[NAV] 네비게이션 취소 전송")

def _send_packet_to_robot_tcp(packet, timeout=3.0):
    """ASDU 패킷을 로봇 본체에 TCP(30001)로 보장 전송. 성공 True / 실패 False.

    hop ②(receiver↔로봇 본체)의 신뢰 전송 경로. 명령 1건당 짧은 연결을 열고
    닫는다(NAV는 웨이포인트당 1회로 저빈도). 실패하면 호출자가 UDP(30000)로 폴백한다.
    """
    try:
        with socket.create_connection((ROBOT_IP, ROBOT_TCP_PORT), timeout=timeout) as tcp:
            tcp.sendall(packet)
        return True
    except Exception as e:
        print(f"[NAV TCP] 로봇({ROBOT_IP}:{ROBOT_TCP_PORT}) TCP 전송 실패: {e}")
        return False


# NAV 웨이포인트(1003/1) Speed 필드 — 프로토콜 1.4.4: Normal=0 / Low=1 / High=2.
# 안내 저속은 관제가 "speed":"slow" 로 요청한다.
# 매뉴얼 주: Auto NavMode(아래 NavMode=1)는 Flat 보행 + Low speed 만 지원한다.
# 그래서 저속(1)은 Auto 에서 공식 지원되는 조합이고, 보통(0)은 기존 동작 그대로 둔다.
# 고속(2)은 안내 속도 '고속'용이다. Auto 에서 지원 범위 밖일 수 있어 실기 확인 전이다(2026-10-19).
NAV_SPEED_CODES = {
    "normal": 0,
    "slow": 1,
    "fast": 2,
}


# ── NAV 전 보행 전환 대기 ─────────────────────────────────
# 모드 리셋(Mode 0)은 내비 모드인 로봇을 기본 보행(0x1001)으로 바꾼다. 이 전환이 끝나기 전에 NAV 가
# 도착하면 로봇은 '현재 0x3002 라 전환 불필요' 로 출발했다가, 늦게 끝난 0x1001 전환에 내비 모드를
# 빼앗겨 제자리에서 취소된다(0xa328). 보통 1초 안에 끝나지만 가끔 ~10초 걸린다
# (본체 로그 2026-09-30 18:25, 2026-10-01 10:16: waitForGaitChange->0x1001 timeout 3회).
# 그래서 보고된 보행이 0x1001 이 된 뒤에 NAV 를 보낸다 — 그러면 NAV 가 직접 0x3002 로 전환한다.
NAV_GAIT_SETTLE_SEC = _env_int("NAV_GAIT_SETTLE_MS", 12000) / 1000.0
# 관제는 NAV ACK 를 6초 기다린다(sender._tcp_send_nav recv_timeout). 그 안에 답해야 UDP 로 중복 전송하지 않는다.
NAV_ACK_WAIT_SEC = 4.5
_NAV_BASIC_GAIT = 0x1001
_NAV_GAIT = 0x3002
# 관제가 통과 지점을 걷는 중에 넘긴 NAV("continue": true)는 모드 리셋을 생략한다. 리셋은 로봇을
# 기본 보행으로 바꿔 세우므로(실측 2026-10-01~02: 전환 후 NAV 전달까지 중앙값 3.1초) 경유지마다
# 멈췄다 제자리에서 꺾는다. 이미 내비 보행(0x3002)이면 리셋 없이 새 목표만 보낸다 — 리셋을 안 하니
# 0xa328(늦은 보행 전환이 내비 모드를 빼앗음) 경합도 생기지 않는다. 보행을 확인하지 못하면 리셋한다.
NAV_CONTINUE_SKIP_RESET = _env_int("NAV_CONTINUE_SKIP_RESET", 0) == 1

_nav_seq = 0                       # 최신 NAV 번호 — 대기 중 새 NAV·정지·취소가 오면 이전 NAV 는 버린다
_nav_seq_lock = threading.Lock()
_nav_send_lock = threading.Lock()  # NAV 는 한 번에 하나만 로봇에 보낸다


def _next_nav_seq():
    global _nav_seq
    with _nav_seq_lock:
        _nav_seq += 1
        return _nav_seq


def _nav_superseded(seq):
    return seq is not None and seq != _nav_seq


def query_basic_status(timeout=1.5):
    """로봇 Basic Status(1002/6) 를 지금 받아 온다. 못 받으면 None.

    battery_poll_loop 의 latest_basic_status 는 최대 5초 묵은 값이라 보행 전환 판단에 못 쓴다.
    heartbeat 를 보낸 소켓으로 로봇이 상태 패킷들을 push 한다.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        asdu = json.dumps({"PatrolDevice": {"Type": 100, "Command": 100,
                                            "Time": time.strftime("%Y-%m-%d %H:%M:%S"),
                                            "Items": {}}}).encode("utf-8")
        header = struct.pack("<4BHHB7B", 0xEB, 0x91, 0xEB, 0x90, len(asdu), 1, 0x01, *(0x00,) * 7)
        s.sendto(header + asdu, (ROBOT_IP, ROBOT_PORT))
        deadline = time.time() + timeout
        while time.time() < deadline:
            s.settimeout(max(0.05, deadline - time.time()))
            try:
                data, _ = s.recvfrom(16384)
            except socket.timeout:
                break
            try:
                msg = json.loads(data[16:].decode("utf-8"))
            except Exception:
                continue
            pd = msg.get("PatrolDevice", {})
            if pd.get("Type") == 1002 and pd.get("Command") == 6:
                return (pd.get("Items") or {}).get("BasicStatus") or {}
        return None
    except Exception as e:
        print(f"[NAV GAIT] 상태 조회 실패: {e}")
        return None
    finally:
        s.close()


def _reset_mode_for_nav(seq):
    """NAV 전 모드 리셋. 보행 전환이 끝날 때까지 기다린다. 대기 중 새 명령이 오면 False(보내지 말 것)."""
    before = query_basic_status()
    set_control_mode(0)
    if before is None or before.get("Gait") == _NAV_BASIC_GAIT:
        # 이미 기본 보행이면 리셋이 보행을 바꾸지 않는다. 상태를 못 받으면 판단할 수 없으니 예전처럼 바로 보낸다
        # (상태 채널이 죽었는데 매번 12초씩 붙잡지 않게).
        time.sleep(0.3)
        return not _nav_superseded(seq)

    t0 = time.time()
    while time.time() - t0 < NAV_GAIT_SETTLE_SEC:
        if _nav_superseded(seq):
            print(f"[NAV GAIT] 대기 중 새 명령 — 이전 NAV 버림")
            return False
        st = query_basic_status()
        if st and st.get("Gait") == _NAV_BASIC_GAIT:
            waited = time.time() - t0
            if waited > 1.5:
                print(f"[NAV GAIT] 기본 보행 전환 {waited:.1f}s 걸림 — 이제 NAV 전송")
            return not _nav_superseded(seq)
        time.sleep(0.2)
    print(f"[NAV GAIT] {NAV_GAIT_SETTLE_SEC:.0f}s 안에 기본 보행 전환을 확인하지 못함 — 그대로 NAV 전송")
    return not _nav_superseded(seq)


def _submit_nav_job(label, send):
    """NAV 송신 send(seq) 를 별도 스레드로 돌린다. 로봇에 보냈거나 버려지면 set 되는 Event 를 돌려준다."""
    global _last_nav_submit_mono, _nav_live, _nav_seen_running
    _last_nav_submit_mono = time.monotonic()
    _nav_live, _nav_seen_running = True, False
    seq = _next_nav_seq()
    done = threading.Event()
    done.success = False

    def run():
        try:
            with _nav_send_lock:
                if _nav_superseded(seq):
                    print(f"[NAV] {label} 전송 전 새 명령 도착 — 버림")
                    return
                done.success = send(seq) is not False
        except Exception as e:
            print(f"[NAV ERR] {label}: {e}")
        finally:
            done.set()

    threading.Thread(target=run, daemon=True, name=f"nav-{seq}").start()
    return done


def submit_nav(idx, x, y, yaw, speed=None, map_id=None, continuing=False):
    return _submit_nav_job(f"WP{idx}", lambda seq: send_nav_command(
        idx, x, y, yaw, speed, map_id=map_id, seq=seq, continuing=continuing))


def submit_nav_multi(points, map_id=None, continuing=False):
    return _submit_nav_job(f"MULTI({len(points)}점)", lambda seq: send_nav_multi_command(
        points, map_id=map_id, seq=seq, continuing=continuing))


# ── 종료 후 Regular 모드 복귀 ─────────────────────────────
# 관제는 주행이 끝나면 MODE_REGULAR 를 보낸다. NAV 출발로 펌웨어가 Navigation 모드가 되는데, /GAIT 기본 보행만으로는
# 모드가 돌아오지 않아 컨트롤러에 Navigation 으로 남는다. Mode 0 은 모드와 보행(0x1001)을 함께 되돌린다.
# 복귀 명령이 바로 뒤 NAV 보다 늦게 닿으면 늦은 0x1001 전환이 출발한 내비를 빼앗는다(0xa328). 그 NAV 가 아직 살아 있으면
# (취소·정지도, 로봇의 작업 완료도 없었으면) 버린다 — 그 NAV 의 출발 리셋이 이미 Mode 0 을 보냈고, 주행이 끝나면 관제가 다시 보낸다.
# 정지(CANCEL_NAV 뒤)·도착(작업 완료 뒤)은 NAV 가 끝난 뒤라 출발 직후여도 보낸다.
MODE_REGULAR_NAV_GUARD_SEC = _env_int("MODE_REGULAR_NAV_GUARD_MS", 5000) / 1000.0
_last_nav_submit_mono = 0.0
_nav_live = False          # 마지막 NAV 가 아직 끝나지 않았다(취소·정지·작업 완료 전)
_nav_seen_running = False  # 마지막 NAV 이후 로봇이 실행 중(status!=0)이라고 보고했다


def _mark_nav_ended():
    global _nav_live
    _nav_live = False


def _track_nav_progress(status):
    """로봇 내비 상태(1초 주기)로 마지막 NAV 가 끝났는지 본다 — 실행 중을 본 뒤 0 이면 작업 완료."""
    global _nav_seen_running
    if not _nav_live:
        return
    if status != 0:
        _nav_seen_running = True
    elif _nav_seen_running:
        _mark_nav_ended()


def restore_regular_mode():
    """Regular 모드(Mode 0) 복귀. 끝나지 않은 NAV 가 막 보내졌거나 보내는 중이면 보내지 않는다."""
    since_nav = time.monotonic() - _last_nav_submit_mono
    if _nav_live and (since_nav < MODE_REGULAR_NAV_GUARD_SEC or _nav_send_lock.locked()):
        print(f"[MODE] Regular 복귀 생략 — 진행 중인 NAV({since_nav:.1f}s 전)")
        return
    # set_control_mode 는 0.1s 쉰다 — 메인 수신 loop 에서 부르므로 바로 보낸다
    send_asdu(1101, 5, {"Mode": 0})
    print("[MODE] → 0 (Regular 복귀)")


def _already_navigating():
    """이어가기 NAV 에서 모드 리셋을 생략해도 되는가 — 지금 내비 보행(0x3002)인지 바로 확인한다."""
    st = query_basic_status()
    return bool(st) and st.get("Gait") == _NAV_GAIT


def send_nav_command(idx, x, y, yaw, speed=None, map_id=None, seq=None, continuing=False):
    if continuing and NAV_CONTINUE_SKIP_RESET and _already_navigating():
        print(f"[NAV] WP{idx} 이어가기 — 내비 보행 중이라 모드 리셋 생략")
        if _nav_superseded(seq):
            return False
    # 모드 리셋 → 보행 전환 완료 확인 → 새 명령 전송
    elif not _reset_mode_for_nav(seq):
        return False

    asdu = {
        "PatrolDevice": {
            "Type": 1003,
            "Command": 1,
            "Time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "Items": {
                "Value": idx,
                "PosX": x,
                "PosY": y,
                "PosZ": 0.0,
                "AngleYaw": yaw,
                "PointInfo": 2,
                "Gait": 0x3002,
                "Speed": NAV_SPEED_CODES.get(speed, NAV_SPEED_CODES["normal"]),
                "Manner": 0,
                "ObsMode": 0,
                "NavMode": 1,
            }
        }
    }
    if map_id is not None:
        asdu["PatrolDevice"]["Items"]["MapID"] = int(map_id)

    _send_nav_asdu(asdu, idx, f"WP{idx} → X:{x}, Y:{y}, Yaw:{yaw}, Speed:{speed or 'normal'}")
    return True


def nav_multi_bytes(asdu):
    """다지점 ASDU 를 보낼 꼴(공백 없음)로 만든 바이트."""
    return json.dumps(asdu, separators=NAV_MULTI_SEPARATORS).encode()


def _send_nav_asdu(asdu, idx, what, asdu_bytes=None):
    """1003 ASDU 를 로봇 본체에 보낸다 — hop ②: TCP(30001) 보장 전송, 실패 시 UDP(30000) 폴백."""
    asdu_bytes = asdu_bytes if asdu_bytes is not None else json.dumps(asdu).encode()
    header = struct.pack(
        "<4BHHB7B",
        0xEB, 0x91, 0xEB, 0x90,
        len(asdu_bytes),
        idx,
        0x01,
        *(0x00,) * 7
    )
    packet = header + asdu_bytes
    if _send_packet_to_robot_tcp(packet):
        print(f"[NAV] (TCP) Send {what}")
    else:
        sock.sendto(packet, (ROBOT_IP, ROBOT_PORT))
        print(f"[NAV] (UDP 폴백) Send {what}")


# ── 다지점 NAV ─────────────────────────────────────────────
# 1003/1 Items 를 {"LoopCnt", "Item": [점…]} 로 보내면 basic_server 가 多点任务下发 로 받아 점을 차례로 간다
# (2026-10-20 1F 실기: 점 사이 전환 0.1~0.2s, 모드 리셋·보행 전환 없음, 직선·자율·속도 3단계 모두 통과).
# 경유점(PointInfo 0)도 0.1m 안까지 들어가 감속하고, 끝점(1)은 정확히 서서 다음 명령을 기다린다.
# LoopCnt 는 반복 횟수다 — 1 이면 끝난 뒤 30초 쉬고 한 바퀴 더 돌며, 그 대기 중엔 취소(1004)가 안 먹는다(실측).
# 관제 값과 상관없이 늘 0(한 번)으로 보낸다.
NAV_MULTI_MAX_POINTS = 64
NAV_MODE_CODES = {"straight": 0, "auto": 1}
# 본체 basic_server 는 약 1KB 넘는 1003 을 해석하지 않는다(10-27: 4점 976B 수락, 7점 1,612B 무시). 그 사이는 미시험이라
# 받은 것이 확인된 976B 를 넘으면 보내지 않고 거절한다 — 관제는 거절을 받으면 그 구간을 단일 NAV 로 보낸다.
# 다지점은 공백 없는 형식·소수 3자리(1mm)로 보내 5점이 약 845B 에 들어간다(10-29).
NAV_MULTI_MAX_BYTES = _env_int("NAV_MULTI_MAX_BYTES", 976)
NAV_MULTI_SEPARATORS = (",", ":")


def parse_nav_multi(msg):
    """관제 NAV_MULTI 메시지 → (점 목록, map_id). 형식이 틀리면 ValueError."""
    points = msg.get("points")
    if not isinstance(points, list) or not 1 <= len(points) <= NAV_MULTI_MAX_POINTS:
        raise ValueError(f"points 는 1~{NAV_MULTI_MAX_POINTS}개 목록이어야 합니다")
    out = []
    for p in points:
        x, y, yaw = float(p["x"]), float(p["y"]), float(p.get("yaw", 0.0))
        if not all(math.isfinite(v) for v in (x, y, yaw)):
            raise ValueError("좌표에 NaN/inf 가 있습니다")
        mode = p.get("mode", "auto")
        if mode not in NAV_MODE_CODES:
            raise ValueError(f"알 수 없는 mode: {mode}")
        out.append({"x": x, "y": y, "yaw": yaw, "speed": p.get("speed"), "mode": mode})
    map_id = msg.get("map_id")
    return out, (int(map_id) if map_id is not None else None)


def build_nav_multi_asdu(points, map_id=None):
    """다지점 1003 ASDU. 마지막 점만 작업점(1), 나머지는 경유점(0)."""
    items = []
    for i, p in enumerate(points, start=1):
        item = {
            "Value": i,
            "PosX": round(p["x"], 3),
            "PosY": round(p["y"], 3),
            "PosZ": 0.0,
            "AngleYaw": round(p["yaw"], 3),
            "PointInfo": 1 if i == len(points) else 0,
            "Gait": 0x3002,
            "Speed": NAV_SPEED_CODES.get(p.get("speed"), NAV_SPEED_CODES["normal"]),
            "Manner": 0,
            "ObsMode": 0,
            "NavMode": NAV_MODE_CODES[p["mode"]],
        }
        if map_id is not None:
            item["MapID"] = int(map_id)
        items.append(item)
    return {
        "PatrolDevice": {
            "Type": 1003,
            "Command": 1,
            "Time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "Items": {"LoopCnt": 0, "Item": items},
        }
    }


def send_nav_multi_command(points, map_id=None, seq=None, continuing=False):
    # 첫 점 앞에서 한 번만 모드 리셋한다. 관제가 내비 보행 유지 출발(continue)이고 지금 내비 보행이면
    # 단일 NAV 와 같이 리셋을 생략한다 — 리셋하면 작업지마다 기본 보행 전환·다리 재정렬이 생긴다.
    if continuing and NAV_CONTINUE_SKIP_RESET and _already_navigating():
        print(f"[NAV] MULTI {len(points)}점 이어가기 — 내비 보행 중이라 모드 리셋 생략")
        if _nav_superseded(seq):
            return False
    elif not _reset_mode_for_nav(seq):
        return False
    last = points[-1]
    asdu = build_nav_multi_asdu(points, map_id)
    _send_nav_asdu(asdu, 1,
                   f"MULTI {len(points)}점 → 끝 X:{last['x']}, Y:{last['y']}, "
                   f"Mode:{last['mode']}, Speed:{last.get('speed') or 'normal'}",
                   asdu_bytes=nav_multi_bytes(asdu))
    return True


def _drain_mapping_sock():
    """이전 요청에 늦게 도착한 relay 응답을 버린다 — 안 버리면 다음 요청의 답으로 잘못 읽힌다."""
    mapping_sock.setblocking(False)
    try:
        while True:
            mapping_sock.recvfrom(65535)
    except (BlockingIOError, OSError):
        pass
    finally:
        mapping_sock.settimeout(1.0)


def get_mapping_data(action):
    """relay_map.py에 로컬 UDP로 매핑 데이터 요청.

    mapping_sock 을 공유하므로 _mapping_executor(단일 스레드)에서만 호출한다.
    """
    _drain_mapping_sock()
    try:
        mapping_sock.sendto(
            json.dumps({"action": action}).encode("utf-8"),
            ("127.0.0.1", MAPPING_LOCAL_PORT)
        )
        data, _ = mapping_sock.recvfrom(65535)
        return data
    except socket.timeout:
        return b'{}'
    except Exception as e:
        print(f"[MAPPING ERR] {e}")
        return b'{}'


hold_move = True

def hold_motion(func, duration=1.0, interval=0.1):
    global hold_move
    hold_move = True

    t = time.time()
    while time.time() - t < duration and hold_move:
        func()
        time.sleep(interval)
    stop_robot()


_flash_state = {"Front": 0, "Back": 0}

def set_flash(pos, status):
    key = "Front" if pos == "front" else "Back"
    _flash_state[key] = 1 if status == "on" else 0
    send_asdu(1101, 2, dict(_flash_state))
    print(f"▶ flash {_flash_state}")


# ============================================================
# 보행(gait) 전환 — relay_motion.py 경유 ROS2 /GAIT 발행
# ============================================================
# Standard 모드 보행 (수동 원격 제어). Agile(0x300X)은 네비 주행용으로 별도.
GAIT_BASIC = 0x1001          # 기본 (서서 이동)
GAIT_HIGH_OBSTACLE = 0x1002  # 고장애물
GAIT_STAIR = 0x1003          # 계단
GAIT_POSTURE = 0xF001        # 자세

def send_motion_cmd(cmd, value):
    """relay_motion.py(127.0.0.1:50003)로 보행/상태 전환 명령 전달."""
    payload = json.dumps({"cmd": cmd, "value": int(value)}).encode("utf-8")
    motion_sock.sendto(payload, ("127.0.0.1", MOTION_RELAY_PORT))
    print(f"[MOTION] → relay_motion: cmd={cmd}, value=0x{int(value):04x}")



# ============================================================
# PC → 로봇 UDP 명령 수신 쓰레드
# ============================================================
def robot_sniff_loop():
    """로봇(30000)에서 오는 모든 패킷을 수신하여 로깅 (디버그용)"""
    sniff_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sniff_sock.bind(("0.0.0.0", 30002))
    sniff_sock.settimeout(1.0)
    print("🔍 [SNIFF] 로봇 응답 모니터 시작 (port 30002)")

    while True:
        try:
            data, addr = sniff_sock.recvfrom(8192)
            try:
                msg = json.loads(data.decode("utf-8"))
            except:
                msg = json.loads(data[16:].decode("utf-8"))
            pd = msg.get("PatrolDevice", {})
            t = pd.get("Type")
            c = pd.get("Command")
            print(f"🔍 [SNIFF] from {addr} | Type={t}, Command={c} | {json.dumps(msg, ensure_ascii=False)[:200]}")
            if t == 2101:
                print(f"🎯 [SNIFF] ★ INIT_POSE 응답 감지! Items={pd.get('Items', {})}")
        except socket.timeout:
            continue
        except Exception as e:
            print(f"[SNIFF ERR] {e}")


# ============================================================
# UDP 수신 핸들러 (멀티스레드)
#
# - FAST: 즉시 응답해야 하는 폴링/빠른 명령 → 메인 loop에서 직접 처리
# - SLOW: hold_motion / wait_stand_ready / NAV ASDU / INIT_POSE 등
#         수백 ms~수 초 block 하는 명령 → 워커 스레드에 위임
#         (하나의 느린 명령이 폴링 응답을 막지 않도록 함)
# ============================================================

_FAST_RESPONSE_ACTIONS = {
    "PING", "POSITION", "STATUS", "NAV_STATUS",
    "STOP", "CANCEL_NAV", "SLOW", "NORMAL", "FAST", "WAKE",
    "FRONTON", "FRONTOFF", "REARON", "REAROFF",
    "GAIT_BASIC", "GAIT_HIGH_OBSTACLE", "GAIT_STAIR", "GAIT_POSTURE",
    "POSTURE", "MODE_REGULAR",
}

_SLOW_CMD_ACTIONS = {
    "UP", "DOWN", "LEFT", "RIGHT", "LEFTTURN", "RIGHTTURN",
    "STAND", "SIT", "NAV", "NAV_MULTI", "INIT_POSE",
}

# 대기 중인 NAV 를 무효로 만드는 명령 — 정지·취소·수동 조작 뒤에 묵은 NAV 가 출발하면 안 된다.
# STAND 는 넣지 않는다: 관제가 NAV 직전(안내 기립)과 일시정지(255) 중에도 보낸다.
_NAV_ABORT_ACTIONS = {
    "STOP", "CANCEL_NAV", "SIT",
    "UP", "DOWN", "LEFT", "RIGHT", "LEFTTURN", "RIGHTTURN",
}

_cmd_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cmd-worker")

# 매핑/스캔 데이터 요청 — relay_map 응답을 최대 1초 기다린다. 메인 loop 에서 처리하면
# relay 가 없을 때 요청마다 1초씩 막혀 STATUS/POSITION/NAV_STATUS 가 굶는다
# (2026-09-29: 좀비 스캔 요청으로 수신 큐 177KB, 관제 폴링 0/60).
# mapping_sock 을 공유하므로 단일 스레드로 직렬화하고, 밀려 있으면 쌓지 않고 빈 응답한다.
_MAPPING_ACTIONS = {
    "MAPPING_ODOM", "MAPPING_CLOUD", "MAPPING_ALIGNED", "LIO_ALIGNED", "LOC_BODY",
}
_MAPPING_MAX_PENDING = 2
_mapping_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mapping-worker")
_mapping_pending = 0
_mapping_pending_lock = threading.Lock()


def _read_boot_id():
    """NOS 커널 부팅 번호. receiver 만 재시작하면 그대로고 NOS 가 재부팅하면 바뀐다(관제 재부팅 판정용)."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
            return f.read().strip() or None
    except OSError:
        return None


BOOT_ID = _read_boot_id()


def _basic_status_snapshot():
    with latest_basic_status_lock:
        payload = dict(latest_basic_status)
        received_at = latest_basic_status_received_at
    age = time.time() - received_at if received_at > 0 else None
    payload["receiver_age"] = age
    payload["receiver_stale"] = age is None or age > 15.0
    payload["BootId"] = BOOT_ID
    return payload


def _send_status_udp(addr):
    """STATUS는 지연 민감한 polling 채널이므로 전용 UDP 소켓으로 응답한다.

    수신 루프의 server_sock을 응답에도 공유하면, 상태 payload 직렬화/전송 문제가
    수신 루프 예외와 섞여 원인 파악이 어려워진다. 응답 소켓을 분리해 STATUS
    응답 자체가 항상 UDP로 반환되도록 하고, 실패 원인을 로그에 남긴다.
    """
    payload = {
        "BatteryStatus": latest_battery,
        "ChargeStatus": latest_charge_state,
        "DeviceTemperature": latest_device_temp,
        "BasicStatus": _basic_status_snapshot(),
        "MotionInfo": latest_motion_info,
        "AbnormalStatus": latest_abnormal_status,
        "timestamp": time.time(),
    }
    try:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        # 관제 방화벽/ACL이 receiver의 고정 UDP 포트(40000)를 기준으로 허용되어
        # 있으므로, 임시 송신 포트를 쓰지 않고 수신 소켓의 포트에서 응답한다.
        server_sock.sendto(data, addr)
        print(f"[UDP STATUS] 응답 전송: {addr} bytes={len(data)}")
    except Exception as exc:
        print(f"[UDP STATUS ERR] addr={addr}: {exc}")


def _handle_mapping(action, addr):
    global _mapping_pending
    try:
        server_sock.sendto(get_mapping_data(action), addr)
    except Exception as e:
        print(f"[MAPPING CMD ERR] action={action}: {e}")
    finally:
        with _mapping_pending_lock:
            _mapping_pending -= 1


def _submit_mapping(action, addr):
    """밀림 방지 — 대기가 한도 이상이면 relay 를 기다리지 않고 즉시 빈 응답."""
    global _mapping_pending
    with _mapping_pending_lock:
        if _mapping_pending >= _MAPPING_MAX_PENDING:
            busy = True
        else:
            _mapping_pending += 1
            busy = False
    if busy:
        server_sock.sendto(b'{}', addr)
        return
    _mapping_executor.submit(_handle_mapping, action, addr)


def _handle_fast(action, msg, addr):
    """메인 loop에서 직접 호출 — 모두 1ms 내 처리되어야 함."""
    if action == "PING":
        server_sock.sendto(json.dumps({
            "status": "ok",
            "receiver": {
                "udp_port": SERVER_UDP_PORT,
                "tcp_port": SERVER_TCP_PORT,
            },
            "robot": {
                "ip": ROBOT_IP,
                "udp_port": ROBOT_PORT,
                "tcp_port": ROBOT_TCP_PORT,
            },
            "timestamp": time.time(),
        }).encode("utf-8"), addr)
    elif action == "POSITION":
        server_sock.sendto(json.dumps(_position_payload()).encode("utf-8"), addr)
    elif action == "STATUS":
        _send_status_udp(addr)
    elif action == "NAV_STATUS":
        # 절대 timestamp 대신, receiver의 monotonic 시계로 계산한 경과시간(age)을 실어 보낸다.
        # 백엔드는 이 age만 보고 stale을 판정하므로 서버-로봇 간 시계 오프셋의 영향을 받지 않는다.
        payload = dict(latest_nav_status)
        mono = latest_nav_status.get("mono", 0.0)
        payload["age"] = (time.monotonic() - mono) if mono > 0 else None
        server_sock.sendto(json.dumps(payload).encode("utf-8"), addr)
    elif action == "STOP": stop_robot()
    elif action == "CANCEL_NAV": cancel_nav()
    elif action == "SLOW": set_slow()
    elif action == "NORMAL": set_normal()
    elif action == "FAST": set_fast()
    elif action == "WAKE": set_wake()
    elif action == "FRONTON": set_flash("front", "on")
    elif action == "FRONTOFF": set_flash("front", "off")
    elif action == "REARON": set_flash("rear", "on")
    elif action == "REAROFF": set_flash("rear", "off")
    elif action == "GAIT_BASIC": send_motion_cmd("gait", GAIT_BASIC)
    elif action == "GAIT_HIGH_OBSTACLE": send_motion_cmd("gait", GAIT_HIGH_OBSTACLE)
    elif action == "GAIT_STAIR": send_motion_cmd("gait", GAIT_STAIR)
    elif action == "GAIT_POSTURE": send_motion_cmd("gait", GAIT_POSTURE)
    elif action == "MODE_REGULAR": restore_regular_mode()
    elif action == "POSTURE":
        # 자세 6축 setpoint — Type 2/21 (Regular 모드 전용). 값 [-1,1] = 최대 기울기 비율.
        # 자세(0xf001) gait 진입 후 사용. 셋포인트라 바뀔 때만 보내면 로봇이 유지한다.
        items = {k: float(msg.get(k, 0.0)) for k in ("X", "Y", "Z", "Roll", "Pitch", "Yaw")}
        send_asdu(2, 21, items)


def _handle_nav(msg, addr):
    """NAV 명령 — send_nav_command 내부에 sleep(0.3) 있음."""
    idx = msg.get("idx", 1)
    x = msg.get("x", 0.0)
    y = msg.get("y", 0.0)
    yaw = msg.get("yaw", 0.0)
    speed = msg.get("speed")
    map_id = msg.get("map_id")
    continuing = bool(msg.get("continue"))
    print(f"[RECV NAV] idx={idx}, x={x}, y={y}, yaw={yaw}, speed={speed}, continue={continuing}")
    done = submit_nav(int(idx), float(x), float(y), float(yaw), speed, map_id=map_id,
                      continuing=continuing)
    done.wait(NAV_ACK_WAIT_SEC)
    ack = json.dumps({"ack": bool(getattr(done, "success", False)), "idx": int(idx)}).encode("utf-8")
    server_sock.sendto(ack, addr)


def _nav_multi_ack(msg):
    """NAV_MULTI 를 받아 로봇에 보내고 ACK dict 를 돌려준다. ACK 의 idx 는 첫 점 번호(1) — 관제 NAV ACK 와 같은 꼴."""
    try:
        points, map_id = parse_nav_multi(msg)
    except (KeyError, TypeError, ValueError) as e:
        print(f"[RECV NAV_MULTI] 형식 오류: {e}")
        return {"ack": False, "idx": 1, "error": str(e)}
    size = len(nav_multi_bytes(build_nav_multi_asdu(points, map_id)))
    if size > NAV_MULTI_MAX_BYTES:
        print(f"[RECV NAV_MULTI] {len(points)}점 {size}B — 본체 한도 {NAV_MULTI_MAX_BYTES}B 초과로 거절")
        return {"ack": False, "idx": 1, "error": f"too_large {size}B > {NAV_MULTI_MAX_BYTES}B"}
    continuing = bool(msg.get("continue"))
    print(f"[RECV NAV_MULTI] {len(points)}점 {size}B, map_id={map_id}, continue={continuing}")
    done = submit_nav_multi(points, map_id=map_id, continuing=continuing)
    done.wait(NAV_ACK_WAIT_SEC)
    return {"ack": bool(getattr(done, "success", False)), "idx": 1, "n": len(points)}


def _handle_nav_multi(msg, addr):
    server_sock.sendto(json.dumps(_nav_multi_ack(msg)).encode("utf-8"), addr)


def _send_tcp_json(conn, payload):
    conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")


def _reply_json(payload, addr=None, conn=None):
    if conn is not None:
        _send_tcp_json(conn, payload)
    else:
        server_sock.sendto(json.dumps(payload).encode("utf-8"), addr)


def _handle_init_pose(msg, addr=None, conn=None):
    """INIT_POSE — 로봇 응답 대기 최대 3초."""
    init_items = msg.get("items", {"PosX": 3.635, "PosY": 0.144, "PosZ": 0.0, "Yaw": -0.042})
    target_ip = msg.get("robot_ip") or ROBOT_IP
    target_port = int(msg.get("robot_port") or ROBOT_PORT)
    print(f"🔧 [INIT_POSE] receiver 경유 수신: {target_ip}:{target_port} | {init_items}")
    init_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    init_sock.settimeout(3.0)
    try:
        asdu = {
            "PatrolDevice": {
                "Type": 2101,
                "Command": 1,
                "Time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "Items": init_items
            }
        }
        asdu_bytes = json.dumps(asdu).encode("utf-8")
        asdu_len = len(asdu_bytes)
        header = struct.pack(
            "<4BHHB7B",
            0xEB, 0x91, 0xEB, 0x90,
            asdu_len, 1, 0x01,
            *(0x00,) * 7
        )
        packet = header + asdu_bytes
        init_sock.sendto(packet, (target_ip, target_port))
        print(f"🔧 [INIT_POSE] 로봇에 전송 완료 ({len(packet)} bytes)")
        resp_data, resp_addr = init_sock.recvfrom(4096)
        try:
            resp = json.loads(resp_data.decode("utf-8"))
        except:
            resp = json.loads(resp_data[16:].decode("utf-8"))
        print(f"✅ [INIT_POSE] 로봇 응답: {resp} (from {resp_addr})")
        # status=ok 는 응답 수신/전달 성공이다. ErrorCode 를 숨기거나 성공으로 변환하지 않는다.
        # 실제 위치 초기화 성공은 관제가 POSITION의 Location=0 및 좌표 안정성으로 검증한다.
        items = ((resp or {}).get("PatrolDevice") or {}).get("Items") or {}
        error_code = items.get("ErrorCode")
        _reply_json({
            "status": "ok",
            "transport_ok": True,
            "error_code": error_code,
            "response": resp,
        }, addr=addr, conn=conn)
    except socket.timeout:
        print("⚠️ [INIT_POSE] 로봇 응답 타임아웃 (3초)")
        _reply_json({"status": "timeout"}, addr=addr, conn=conn)
    except Exception as e:
        print(f"❌ [INIT_POSE] 에러: {e}")
        _reply_json({"status": "error", "msg": str(e)}, addr=addr, conn=conn)
    finally:
        init_sock.close()


def _handle_slow(action, msg, addr):
    """워커 스레드에서 실행 — hold_motion / wait_stand_ready / ASDU 송신 등 block 작업."""
    try:
        if action == "UP": hold_motion(move_forward, duration=1.0)
        elif action == "DOWN": hold_motion(move_backward, duration=1.0)
        elif action == "LEFT": hold_motion(move_left, duration=1.5, interval=0.0001)
        elif action == "RIGHT": hold_motion(move_right, duration=1.5, interval=0.0001)
        elif action == "LEFTTURN": hold_motion(turn_left, duration=0.5)
        elif action == "RIGHTTURN": hold_motion(turn_right, duration=0.5)
        elif action == "STAND": stand_robot()
        elif action == "SIT": sit_robot()
        elif action == "NAV": _handle_nav(msg, addr)
        elif action == "NAV_MULTI": _handle_nav_multi(msg, addr)
        elif action == "INIT_POSE": _handle_init_pose(msg, addr)
    except Exception as e:
        print(f"[SLOW CMD ERR] action={action}: {e}")


def udp_receiver_loop():
    print(f"PC 명령 수신 대기중... (UDP:{SERVER_UDP_PORT})")

    while True:
        try:
            data, addr = server_sock.recvfrom(4096)
            msg = json.loads(data.decode("utf-8"))
            action = msg.get("action")

            print(f"수신: {action}")

            if action in _NAV_ABORT_ACTIONS:
                _next_nav_seq()   # 보행 전환을 기다리는 NAV 가 있으면 보내지 않는다
                _mark_nav_ended()

            if action in _FAST_RESPONSE_ACTIONS:
                _handle_fast(action, msg, addr)
            elif action in _MAPPING_ACTIONS:
                _submit_mapping(action, addr)
            elif action in _SLOW_CMD_ACTIONS:
                # 워커 풀에 위임 — 메인 loop은 즉시 다음 패킷 수신으로 복귀
                _cmd_executor.submit(_handle_slow, action, msg, addr)
            else:
                print(f"[UNKNOWN ACTION] {action}")

        except socket.timeout:
            continue
        except Exception as e:
            print("[UDP Receiver Error]", e)


# ============================================================
# PC → 로봇 TCP 신뢰성 명령 수신 (hop ①)
#
# WiFi(PC↔NOS) 구간 패킷 유실/순단으로 NAV 명령이 사라지는 것을 막기 위한
# 보장 전송 채널. 현재는 NAV만 처리하며, STOP·긴급정지·폴링 등 지연 민감/손실
# 허용 트래픽은 기존 UDP 채널(udp_receiver_loop)을 그대로 사용한다.
#
# 프로토콜: 개행(\n)으로 구분된 JSON 한 줄을 받고, 동일하게 JSON+\n으로 ACK 응답.
# 명령 1건당 짧은 연결(connect→전송→ACK→close)이라 재연결 상태관리가 불필요하다.
# ============================================================
def _handle_tcp_conn(conn, addr):
    """수락된 TCP 연결 1건 처리 — JSON 한 줄 수신 → 명령 실행 → ACK 송신."""
    try:
        conn.settimeout(5.0)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 65536:  # 비정상 과대 입력 방지
                break
        if not buf:
            return

        line = buf.split(b"\n", 1)[0]
        msg = json.loads(line.decode("utf-8"))
        action = msg.get("action")
        print(f"[TCP] 수신: {action} (from {addr})")

        if action == "STATUS":
            _send_tcp_json(conn, {
                "BatteryStatus": latest_battery,
                "ChargeStatus": latest_charge_state,
                "DeviceTemperature": latest_device_temp,
                "BasicStatus": _basic_status_snapshot(),
                "MotionInfo": latest_motion_info,
                "AbnormalStatus": latest_abnormal_status,
            })
        elif action == "POSITION":
            _send_tcp_json(conn, _position_payload())
        elif action == "INIT_POSE":
            _handle_init_pose(msg, conn=conn)
        elif action == "NAV":
            idx = int(msg.get("idx", 1))
            x = float(msg.get("x", 0.0))
            y = float(msg.get("y", 0.0))
            yaw = float(msg.get("yaw", 0.0))
            speed = msg.get("speed")
            map_id = msg.get("map_id")
            continuing = bool(msg.get("continue"))
            print(f"[RECV NAV/TCP] idx={idx}, x={x}, y={y}, yaw={yaw}, speed={speed}, continue={continuing}")
            # 보행 전환을 기다리는 동안에도 관제의 ACK 대기(6초) 안에 답한다 — 늦으면 UDP 로 중복 전송된다.
            # 보통은 로봇에 보낸 뒤 답하고, 전환이 길어지면 대기 중인 채로 답한다.
            done = submit_nav(idx, x, y, yaw, speed, map_id=map_id, continuing=continuing)
            done.wait(NAV_ACK_WAIT_SEC)
            ack = json.dumps({"ack": bool(getattr(done, "success", False)), "idx": idx}).encode("utf-8") + b"\n"
            conn.sendall(ack)
        elif action == "NAV_MULTI":
            _send_tcp_json(conn, _nav_multi_ack(msg))
        else:
            # 신뢰 채널은 NAV 전용. 그 외 액션은 UDP 채널을 쓰도록 거부 응답.
            nack = json.dumps(
                {"ack": False, "error": f"unsupported action: {action}"}
            ).encode("utf-8") + b"\n"
            conn.sendall(nack)
    except Exception as e:
        print(f"[TCP CONN ERR] {addr}: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def tcp_command_loop():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", SERVER_TCP_PORT))
    srv.listen(8)
    print(f"PC 신뢰성 명령 수신 대기중... (TCP:{SERVER_TCP_PORT})")

    while True:
        try:
            conn, addr = srv.accept()
            # 연결마다 워커 풀에 위임 — send_nav_command 내부 sleep(0.3)이
            # accept 루프를 막지 않도록 한다.
            _cmd_executor.submit(_handle_tcp_conn, conn, addr)
        except Exception as e:
            print(f"[TCP ACCEPT ERR] {e}")


# ============================================================
# 실행 시작
# ============================================================
threading.Thread(target=battery_poll_loop, daemon=True).start()
threading.Thread(target=basic_status_poll_loop, daemon=True).start()
threading.Thread(target=position_poll_loop, daemon=True).start()
threading.Thread(target=nav_poll_loop, daemon=True).start()
threading.Thread(target=charge_status_listener, daemon=True).start()
threading.Thread(target=motion_info_listener, daemon=True).start()
threading.Thread(target=udp_receiver_loop, daemon=True).start()
threading.Thread(target=tcp_command_loop, daemon=True).start()
threading.Thread(target=robot_sniff_loop, daemon=True).start()

print("✅ 로봇 제어 시스템 실행 중...")
while True:
    time.sleep(1)
