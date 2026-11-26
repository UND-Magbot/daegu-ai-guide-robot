#!/usr/bin/env bash
# Jetson journald 를 디스크 보존으로 바꾼다. Jetson 에서 root 로 실행.
#
# 목적: 재부팅하면 robot-receiver·robot-voice·voice-* 로그가 사라져 장애 원인을 못 찾는다.
#       (2026-09-29: 전날 18시대 로그가 재부팅으로 전부 날아가 대조 분석이 불가능했다.)
#
#   sudo bash setup_persistent_logs.sh              # 설정 + journald 재시작
#   sudo bash setup_persistent_logs.sh --uninstall  # 되돌리기(휘발성 로그로 복귀)
#
# 조회 예:
#   journalctl --list-boots                       # 부팅별 목록
#   journalctl -b -1 -u robot-receiver            # 직전 부팅의 receiver 로그
#   journalctl -u robot-receiver --since "2026-09-29 09:48" --until "09:51"
#
# ⚠️ Jetson 은 RTC 배터리가 없어 부팅 직후 NTP 동기화 전까지 시각이 전 종료 시각으로 찍힌다.
#    시간으로 찾기 전에 --list-boots 로 부팅 구간을 먼저 확인할 것.

set -uo pipefail

CONF_DIR=/etc/systemd/journald.conf.d
CONF_F=$CONF_DIR/90-robot-persistent.conf
JOURNAL_DIR=/var/log/journal

# receiver 로그는 하루 약 1GB 쌓인다(BAT/STATUS 폴링 로그). 8GB ≈ 1주일치.
MAX_USE="${MAX_USE:-8G}"
KEEP_FREE="${KEEP_FREE:-20G}"
RETENTION="${RETENTION:-30day}"

say() { printf '%s\n' "$*"; }
hr()  { printf -- '---- %s ----\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { say "[!] root 로 실행할 것: sudo bash $0 $*"; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    hr "되돌리기"
    rm -f "$CONF_F"
    systemctl restart systemd-journald
    say "[OK] 설정 파일 제거. 이미 쌓인 $JOURNAL_DIR 는 남겨 둔다(지우려면 수동 삭제)."
    say "     /var/log/journal 이 남아 있는 한 Storage=auto 도 디스크에 기록한다."
    exit 0
fi

hr "journald 보존 설정"
mkdir -p "$CONF_DIR"
cat > "$CONF_F" <<EOF
# setup_persistent_logs.sh 가 생성 — 재부팅 후에도 로그를 남긴다.
[Journal]
Storage=persistent
SystemMaxUse=$MAX_USE
SystemKeepFree=$KEEP_FREE
MaxRetentionSec=$RETENTION
EOF
say "[OK] $CONF_F"
cat "$CONF_F"

hr "저장소 생성 + 재시작"
mkdir -p "$JOURNAL_DIR"
systemd-tmpfiles --create --prefix "$JOURNAL_DIR"
# systemd 249 는 fd store 로 서비스 stdout 연결을 유지하므로 재시작해도 서비스 로그가 끊기지 않는다.
systemctl restart systemd-journald
journalctl --flush

hr "확인"
journalctl --disk-usage
ls -ld "$JOURNAL_DIR"/*/ 2>/dev/null | head -3
if journalctl -u robot-receiver -n 1 --since "-2min" --no-pager >/dev/null 2>&1; then
    say "[OK] robot-receiver 로그 계속 기록 중:"
    journalctl -u robot-receiver -n 2 --no-pager | tail -2
fi
say "[OK] 완료. 다음 부팅부터 'journalctl --list-boots' 에 이전 부팅이 남는다."
