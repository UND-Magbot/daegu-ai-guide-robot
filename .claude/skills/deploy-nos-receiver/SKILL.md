---
name: deploy-nos-receiver
description: 로봇 NOS(10.21.31.106)의 중계기 receiver.py 를 확인하거나 배포한다. 로컬 대 NOS 차이 확인 → 승인 → 백업·교체·서비스 3개 재시작 → 상태 확인. "NOS receiver 배포", "중계기 반영" 요청 때 쓴다.
disable-model-invocation: true
---

# NOS 중계기 배포

중계기는 NOS `/home/user/Control_system/receiver.py` 로 돈다. 서비스 이름은 NOS 마다 다르다 —
296: `receiver`·`relay_charge`·`relay_motion`, 669(10-29 부터 같은 주소로 붙음): `control-receiver`·`relay-charge`·`relay-motion`.
도구가 접속 뒤 실제로 있는 이름을 골라 쓰고, 첫 줄 "서비스:" 에 출력한다. `relay_map.py` 도 함께 올린다(서비스 아님, 재시작 없음).
NOS 파이썬은 3.8 이라 교체 뒤 NOS 에서 문법 검사가 실패하면 도구가 백업으로 되돌리고 재시작하지 않는다.
Jetson 중계기는 쓰지 않는다(10-28 제거 완료) — `install_service.sh` 는 Jetson 에서 설치를 스스로 거부한다.
관제 컨테이너 안에서 `robot_receiver/tools/nos_receiver.py` 를 돌려 접속한다(.env 의 NOS_* 사용, 비밀번호는 출력하지 않는다).

## 0. 연결 확인

노트북이 로봇 망에 있어야 한다(층 Wi-Fi 172.30.1.x + 10.21.31.0/24 경로). 접속 시간 초과면 망부터 확인하고 멈춘다.

## 1. 차이 확인 (읽기 전용 — 바로 실행)

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/deploy-nos-receiver/nos.py check
```

- 로컬과 NOS 본은 일부러 다른 부분이 있었다(10-20: 다지점만 얹고 위치 age 기능은 뺌). **diff 전체를 사용자에게 보이고**,
  이번 요청과 관계없는 차이가 들어 있으면 그것까지 올라가도 되는지 확인받는다.
- 바꾸려는 동작이 있으면 presim(사전 시뮬) 단독 스크립트로 먼저 검증했는지 확인한다.

## 2. 승인받기

보고: 로컬·NOS 해시, 바뀌는 줄 요약(이번 요청분 / 그 밖), 재시작 영향(서비스 3개가 수 초 끊김 — 걷는 중이면 멈출 수 있음), "배포할까요?"

## 3. 배포 (승인 후)

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/deploy-nos-receiver/nos.py deploy --yes
```

래퍼가 하는 일: 진행 중 안내 세션·작업 회차 0건 확인 → 로컬 문법 검사 → 도구 복사 →
NOS 에서 `receiver.py.bak-날짜-시각` 백업 → 줄바꿈 정리해 교체 → 문법 검사 → `receiver relay_charge relay_motion` 함께 재시작 → 상태·최근 로그.
receiver 만 재시작하면 충전 상태가 대기로 보이는 문제가 있어(10-11) 셋을 함께 재시작한다.

권한 검사에 막히면 같은 명령을 사용자에게 `!` 로 실행해 달라고 한다.

## 4. 확인

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/deploy-nos-receiver/nos.py status
```

- 세 서비스 `active`, receiver 로그에 오류 없음.
- 관제 화면에서 로봇 상태·충전 표시가 정상인지(관제 로그 `docker logs --since 2m control-backend`).
- 속도 기능을 바꿨으면 `nos.py speed`.

## 장애물 감지 노드 (obstacle_guard, 10-29)

중계기와 별개 서비스 `obstacle-guard`(NOS `/home/user/Control_system/obstacle_guard.py`·`obstacle_core.py`).
아래 모드는 중계기 서비스 3개를 **재시작하지 않는다**. 쓰는 모드는 승인 뒤 `--yes`.

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/deploy-nos-receiver/nos.py guard-check          # 로컬 대 NOS 해시, 서비스·설정 유무(읽기 전용)
PYTHONIOENCODING=utf-8 python .claude/skills/deploy-nos-receiver/nos.py guard-deploy --yes   # 두 파일 백업·교체·NOS 문법 검사(실패면 되돌림), 서비스 있으면 그것만 재시작
PYTHONIOENCODING=utf-8 python .claude/skills/deploy-nos-receiver/nos.py guard-install --yes  # 서비스 만들고 켬. 설정 obstacle_guard.env 는 없을 때만(기본 '기록만')
PYTHONIOENCODING=utf-8 python .claude/skills/deploy-nos-receiver/nos.py guard-status         # 상태·로그·설정·기록 파일·CPU
PYTHONIOENCODING=utf-8 python .claude/skills/deploy-nos-receiver/nos.py guard-remove --yes   # 서비스 끄고 지움(파일·설정·기록은 남김)
```

- 판단은 관제가 **안내 시나리오 세션 이동 중**에 켤 때만 한다(관제 `OBSTACLE_GUARD_MODE` = off|record|on).
- NOS 설정 `OBSTACLE_RECORD_ONLY=1`(설치 기본)이면 관제가 on 이어도 이동을 취소하지 않는다 — 실기 확인 전 안전장치.
- 기록: `/home/user/Control_system/obstacle_rec/rec_*.jsonl`(상한 300MB). 서비스는 `Nice=10`·`CPUQuota=60%`.
- NOS 에서 토픽을 볼 때는 `ros2 topic list --no-daemon` — 그냥 돌리면 `_ros2_daemon` 이 남아 코어 하나를 100% 쓴다(10-29).

## 되돌리기

NOS 에서(사용자 실행, sudo 비밀번호 필요):
```bash
cd /home/user/Control_system && cp -p receiver.py.bak-<날짜-시각> receiver.py && sudo systemctl restart receiver relay_charge relay_motion
# 669: sudo systemctl restart control-receiver relay-charge relay-motion  (relay_map.py 도 같은 꼬리표 백업으로 되돌림)
```

## 보고

배포 시각(한국 시각, NOS 시계는 수십 초 빠름), 백업 파일 이름, 해시(전·후), 서비스 상태, 실기 확인 필요 항목, 커밋 여부.
