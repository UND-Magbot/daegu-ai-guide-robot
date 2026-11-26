# daegu-ai-guide-robot
> AI 안내견 로봇 관제 플랫폼 — 로봇 위치/상태 모니터링, 맵·경로·스케줄 관리, 음성 안내 시나리오, 영상 녹화, 사용자/공지 관리

병원·관공서·주차장에서 방문객이 목적지를 찾아가도록 돕는 안내 로봇 프로젝트이다. 4족(바퀴형) 보행 로봇 "큐리"가 음성으로 목적지를 확인하고 직접 데려다 준다.

Next.js Web UI + FastAPI 관제 서버 + ROS2 UDP 중계기 + Jetson 음성 안내로 구성되며, 관제 시스템이 지도·일정·음성 시나리오로 로봇을 움직인다.

> **처음이라면 `docs/프로젝트_전체_가이드/00_README_읽는순서.md` 부터 읽는다.** 시스템 구조, 네트워크, 배포 절차, 운영 점검, 이슈 대장이 14개 문서(00~13)로 정리되어 있다. 아래 내용은 빌드·실행 요약이다.

---

## 구성

| 컴포넌트 | 경로 | 스택 | 진입점 / 포트 |
|----------|------|------|----------------|
| Frontend (Web UI) | `Frontend/` | Next.js 15.5.7, React 18, TypeScript 5, Tailwind 4, Three.js | `app/` (App Router) · 3000(dev) / 3001(docker) |
| Backend (관제 서버) | `Backend/` | FastAPI, Python 3.10.9, SQLAlchemy + PyMySQL, OpenCV, FFmpeg | `app/main.py` · 8010 |
| robot_receiver (ROS2 중계) | `robot_receiver/` | ROS2 (rclpy), 표준 라이브러리 | `receiver.py` UDP 40000·TCP 40001 · `relay_map.py` UDP 50000 · `relay_charge.py` UDP 50001 · `obstacle_guard.py` UDP 40100 |
| robot_voice (음성 안내) | `robot_voice/` | Python, Jetson | systemd `robot-voice`·`stt-server`(Jetson 에서 실행) |
| robot_vision (사람 인식) | `robot_vision/` | Python | `control-vision` 컨테이너 |
| tools/robotlog (로그 수집) | `tools/robotlog/` | Python | `control-robotlog` 컨테이너 |
| sim (사전 시뮬 하네스) | `sim/` | Python | `sim/README.md` |
| Docker | `docker-compose.local.yml` · `windows.yml` · `linux.yml` | db(MariaDB) + backend(8010) + mediamtx(카메라 중계) + frontend(3001) + vision + robotlog | 아래 "빌드 & 실행" 참고 |

### 1. Frontend (`Frontend/`)
Next.js 15 App Router 기반 정적 사이트(`output: 'export'`).
주요 페이지: 대시보드 / 알림 / 데이터관리 / 맵관리 / 운영관리 / 스케줄관리 / 설정.
개발 모드에서는 `next.config`의 프록시로 `localhost:8010` Backend에 붙음.

### 2. Backend (`Backend/`)
FastAPI 관제 서버. 주요 모듈 — `auth`, `map`, `navigation`, `robot_control`, `robot_io`(UDP/ASDU), `recording`(FFmpeg), `alerts`, `notices`, `scheduler`, `statistics`, `backup`, `users`, `businesses`. DB는 MySQL/MariaDB.

### 3. robot_receiver (`robot_receiver/`)
로봇 PC(ROS2 환경)에서 실행되는 UDP 중계기.
- `receiver.py` — Backend에서 오는 명령을 UDP 40000으로 수신
- `relay_map.py` — ROS2 `/odom` + PointCloud2 → UDP 50000
- `relay_charge.py` — ROS2 `/CHARGE_STATUS` → UDP 50001

---

## 빌드 & 실행

### Docker (전체) — 권장

compose 파일은 용도별로 세 개다. 모두 db + backend + mediamtx + frontend + vision + robotlog 를 한 번에 띄운다(프로젝트명 `und-control`).

| 파일 | 용도 | backend 네트워크 |
|------|------|------------------|
| `docker-compose.local.yml` | 관제 PC 운영본(Windows Docker Desktop) | bridge |
| `docker-compose.windows.yml` | Windows 신규 PC 배포 (`db/init/` 덤프 자동 import) | bridge — 호스트에 로봇망 라우트 필요 |
| `docker-compose.linux.yml` | Linux 신규 PC 배포 | host — 로봇/receiver 와 직접 UDP·TCP |

`.env` 의 `COMPOSE_FILE` 로 기본 대상을 정하면 `-f` 없이 `docker compose` 와 `start.bat` / `stop.bat` / `logs.bat` 을 그대로 쓸 수 있다.

```bash
cp .env.example .env              # 값 채우기 (JWT_SECRET_KEY 필수). 기본 COMPOSE_FILE=docker-compose.windows.yml
# Linux 면 .env 의 COMPOSE_FILE 을 docker-compose.linux.yml 로 바꾼다
docker compose up -d --build      # Windows 는 start.bat (기동 + 백엔드 healthy 대기)
# frontend → http://localhost:3001
# backend  → http://localhost:8010
```

- 종료: `stop.bat` (DB 보존) / `stop.bat --clean` (DB 볼륨까지 삭제)
- 로그: `logs.bat` 또는 `logs.bat backend`
- 이미 운영 중인 PC 에서 한 서비스만 다시 빌드할 때는 `--no-deps` 를 준다: `docker compose up -d --build --no-deps backend frontend`
- `.env` 에 넣은 값은 compose 의 backend `environment:` 목록에도 있어야 컨테이너에 전달된다. 새 환경 변수는 `Backend/app/robot_io/config.py`·`.env.example`·compose 3개를 함께 고친다(`docs/프로젝트_전체_가이드/08_설정_환경변수_배포.md`).
- 컨테이너는 떠 있는데 8010·3001 접속이 안 되면 Windows 동적 포트 범위를 확인한다: `netsh int ipv4 show dynamicport tcp` (시작이 49152 여야 함, `docs/프로젝트_전체_가이드/02_네트워크_포트_장비.md` 8절).

**외부 DB 사용** — 내장 db 대신 다른 PC 의 DB(예: `192.168.0.21`)에 붙을 때는 `.env` 에 `DATABASE_URL` 을 넣고 db 없이 띄운다. 재빌드는 필요 없다. 스키마명은 `APP_ENV` 와 짝을 맞춘다(prod=`control_system` / dev=`control_system_dev`).

```env
DATABASE_URL=mysql+pymysql://계정:비밀번호@192.168.0.21:3306/control_system?charset=utf8mb4&connect_timeout=5&read_timeout=10&write_timeout=10
```
```bash
docker compose up -d --no-deps backend mediamtx frontend
```

공유기/로봇망에 관제 PC와 로봇/NOS가 함께 붙는 현장에서는 `.env`의 로봇 통신 값을 먼저 맞춘다. 이미지를 다시 빌드하지 않아도 `.env` 수정 후 backend 컨테이너 재생성만 하면 반영된다.

```env
ROBOT_IP=10.21.31.103
ROBOT_PORT=30000
RECEIVER_IP=
RECEIVER_PORT=40000
RECEIVER_TCP_PORT=40001
NOS_HOST=10.21.31.106
```

`RECEIVER_IP`를 비워두면 backend는 `NOS_HOST`를 receiver 주소로 사용한다.

```bash
docker compose up -d backend
curl http://localhost:8010/robot/connection
```

### Frontend (개별)
```bash
cd Frontend
npm install
npm run dev        # 개발 서버 :3000
npm run build      # 정적 export
npm run start      # 프로덕션
```

### Backend (개별)
```bash
cd Backend
python -m venv venv              # 가상환경 이름은 venv (.gitignore 처리됨)
venv\Scripts\activate            # Windows
source venv/bin/activate         # Linux/macOS
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload
```

### robot_receiver (로봇 PC, ROS2 환경)
```bash
# ROS2 환경 source 후 실행
python3 receiver.py        # 명령 수신 :40000
python3 relay_map.py       # 맵/odom 중계 → :50000
python3 relay_charge.py    # 충전 상태 중계 → :50001
```

로봇 IP나 포트가 현장마다 다르면 receiver 실행 환경에도 같은 변수를 export한다.

```bash
export ROBOT_IP=10.21.31.103
export ROBOT_PORT=30000
export RECEIVER_PORT=40000
export RECEIVER_TCP_PORT=40001
python3 receiver.py
```

---

## 환경변수 (루트 `.env`, 템플릿 `.env.example`)

> 실제 값은 레포에 포함되지 않음. `.env`는 `.gitignore` 처리됨.

| 키 | 용도 |
|----|------|
| `JWT_SECRET_KEY` | 인증 토큰 서명 키 |
| `COMPOSE_FILE` | `docker compose`·bat 의 기본 compose 파일 |
| `DB_ROOT_PASSWORD`, `DB_NAME`, `DB_HOST_PORT`, `APP_ENV` | 내장 DB 설정 / 스키마 프로필 |
| `DATABASE_URL` | (선택) 외부 DB 접속 주소 — 넣으면 내장 db 대신 사용 |
| `CORS_ALLOWED_ORIGINS` | 허용 오리진 (localhost:3000, 3001 등) |
| `CORS_ALLOWED_ORIGIN_REGEX` | 사설망 IP 대역 (10.x, 192.168.x, 172.16-31.x) |
| `ROBOT_IP`, `ROBOT_PORT` | 로봇 본체 ASDU UDP 엔드포인트 |
| `RECEIVER_IP`, `RECEIVER_PORT`, `RECEIVER_TCP_PORT` | robot_receiver UDP/TCP 엔드포인트 |
| `NOS_HOST`, `NOS_PORT`, `NOS_USER`, `NOS_PASSWORD` | 매핑/파일 전송용 로봇 PC SSH 접속 정보 |

**열화상 카메라 계정**은 루트 `.env` 가 아니라 `robot_vision/thermal.env` 에 둔다(`thermal_max_overlay.py` 가 읽음). 저장소에는 비밀번호가 없으므로 받은 뒤 `robot_vision/thermal_env.sample` 을 `thermal.env` 로 복사해 `THERMAL_CAMERA_PASS` 를 채운다(같은 이름의 환경 변수로 줘도 된다). 값이 없으면 스크립트는 실행하지 않고 종료한다. 자세한 설명은 `docs/프로젝트_전체_가이드/08_설정_환경변수_배포.md`.

---

## 데이터 볼륨 (Docker)

| 경로 | 용도 |
|------|------|
| `db_data` (named volume) | 내장 MariaDB 데이터 |
| `Backend/data/` | DB 백업, 영구 데이터 |
| `Backend/static/` | 맵 이미지 등 정적 자원 |
| `backend_logs/` | 백엔드 실행 로그 |

---

## 트러블슈팅

| 증상 | 원인 | 조치 |
|------|------|------|
| 화면이 backend 에 연결 안 됨 | 프론트는 브라우저 접속 hostname 의 8010 으로 직접 호출함 | 8010 포트 개방·`BACKEND_PORT` 변경 금지 확인 |
| `docker compose` / bat 실행 시 `no configuration file provided` | `.env` 에 `COMPOSE_FILE` 이 없음(루트에 `docker-compose.yml` 은 없다) | `.env` 에 `COMPOSE_FILE=docker-compose.<local\|windows\|linux>.yml` 지정 |
| 영상 녹화 시작 실패 | FFmpeg 미설치 / PATH 누락 | `Backend/ffmpeg_bin/` 사용 또는 시스템 PATH에 ffmpeg 추가 |
| Backend 기동 시 DB 연결 실패 | DB 미기동, `.env` DB 설정 누락, `APP_ENV`↔스키마명 불일치 | `docker compose ps` 로 db 확인, 루트 `.env` 채움 |
| robot_receiver 데이터 안 들어옴 | ROS2 토픽 미수신 또는 UDP 50000/50001 방화벽 차단 | `ros2 topic echo /odom` 확인, 해당 포트 개방 |
| receiver.py를 윈도우에서 리눅스 서버로 복사 시 `/usr/bin/env: 'python3\r'` 오류 | Windows CRLF 줄바꿈이 셸뱅에 섞임(`python3\r`) | 리눅스에서 `sed -i 's/\r$//' ~/Control_system/receiver.py` (또는 `dos2unix`) 실행 |

---

## 주요 설계 포인트

- **컴포넌트 분리** — 관제 PC (FE+BE) ↔ 로봇 PC (robot_receiver/ROS2) UDP 통신으로 ROS2 의존성을 로봇 PC에만 격리
- **Frontend Static Export** — `output: 'export'`로 정적 사이트 빌드 → CDN/Nginx 손쉽게 배포, dev 모드에서만 `next.config` 프록시 사용
- **Backend 네트워크** — Windows(Docker Desktop)는 bridge: polling 이 같은 소켓 req/resp 라 NAT 로 응답이 돌아온다(호스트에 로봇망 라우트 필요). Linux 배포는 host network 로 직접 송수신
- **권한 정책** — 대시보드는 `dashboard` 권한 하나로만 통제 (내부 섹션 추가 필터 없음)
- **데이터 영속화** — `db_data`, `Backend/data/`, `Backend/static/` 볼륨 마운트로 컨테이너 재시작 시에도 보존

---

## 라이선스
Private / UND-Magbot 내부 사용
