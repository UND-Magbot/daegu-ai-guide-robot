#!/usr/bin/env bash
# 106 Jetson 제어망 watchdog
# 인터넷이 아니라 로봇 제어망의 실제 endpoint 복귀를 감시한다.
set -u

ROBOT_IP="${ROBOT_IP:-10.21.31.103}"
RTSP_PORT="${RTSP_PORT:-8554}"
CHECK_SEC="${CHECK_SEC:-10}"
FAIL_LIMIT="${FAIL_LIMIT:-3}"
STATE_FILE="/run/robot-network-watchdog.state"

tcp_ok() {
    timeout 2 bash -c "</dev/tcp/$1/$2" >/dev/null 2>&1
}

receiver_service="receiver.service"
systemctl cat "$receiver_service" >/dev/null 2>&1 || receiver_service="robot-receiver.service"

previous="unknown"
[ -r "$STATE_FILE" ] && previous="$(cat "$STATE_FILE" 2>/dev/null || echo unknown)"

while :; do
    receiver_ok=0
    localization_ok=0
    rtsp_ok=0
    systemctl is-active --quiet "$receiver_service" && receiver_ok=1
    systemctl is-active --quiet localization.service && localization_ok=1
    tcp_ok "$ROBOT_IP" "$RTSP_PORT" && rtsp_ok=1

    if [ "$receiver_ok" -eq 1 ] && [ "$localization_ok" -eq 1 ] && [ "$rtsp_ok" -eq 1 ]; then
        current="online"
    else
        current="degraded"
    fi

    if [ "$current" != "$previous" ]; then
        logger -t robot-network-watchdog \
            "control-plane=$current receiver=$receiver_ok localization=$localization_ok rtsp=$rtsp_ok robot=$ROBOT_IP:$RTSP_PORT"
        printf '%s' "$current" > "$STATE_FILE"
        previous="$current"
    fi

    # receiver 자체가 죽은 경우에만 재기동한다. Wi-Fi/로봇 단절 중에는
    # 원격 endpoint를 무리하게 재시작하지 않고 네트워크 복귀를 기다린다.
    if [ "$receiver_ok" -eq 0 ]; then
        systemctl restart "$receiver_service" 2>/dev/null || true
    fi
    sleep "$CHECK_SEC"
done
