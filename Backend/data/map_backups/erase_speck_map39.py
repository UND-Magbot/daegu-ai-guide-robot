"""UND_1F_병원(map 39) 안내 장소↔접수 장소 사이 0.3m 점유 덩어리 제거.

2026-10-02. 직선 (0.233,-2.364)→(1.501,-2.01) 위 (0.6~0.9, -2.5~-2.2) 6칸 덩어리가 벽으로
판정돼 자동 우회가 5m 남쪽 '경유지'로 돌아갔다. 현장 확인 결과 빈 공간.
관제 우회 판정용 PGM 만 고치고 공유 DB 사본도 갱신한다(로봇 지도 zip·표시용 PNG 는 그대로).
원본: data/map_backups/UND_1F_병원.pgm.20261002-before-speck-erase

실행: docker exec control-backend python /app/data/map_backups/erase_speck_map39.py
"""
import itertools

import cv2
import numpy as np

from app.database.database import SessionLocal
from app.database.models import LocationInfo, RobotMapInfo
from app.map.map_file_store import file_status, store_file
from app.navigation import via_route as V

MAP_ID = 39
PGM = "/app/static/maps/UND_1F_병원.pgm"
YAML = "/app/static/maps/UND_1F_병원.yaml"
HIT_CELL = (112, 126)   # 직선이 맞은 칸(row, col)
MAX_BLOB_CELLS = 10     # 이보다 크면 엉뚱한 덩어리 — 중단

db = SessionLocal()
places = [p for p in db.query(LocationInfo).filter(LocationInfo.MapId == MAP_ID).all()
          if p.LocationX is not None]


def decisions() -> dict:
    V._cache.clear()
    g = V._load_grid(db, MAP_ID)
    return {(a.LacationName, b.LacationName):
            g.line_blocked((a.LocationX, a.LocationY), (b.LocationX, b.LocationY))
            for a, b in itertools.permutations(places, 2)}


before = decisions()

data = bytearray(open(PGM, "rb").read())
pos, toks = 0, []
while len(toks) < 4:
    while data[pos:pos + 1].isspace():
        pos += 1
    if data[pos:pos + 1] == b"#":
        pos = data.index(b"\n", pos) + 1
        continue
    start = pos
    while not data[pos:pos + 1].isspace():
        pos += 1
    toks.append(bytes(data[start:pos]))
w, h, maxval = int(toks[1]), int(toks[2]), int(toks[3])
off = pos + 1
assert toks[0] == b"P5" and maxval < 256

_res, _org, negate, occ_thresh = V._parse_yaml(YAML)
px = np.frombuffer(bytes(data[off:off + w * h]), np.uint8).reshape(h, w)
occupied = ((px / maxval) if negate else 1 - px / maxval) > occ_thresh
_n, lab, stats, _ = cv2.connectedComponentsWithStats(occupied.astype(np.uint8), connectivity=8)
blob = lab[HIT_CELL]
assert blob != 0, "직선이 맞은 칸이 이미 비어 있음 — 이미 적용됐을 수 있다"
assert stats[blob, cv2.CC_STAT_AREA] <= MAX_BLOB_CELLS, f"덩어리가 너무 큼: {stats[blob]}"
cells = np.argwhere(lab == blob)
r0, c0 = cells.min(axis=0) - 3
r1, c1 = cells.max(axis=0) + 4
free = int(np.median(px[r0:r1, c0:c1][~occupied[r0:r1, c0:c1]]))
print(f"덩어리 {len(cells)}칸 {[int(px[r, c]) for r, c in cells]} → 빈 칸 값 {free}")
for r, c in cells:
    data[off + r * w + c] = free
open(PGM, "wb").write(bytes(data))

after = decisions()
changed = [k for k in before if before[k] != after[k]]
print(f"판정 바뀐 장소 쌍 {len(changed)}개:")
for k in changed:
    print("  ", k, before[k], "->", after[k])
target = db.query(LocationInfo).filter(LocationInfo.id == 350).first()
print("안내 장소→접수 장소:", V.plan_via_waypoints(db, target, (0.233, -2.364)))

store_file(db, MAP_ID, "pgm", PGM)
m = db.query(RobotMapInfo).filter(RobotMapInfo.id == MAP_ID).first()
print("DB 동기화:", file_status(db, m)["pgm"])
