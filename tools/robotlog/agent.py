"""관제 컨테이너(control-backend) 안에서 도는 중계 — robotlog.py 가 복사해 넣고 `python /tmp/robotlog_agent.py '<JSON>'` 로 부른다.

- 본체·NOS 에 SSH(.env NOS_USER/NOS_PASSWORD, 관제가 이미 쓰는 값)로 들어가 remote.py 를 표준 입력으로 실행한다.
- 실행할 때마다 로봇 시계 차이(dE = 로봇 epoch − 실제 epoch)를 SSH 왕복 중간값으로 잰다.
- 관제 DB 의 사건(logdata_info)·안내 세션(service_session)도 같은 모양으로 낸다.
읽기 전용이다. 출력은 한 줄에 JSON 하나.
"""
import json
import os
import sys
import time

import paramiko

HOSTS = {"nos": "10.21.31.106", "body": "10.21.31.103"}

# 출처별 파일과 잡음(반복 줄) 규칙 — 11-05 실측(분석보고 2026-11-05) 기준. 늘릴 때는 여기만 고친다.
FILE_SOURCES = {
    "nos": [
        {"label": "NOS측위", "file": "localization", "noise": r"定位正常运行中|重置定位状态|输入源："},
        {"label": "NOS계획", "file": "planner",
         "noise": r"planner_status is|global_dis in path|valid velocity command|Can't find path|当前参数值|追踪距离|角度容差|"
                  r"直线纠偏|最小X速度|最小Y速度|最小Yaw速度|最大X速度|最大Y速度|最大Yaw速度|导航模式|后退模式|减速率|"
                  r"关闭停障|停障距离|位置容差|calculation time"},
        {"label": "NOS충전", "file": "charge_manager",
         "noise": r"^\d+$|mInitYaw|sendX:|get Target position|最近障碍物距离"},
        {"label": "NOS센서", "file": "handler", "noise": r"sensor loss|Data freq"},
    ],
    "body": [
        {"label": "본체작업", "file": "Basic_server",
         # 이동 점마다 반복되는 설정 확인 줄(결과 1 = 성공)·상태 정상 줄은 뺀다. 실패(결과 0)·오류는 남는다.
         "noise": r"与设置步态一致|与设置motion状态一致|运动速度：|导航速度：|匹配数|硬件状态正常|软件状态正常|结果:1$|"
                  r"地图文件为空|wayPoints is empty|设置低速度|设置导航速度|开启障碍物检测|设置正走|设置directLine_mode|^$"},
        {"label": "본체보행", "file": "rl_deploy", "noise": r"send Data update timeout"},
        {"label": "본체MCU", "file": "ctrlmcu_node", "noise": r"ledStausHandler|Receive Led Status"},
    ],
}
# 우리 서비스 journal(NOS). 669 는 하이픈 이름, 296 은 다른 이름일 수 있어 둘 다 넣는다.
JOURNAL = {
    "label": "NOS서비스",
    "units": ["control-receiver", "robot-receiver", "obstacle-guard", "relay-charge", "relay-motion",
              "relay_charge", "relay_motion"],
    "noise": r"활성 에러 \d+건: \[\{'errorCode': 0, 'component': 0\}\]|\[BAT\] 업데이트|BASIC STATUS|\[CHARGE\] state=0, error_code=0|"
             r"\[TCP\] 수신: (STATUS|POSITION)|^수신: (NAV_STATUS|STATUS|POSITION)$|CHARGE_STATUS → state=0, error_code=0|"
             r"UDP STATUS\] 응답 전송",
}

REMOTE_PATH = os.environ.get("ROBOTLOG_REMOTE", "/tmp/robotlog_remote.py")   # 수집 컨테이너는 /robotlog/remote.py


def remote_src():
    with open(REMOTE_PATH, encoding="utf-8") as fh:
        return fh.read()


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def connect(host):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, port=int(os.environ.get("NOS_PORT") or 22), username=os.environ["NOS_USER"],
              password=os.environ["NOS_PASSWORD"], timeout=8)
    return c


def measure_clock(c):
    """로봇 epoch − 실제 epoch. 세 번 재서 왕복이 가장 짧은 것."""
    best = None
    for _ in range(3):
        t0 = time.time()
        _, o, _ = c.exec_command("date +%s.%N")
        robot = float(o.read().decode().strip())
        t1 = time.time()
        if best is None or t1 - t0 < best[0]:
            best = (t1 - t0, robot - (t0 + t1) / 2)
    _, o, _ = c.exec_command("date +%z")
    return best[1], best[0], o.read().decode().strip()


def run_remote(c, job, host_key, sink=None):
    """remote.py 를 로봇에서 표준 입력으로 실행하고(파일 안 남김) 작업 JSON 은 인자로 넘긴다. 결과 한 줄마다 sink 로."""
    sink = sink or emit
    stdin, stdout, stderr = c.exec_command(
        "python3 - '%s'" % json.dumps(job, ensure_ascii=False).replace("'", "'\\''"), timeout=600)
    stdin.write(remote_src())
    stdin.channel.shutdown_write()
    for raw in stdout:
        raw = raw.strip()
        if raw:
            try:
                d = json.loads(raw)
            except ValueError:
                d = {"kind": "error", "text": raw}
            d["host"] = host_key
            sink(d)
    err = stderr.read().decode("utf-8", "replace").strip()
    if err:
        sink({"kind": "error", "host": host_key, "text": err[-2000:]})


def db_events(lo, hi):
    """관제 DB: 사건 로그·안내 세션 시작/끝 (관제 시각 = 실제 시각, 컨테이너 TZ 는 한국)."""
    from datetime import datetime
    from app.database.database import SessionLocal
    from app.database.models import LogDataInfo, ServiceSession
    a, b = datetime.fromtimestamp(lo), datetime.fromtimestamp(hi)
    db = SessionLocal()
    try:
        for r in db.query(LogDataInfo).filter(LogDataInfo.CreatedAt >= a, LogDataInfo.CreatedAt <= b):
            emit({"t": r.CreatedAt.timestamp(), "src": "관제사건", "text": "%s/%s %s" % (r.Category, r.Action, r.Message or "")})
        for s in db.query(ServiceSession).filter(ServiceSession.StartedAt <= b,
                                                 (ServiceSession.EndedAt.is_(None)) | (ServiceSession.EndedAt >= a)):
            if a <= s.StartedAt <= b:
                emit({"t": s.StartedAt.timestamp(), "src": "관제세션", "text": "세션 #%d 시작 (일정 %s)" % (s.id, s.ScheduleId)})
            if s.EndedAt and a <= s.EndedAt <= b:
                emit({"t": s.EndedAt.timestamp(), "src": "관제세션", "text": "세션 #%d 끝 — %s" % (s.id, s.Result)})
    finally:
        db.close()


def main():
    req = json.loads(sys.argv[1])
    op = req["op"]
    if op == "db":
        sys.path.insert(0, "/app")
        try:
            db_events(req["lo"], req["hi"])
        except Exception as e:  # noqa: BLE001 — DB 가 안 되면 그 출처만 빠진다
            emit({"kind": "error", "host": "db", "text": repr(e)})
        return
    for key in req.get("hosts", ["nos", "body"]):
        try:
            c = connect(HOSTS[key])
        except Exception as e:  # noqa: BLE001 — 로봇이 다른 망이면 접속이 안 된다
            emit({"kind": "error", "host": key, "text": "접속 실패: %r" % e})
            continue
        try:
            dE, rtt, tz = measure_clock(c)
            emit({"kind": "clock", "host": key, "dE": dE, "rtt": rtt, "tz": tz})
            if op == "clock":
                continue
            base = {"dE": dE, "lo": req.get("lo"), "hi": req.get("hi")}
            if op == "timeline":
                run_remote(c, dict(base, op="logs", sources=FILE_SOURCES[key], limit=req.get("limit", 4000)), key)
                if key == "nos":
                    run_remote(c, dict(base, op="journal", **JOURNAL), key)
                    if req.get("pose", True):
                        run_remote(c, dict(base, op="pose", step=req.get("step", 0.5)), key)
                        run_remote(c, dict(base, op="jumps"), key)
            elif op in ("pose", "jumps") and key == "nos":
                run_remote(c, dict(base, op=op, step=req.get("step", 0.5)), key)
                if op == "pose":
                    run_remote(c, dict(base, op="jumps"), key)
            elif op == "grep":
                run_remote(c, dict(base, op="grep", sources=FILE_SOURCES[key], days=req["days"],
                                   pattern=req["pattern"], pattern_grep=req["pattern"], limit=req.get("limit", 300)), key)
        finally:
            c.close()


if __name__ == "__main__":
    main()
