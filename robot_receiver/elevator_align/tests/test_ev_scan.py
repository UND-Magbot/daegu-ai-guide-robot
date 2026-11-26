"""엘리베이터 안/밖 기록기 파싱·분석기 판정 검증(합성 데이터)."""
import gzip
import json
import math
import random
import struct
from types import SimpleNamespace

from elevator_align.align_target import BoxSpec
from elevator_align.ev_auto_truth import find_rides
from elevator_align.ev_scan_analyze import analyze, build_parser, load_record
from elevator_align.ev_scan_recorder import parse_points
from elevator_align.sim_scan import generate_scan

CAR = BoxSpec()                                   # 2.2 x 2.2 엘리베이터
HALL = BoxSpec(depth=10.0, width=8.0, door_w=0.0)  # 넓은 홀


def _frame(t, box, pose, rng):
    pts = generate_scan(box, pose, n_rays=360, noise=0.01, rng=rng)
    return {"t": t, "n": len(pts), "pts": [[x, y, 0.0] for x, y in pts], "ms": 1}


WALKING = [(0, 50), (55, 62), (178, 186)]   # 걷는 구간(나머지는 서 있음)


def _imu_line(t, rng):
    """20Hz IMU — 걸을 땐 몸이 크게 흔들리고, 70s 상승 출발·168s 도착 감속."""
    walking = any(a <= t < b for a, b in WALKING)
    az = 9.81 + rng.gauss(0, 0.02)
    w = [rng.gauss(0, 0.01) for _ in range(3)]
    if walking:
        az += 2.0 * math.sin(2 * math.pi * 2.5 * t)
        w = [0.3 * math.sin(2 * math.pi * 2.5 * t + k) for k in range(3)]
    if 70 <= t < 72:
        az += 0.6
    if 168 <= t < 170:
        az -= 0.6
    return {"i": round(t, 2), "tp": "/IMU", "a": [0.01, -0.02, round(az, 3)], "w": w}


def _write_trip(path):
    """0~60s 홀 → 60~180s 엘리베이터 안 → 180~240s 홀 (라이다 2Hz, IMU 20Hz)."""
    rng = random.Random(1)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"meta": {"label": "trip", "start_local": "test"}}) + "\n")
        for k in range(240 * 20):
            f.write(json.dumps(_imu_line(k / 20, rng)) + "\n")
        for i in range(480):
            t = i / 2
            if 60 <= t < 180:
                f.write(json.dumps(_frame(t, CAR, (1.1, 0.05, 0.1), rng)) + "\n")
            else:
                f.write(json.dumps(_frame(t, HALL, (5.0, 0.3, 0.4), rng)) + "\n")
        f.write('{"t": 240.5, "n": 3, "pts": [[0')  # 끊긴 꼬리


def test_parse_points_reads_xyz_by_field_offset():
    raw = b"".join(struct.pack("<ffff", x, y, z, 9.0) for x, y, z in [(1, 2, 3), (-1, 0.5, 0.2)])
    fields = [SimpleNamespace(name=n, offset=o) for n, o in [("x", 0), ("y", 4), ("z", 8)]]
    msg = SimpleNamespace(data=raw, point_step=16, fields=fields)
    assert parse_points(msg) == [[1.0, 2.0, 3.0], [-1.0, 0.5, 0.2]]


def test_load_record_tolerates_truncated_tail(tmp_path):
    p = str(tmp_path / "scan_trip.jsonl.gz")
    _write_trip(p)
    meta, frames, extra = load_record(p)
    assert meta["label"] == "trip"
    assert len(frames) == 480
    assert len(extra["imu"]) == 4800


def test_trip_judged_in_only_inside_car(tmp_path):
    p = str(tmp_path / "scan_trip.jsonl.gz")
    _write_trip(p)
    labels = tmp_path / "labels.csv"
    # 음성 녹음 시각(기록 시작을 5초에 말함) + 구간 이름
    labels.write_text("start,end,label\n5,62,out_hall_1f\n68,182,in_1to3\n188,245,out_hall_3f\n",
                      encoding="utf-8")
    args = build_parser().parse_args([p, "--labels", str(labels), "--offset", "5"])
    res = analyze(p, args)
    assert res["scored"] > 400
    assert res["false_in"] == 0
    assert res["missed_in"] == 0
    assert (tmp_path / "scan_trip.html").exists()
    assert (tmp_path / "scan_trip.csv").exists()


def test_auto_truth_finds_ride_from_imu(tmp_path):
    p = str(tmp_path / "scan_trip.jsonl.gz")
    _write_trip(p)
    args = build_parser().parse_args([p, "--auto"])
    res = analyze(p, args)
    assert res["scored"] > 250          # 안 112s + 밖(앞뒤 여유 제외) 약 84s, 2Hz
    assert res["false_in"] == 0
    assert res["missed_in"] == 0


def test_find_rides_direction_and_still_window(tmp_path):
    rng = random.Random(1)
    imu = [_imu_line(k / 20, rng) for k in range(240 * 20)]
    rides = find_rides(imu)
    assert len(rides) == 1
    r = rides[0]
    assert r.direction == "up"
    assert 61 <= r.still_start <= 64 and 176 <= r.still_end <= 179
    assert 69 <= r.depart <= 73 and 167 <= r.arrive <= 171


def test_flatten_numbers_walks_nested_fields():
    from elevator_align.ev_scan_recorder import flatten_numbers
    msg = SimpleNamespace(motion_state=SimpleNamespace(state=17), vel_x=0.12, ok=True, name="x")
    assert flatten_numbers(msg) == {"motion_state.state": 17.0, "vel_x": 0.12, "ok": 1}
