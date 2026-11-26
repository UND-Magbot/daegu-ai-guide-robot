---
name: deploy-jetson-voice
description: Jetson 음성 안내(robot-voice) 코드를 반영한다. 로컬·Jetson 작업 폴더·설치본(/opt) 3곳 대조 → 바뀐 파일만 올림 → 사용자가 sudo 로 설치·재시작 → 상태 확인. "Jetson 배포", "음성 반영" 요청 때 쓴다.
disable-model-invocation: true
---

# Jetson 음성 배포

robot-voice 는 `/opt/robot/voice/robot_voice` **설치본**으로 돈다. 작업 폴더(`/home/und02/project/robot_voice`)만 바꾸면 적용되지 않는다(10-12 "안 먹음").
반대로 작업 폴더가 설치본보다 오래된 적도 있다(10-09, 10-21) — 그래서 반드시 3곳을 대조한 뒤 **바뀐 파일만** 올린다.

- `install.sh` 는 쓰지 않는다: 폴더 전체를 덮고 파이썬 패키지까지 올려서(시연 직전 위험), 설정 일부도 다시 묻는다.
  새 패키지가 필요한 변경일 때만 사용자와 따로 정한다.
- sudo 는 비밀번호가 필요하다 → 명령을 만들어 사용자가 `!` 로 실행한다.
- 관제 화면의 음성 설정(`sttModel` 등)은 Jetson 이 읽지 않는다. 설정은 `/etc/robot-voice.env`.

## 0. 연결

기본 `ssh jetson`(172.30.1.44, 층 Wi-Fi). 사무실 망은 `--host jetson-office`. 데모 망·로봇 AP 경유는 사용자에게 주소를 물어 ssh 별칭을 정한다.

## 1. 3곳 대조 (읽기 전용 — 바로 실행)

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/deploy-jetson-voice/jetson_voice.py diff
```

- `⚠ 로컬이 다름` = 배포 후보. 이번 요청분이 아닌 파일이 섞였으면 사용자에게 묻는다.
- `⚠⚠ 설치본이 둘 다와 다름` = 설치본에만 있는 수정이 있다. `show <파일>` 로 줄 차이를 보이고, 덮어도 되는지 확인받는다.
  필요하면 "설치본 + 이번 변경분"으로 따로 합친 파일을 만들어 그것을 올린다(10-21 방식).
- 코드 동작 변경이면 presim(사전 시뮬) 단독 스크립트 검증을 거쳤는지 확인한다.

## 2. 승인받기

보고: 올릴 파일 목록(이번 요청분 / 그 밖), 설치본 차이 요약, 재시작 영향(재시작 동안 음성 응대 중단 — 진행 중 안내 세션이 없을 때),
"올릴까요?"
진행 중 세션 확인: `cd .claude/skills/deploy-control && PYTHONIOENCODING=utf-8 python -c "from common import db_query; print(db_query('SELECT COUNT(*) FROM service_session WHERE EndedAt IS NULL'))"`

## 3. 올리기 (승인 후)

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/deploy-jetson-voice/jetson_voice.py stage a.py b.py
```

로컬 문법 검사 → `/tmp/voice_deploy_날짜/` 와 Jetson 작업 폴더에 올림(작업 폴더는 `.bak_날짜` 백업) → **sudo 명령 출력**.
출력된 `! ssh -t jetson "sudo bash -c '...'"` 를 사용자에게 그대로 전하고 실행을 부탁한다
(설치본 `.bak_날짜` 백업 → 교체 → **30초 뒤 재시작 예약**(`systemd-run --on-active=30`)).

- **재시작을 ssh 명령 안에서 바로 하지 않는다.** und02 는 Linger 가 꺼져 있어 ssh 로그인마다 pulseaudio 가 새로 떠
  USB 스피커(UACDemo)를 잠깐 잡는다. 그 순간 시작한 robot-voice 는 장치 목록에서 스피커를 빠뜨려 다음 재시작까지
  "output 장치를 찾지 못했다" → 안내 aborted_tts(2026-10-27 2회 재현, 예약 재시작으로 해결).
- 사용자가 실행한 뒤 **1분 동안 Jetson 에 ssh 접속하지 않는다**(확인 명령도 기다렸다가 — 백그라운드 sleep 70).
- 재시작만 다시 해야 하면: `! ssh -t jetson "sudo systemd-run --on-active=30 --unit=robot-voice-delayed-restart systemctl restart robot-voice"`

## 4. 확인

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/deploy-jetson-voice/jetson_voice.py check
PYTHONIOENCODING=utf-8 python .claude/skills/deploy-jetson-voice/jetson_voice.py diff
```

- `robot-voice` `active`, 최근 로그에 Traceback 없음, 배포 파일이 3곳 모두 "같음".
- 안내 시작을 한 번 눌러 첫 멘트가 나오는지 확인한다. 로그에 `output 장치를 찾지 못했다` 가 있으면 위 예약 재시작을 다시 한다.
- 설정 키를 새로 쓰는 변경이면 실행 중 설정 키 목록에 있는지 본다(없으면 `/etc/robot-voice.env` 에 추가 — sudo, 사용자 실행).
- 마이크가 먹통이면(USB `-71`) 메모리 "reSpeaker USB -71 복구" 절차.

## 되돌리기

`stage` 가 출력한 되돌리기 명령(설치본 `.bak_날짜` 복원 + 재시작)을 사용자가 실행한다.

## 보고

배포 시각(한국 시각), 올린 파일·백업 이름, 3곳 대조 결과, 서비스 상태, 실기 확인 필요(실제 대화 시험), 커밋 여부.
