#!/bin/bash
# reSpeaker XVF3800 운영 파라미터 재적용 + 오디오 소비자 재기동.
# udev(ACTION=="add") → respeaker-tune.service 가 이 스크립트를 부른다.
#
# ── 왜 필요한가 ───────────────────────────────────────────────────────────
# ① 이 보드는 설정을 플래시에 저장하지 않으면 **USB 재열거·전원 차단마다 공장값으로
#    돌아간다.** 2026-09-29 실측: USB 재연결 3회 모두 초기화됐고, 그때마다
#    AUDIO_MGR_OP_R 이 MUX_AEC_RESIDUALS[7](잔차)로 복귀했다. 잔차는 ASR 입력이
#    아니라 진단 신호라 잡음이 그대로 실린다(무음 rms 0.099 vs 처리된마이크 0.015).
# ② **ALSA 카드 인덱스가 밀린다.** 같은 날 card 1 → card 4 로 이동했다. PortAudio 는
#    프로세스 시작 시점의 장치 목록을 굳히므로, robot-voice 는 재기동해야 새 장치를
#    잡는다. 재기동하지 않으면 예외를 삼킨 채 `active (running)` 으로 귀머거리가 된다
#    (2026-09-29: Device unavailable [-9985] 반복, NRestarts=0).
#
# ── 플래시에 저장하지 않는다 ──────────────────────────────────────────────
# SAVE_CONFIGURATION 으로 저장한 설정 때문에 2026-08-05 보드가 일반모드에서 USB
# 열거를 못 했다(Safe Mode 만 생존, 2.0.10 재기입으로 겨우 복구). 휘발성으로 두고
# 재열거마다 이 스크립트로 다시 넣는다. 그게 이 파일이 존재하는 이유다.
set -u

CONF=/etc/respeaker-tune.conf
[ -r "$CONF" ] && . "$CONF"

XVF_DIR="${XVF_DIR:-/home/und02/project/voice/voice_assistant/xvf_host}"
ALSA_CARD_NAME="${ALSA_CARD_NAME:-Array}"
RESTART_UNITS="${RESTART_UNITS:-robot-voice}"
FIXED_BEAMS="${FIXED_BEAMS:-0}"
# 고정 빔 방향(도). 보드 기준 각도라 로봇의 어느 쪽인지는 장착 방향에 달렸다 — 현장에서 잰 값을 넣는다.
# 빔은 2개라 두 위치(예: 정뒤 · 좌후방 45°)를 함께 덮을 수 있다. EL 은 위쪽(+)으로 —
# 로봇 키가 낮아 서 있는 사람의 입은 마이크보다 위에 있다.
FIXED_AZ1_DEG="${FIXED_AZ1_DEG:-180}"
FIXED_AZ2_DEG="${FIXED_AZ2_DEG:-180}"
FIXED_EL1_DEG="${FIXED_EL1_DEG:-0}"
FIXED_EL2_DEG="${FIXED_EL2_DEG:-0}"
WAIT_SEC="${WAIT_SEC:-20}"

log() { echo "[respeaker-tune] $*"; }

# 비상 정지 — 튜닝이 장비를 이상하게 만들면 이 파일만 만들면 훅이 멈춘다.
# udev 가 재열거마다 자동 적용하므로, 이 탈출구가 없으면 USB 를 뽑아도 원복되지 않는다.
if [ -e /etc/respeaker-tune.disable ]; then
    log "/etc/respeaker-tune.disable 있음 — 적용을 건너뛴다"
    exit 0
fi

XVF="$XVF_DIR/xvf_host"
if [ ! -x "$XVF" ]; then
    log "!! xvf_host 실행 불가: $XVF"
    exit 1
fi

# ALSA 카드까지 떠야 보드가 실제로 준비된 것이다(USB 열거 + snd-usb-audio 바인딩).
deadline=$((SECONDS + WAIT_SEC))
while [ "$SECONDS" -lt "$deadline" ]; do
    grep -q "$ALSA_CARD_NAME" /proc/asound/cards 2>/dev/null && break
    sleep 1
done
if ! grep -q "$ALSA_CARD_NAME" /proc/asound/cards 2>/dev/null; then
    log "!! ${WAIT_SEC}초 안에 ALSA card '$ALSA_CARD_NAME' 이 안 떴다 — 중단"
    exit 1
fi
CARD_IDX=$(awk -v n="$ALSA_CARD_NAME" '$0 ~ n && $1 ~ /^[0-9]+$/ {print $1; exit}' /proc/asound/cards)
log "ALSA card '$ALSA_CARD_NAME' = index ${CARD_IDX:-?} (인덱스는 재열거마다 바뀐다 — 이름으로만 참조할 것)"

xv() { LD_LIBRARY_PATH="$XVF_DIR" "$XVF" "$@" 2>&1 | grep -v device_init; }

if ! xv VERSION >/dev/null 2>&1; then
    log "!! 제어 인터페이스 접근 실패 — udev 규칙/권한 확인"
    exit 1
fi
log "펌웨어 $(xv VERSION | tr -d '\n')"

# ── 1단계: mux · AGC · 게인 · 노이즈억제 · LED ────────────────────────────
# 값의 근거는 2026-09-29~30 실측이다. 바꾸기 전에 그 기록을 볼 것.

# 좌우를 같은 신호로 통일한다 — INPUT_PICK_CHANNEL 을 잘못 건드려 잔차 채널을 듣는
# 사고를 원천 차단한다(잔차는 무음 rms 0.099 vs 처리된마이크 0.015).
# 8(USER_CHOSEN) vs 6(PROCESSED_MICS) A/B 실측(2026-09-30, 같은 발화를 좌우에 동시 녹음):
#   8 → ratio 4.2 · maxRun 0.24s   ·   6 → ratio 3.9 · maxRun 0.21s   → 8 채택
xv AUDIO_MGR_OP_L 8 0 >/dev/null
xv AUDIO_MGR_OP_R 8 0 >/dev/null

# AGC 는 끈다. 켜두면 음절 강약을 평탄화해(프레임 에너지 CV 0.10~0.38, 사람 말은 0.7 이상)
# Whisper 가 말의 리듬을 읽지 못한다. 공장 목표 레벨 0.0045 는 너무 낮아 녹음이
# 거의 안 들린다 — 2026-09-29 실측에서 PP_AGCGAIN 이 0.52(감쇠)로 얼어 있었다.
# ⚠ 순서가 중요하다. AGC 를 끄면 **그 순간의 PP_AGCGAIN 이 고정**된다
#   (XMOS: "whether the value of PP_AGCGAIN is fixed"). 그래서
#   목표/상한 → AGC off → **게인 명시** 순으로 넣는다. 게인을 빼먹으면 공장 상태의
#   감쇠값(0.045~2)이 그대로 얼어붙어 레벨이 -60dBFS 까지 떨어진다(실측).
xv PP_AGCDESIREDLEVEL 0.03 >/dev/null
xv PP_AGCMAXGAIN 64 >/dev/null
xv PP_AGCONOFF 0 >/dev/null
# 게인 스윕 실측(무음 기준, 발화는 이것의 3~5배):
#   1 → peak 0.05   4 → peak 0.22 ★   8 → peak 0.49(발화 시 포화)   16 → peak 1.00(클리핑)
xv PP_AGCGAIN 4 >/dev/null

# ASR 출력 게인. ASROUTONOFF=1 일 때만 적용된다.
xv AEC_ASROUTONOFF 1 >/dev/null
xv AEC_ASROUTGAIN 8 >/dev/null

# 노이즈 억제 하한. 낮추면 억제가 강해진다. 팬·모터 같은 **정상 소음**의 정공법이다.
# 스윕 실측(2026-09-30, 무음 배경):
#   0.15 → bg 0.060   0.08 → 0.036   0.04 → 0.020 ★   0.01 → 0.014
# 0.01 은 저역 비중이 90.5%→93.9% 로 올라 **중고역(음성 대역)까지 눌린다.** 0.04 채택.
xv PP_MIN_NS 0.04 >/dev/null

# LED 링 끄기. 공장값은 4(doa 방향 표시) · 밝기 127 이다.
# 디버깅에는 유용하지만(말하는 방향이 눈에 보인다) 노출이 부담이면 끈다.
# 되살리려면: LED_BRIGHTNESS 40 · LED_EFFECT 4
xv LED_EFFECT 0 >/dev/null
xv LED_BRIGHTNESS 0 >/dev/null

log "1단계 적용: mux 8 0 · AGC off(목표 0.03/상한 64/게인 4) · ASROUTGAIN 8 · MIN_NS 0.04 · LED off"

# ── 2단계: 고정 빔 (기본 off) ─────────────────────────────────────────────
# 자유주행 빔(공장값)은 무음에 방향을 배회하다가 발화 앞부분을 놓쳐 음절을 뭉갠다.
# 선행 실측(펌웨어 2.0.6, 1m 정면): 자유주행 no_speech 0.76~0.92 · 목적지 0/3,
#                                  180° 고정 no_speech 0.04~0.30 · 목적지 2/3.
# 기본 off 인 이유: ① 아직 2.0.10 에서 검증되지 않았고, ② 이 훅이 재열거마다 자동
# 적용하므로, 문제가 생겨도 "USB 를 뽑아 원복" 이라는 탈출구가 막힌다.
# 수동으로 효과를 확인한 뒤 /etc/respeaker-tune.conf 에서 FIXED_BEAMS=1 로 켤 것.
rad() { awk -v d="$1" 'BEGIN { printf "%.5f", d * 3.14159265 / 180 }'; }

if [ "$FIXED_BEAMS" = "1" ]; then
    xv AEC_FIXEDBEAMSAZIMUTH_VALUES "$(rad "$FIXED_AZ1_DEG")" "$(rad "$FIXED_AZ2_DEG")" >/dev/null
    xv AEC_FIXEDBEAMSELEVATION_VALUES "$(rad "$FIXED_EL1_DEG")" "$(rad "$FIXED_EL2_DEG")" >/dev/null
    xv AEC_FIXEDBEAMSGATING 0 >/dev/null
    xv AEC_FIXEDBEAMSONOFF 1 >/dev/null
    log "2단계 적용: 고정 빔 방위 ${FIXED_AZ1_DEG}°/${FIXED_AZ2_DEG}° · 높이 ${FIXED_EL1_DEG}°/${FIXED_EL2_DEG}° (보드 기준) · 게이팅 off"
else
    # 건너뛰기만 하면 앞서 켠 고정 빔이 전원을 끌 때까지 남는다 — 명시적으로 끈다.
    # 그래서 되돌리기는 conf 를 FIXED_BEAMS=0 으로 두고 이 스크립트를 다시 돌리는 것으로 끝난다.
    xv AEC_FIXEDBEAMSONOFF 0 >/dev/null
    log "2단계: 고정 빔 끔 (FIXED_BEAMS=0) — 자유주행 빔"
fi

# ── 확인 ─────────────────────────────────────────────────────────────────
for c in AUDIO_MGR_OP_L AUDIO_MGR_OP_R PP_AGCONOFF PP_AGCDESIREDLEVEL PP_AGCGAIN PP_MIN_NS LED_EFFECT \
         AEC_ASROUTONOFF AEC_ASROUTGAIN AEC_FIXEDBEAMSONOFF AEC_FIXEDBEAMSAZIMUTH_VALUES \
         AEC_FIXEDBEAMSELEVATION_VALUES; do
    log "  $(xv "$c" | tr -d '\n')"
done

# ── 오디오 소비자 재기동 ─────────────────────────────────────────────────
# 카드 인덱스가 바뀌었을 때 PortAudio 목록을 다시 읽게 하는 유일한 방법이다.
# try-restart 라서 멈춰 있는 유닛을 깨우지는 않는다.
for u in $RESTART_UNITS; do
    if systemctl is-enabled "$u" >/dev/null 2>&1 || systemctl is-active "$u" >/dev/null 2>&1; then
        log "$u 재기동 (카드 인덱스 변동 대응)"
        systemctl try-restart "$u"
    fi
done

# ── LED 재확인 ───────────────────────────────────────────────────────────
# 재열거 직후(1초 안)에 넣은 LED 값은 펌웨어가 뒤늦게 공장값(4/127)으로 덮는다.
# 2026-10-02 실측: 10:54:48 확인 단계에서 LED_EFFECT 0 이었는데 10:59 에 4/127 로
# 돌아와 있었다(다른 파라미터는 유지). 같은 명령을 부팅이 끝난 뒤 넣으면 유지된다.
# 그래서 몇 초 간격으로 읽어 보고 켜져 있으면 다시 끈다.
for _ in 1 2 3 4 5 6; do
    sleep 5
    if xv LED_EFFECT | grep -qE '^LED_EFFECT 0 *$'; then
        continue
    fi
    log "LED 가 펌웨어에 의해 다시 켜짐 — 재적용"
    xv LED_EFFECT 0 >/dev/null
    xv LED_BRIGHTNESS 0 >/dev/null
done
log "  $(xv LED_EFFECT | tr -d '\n') / $(xv LED_BRIGHTNESS | tr -d '\n')"

log "완료 — 플래시 미저장(휘발성). 다음 재열거 때 이 훅이 다시 넣는다."
