#!/usr/bin/env bash
# receiver.py / relay_*.py 를 systemd 서비스로 등록한다. NOS 에서만 root 로 실행.
# Jetson 은 2026-10-11 부터 중계기를 쓰지 않는다(관제·음성 모두 NOS 사용) — Jetson 에서는 설치를 거부한다.
#
#   sudo bash install_service.sh              # 설치 + 기동
#   sudo bash install_service.sh --uninstall  # 되돌리기
#
# receiver 는 ROS2 가 필요 없고, relay 2종은 필요하다.
# setup_ros2.sh 가 없으면 relay 는 건너뛰고 receiver 만 올린다.

set -uo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/robot/receiver}"
UNIT_DIR=/etc/systemd/system
ROS2_SETUP="${ROS2_SETUP:-/opt/robot/scripts/setup_ros2.sh}"
ROBOT_IP_ENV="${ROBOT_IP:-10.21.31.103}"

UNITS_ALL=(robot-receiver robot-relay-map robot-relay-charge)

say() { printf '%s\n' "$*"; }
hr()  { printf -- '---- %s ----\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { say "[!] root 로 실행할 것: sudo bash $0 $*"; exit 1; }

# ── 되돌리기 ────────────────────────────────────────────────
if [ "${1:-}" = "--uninstall" ]; then
    hr "제거"
    for u in "${UNITS_ALL[@]}"; do
        systemctl disable --now "$u.service" 2>/dev/null
        rm -f "$UNIT_DIR/$u.service"
        say "  $u 제거"
    done
    systemctl daemon-reload
    say "완료. 설치 파일은 $INSTALL_DIR 에 남겨둔다(수동 삭제)."
    exit 0
fi

# Jetson 판별: 음성 안내(robot-voice) 유닛이 있는 기기. 제거는 위에서 허용하고 설치만 막는다.
JETSON_MARK="${JETSON_MARK:-$UNIT_DIR/robot-voice.service}"
if [ -e "$JETSON_MARK" ]; then
    say "[!] Jetson($JETSON_MARK 있음)에서는 중계기를 설치하지 않는다. NOS 배포는 robot_receiver/tools/nos_receiver.py 로 한다."
    exit 1
fi

# ── 사전 점검 ───────────────────────────────────────────────
hr "사전 점검"
command -v python3 >/dev/null || { say "[!] python3 가 없다"; exit 1; }
say "  python3  : $(command -v python3) ($(python3 -V 2>&1))"

for f in receiver.py relay_map.py relay_charge.py; do
    [ -f "$SRC_DIR/$f" ] || { say "[!] $SRC_DIR/$f 가 없다"; exit 1; }
done
say "  소스     : $SRC_DIR"

UNITS=(robot-receiver)
if [ -f "$ROS2_SETUP" ]; then
    say "  ROS2     : $ROS2_SETUP (relay 포함 설치)"
    UNITS+=(robot-relay-map robot-relay-charge)
else
    say "  ROS2     : $ROS2_SETUP 없음 -> relay 2종은 건너뛴다"
    say "             (매핑/충전상태는 동작하지 않는다. 경로가 다르면 ROS2_SETUP= 로 지정)"
fi

# 이미 receiver 가 떠 있으면 포트가 겹쳐 새 프로세스가 죽는다. 먼저 정리한다.
if pgrep -af 'python3?.*receiver\.py' >/dev/null 2>&1; then
    say ""
    say "  [주의] 수동 실행 중인 receiver.py 를 발견했다. 포트(40000/30001) 충돌을 피해 종료한다:"
    pgrep -af 'python3?.*receiver\.py' | sed 's/^/           /'
    pkill -f 'python3?.*receiver\.py' 2>/dev/null
    sleep 1
fi

# ── 배치 ────────────────────────────────────────────────────
hr "파일 배치 -> $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
for f in receiver.py relay_map.py relay_charge.py; do
    install -m 0755 "$SRC_DIR/$f" "$INSTALL_DIR/$f"
    say "  $f"
done

hr "유닛 설치"
for u in "${UNITS[@]}"; do
    src="$SRC_DIR/systemd/$u.service"
    [ -f "$src" ] || { say "  [!] $src 없음 - 건너뜀"; continue; }
    # 유닛의 기본 주소를 환경변수로 덮어쓸 수 있게 한다.
    sed -e "s|^Environment=ROBOT_IP=.*|Environment=ROBOT_IP=$ROBOT_IP_ENV|" \
        -e "s|/opt/robot/receiver|$INSTALL_DIR|g" \
        -e "s|/opt/robot/scripts/setup_ros2.sh|$ROS2_SETUP|g" \
        "$src" > "$UNIT_DIR/$u.service"
    say "  $u.service"
done

systemctl daemon-reload

hr "기동"
for u in "${UNITS[@]}"; do
    systemctl enable "$u.service" >/dev/null 2>&1
    systemctl restart "$u.service"
    sleep 1
    state=$(systemctl is-active "$u.service" 2>/dev/null)
    say "  $u : $state"
done

# ── 검증 ────────────────────────────────────────────────────
hr "검증"
sleep 2
if ss -ulnp 2>/dev/null | grep -q ':40000'; then
    say "  [OK] UDP 40000 리슨 중"
else
    say "  [FAIL] UDP 40000 이 안 열렸다 -> journalctl -u robot-receiver -n 50"
fi

say "  로봇 본체($ROBOT_IP_ENV) 도달 확인:"
if ping -c1 -W1 "$ROBOT_IP_ENV" >/dev/null 2>&1; then
    say "    [OK] 응답"
else
    say "    [FAIL] 무응답 - 로봇 eth 주소를 먼저 복구할 것 (restore_eth.sh)"
fi

say "  로컬 STATUS 왕복 확인:"
python3 - "$INSTALL_DIR" <<'PY'
import socket, json
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
try:
    s.sendto(json.dumps({"action": "STATUS"}).encode(), ("127.0.0.1", 40000))
    d, _ = s.recvfrom(8192)
    print(f"    [OK] receiver 응답 {len(d)} bytes")
except socket.timeout:
    print("    [FAIL] receiver 무응답")
finally:
    s.close()
PY

say ""
say "로그:   journalctl -u robot-receiver -f"
say "상태:   systemctl status robot-receiver"
say "되돌림: sudo bash $0 --uninstall"
