#!/usr/bin/env bash
# Jetson 을 관제 -> 로봇 내부망 게이트웨이로 만든다. Jetson 에서 root 로 실행.
#
# 목적: 관제 PC 가 WiFi 동글이 없이 사무실망만으로 로봇(10.21.31.x)에 접근하게 한다.
#
#   sudo bash setup_gateway.sh              # 설정 + 영구화
#   sudo bash setup_gateway.sh --uninstall  # 되돌리기
#
# 관제 PC(Windows)에서는 별도로 정적 경로를 추가해야 한다(관리자 권한):
#   route -p add 10.21.31.0 mask 255.255.255.0 192.168.0.44

set -uo pipefail

ROBOT_NET="${ROBOT_NET:-10.21.31.0/24}"   # 로봇 내부망
LAN_IF="${LAN_IF:-wlP1p1s0}"              # 사무실망 쪽 (관제가 들어오는 인터페이스)
ROBOT_IF="${ROBOT_IF:-eno1}"              # 로봇 eth 쪽
ROBOT_IP="${ROBOT_IP:-10.21.31.103}"

SYSCTL_F=/etc/sysctl.d/99-robot-gateway.conf
RULES_F=/etc/iptables/rules.v4

say() { printf '%s\n' "$*"; }
hr()  { printf -- '---- %s ----\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { say "[!] root 로 실행할 것: sudo bash $0 $*"; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    hr "되돌리기"
    rm -f "$SYSCTL_F"
    sysctl -w net.ipv4.ip_forward=0 >/dev/null
    iptables -t nat -D POSTROUTING -s "$ROBOT_NET" -o "$ROBOT_IF" -j MASQUERADE 2>/dev/null
    iptables -t nat -D POSTROUTING -o "$ROBOT_IF" -j MASQUERADE 2>/dev/null
    iptables -D FORWARD -i "$LAN_IF" -o "$ROBOT_IF" -j ACCEPT 2>/dev/null
    iptables -D FORWARD -i "$ROBOT_IF" -o "$LAN_IF" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null
    command -v netfilter-persistent >/dev/null && netfilter-persistent save >/dev/null 2>&1
    say "  포워딩/NAT 해제 완료"
    say "  관제 PC 에서도 경로를 지울 것: route delete 10.21.31.0"
    exit 0
fi

hr "인터페이스 확인"
for i in "$LAN_IF" "$ROBOT_IF"; do
    if [ ! -e "/sys/class/net/$i" ]; then
        say "[!] 인터페이스 $i 가 없다. ip -br addr 로 확인 후 LAN_IF=/ROBOT_IF= 로 지정할 것"
        ip -br addr
        exit 1
    fi
    say "  $i : $(ip -4 -o addr show "$i" | awk '{print $4}')"
done

hr "IPv4 포워딩"
sysctl -w net.ipv4.ip_forward=1 >/dev/null
printf 'net.ipv4.ip_forward = 1\n' > "$SYSCTL_F"
say "  활성화 + 영구화 ($SYSCTL_F)"

hr "NAT / FORWARD 규칙"
# MASQUERADE 를 쓰면 로봇에는 Jetson 에서 온 패킷으로 보이므로,
# 로봇 쪽에 관제로 돌아가는 경로를 따로 넣어줄 필요가 없다.
add_rule() {  # 중복 추가 방지
    local tbl=$1; shift
    if ! iptables ${tbl:+-t $tbl} -C "$@" 2>/dev/null; then
        iptables ${tbl:+-t $tbl} -A "$@"
        say "  추가: ${tbl:-filter} $*"
    else
        say "  이미 존재: ${tbl:-filter} $*"
    fi
}
add_rule nat POSTROUTING -o "$ROBOT_IF" -j MASQUERADE
add_rule "" FORWARD -i "$LAN_IF" -o "$ROBOT_IF" -j ACCEPT
add_rule "" FORWARD -i "$ROBOT_IF" -o "$LAN_IF" -m state --state RELATED,ESTABLISHED -j ACCEPT

hr "규칙 영구화"
if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save >/dev/null 2>&1 && say "  netfilter-persistent 저장 완료"
elif [ -d /etc/iptables ]; then
    iptables-save > "$RULES_F" && say "  $RULES_F 저장 완료"
else
    say "  [주의] iptables-persistent 가 없어 재부팅 시 규칙이 사라진다."
    say "         sudo apt install iptables-persistent  후 이 스크립트를 다시 실행할 것"
fi

hr "검증"
say "  ip_forward = $(cat /proc/sys/net/ipv4/ip_forward)"
if ping -c1 -W1 "$ROBOT_IP" >/dev/null 2>&1; then
    say "  [OK] 로봇($ROBOT_IP) 도달"
else
    say "  [FAIL] 로봇($ROBOT_IP) 무응답 - eth 연결부터 확인할 것"
fi

say ""
say "다음: 관제 PC(Windows)에서 관리자 권한으로"
say "  route -p add 10.21.31.0 mask 255.255.255.0 192.168.1.44"
