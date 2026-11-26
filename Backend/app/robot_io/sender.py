"""로봇 NOS의 receiver.py로 JSON 액션 송신.

- send_to_robot: 단순 액션(UP/DOWN/STAND/SIT 등) fire-and-forget (UDP)
- send_nav_to_robot: NAV 명령 — 신뢰 채널(TCP 40001) 우선, 실패 시 UDP 폴백

NAV는 WiFi(PC↔NOS) 구간 유실 시 자율주행 명령이 사라지는 문제가 있어 TCP로
보장 전송한다. STOP·긴급정지·수동 조작 등 지연 민감/손실 허용 명령은 UDP를 유지한다.
"""

import json
import socket

from app.robot_io.config import RECEIVER_IP, RECEIVER_PORT, RECEIVER_TCP_PORT
from app.robot_io import endpoints as ep


def _receiver_addr(robot_id: int | None) -> tuple[str, int]:
    """대상 로봇의 receiver 주소. 미등록이면 env 전역값으로 폴백한다.

    robot_id 를 넘기지 않으면 기존과 동일하게 전역값을 쓴다 — 호출부를 한 번에
    다 고치지 않아도 되도록 한 것이고, 새 호출은 robot_id 를 넘겨야 한다.
    """
    if robot_id is None:
        return (RECEIVER_IP, RECEIVER_PORT)
    e = ep.resolve(robot_id, ep.ROLE_RECEIVER)
    addr = e.udp_addr()
    return addr if addr else (RECEIVER_IP, RECEIVER_PORT)


def _udp_send(
    msg: dict,
    *,
    wait_ack_idx: int | None = None,
    timeout: float = 2.0,
    robot_id: int | None = None,
) -> dict | None:
    """대상 로봇의 receiver로 JSON 메시지 UDP 송신. wait_ack_idx가 있으면 ack 대기."""
    data = json.dumps(msg).encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if wait_ack_idx is not None:
            sock.settimeout(timeout)
        sock.sendto(data, _receiver_addr(robot_id))
        if wait_ack_idx is None:
            return None
        try:
            ack_data, _ = sock.recvfrom(4096)
            return json.loads(ack_data.decode("utf-8"))
        except socket.timeout:
            return None
    finally:
        sock.close()


def send_to_robot(action: str, robot_id: int | None = None) -> None:
    """액션 문자열을 대상 로봇의 receiver로 fire-and-forget 송신.
    action 예: "UP", "DOWN", "LEFT", "RIGHT", "STOP", "STAND", "SIT" 등.
    robot_id 미지정 시 env 전역 receiver 로 나간다(레거시 호출부 호환).
    """
    msg = {"action": action}
    _udp_send(msg, robot_id=robot_id)
    print(f"서버 → 로봇[{robot_id or 'env'}] UDP 송신 완료: {msg}")


def send_posture_to_robot(x: float, y: float, z: float,
                          roll: float, pitch: float, yaw: float,
                          robot_id: int | None = None) -> None:
    """자세(Posture) 제어 — Type 2/21 6축 setpoint를 receiver로 fire-and-forget 송신.
    각 값 [-1,1] = 최대 기울기/이동 비율. (Regular/Standard 모드 전용)
    """
    msg = {"action": "POSTURE", "X": x, "Y": y, "Z": z,
           "Roll": roll, "Pitch": pitch, "Yaw": yaw}
    _udp_send(msg, robot_id=robot_id)
def _tcp_send_nav(
    msg: dict, connect_timeout: float = 5.0, recv_timeout: float = 6.0
) -> dict | None:
    """NAV를 receiver에 TCP(40001)로 보장 전송하고 ACK(JSON 한 줄)를 받는다.

    개행(\\n)으로 구분된 JSON 한 줄을 보내고 같은 형식의 ACK를 읽는다.
    연결/통신 실패 시 예외를 던져 호출자가 UDP로 폴백하게 한다.
    응답이 비면 None.

    connect/recv 타임아웃 분리:
    - connect_timeout(5s): "WiFi 혼잡하지만 살아있는" 구간을 두절로 오판해
      UDP로 조기 폴백(=TCP 신뢰성 이득 상실)하지 않도록 여유를 둔다.
      정상 LAN이면 ms 단위로 반환되므로 평시 비용은 없다.
    - recv_timeout(6s): connect 성공 후 receiver가 hop②(로봇 본체)로 전달하고
      응답할 시간. receiver의 hop② TCP 예산(_send_packet_to_robot_tcp, 3s)보다
      커야 receiver가 아직 전달/UDP폴백 중인데 백엔드가 먼저 포기하는 레이스를
      막는다.
    블록은 nav_thread 폴링 루프 안에서 발생하므로(두절 시 최대 ~5s connect),
    안전정지 예산(RECEIVER_LOST_STOP_SEC=60)·1초 폴링 봉투 안에서만 키운다.
    """
    data = json.dumps(msg).encode("utf-8") + b"\n"
    with socket.create_connection(
        (RECEIVER_IP, RECEIVER_TCP_PORT), timeout=connect_timeout
    ) as sock:
        sock.settimeout(recv_timeout)
        sock.sendall(data)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    if not buf:
        return None
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))


def request_receiver_tcp(
    msg: dict,
    *,
    connect_timeout: float = 1.5,
    recv_timeout: float = 3.0,
    robot_id: int | None = None,
) -> dict | None:
    """receiver TCP 신뢰 채널로 JSON 요청 1건을 보내고 응답을 받는다.

    위치·상태·초기화 결과처럼 누락되면 오판을 유발하는 요청에 사용한다.
    기존 UDP 경로는 호출자가 fallback으로 사용할 수 있도록 이 함수는 예외를
    그대로 전달하고, 응답이 없으면 None을 반환한다.
    """
    address = None
    if robot_id is not None:
        endpoint = ep.resolve(robot_id, ep.ROLE_RECEIVER)
        address = endpoint.tcp_addr()
    if address is None:
        address = (RECEIVER_IP, RECEIVER_TCP_PORT)

    data = json.dumps(msg).encode("utf-8") + b"\n"
    with socket.create_connection(address, timeout=connect_timeout) as sock:
        sock.settimeout(recv_timeout)
        sock.sendall(data)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(8192)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 1_048_576:
                raise ValueError("receiver TCP response is too large")
    if not buf:
        return None
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))


def send_nav_to_robot(idx: int, x: float, y: float, yaw: float,
                      speed: str | None = None,
                      map_id: int | None = None,
                      continuing: bool = False) -> None:
    """NAV 명령 송신 — 신뢰 채널(TCP) 우선, 실패 시 UDP 폴백.

    TCP ACK 의미: receiver(NOS)가 NAV를 수신하고 로봇 본체로 전달했음을 보장.
    (WiFi 구간 PC↔NOS 유실 해결. 로봇 본체로의 최종 전달도 receiver가 TCP/UDP로 수행.)

    speed: "slow" | "normal" | "fast" | None. 안내 단계 이동만 시나리오의 안내 속도를 싣는다.
    None 이면 키를 빼서 보낸다 — 기존 receiver 와 같은 패킷이다.
    continuing: 통과 지점을 걷는 중에 넘긴 NAV. receiver 가 모드 리셋을 생략할 수 있다는 표시다.
    재전송에는 절대 싣지 않는다(로봇이 서 있을 수 있다).
    """
    msg = {"action": "NAV", "idx": idx, "x": x, "y": y, "yaw": yaw}
    if map_id is not None:
        msg["map_id"] = int(map_id)
    if speed:
        msg["speed"] = speed
    if continuing:
        msg["continue"] = True
    print(f"[FASTAPI → ROBOT NAV] {msg}")

    # 1) 신뢰 채널(TCP 40001) 우선
    try:
        ack = _tcp_send_nav(msg)
        if ack is not None:
            if ack.get("ack") and ack.get("idx") == idx:
                print(f"[NAV ACK] (TCP) receiver 수신 확인 (WP{idx})")
            else:
                print(f"[NAV ACK] (TCP) 예상치 못한 응답: {ack}")
            return
        # 연결은 됐으나 ACK 없음(연결 중 끊김) → UDP 폴백
        print(f"[NAV ACK] (TCP) 응답 없음 — UDP 폴백 (WP{idx})")
    except Exception as e:
        print(f"[NAV TCP] 연결 실패 → UDP 폴백 (WP{idx}): {e}")

    # 2) 폴백: 기존 UDP 경로 (receiver 미업데이트/TCP 차단 시 회귀 방지)
    try:
        ack = _udp_send(msg, wait_ack_idx=idx)
    except Exception as e:
        print(f"[NAV ACK] (UDP) 오류: {e}")
        return

    if ack is None:
        print(f"[NAV ACK] (UDP) [WARN] receiver 응답 없음 (WP{idx}) — 명령 유실 가능")
        return

    if ack.get("ack") and ack.get("idx") == idx:
        print(f"[NAV ACK] (UDP) receiver 수신 확인 (WP{idx})")
    else:
        print(f"[NAV ACK] (UDP) 예상치 못한 응답: {ack}")

def _multi_accepted(ack: dict) -> bool:
    """다지점 ACK 판정. receiver 가 묶음을 알아들었으면(n 있음·형식 오류 없음) 받은 것으로 본다.

    receiver 는 로봇의 보행 전환을 4.5초만 기다리고 ACK 를 보낸다. 앉았다 일어서는 등 전환이 늦으면 ack 가
    False 로 오지만 작업은 남아 있어 준비되는 대로 로봇에 나간다(단일 NAV 와 같다). 이것을 '다지점 모름'으로
    보면 같은 구간을 단일 NAV 로 다시 보내 묶음을 버리고, 관제가 재시작될 때까지 다지점을 끈다(10-27 실기).
    """
    if ack.get("ack"):
        return True
    if "n" in ack and not ack.get("error"):
        print(f"[NAV ACK] 다지점 접수됨 — 로봇 보행 전환이 늦어 확인만 늦음, 이어서 진행: {ack}")
        return True
    return False


def send_nav_multi_to_robot(points: list[dict], map_id: int | None = None,
                            continuing: bool = False) -> bool:
    """다지점 NAV 1건 — 멈출 곳까지의 점을 한 번에 보낸다(TCP 우선, 실패 시 UDP).

    points: [{"x", "y", "yaw", "speed", "mode"}] — mode "straight"|"auto". 마지막 점에서 로봇이 선다.
    receiver 가 LoopCnt 0(한 번)으로 고정해 보낸다. 반환: receiver 가 로봇에 보냈다고 답했는가.
    """
    msg = {"action": "NAV_MULTI", "points": points}
    if map_id is not None:
        msg["map_id"] = int(map_id)
    if continuing:
        # 내비 보행으로 서 있다 — receiver 가 지금 내비 보행이면 모드 리셋(기본 보행 전환)을 생략한다
        msg["continue"] = True
    print(f"[FASTAPI → ROBOT NAV_MULTI] {len(points)}점 → "
          f"({points[-1]['x']:.2f}, {points[-1]['y']:.2f}) mode={points[-1].get('mode')}"
          + (" (이어가기)" if continuing else ""))
    try:
        ack = _tcp_send_nav(msg)
        if ack is not None:
            ok = _multi_accepted(ack)
            if not ok:
                print(f"[NAV ACK] (TCP) 다지점 거부: {ack}")
            return ok
        print("[NAV ACK] (TCP) 다지점 응답 없음 — UDP 폴백")
    except Exception as e:
        print(f"[NAV TCP] 연결 실패 → UDP 폴백 (다지점): {e}")
    try:
        ack = _udp_send(msg, wait_ack_idx=1)
    except Exception as e:
        print(f"[NAV ACK] (UDP) 오류: {e}")
        return False
    if not ack:
        print("[NAV ACK] (UDP) [WARN] receiver 응답 없음 (다지점) — 명령 유실 가능")
        return False
    return _multi_accepted(ack)
