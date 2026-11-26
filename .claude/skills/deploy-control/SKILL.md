---
name: deploy-control
description: 관제(노트북 도커의 control-backend·control-frontend)를 다시 빌드해 배포한다. 사전 점검 → 사용자 승인 → 백업 이름표·재빌드 → 사후 확인 → 되돌리기 안내까지. "관제 배포", "백엔드/화면 재빌드" 요청 때 쓴다.
disable-model-invocation: true
---

# 관제 배포

배포는 되돌리기 어렵고 운영 중인 로봇에 영향을 준다. **사전 점검 결과를 보이고 승인을 받기 전에는 빌드·재생성하지 않는다.**
배포는 한 세션만 한다(10-08 여러 세션 동시 배포로 5분 중단). `down -v` 는 DB 를 지우므로 절대 쓰지 않는다.

## 1. 사전 점검 (읽기 전용 — 바로 실행)

```bash
cd .claude/skills/deploy-control && PYTHONIOENCODING=utf-8 python preflight.py
```

점검 내용: 컨테이너 상태 / 진행 중 안내 세션·작업 회차(0건이어야 함) / **이번 배포로 바뀔 백엔드 파일**(실행 중 컨테이너 대 작업 폴더) /
화면 이미지 이후 바뀐 화면 파일 / 환경 변수 전달 대조 / 실행할 명령.

- 판정이 ❌ 면 이유를 보고하고 멈춘다(진행 중 세션이 있으면 끝난 뒤 다시).
- 3번·4번 목록이 이번 요청과 관계없는 파일을 포함하면(다른 세션의 커밋 안 된 작업) **파일 목록을 보이고** 같이 나가도 되는지 묻는다.
  이미지는 작업 폴더 전체로 만들어지므로 일부만 뺄 수 없다.
- 바뀐 쪽만 배포한다: 백엔드 변화 0 이면 `frontend` 만, 화면 변화 0 이면 `backend` 만.
- 코드 변경이 presim(사전 시뮬) 승인을 거쳤는지 확인한다. 안 거쳤으면 먼저 그 절차를 안내한다.

## 2. 보고하고 승인받기

형식:
1. 진행 중 작업: 세션 N · 회차 N
2. 같이 나갈 변경: 백엔드 N개 / 화면 N개 (목록, 이번 요청 외 항목 표시)
3. 환경 변수: 전달 안 되는 키 N개
4. 실행할 명령 (preflight 6번 그대로)
5. "배포할까요?"

## 3. 실행 (승인 후)

preflight 6번의 명령을 순서대로: 백업 이름표 2줄 → `docker compose ... up -d --build --no-deps <대상>`.
- 빌드는 수 분 걸린다. 메모리 부족으로 10분 넘게 걸린 적이 있다(10-07) — 백그라운드로 돌리고 끝나면 이어 간다.
- 권한 검사에 막히면 같은 명령을 적어 사용자에게 `! <명령>` 실행을 부탁하고, 끝났다고 하면 4단계로 간다.
- 환경 변수만 바뀌었으면 빌드 없이 `up -d --no-deps --force-recreate backend` (env-flag-add 스킬 참고).

## 4. 사후 확인 (읽기 전용 — 바로 실행)

```bash
cd .claude/skills/deploy-control && PYTHONIOENCODING=utf-8 python postcheck.py --since 10m
```

정상 기동 / 8010 `/api` 200·`/robot/status` 로그인 없이 401·3001 `/` 200 / 기동 로그 오류 / 컨테이너 코드 = 작업 폴더.
화면 변경이면 바뀐 문구가 번들에 들어갔는지 한 줄 확인: `docker exec control-frontend grep -rl "<바뀐 문구>" /usr/share/nginx/html | head -3`

❌ 가 나오면 원인 로그를 보이고 되돌리기 여부를 묻는다(자동으로 되돌리지 않는다).

## 5. 마무리 보고

- 배포 대상·시각(한국 시각)·백업 이름표·확인 결과 표
- 되돌리기 명령(preflight 6번)
- **커밋 안 됨** 여부, 실기 확인 필요 항목
- 작업일지나 작업보고가 있는 작업이면 "바꾼 파일·배포" 칸에 배포 사실을 적는다.

## 되돌리기

```bash
docker tag und-control-backend:<이름표> und-control-backend:latest
docker tag und-control-frontend:<이름표> und-control-frontend:latest
docker compose -f docker-compose.local.yml -p und-control up -d --no-deps backend frontend   # --build 넣지 않는다
```
