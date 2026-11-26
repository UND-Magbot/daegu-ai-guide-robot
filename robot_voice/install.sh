#!/usr/bin/env bash
# robot_voice 를 Jetson 에 설치한다.
#
#   sudo bash install.sh                 # 설치 + 기동
#   sudo bash install.sh --uninstall
#
# 필요한 값 (없으면 프롬프트로 묻는다):
#   AGENT_TOKEN     관제 .env 의 AGENT_TOKEN 과 같은 값 (agent_bridge 와 공유)
#   ROBOT_ID        이 Jetson 이 담당하는 로봇 id (robot_info.id)
#   OPENAI_API_KEY  관제 .env 의 OPEN_AI_SECRET_KEY
#
#   sudo AGENT_TOKEN=xxx ROBOT_ID=10 OPENAI_API_KEY=sk-xxx \
#        BACKEND_BASE=http://192.168.0.14:8010 bash install.sh

set -uo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/robot/voice}"
VENV="${VENV:-$INSTALL_DIR/venv}"
ENV_FILE=/etc/robot-voice.env
UNIT=/etc/systemd/system/robot-voice.service
RUN_USER="${RUN_USER:-${SUDO_USER:-$(id -un)}}"

say() { printf '%s\n' "$*"; }
hr()  { printf -- '---- %s ----\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { say "[!] root 로 실행할 것: sudo bash $0 $*"; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    hr "제거"
    systemctl disable --now robot-voice.service 2>/dev/null
    rm -f "$UNIT"
    systemctl daemon-reload
    say "  robot-voice 제거 완료 (설정 $ENV_FILE 은 남겨둔다)"
    exit 0
fi

hr "의존성"
for pkg in libportaudio2 python3-venv; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        say "  $pkg 설치 중..."
        apt-get install -y "$pkg" >/dev/null 2>&1 || say "  [주의] $pkg 설치 실패 - 수동 설치 필요"
    fi
done

# venv 로 격리하는 이유: 이 Jetson 의 시스템 python3 에는 ROS2 Humble 이 깔아둔
# 패키지(pydantic v1 등)가 있어 openai SDK 와 충돌한다. 그 증상이
# "process() takes no keyword arguments" 로 나타났다.
mkdir -p "$INSTALL_DIR"
if [ ! -x "$VENV/bin/python" ]; then
    say "  venv 생성: $VENV"
    python3 -m venv "$VENV" || { say "[!] venv 생성 실패 (python3-venv 필요)"; exit 1; }
fi
"$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1
"$VENV/bin/pip" install --quiet --upgrade -r "$SRC_DIR/requirements.txt" \
    && say "  venv 의존성 설치 완료" \
    || { say "[!] pip 설치 실패"; exit 1; }
say "  $("$VENV/bin/python" -c 'import openai,pydantic;print("openai "+openai.__version__+" / pydantic "+pydantic.VERSION)' 2>/dev/null || echo "버전 확인 실패")"

hr "오디오 장치"
mic=$(arecord -l 2>/dev/null | grep -c '^card' || echo 0)
spk=$(aplay -l 2>/dev/null | grep -c '^card' || echo 0)
say "  마이크 $mic개 / 스피커 $spk개"
[ "$mic" -eq 0 ] && say "  [주의] 마이크가 잡히지 않았다. USB 연결을 확인할 것."
[ "$spk" -eq 0 ] && say "  [주의] 스피커가 잡히지 않았다."

hr "설정 파일 $ENV_FILE"
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"

# 키 단위로 보충한다. 파일을 통째로 덮으면 현장에서 맞춰둔 장치 인덱스·VAD 값이 날아간다.
env_get() { grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-; }

env_set() {  # env_set KEY VALUE   — 있으면 교체, 없으면 추가
    local key="$1" val="$2"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
    fi
}

env_need() {  # env_need KEY [주입값]  — 비어 있으면 묻는다
    local key="$1" val="${2:-}"
    local cur; cur="$(env_get "$key")"
    if [ -n "$val" ]; then env_set "$key" "$val"; return; fi
    if [ -n "$cur" ]; then return; fi
    read -rp "  ${key}: " val
    env_set "$key" "$val"
}

env_opt() {  # env_opt KEY 기본값  — 키가 아예 없을 때만 넣는다(빈 값 허용)
    grep -q "^$1=" "$ENV_FILE" 2>/dev/null || printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
}

env_set BACKEND_BASE "${BACKEND_BASE:-$(env_get BACKEND_BASE)}"
[ -n "$(env_get BACKEND_BASE)" ] || env_set BACKEND_BASE "http://192.168.1.58:8010"
env_need AGENT_TOKEN    "${AGENT_TOKEN:-}"
env_need ROBOT_ID       "${ROBOT_ID:-}"
env_need OPENAI_API_KEY "${OPENAI_API_KEY:-}"
env_opt  SCENARIO_CODE  "${SCENARIO_CODE:-hospital}"     # hospital | public_office
# 오디오 장치 (비우면 이름 힌트로 자동 탐색). 확인: python -m robot_voice.audio --list
env_opt  INPUT_DEVICE   "${INPUT_DEVICE:-}"
env_opt  OUTPUT_DEVICE  "${OUTPUT_DEVICE:-}"
env_opt  VAD_THRESHOLD  "${VAD_THRESHOLD:-0.015}"        # 현장 소음에 맞춰 조정

# 구버전 키. 지금은 X-Agent-Token 하나만 쓰므로 남겨두면 혼선만 준다.
if grep -q '^VOICE_API_KEY=' "$ENV_FILE"; then
    sed -i '/^VOICE_API_KEY=/d' "$ENV_FILE"
    say "  (구버전 VOICE_API_KEY 제거 — 이제 AGENT_TOKEN 을 쓴다)"
fi
say "  키: $(grep -o '^[A-Z_]*' "$ENV_FILE" | tr '\n' ' ')"

hr "파일 배치 -> $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/robot_voice"
install -m 0644 "$SRC_DIR"/*.py "$INSTALL_DIR/robot_voice/"
install -m 0644 "$SRC_DIR/requirements.txt" "$INSTALL_DIR/"
say "  $(ls "$INSTALL_DIR/robot_voice" | tr '\n' ' ')"

hr "유닛 설치 (실행 사용자: $RUN_USER)"
sed -e "s|^User=.*|User=$RUN_USER|" \
    -e "s|/opt/robot/voice|$INSTALL_DIR|g" \
    "$SRC_DIR/systemd/robot-voice.service" > "$UNIT"
systemctl daemon-reload

hr "연결 확인"
# 기동 전에 관제 API 가 열려 있는지 본다 - 토큰·로봇 id 가 틀리면 여기서 걸린다.
set -a; source "$ENV_FILE"; set +a
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
       -H "X-Agent-Token: $AGENT_TOKEN" "$BACKEND_BASE/voice/bundle/$ROBOT_ID")
case "$code" in
    200) say "  [OK] 관제 /voice/bundle/$ROBOT_ID 응답 200" ;;
    401) say "  [FAIL] 토큰 거부(401) - 관제 .env 의 AGENT_TOKEN 과 맞출 것"; exit 1 ;;
    404) say "  [FAIL] 로봇 $ROBOT_ID 를 관제에서 찾지 못했다(404)."
         say "         ROBOT_ID 가 robot_info.id 와 같은지, 관제에 voice_scenario 라우터가"
         say "         올라와 있는지 확인할 것: curl $BACKEND_BASE/docs"; exit 1 ;;
    503) say "  [FAIL] 관제에 AGENT_TOKEN 미설정(503) - 채널이 꺼져 있다"; exit 1 ;;
    000) say "  [FAIL] 관제($BACKEND_BASE)에 연결 불가"; exit 1 ;;
    *)   say "  [FAIL] 예상치 못한 응답: HTTP $code"; exit 1 ;;
esac

hr "기동"
systemctl enable robot-voice.service >/dev/null 2>&1
systemctl restart robot-voice.service
sleep 3
say "  상태: $(systemctl is-active robot-voice.service)"

say ""
say "로그:   journalctl -u robot-voice -f"
say "설정:   sudo nano $ENV_FILE"
say "장치:   cd $INSTALL_DIR && $VENV/bin/python -m robot_voice.audio --list"
say "녹음:   cd $INSTALL_DIR && $VENV/bin/python -m robot_voice.audio --test"
say "되돌림: sudo bash $0 --uninstall"
