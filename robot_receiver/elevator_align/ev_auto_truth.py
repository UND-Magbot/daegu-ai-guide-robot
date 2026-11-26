"""IMU 로 엘리베이터 탑승 구간(정답)을 자동으로 찾는다 — 운영자 기록 없이 채점하기 위함.

원리
  - 로봇이 걸으면 몸이 크게 흔들린다(가속도·각속도 분산 큼). 서 있으면 조용하다.
  - 엘리베이터는 로봇이 서 있는 동안 출발(위/아래 가속)과 정지(반대 방향 가속)를 낸다.
  - 그래서 '조용한 구간' 안에 반대 부호의 수직 가속 두 번이 있으면 그 조용한 구간 = 엘리베이터 안.
  - 밖 = 탑승 구간 앞뒤로 여유(OUT_GAP_SEC)를 둔 나머지. 문 통과 중은 채점하지 않는다.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

MOVE_STD = 0.25          # 1초 창 수직 가속 표준편차(m/s²) 이상이면 걷는 중
MOVE_GYRO = 0.15         # 1초 창 평균 각속도(rad/s) 이상이면 도는 중
STILL_MIN_SEC = 3.0      # 이보다 짧은 정지는 무시
ACC_WIN_SEC = 1.5        # 엘리베이터 가속 평균 창
ACC_THR = 0.12           # 창 평균 수직 가속(m/s²) — 엘리베이터 가감속 판정
RIDE_MIN_SEC = 3.0       # 출발~정지 최소·최대 간격
RIDE_MAX_SEC = 180.0
IN_MARGIN_SEC = 2.0      # 정지 구간 양 끝에서 깎는 여유
OUT_GAP_SEC = 20.0       # 탑승 구간 앞뒤로 채점에서 빼는 시간(문 앞 대기·통과)


@dataclass
class Ride:
    still_start: float
    still_end: float
    depart: float
    arrive: float
    direction: str          # "up" | "down"
    peak: float
    dz: float | None = None


def _window(samples, i, half):
    return samples[max(0, i - half): i + half + 1]


def pick_imu(imu_lines: list[dict]) -> list[dict]:
    """IMU 토픽이 여럿이면 샘플이 가장 많은 것."""
    by_topic: dict[str, list[dict]] = {}
    for line in imu_lines:
        by_topic.setdefault(line.get("tp", ""), []).append(line)
    return max(by_topic.values(), key=len) if by_topic else []


def vertical_accel(imu: list[dict]) -> list[float]:
    """중력 축(평균 |a| 최대)을 위(+)로 맞춘 수직 가속에서 중앙값(중력)을 뺀 값."""
    axis = max(range(3), key=lambda k: abs(statistics.fmean(s["a"][k] for s in imu)))
    sign = 1.0 if statistics.fmean(s["a"][axis] for s in imu) > 0 else -1.0
    av = [sign * s["a"][axis] for s in imu]
    g = statistics.median(av)
    return [v - g for v in av]


def still_segments(imu: list[dict], av: list[float], hz: float) -> list[tuple[float, float]]:
    """서 있는 구간. 걸음은 빠른 흔들림이라, 0.45초 평균을 뺀 고주파만 본다
    (엘리베이터 가감속처럼 1~2초 이어지는 가속은 '걷기'로 보지 않기 위함)."""
    half = max(1, int(hz * 0.5))
    short = max(1, int(hz * 0.225))
    hp = [v - statistics.fmean(_window(av, i, short)) for i, v in enumerate(av)]
    moving = []
    for i in range(len(imu)):
        win = _window(hp, i, half)
        std = statistics.pstdev(win) if len(win) > 1 else 0.0
        rot = statistics.fmean(sum(abs(x) for x in g["w"]) for g in _window(imu, i, half))
        moving.append(std > MOVE_STD or rot > MOVE_GYRO)
    segs, start = [], None
    for s, mv in zip(imu, moving):
        if not mv and start is None:
            start = s["i"]
        elif mv and start is not None:
            segs.append((start, s["i"]))
            start = None
    if start is not None:
        segs.append((start, imu[-1]["i"]))
    # 1.5초 미만 흔들림(자세 미세 조정 등)으로 끊긴 정지는 잇는다
    merged: list[tuple[float, float]] = []
    for a, b in segs:
        if merged and a - merged[-1][1] < 1.5:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [(a, b) for a, b in merged if b - a >= STILL_MIN_SEC]


def accel_events(imu, av, hz, seg) -> list[tuple[float, float]]:
    """정지 구간 안의 엘리베이터 가감속 — [(시각, 부호 있는 최대 평균 가속)]."""
    half = max(1, int(hz * ACC_WIN_SEC / 2))
    # 구간 양 끝은 걸음의 잔여 흔들림이 섞이므로 창 길이만큼 버리고, 창도 구간 안으로 자른다
    idx = [i for i, s in enumerate(imu) if seg[0] + ACC_WIN_SEC <= s["i"] <= seg[1] - ACC_WIN_SEC]
    lo, hi = (idx[0], idx[-1]) if idx else (0, -1)
    events, run = [], []
    for i in idx:
        s = imu[i]
        m = statistics.fmean(av[max(lo, i - half): min(hi, i + half) + 1])
        same_run = run and (run[-1][1] > 0) == (m > 0)
        if abs(m) >= ACC_THR and same_run:
            run.append((s["i"], m))
            continue
        if run:  # 가속이 끝났거나 부호가 바뀜 → 지금까지를 한 사건으로
            events.append(max(run, key=lambda r: abs(r[1])))
        run = [(s["i"], m)] if abs(m) >= ACC_THR else []
    if run:
        events.append(max(run, key=lambda r: abs(r[1])))
    return events


def _odom_dz(odom: list[dict], t0: float, t1: float) -> float | None:
    def z_at(t):
        near = min(odom, key=lambda o: abs(o["o"] - t), default=None)
        return near["p"][2] if near and abs(near["o"] - t) < 5 else None

    a, b = z_at(t0), z_at(t1)
    return None if a is None or b is None else round(b - a, 2)


def find_rides(imu_lines: list[dict], odom_lines: list[dict] | None = None) -> list[Ride]:
    imu = sorted(pick_imu(imu_lines), key=lambda s: s["i"])
    if len(imu) < 50:
        return []
    span = imu[-1]["i"] - imu[0]["i"]
    hz = (len(imu) - 1) / span if span > 0 else 20.0
    av = vertical_accel(imu)
    rides = []
    for seg in still_segments(imu, av, hz):
        ev = accel_events(imu, av, hz, seg)
        for j, (t0, m0) in enumerate(ev):
            later = [(t1, m1) for (t1, m1) in ev[j + 1:]
                     if (m1 > 0) != (m0 > 0) and RIDE_MIN_SEC <= t1 - t0 <= RIDE_MAX_SEC]
            if later:
                t1, _ = later[-1]
                rides.append(Ride(seg[0], seg[1], t0, t1, "up" if m0 > 0 else "down",
                                  round(abs(m0), 3), _odom_dz(odom_lines or [], t0, t1)))
                break
    return rides


def labels_from_rides(rides: list[Ride], t_end: float) -> list[tuple[float, float, str]]:
    """탑승 구간 → in, 그 앞뒤 OUT_GAP 밖 → out."""
    labels = []
    cursor = 0.0
    for k, r in enumerate(rides, 1):
        if r.still_start - OUT_GAP_SEC > cursor:
            labels.append((cursor, r.still_start - OUT_GAP_SEC, "out_auto"))
        labels.append((r.still_start + IN_MARGIN_SEC, r.still_end - IN_MARGIN_SEC, f"in_ride{k}_{r.direction}"))
        cursor = r.still_end + OUT_GAP_SEC
    if t_end > cursor:
        labels.append((cursor, t_end, "out_auto"))
    return labels
