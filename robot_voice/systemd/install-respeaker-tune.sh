#!/bin/bash
# respeaker-tune 설치 — udev 규칙 · systemd 유닛 · 적용 스크립트 · 설정 파일.
#
#   sudo bash install-respeaker-tune.sh          # 설치만
#   sudo bash install-respeaker-tune.sh --now    # 설치 후 즉시 1회 적용
#
# 재실행해도 안전하다. /etc/respeaker-tune.conf 는 이미 있으면 덮지 않는다
# (현장에서 맞춘 FIXED_BEAMS·XVF_DIR 값을 보존한다 — 재설치마다 튜닝값이 날아가는
#  사고가 2026-09-23 에 있었다).
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC_SH="$HERE/../respeaker_tune.sh"

[ "$(id -u)" -eq 0 ] || { echo "!! sudo 로 실행하세요"; exit 1; }
[ -r "$SRC_SH" ] || { echo "!! $SRC_SH 없음"; exit 1; }

install -m 0755 "$SRC_SH" /usr/local/sbin/respeaker_tune.sh
install -m 0644 "$HERE/respeaker-tune.service" /etc/systemd/system/respeaker-tune.service
install -m 0644 "$HERE/99-respeaker-xvf3800-tune.rules" /etc/udev/rules.d/99-respeaker-xvf3800-tune.rules

if [ ! -e /etc/respeaker-tune.conf ]; then
    cat > /etc/respeaker-tune.conf <<'CONF'
# respeaker_tune.sh 설정. 값만 바꾼다.

# xvf_host 바이너리와 .so 가 있는 디렉터리.
XVF_DIR=/home/und02/project/voice/voice_assistant/xvf_host

# 재열거 뒤 재기동할 유닛(공백 구분). 카드 인덱스가 밀리면 PortAudio 목록을 다시
# 읽어야 하므로 마이크를 쓰는 유닛을 전부 넣는다.
RESTART_UNITS=robot-voice

# 고정 빔(2단계). 기본 0.
#   1 로 켜면 빔을 180°(로봇 정면)에 고정한다. 선행 실측(펌웨어 2.0.6, 1m 정면):
#     자유주행  no_speech 0.76~0.92 · 목적지 0/3
#     180° 고정 no_speech 0.04~0.30 · 목적지 2/3
#   아직 펌웨어 2.0.10 에서 검증되지 않았다. 수동으로 효과를 확인한 뒤 켤 것.
#   ⚠ 이 훅은 재열거마다 자동 적용하므로, 켠 뒤에는 "USB 를 뽑아 원복" 이 통하지 않는다.
#     문제가 생기면 /etc/respeaker-tune.disable 을 만들어 훅을 멈출 것.
FIXED_BEAMS=0
CONF
    chmod 0644 /etc/respeaker-tune.conf
    echo "  /etc/respeaker-tune.conf 생성"
else
    echo "  /etc/respeaker-tune.conf 이미 있음 — 그대로 둔다"
fi

systemctl daemon-reload
udevadm control --reload-rules
echo "설치 완료"

if [ "${1:-}" = "--now" ]; then
    echo "즉시 1회 적용:"
    /usr/local/sbin/respeaker_tune.sh
fi
