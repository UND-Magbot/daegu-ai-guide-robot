#!/usr/bin/env bash
# Jetson 시계를 관제 백엔드 시각에 맞춘다. Jetson 에서 root 로 실행.
#
# 목적: Jetson 은 RTC 배터리가 없어 전원이 끊기면 시계가 마지막 저장 시각으로 돌아간다.
#       인터넷이 없는 현장에선 NTP 도 안 붙어 몇 시간씩 늦은 채로 돈다.
#       (2026-10-01: 비상정지 후 전원 재투입 → 시계 3시간 35분 지연 → 관제가 STATUS 를
#        '오래된 데이터'로 판정해 로봇이 활성화되지 않았다.)
#
# 방식: 관제 백엔드 HTTP 응답의 Date 헤더를 읽어 date -s 로 맞춘다. 인터넷이 필요 없다.
#       부팅 15초 뒤 + 30분마다 실행하고, 관제가 아직 안 떠 있으면 10분까지 재시도한다.
#       인터넷 NTP 가 이미 동기화돼 있으면 건드리지 않는다(정밀도는 NTP 가 더 좋다).
#
# 시각 출처: /etc/robot-voice.env 의 BACKEND_BASE (관제 PC 가 바뀌면 거기만 고치면 따라간다).
#            따로 지정하려면 /etc/robot-timesync.env 에 TIME_SOURCE=http://<관제IP>:8010
#
#   sudo bash setup_timesync.sh                                    # 설치 + 즉시 1회 동기화
#   sudo TIME_SOURCE=http://192.168.0.14:8010 bash setup_timesync.sh
#   sudo bash setup_timesync.sh --uninstall
#
# 확인:
#   systemctl list-timers robot-timesync.timer
#   journalctl -u robot-timesync -n 20

set -uo pipefail

BIN=/usr/local/sbin/robot-timesync
UNIT=/etc/systemd/system/robot-timesync.service
TIMER=/etc/systemd/system/robot-timesync.timer
OVERRIDE_ENV=/etc/robot-timesync.env

say() { printf '%s\n' "$*"; }
hr()  { printf -- '---- %s ----\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { say "[!] root 로 실행할 것: sudo bash $0 $*"; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    hr "제거"
    systemctl disable --now robot-timesync.timer 2>/dev/null
    rm -f "$TIMER" "$UNIT" "$BIN"
    systemctl daemon-reload
    say "[OK] robot-timesync 제거 완료 ($OVERRIDE_ENV 는 남겨둔다)"
    exit 0
fi

command -v curl >/dev/null || { say "[!] curl 이 없다 — apt-get install curl"; exit 1; }

if [ -n "${TIME_SOURCE:-}" ]; then
    hr "시각 출처 지정 -> $OVERRIDE_ENV"
    printf 'TIME_SOURCE=%s\n' "$TIME_SOURCE" > "$OVERRIDE_ENV"
    say "[OK] TIME_SOURCE=$TIME_SOURCE"
fi

hr "동기화 스크립트 $BIN"
cat > "$BIN" <<'EOF'
#!/usr/bin/env bash
# setup_timesync.sh 가 생성 — 관제 백엔드의 HTTP Date 헤더로 시계를 맞춘다.
set -uo pipefail

MAX_SKEW_SEC=2      # 이보다 작게 어긋나면 그대로 둔다 (Date 헤더가 초 단위라 더 좁혀도 의미 없다)
RETRY_SEC=5
MAX_WAIT_SEC=600    # 부팅 직후 관제·Wi-Fi 가 늦게 올라와도 이만큼은 기다린다

env_get() {  # env_get FILE KEY
    [ -r "$1" ] && grep "^$2=" "$1" | head -1 | cut -d= -f2- | tr -d '"'"'"'\r'
}

source_url() {
    local url
    url="$(env_get /etc/robot-timesync.env TIME_SOURCE)"
    [ -n "$url" ] || url="$(env_get /etc/robot-voice.env BACKEND_BASE)"
    printf '%s' "${url%/}"
}

if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "yes" ]; then
    echo "[SKIP] 인터넷 NTP 동기화 상태 — 관제 시각으로 덮지 않는다"
    exit 0
fi

url="$(source_url)"
if [ -z "$url" ]; then
    echo "[FAIL] 시각 출처 없음 — /etc/robot-voice.env 의 BACKEND_BASE 또는 /etc/robot-timesync.env 의 TIME_SOURCE 필요"
    exit 1
fi

deadline=$(( $(date +%s) + MAX_WAIT_SEC ))
while :; do
    hdr="$(curl -s -D - -o /dev/null --max-time 3 "$url/" 2>/dev/null \
           | tr -d '\r' | awk -F': ' 'tolower($1)=="date"{print $2; exit}')"
    if [ -n "$hdr" ] && src="$(date -u -d "$hdr" +%s 2>/dev/null)"; then
        skew=$(( src - $(date +%s) ))
        if [ "${skew#-}" -le "$MAX_SKEW_SEC" ]; then
            echo "[OK] $url 와 ${skew}s 차이 — 보정 불필요"
            exit 0
        fi
        before="$(date '+%F %T')"
        date -s "@$src" >/dev/null
        hwclock -w 2>/dev/null || true   # RTC 배터리가 없으면 전원 차단 시 어차피 사라진다
        echo "[SET] $before -> $(date '+%F %T') (${skew}s 보정, 출처 $url)"
        exit 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "[FAIL] ${MAX_WAIT_SEC}s 동안 $url 응답 없음 — 시계 미보정"
        exit 1
    fi
    sleep "$RETRY_SEC"
done
EOF
chmod 0755 "$BIN"
say "[OK] $BIN"

hr "systemd 유닛"
cat > "$UNIT" <<EOF
# setup_timesync.sh 가 생성
[Unit]
Description=Sync clock from control backend (no-internet fallback)
After=network.target

[Service]
Type=oneshot
ExecStart=$BIN
TimeoutStartSec=700
EOF

cat > "$TIMER" <<EOF
# setup_timesync.sh 가 생성 — 부팅 직후 + 주기 재동기화(RTC 없이 흐르는 오차 보정)
[Unit]
Description=Periodic clock sync from control backend

[Timer]
OnBootSec=15s
OnUnitActiveSec=30min

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable robot-timesync.timer >/dev/null 2>&1
systemctl start robot-timesync.timer
say "[OK] $UNIT, $TIMER (enable)"

hr "즉시 1회 실행"
before="$(date '+%F %T')"
# 마지막 줄은 systemd 의 "Finished ..." 라서 스크립트가 남긴 판정 줄만 골라 보여준다.
last_result() { journalctl -u robot-timesync -n 10 --no-pager -o cat | grep -E '^\[(OK|SET|SKIP|FAIL)\]' | tail -1; }
if systemctl start robot-timesync.service; then
    say "  $(last_result)"
else
    say "  $(last_result)"
    say "[!] 동기화 실패 — 관제 백엔드 주소와 연결을 확인할 것"
fi
say "  실행 전: $before"
say "  실행 후: $(date '+%F %T')"
systemctl list-timers robot-timesync.timer --no-pager | head -2
