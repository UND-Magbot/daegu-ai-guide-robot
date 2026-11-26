# 엘리베이터 안/밖 라이다 데이터 수집 절차

목적: 라이다만으로 "엘리베이터 안/밖"을 가를 수 있는지 실데이터로 확인한다(층 이동 하차 자동 감지 후보).
도구: `ev_scan_recorder.py`(NOS에서 기록) → `ev_scan_analyze.py`(PC에서 판정·채점).
안전: **읽기 전용.** 라이다 토픽을 구독해 파일로 저장만 한다. 속도·자세 명령을 보내지 않고 측위·주행에도 손대지 않는다.

## 준비

- 노트북이 로봇 망(1F AD43 / 3F C92A)에 붙어 있을 것. `ssh jetson`이 되는지 먼저 본다.
- NOS root 비밀번호(relay 실행할 때 쓰는 것).
- 로봇은 **선 자세(STAND)** 로 둔다. 몸 가림 보정(self-mask)이 선 자세 기준이다.

## 1. 기록기 올리기 (PC → NOS, Jetson 경유)

PC의 Git Bash에서 저장소 루트 기준:

```bash
scp -o ProxyJump=jetson -i ~/.ssh/id_ed25519_jetson \
    robot_receiver/elevator_align/ev_scan_recorder.py user@10.21.31.106:/tmp/
```

접속이 안 되면 Jetson에 먼저 올린 뒤 Jetson에서 NOS로 다시 보낸다(`scp ... jetson:/tmp/` → `ssh jetson` → `scp /tmp/ev_scan_recorder.py user@10.21.31.106:/tmp/`).

## 2. NOS 접속·토픽 확인 (1분)

```bash
ssh -J jetson -i ~/.ssh/id_ed25519_jetson user@10.21.31.106
su
source /opt/robot/scripts/setup_ros2.sh
ros2 topic hz /LOC_BODY_POINTS      # 약 10Hz 나오면 Ctrl-C
ros2 topic list -t | grep -i -E 'imu|odom'   # IMU(sensor_msgs/msg/Imu) 토픽이 있어야 자동 정답 가능
df -h /tmp                          # 여유 300MB 이상
```

## 3. 시험 기록 (1분)

```bash
cd /tmp && python3 ev_scan_recorder.py --label test --frames 5
```

`[REC] 종료 — 프레임 5`가 나오면 된다. 0이면 토픽 이름이나 ROS2 환경을 확인한다.

## 4. 기록 — 켜 두고 평소대로 이동만 (운영자 기록 없음)

정답(언제 엘리베이터 안이었나)은 **로봇 IMU로 자동으로** 뽑는다. 운영자는 시각을 적거나 말할 필요가 없다.
- 걸을 땐 몸이 크게 흔들리고, 서 있으면 조용하다.
- 엘리베이터는 로봇이 서 있는 동안 출발(가속)과 도착(감속)을 낸다.
- 그래서 "조용한 구간 안에 출발·도착 가속이 있음" = 엘리베이터 안. 그 앞뒤 20초를 뺀 나머지 = 밖.

### 4.1 시작 (1F, 노트북 앞, 1분)

§2에서 IMU 토픽이 보였는지 먼저 확인한다(`ros2 topic list -t | grep -i imu`). 없으면 §7을 본다.

```bash
mkdir -p /tmp/ev_scan && cd /tmp && nohup python3 ev_scan_recorder.py --label trip1 --duration 1800 > /tmp/ev_scan/trip1.log 2>&1 &
sleep 5; cat /tmp/ev_scan/trip1.log
```

`[REC] IMU·오도메트리 구독: [...]` 에 토픽이 하나 이상 나오면 노트북을 내려놓는다.

### 4.2 이동 (컨트롤러만)

평소대로 다니면 된다. 지키면 좋은 것 세 가지만:
1. **엘리베이터 안에서는 로봇을 가만히 세워 둔다**(스틱 놓기). 안에서 걸으면 운행 구간이 쪼개진다.
2. 가는 길에 **가장 좁은 복도를 한 번 지나간다**(오판 확인용). 멈출 필요는 없다.
3. 선 자세 유지. 앉히지 않는다.

권장 경로: 1F 좁은 복도 → 1F 홀 → 탑승 → 3F 하차 → 3F 홀 → 다시 3F→1F → 1F 홀. 30분이 지나면 저절로 끝난다.

## 5. 파일 가져오기

```bash
mkdir -p docs/작업보고/2026-10-19/작업보고_2026-10-19_자료/ev_scan
scp -o ProxyJump=jetson -i ~/.ssh/id_ed25519_jetson "user@10.21.31.106:/tmp/ev_scan/*" docs/작업보고/2026-10-19/작업보고_2026-10-19_자료/ev_scan/
```

용량: 30분에 약 50~100MB(프레임당 점 수에 따라 다름). `/tmp` 여유가 300MB 이상인지 §2에서 확인한다.

## 6. 판정 돌리기 (PC)

```bash
cd robot_receiver
export PYTHONIOENCODING=utf-8
D=../docs/작업보고/2026-10-19/작업보고_2026-10-19_자료/ev_scan
python -m elevator_align.ev_scan_analyze $D/scan_trip1_*.jsonl.gz --auto
```

- 콘솔: **자동으로 찾은 엘리베이터 운행(정지 구간·출발·도착 시각·상승/하강)**, 판정 구간, 채점(일치율·잘못 '안'·놓친 '안'), 구간별(`in_ride1_up`, `out_auto`) 판정 안 비율·옆벽 폭·먼 점 비율 분포
- 같은 폴더: `<기록>.csv`(프레임별 지표), `<기록>.html`(시간축 그래프: 정답 띠·판정 띠·폭·먼 점 비율)
- 기준을 바꿔 다시 돌리기: `--width-min 1.2 --width-max 2.5 --far-ratio-max 0.1 --smooth 7`
- 3D 점이면 바닥·천장을 뺄 수 있다: `--zmin -0.2 --zmax 1.5` (먼저 콘솔의 '가까운 점 최대 z' 분포를 본다)

## 판정 기준(초기값)과 알려진 한계

- 안 = 옆벽 2개 인식 + 폭 0.9~2.8m + 3m보다 먼 점 비율 15% 이하, 5프레임(약 2.5초) 다수결.
- 합성 데이터로는 엘리베이터 2.2m 안 / 넓은 홀 / 폭 1.8m 긴 복도를 구분했다(`tests/test_ev_scan.py`).
  다만 복도는 먼 점 비율 0.19로 기준 0.15에 가깝다. 실제 로봇은 몸이 앞뒤를 가려 먼 점이 덜 보일 수 있다.
  그래서 **좁은 복도를 지나는 것이 가장 중요하다.** 자동 정답에서는 복도도 `out_auto` 에 들어가므로,
  html 그래프에서 엘리베이터 밖인데 판정 띠가 빨강으로 튀는 곳이 있는지(잘못 '안') 본다.
- 자동 정답은 '엘리베이터 안에서 로봇이 가만히 서 있음'을 전제로 한다. 안에서 움직였으면 그 운행은 짧게 잡히거나 못 찾는다.
- 엘리베이터 안에 사람이 함께 타면 옆벽이 가려질 수 있다.

## 7. IMU가 없거나 운행을 못 찾을 때

- 콘솔에 "엘리베이터 운행을 찾지 못했습니다"가 나오면, 먼저 `--auto` 기준을 낮춰 본다.
  이 건물 엘리베이터 가속이 약하면 `ev_auto_truth.py` 의 `ACC_THR`(기본 0.12m/s²)를 0.08로 낮춘다.
- IMU 토픽이 아예 없으면 정답을 사람이 줘야 한다. 그때는 판정만 보고(`--auto` 없이) 판정 구간이
  실제 탑승·하차와 맞는지 대략 비교하거나, `--labels`(start,end,label CSV)로 채점한다.
