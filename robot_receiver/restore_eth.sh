#!/usr/bin/env bash
# 로봇 본체(NOS 아님)의 eth static IP 복구 스크립트.
#
# 증상: nmcli 에서 eth 가 "connecting (getting IP configuration)" 에 멈춰 있고
#       ip -br addr 에 eth IPv4 가 없다. 원래 있던 static 프로파일이 사라지고
#       NetworkManager 기본 DHCP 프로파일("Wired connection N")로 대체된 상태다.
#
# 사용법 (로봇에서 실행):
#   bash restore_eth.sh            # 진단만 — 아무것도 바꾸지 않는다
#   sudo bash restore_eth.sh --apply
#
# 되돌리기:
#   sudo nmcli con mod "<프로파일명>" ipv4.method auto && sudo nmcli con up "<프로파일명>"

set -uo pipefail

ETH_IP="${ETH_IP:-10.21.31.103}"      # 로봇 본체 eth 주소 (관제 코드 기준값)
PREFIX="${PREFIX:-24}"
PEER_IP="${PEER_IP:-10.21.31.106}"    # Jetson(NOS) — receiver.py 가 도는 장비
CANDIDATES="${CANDIDATES:-eth0 eth1 eth2}"

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

say() { printf '%s\n' "$*"; }
hr()  { printf -- '---- %s ----\n' "$*"; }

hr "현재 상태"
ip -br addr show | grep -E 'eth|wlan' || true
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device 2>/dev/null | grep -E '^eth' || true

hr "netplan 백업"
for f in /etc/netplan/config.yaml /etc/netplan/config.yaml.bak; do
    if [ -r "$f" ]; then
        say "== $f =="
        cat "$f"
    else
        say "== $f == (읽을 수 없음 — sudo 로 다시 실행할 것)"
    fi
done

# ── Jetson 이 물린 포트 탐지 ────────────────────────────────
# 캐리어가 살아 있는 포트에 임시 IP 를 붙여 PEER 응답을 확인하고 즉시 되돌린다.
hr "Jetson($PEER_IP) 연결 포트 탐지"
TARGET=""
for i in $CANDIDATES; do
    [ -e "/sys/class/net/$i" ] || continue
    carrier=$(cat "/sys/class/net/$i/carrier" 2>/dev/null || echo 0)
    if [ "$carrier" != "1" ]; then
        say "  $i : 케이블 없음 (carrier=0) — 건너뜀"
        continue
    fi

    if ! ip addr add "$ETH_IP/$PREFIX" dev "$i" 2>/dev/null; then
        say "  $i : 임시 IP 부여 실패 (root 권한 필요)"
        continue
    fi
    if ping -c1 -W1 "$PEER_IP" >/dev/null 2>&1; then
        say "  $i : [HIT] $PEER_IP 응답 → Jetson 연결 포트"
        TARGET="$i"
    else
        say "  $i : 응답 없음"
    fi
    ip addr del "$ETH_IP/$PREFIX" dev "$i" 2>/dev/null

    [ -n "$TARGET" ] && break
done

if [ -z "$TARGET" ]; then
    say ""
    say "[!] Jetson 응답 포트를 찾지 못했다. 확인할 것:"
    say "    - Jetson 이 켜져 있고 eth 에 $PEER_IP 가 설정돼 있는가"
    say "    - Jetson eth 도 같이 날아갔다면 그쪽부터 복구해야 한다"
    say "    - 케이블이 eth2 나 다른 포트에 물려 있지 않은가"
    exit 1
fi

CONN=$(nmcli -t -f DEVICE,CONNECTION device 2>/dev/null | awk -F: -v d="$TARGET" '$1==d{print $2}')
[ -z "$CONN" ] && CONN="Wired connection 1"

say ""
hr "적용할 설정"
say "  포트      : $TARGET"
say "  프로파일  : $CONN"
say "  주소      : $ETH_IP/$PREFIX"
say "  (게이트웨이/DNS 는 설정하지 않는다 — 로봇 내부망 전용 링크다)"

if [ "$APPLY" -ne 1 ]; then
    say ""
    say "진단 모드다. 적용하려면: sudo bash $0 --apply"
    exit 0
fi

hr "적용"
nmcli con mod "$CONN" ipv4.method manual ipv4.addresses "$ETH_IP/$PREFIX" ipv4.gateway "" ipv4.dns "" \
    && nmcli con up "$CONN" \
    && say "  적용 완료"

hr "검증"
ip -br addr show "$TARGET"
if ping -c2 -W1 "$PEER_IP" >/dev/null 2>&1; then
    say "  [OK] $PEER_IP 도달 — Jetson 과 통신 가능"
else
    say "  [FAIL] $PEER_IP 무응답 — Jetson 쪽 eth 설정도 확인할 것"
fi
