"""위치 재조정 맞음 점수·자동 맞춤 API(POST /robot/relocalize/match, 11-05).

실제 관제 코드: relocalize.relocalize_match → status._select_robot_id/_runtime_entry(현재 맵),
via_route._load_grid(지도), scan_match(점수·맞춤).
가짜로 둔 것: _load_grid 가 돌려줄 지도(4m×42m 통로, 기둥 하나), DB 세션(None — 지도 읽기를 가짜로 바꿔 쓰지 않음).
점수·맞춤 정확도 자체는 sim/scenarios/reloc_match_eval.py(실제 맵 47·49 + 3D 점군)에서 본다.
"""
import importlib
import math
import sys
from unittest.mock import MagicMock

from harness import scenario

RID = 10


def _real_numeric():
    """하네스는 numpy·cv2 를 가짜(MagicMock)로 둔다(sim/harness/backend.py HEAVY_MODULES) — 이 시나리오는 계산을
    실제로 봐야 하므로 진짜를 다시 들인다. 이미 import 된 다른 관제 모듈은 그대로 가짜를 쓴다."""
    for m in ("numpy", "cv2"):
        if isinstance(sys.modules.get(m), MagicMock):
            del sys.modules[m]
    np = importlib.import_module("numpy")
    importlib.import_module("cv2")
    if "app.robot_control.scan_match" in sys.modules:
        importlib.reload(sys.modules["app.robot_control.scan_match"])
    return np


def _api(sim):
    global np
    np = _real_numeric()
    import app.robot_control.relocalize as rl
    return rl if hasattr(rl, "relocalize_match") else None


def _runtime(sim, map_id):
    rt = sim.rt
    with rt._lock:
        rt._runtime[RID] = {"robot_id": RID, "robot_name": "SIM", "robot_ip": "sim", "robot_port": 30000,
                            "current_map_id": map_id, "position": {"x": 0, "y": 0, "yaw": 0, "timestamp": 0}}


class _Grid:
    def __init__(self, pillar=True):
        walls = np.zeros((40, 420), bool)
        walls[9, 10:410] = True
        walls[30, 10:410] = True
        if pillar:
            walls[27:30, 200:203] = True
        self.raw, self.resolution, self.origin = walls, 0.1, (0.0, 0.0)


def _scan():
    """로봇 (21.0, 2.0) 동쪽에서 본 통로 벽 + 기둥(기둥 뒤 벽은 가림). 로봇 기준 x·y."""
    global np
    np = _real_numeric()
    xs = np.linspace(1.5, 40.5, 400)
    pts = [(x, 0.95) for x in xs] + [(x, 3.05) for x in xs]
    pil = [(20.0 + i * 0.1, 1.05 + j * 0.1) for i in range(3) for j in range(3)]
    ang = lambda px, py: math.atan2(py - 2.0, px - 21.0)
    lo, hi = min(ang(*q) for q in pil), max(ang(*q) for q in pil)
    pts = [q for q in pts if not (lo <= ang(*q) <= hi)] + pil
    return [[round(px - 21.0, 3), round(py - 2.0, 3)] for px, py in pts]


def _call(sim, rl, **kw):
    from fastapi import HTTPException
    body = rl.MatchBody(robot_id=RID, **kw)
    try:
        return {"ok": rl.relocalize_match(body, db=None)}
    except HTTPException as e:
        return {"http": e.status_code, "detail": e.detail}


def _patch_grid(sim, grid):
    import app.navigation.via_route as vr
    orig = vr._load_grid
    vr._load_grid = lambda db, map_id: grid
    return lambda: setattr(vr, "_load_grid", orig)


@scenario("M1 API 있음 — 현행에는 없다(새 기능)", expect=lambda r: r["has_api"])
def m1(sim):
    return {"has_api": _api(sim) is not None}


@scenario("M2 로봇 현재 맵 모름 → 409", expect=lambda r: r.get("http") == 409)
def m2(sim):
    rl = _api(sim)
    if rl is None:
        return {"http": None}
    _runtime(sim, None)
    return _call(sim, rl, points=_scan(), x=21, y=2, yaw=0)


@scenario("M3 맵 파일 못 읽음 → 409", expect=lambda r: r.get("http") == 409)
def m3(sim):
    rl = _api(sim)
    if rl is None:
        return {"http": None}
    _runtime(sim, 49)
    undo = _patch_grid(sim, None)
    try:
        return _call(sim, rl, points=_scan(), x=21, y=2, yaw=0)
    finally:
        undo()


@scenario("M4 점수만(refine 없음) — 맞는 자리 높음·0.5m 어긋난 자리 낮음, 로봇 명령 0",
          expect=lambda r: r.get("s_true", 0) >= 90 and r.get("s_off", 100) < r.get("s_true", 0) and r.get("cmds") == []
          and r.get("refined") is False and r.get("map_id") == 49)
def m4(sim):
    rl = _api(sim)
    if rl is None:
        return {}
    _runtime(sim, 49)
    undo = _patch_grid(sim, _Grid())
    try:
        a = _call(sim, rl, points=_scan(), x=21.0, y=2.0, yaw=0.0)["ok"]
        b = _call(sim, rl, points=_scan(), x=21.5, y=2.0, yaw=0.0)["ok"]
        # 로봇 명령은 하네스 결과의 cmds(가짜 로봇이 받은 명령열)로 본다 — 비어 있어야 한다
        return {"s_true": a["at"]["score"], "s_off": b["at"]["score"], "refined": "refined" in a,
                "map_id": a["map_id"]}
    finally:
        undo()


@scenario("M5 자동 맞춤 — 0.6m·15° 어긋난 시드 → 0.1m·2° 안, 모호 아님",
          expect=lambda r: r.get("e_xy", 9) <= 0.10 and r.get("e_yaw", 99) <= 2.0 and r.get("ambiguous") is False)
def m5(sim):
    rl = _api(sim)
    if rl is None:
        return {}
    _runtime(sim, 49)
    undo = _patch_grid(sim, _Grid())
    try:
        rf = _call(sim, rl, points=_scan(), x=21.6, y=2.0, yaw=math.radians(15), refine=True)["ok"]["refined"]
        return {"e_xy": round(math.hypot(rf["x"] - 21.0, rf["y"] - 2.0), 3),
                "e_yaw": round(abs(math.degrees(rf["yaw"])), 2), "ambiguous": rf["ambiguous"], "score": rf["score"]}
    finally:
        undo()


@scenario("M6 특징 없는 통로 → 모호 표시", expect=lambda r: r.get("ambiguous") is True)
def m6(sim):
    rl = _api(sim)
    if rl is None:
        return {}
    _runtime(sim, 49)
    undo = _patch_grid(sim, _Grid(pillar=False))
    try:
        xs = np.linspace(1.5, 40.5, 400)
        pts = [[round(x - 21.0, 3), -1.05] for x in xs] + [[round(x - 21.0, 3), 1.05] for x in xs]
        rf = _call(sim, rl, points=pts, x=21.6, y=2.0, yaw=0.0, refine=True)["ok"]["refined"]
        return {"ambiguous": rf["ambiguous"], "alt": rf.get("alt")}
    finally:
        undo()


@scenario("M7 점 적음(10개) → 판단 안 함(few_points)", expect=lambda r: r.get("status") == "few_points")
def m7(sim):
    rl = _api(sim)
    if rl is None:
        return {}
    _runtime(sim, 49)
    undo = _patch_grid(sim, _Grid())
    try:
        return {"status": _call(sim, rl, points=[[1.0, 0.0]] * 10, x=0, y=0, yaw=0, refine=True)["ok"]["status"]}
    finally:
        undo()


@scenario("M8 점 4001개 → 요청 거부(입력 상한)", expect=lambda r: r.get("rejected") is True)
def m8(sim):
    rl = _api(sim)
    if rl is None:
        return {}
    from pydantic import ValidationError
    try:
        rl.MatchBody(robot_id=RID, points=[[1.0, 0.0]] * 4001, x=0, y=0, yaw=0)
        return {"rejected": False}
    except ValidationError:
        return {"rejected": True}


# ── 층 도착 시드: 방향을 스캔으로 정한다(11-05) ─────────────────────────────────
# 상황: 로봇은 (21.0, 2.0) 에서 동쪽(0°)을 보고 서 있다. 저장된 도착 시드는 같은 좌표에 방향 −69°(1층 값).
STORED = {"PosX": 21.0, "PosY": 2.0, "PosZ": 0.0, "Yaw": -1.2066}


def _lobby(sim, *, transit=True, points=None, grid="pillar"):
    import inspect
    import app.robot_control.status as st
    import app.robot_io.runtime as rt
    global np
    np = _real_numeric()
    try:
        import app.robot_control.scan_match  # noqa: F401  (진짜 numpy 로 다시 읽힘 — 현행에는 없다)
    except ImportError:
        pass
    _runtime(sim, 49)
    sent = []
    saved = (st.init_pose, st._elevator_lobby_pose, rt.is_initpose_pending, rt.get_initpose_reason)
    st.init_pose = lambda b: sent.append((b.x, b.y, b.yaw)) or {"status": "ok", "transport_ok": True}
    st._elevator_lobby_pose = lambda db, map_id: (dict(STORED), "1F_엘리베이터입구")
    rt.is_initpose_pending = lambda rid: transit
    rt.get_initpose_reason = lambda rid: rt.FLOOR_TRANSIT_REASON if transit else ""
    undo = _patch_grid(sim, None if grid is None else (_LobbyGrid() if grid == "lobby" else _Grid(pillar=(grid == "pillar"))))
    try:
        fn = st.init_pose_elevator_lobby
        has_body = "body" in inspect.signature(fn).parameters
        if has_body:
            res = fn(robot_id=RID, body=st.LobbySeedBody(points=points) if points is not None else None, db=None)
        else:
            res = fn(robot_id=RID, db=None)      # 현행: 스캔을 받지 않는다
        return {"status": res.get("status"), "sent": sent, "scan": (res.get("seed") or {}).get("scan"),
                "has_body": has_body}
    finally:
        undo()
        st.init_pose, st._elevator_lobby_pose, rt.is_initpose_pending, rt.get_initpose_reason = saved


class _LobbyGrid:
    """통로 + 동쪽 끝벽(x≈26, 엘리베이터 쪽 막힌 끝) + 기둥 — 앞뒤가 다른 실제 엘리베이터 앞에 가깝게."""
    def __init__(self):
        g = _Grid(pillar=True)
        g.raw[9:31, 260] = True
        self.raw, self.resolution, self.origin = g.raw, g.resolution, g.origin


def _scan_lobby():
    """(21.0, 2.0) 동쪽에서 본 모습 — 끝벽 너머 통로 벽은 가려 안 보인다."""
    pts = [p for p in _scan() if p[0] + 21.0 < 26.0]
    pts += [[5.05, round(-0.95 + i * 0.1, 3)] for i in range(20)]   # 끝벽 x=26.05(칸 중심)
    return pts


def _yaw_deg(sent):
    return round(math.degrees(sent[0][2]), 1) if sent else None


@scenario("L1 [재현] 층 도착 — 로봇은 동쪽(0°)인데 저장 방향 −69° → 스캔으로 0° 를 정해 한 번 주입",
          expect=lambda r: len(r.get("sent", [])) == 1 and r.get("yaw_deg") is not None and abs(r["yaw_deg"]) <= 3
          and r.get("xy_err", 9) <= 0.15)
def l1(sim):
    r = _lobby(sim, points=_scan_lobby(), grid="lobby")
    if r["sent"]:
        r["yaw_deg"] = _yaw_deg(r["sent"])
        r["xy_err"] = round(math.hypot(r["sent"][0][0] - 21.0, r["sent"][0][1] - 2.0), 3)
    r.pop("scan", None)
    return r


@scenario("L2 (회귀) 스캔 없이 요청 → 저장 좌표·방향 그대로 한 번 주입",
          expect=lambda r: r.get("sent") == [(21.0, 2.0, -1.2066)])
def l2(sim):
    r = _lobby(sim, points=None, grid="lobby")
    r.pop("scan", None)
    return r


@scenario("L3 앞뒤가 똑같은 통로(방향 구분 안 됨) → 저장 방향으로 넣고 사유를 돌려줌",
          expect=lambda r: r.get("sent") == [(21.0, 2.0, -1.2066)] and r.get("scan_ok") is False)
def l3(sim):
    global np
    np = _real_numeric()
    xs = np.linspace(1.5, 40.5, 400)
    pts = [[round(x - 21.0, 3), -1.05] for x in xs] + [[round(x - 21.0, 3), 1.05] for x in xs]
    r = _lobby(sim, points=pts, grid="plain")
    r["scan_ok"] = (r.get("scan") or {}).get("ok")
    r["reason"] = (r.pop("scan", None) or {}).get("reason")
    return r


@scenario("L4 스캔이 지도와 안 맞음(다른 곳 스캔) → 저장 방향 + 사유",
          expect=lambda r: r.get("sent") == [(21.0, 2.0, -1.2066)] and r.get("scan_ok") is False)
def l4(sim):
    global np
    np = _real_numeric()
    rng = np.random.default_rng(3)
    pts = (rng.uniform(-6, 6, (300, 2))).round(3).tolist()   # 지도와 무관한 점들
    r = _lobby(sim, points=pts)
    r["scan_ok"] = (r.get("scan") or {}).get("ok")
    r["reason"] = (r.pop("scan", None) or {}).get("reason")
    return r


@scenario("L5 (회귀) 층 이동 중이 아니면 → 넣지 않음(skipped)",
          expect=lambda r: r.get("status") == "skipped" and r.get("sent") == [])
def l5(sim):
    r = _lobby(sim, transit=False, points=_scan_lobby(), grid="lobby")
    r.pop("scan", None)
    return r


@scenario("M9 자동 맞춤 — 끈 방향이 반대(180°)여도 맞는 방향을 찾음",
          expect=lambda r: r.get("e_xy", 9) <= 0.10 and r.get("e_yaw", 99) <= 2.0)
def m9(sim):
    rl = _api(sim)
    if rl is None:
        return {}
    _runtime(sim, 49)
    undo = _patch_grid(sim, _Grid())
    try:
        rf = _call(sim, rl, points=_scan(), x=21.3, y=2.0, yaw=math.pi - 0.2, refine=True)["ok"]["refined"]
        return {"e_xy": round(math.hypot(rf["x"] - 21.0, rf["y"] - 2.0), 3),
                "e_yaw": round(abs(math.degrees(math.atan2(math.sin(rf["yaw"]), math.cos(rf["yaw"])))), 2),
                "ambiguous": rf["ambiguous"]}
    finally:
        undo()
