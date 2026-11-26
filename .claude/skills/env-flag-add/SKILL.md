---
name: env-flag-add
description: 관제 백엔드에 새 환경 변수(.env 설정값·기능 스위치)를 추가하거나 값을 바꿀 때, 또는 ".env 에 넣었는데 적용이 안 된다"는 상황에서 쓴다. config.py·.env.example·compose 3개 목록을 함께 고치고 대조 스크립트로 확인한다.
---

# 환경 변수 추가·변경 점검

`.env` 값은 compose 의 backend `environment:` 목록에 있어야 컨테이너로 전달된다. 이걸 빠뜨려 "켰는데 안 먹는" 일이
10-06~10-22 사이 8회 이상 반복됐다(`NAV_CONTINUOUS_PASS`·`MEDIAMTX_API`·`ROUTE_GRAPH_ALWAYS_MAPS`·`NAV_MULTIPOINT_ENABLED`·`NODE_ROUTING_ENABLED`).

## 새 키를 넣을 때 함께 고칠 곳 (한 번에)

| # | 파일 | 할 일 |
|---|---|---|
| 1 | 코드(`Backend/app/robot_io/config.py` 의 `_env_str/_env_int/_env_bool` 등) | 기본값과 함께 읽기. 기본값은 "꺼짐·지금 동작 유지" 쪽 |
| 2 | `docker-compose.local.yml`·`windows.yml`·`linux.yml` | backend `environment:` 에 `- 키=${키:-기본값}` 와 한 줄 설명 주석. 세 파일 모두 |
| 3 | `.env.example` | 키와 설명(비밀값은 빈 값) |
| 4 | `.env` | 운영 값이 필요할 때만. 바꾸기 전 `.env.bak_날짜_주제` 백업 |

코드 변경이 들어가면 presim 절차(사전 시뮬)를 먼저 따른다. compose·.env 만 바꾸는 건 동작 변경이므로 "경량"으로 보고하고 진행 여부를 묻는다.

## 대조

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/env-flag-add/check_env.py            # 전체
PYTHONIOENCODING=utf-8 python .claude/skills/env-flag-add/check_env.py 키1 키2     # 특정 키
PYTHONIOENCODING=utf-8 python .claude/skills/env-flag-add/check_env.py --running  # 실행 중 컨테이너까지
```

- `❌ .env 에 있는데 compose 목록에 없음` → 반드시 고친다(종료 코드 1).
- `⚠ 컨테이너에 없음` → 컨테이너가 옛 설정으로 떠 있다. 아래 "적용" 필요.
- 알려진 의도적 차이(고치지 않음): `ARM_MOCK`(local 에서 일부러 뺌, 팔 미연결), `SEED_*_PASSWORD`(운영 local 에는 없음, 신규 PC 용).
- 비밀값은 출력하지 않는다. 사용자에게 값을 물어 받아야 하면 채팅에 그대로 남지 않게 `.env` 를 직접 고치도록 안내한다.

## 적용 (사용자 승인 후)

- 값만 바뀌면 재빌드 없이: `docker compose -f docker-compose.local.yml -p und-control up -d --no-deps --force-recreate backend`
- 코드도 바뀌었으면 `/deploy-control` 절차로 배포한다.
- 적용 뒤 `check_env.py 키 --running` 으로 컨테이너에 들어갔는지 확인하고,
  값 확인이 필요하면 `docker exec control-backend printenv 키` (비밀값 키는 하지 않는다).
- 권한 검사에 막히면 명령을 그대로 적어 사용자에게 `!` 실행을 부탁한다.

## 보고

바꾼 파일 표(파일·추가한 줄), 대조 결과(❌·⚠ 개수), 적용 여부(재생성함/안 함), 실기 확인 필요 여부.
