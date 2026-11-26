"""측위 상실 자동 재측위(11-06) — 관제가 로봇 스캔을 지도 전체와 맞춰 위치를 찾아 넣는다.

11-06 13:30 로봇이 매핑 구역 밖으로 나갔다 대기장소로 돌아왔지만 NOS 는 2분 넘게 5.9m 떨어진
(-6.7, 0.84) 를 보고했다(스스로 다시 찾지 않음). 운영자가 위치를 넣자 1초 만에 잡혔다.

흐름(상태 heartbeat 마다 check 호출, 실제 일은 작업 스레드):
  1) 미확정이 사유별 대기 시간(측위 상실 30초·위치 급변 15초)을 넘겼고, 주행·매핑·충전(도크)·층 이동·
     위치 재조정 창이 아니고, 보고 위치가 5초 동안 제자리. 측위 상실 사유인데 로봇이 지금 정상(0)을 보고하면
     곧 저절로 풀리므로 하지 않는다(로봇 스스로 회복·안내 시작 전 30초 회복 대기를 막지 않게).
  2) 스캔 중계를 한 번 켜서(찾기·확인 내내 유지, 끝나면 끔) 스캔 3장 → 장마다 scan_match.global_locate
     → 세 장이 같은 자리(0.3m·5°)를 가리키고 모두 채택 기준을 넘을 때만 다음으로
  3) 미확정 사유를 '자동 재측위 중'으로 바꾼 뒤(측위 정상 보고·급변 복귀로 저절로 풀리지 않게) 위치를 넣고
     수렴(Location=0·넣은 곳 근처)을 기다린 다음, 스캔 1장으로 그 자리 점수를 다시 본다
  4) 통과하면 미확정은 그대로 두고 사유만 '자동 재측위 완료 — 완료로 확정'(기록만, 새 알림 없음 — 상실 때 뜬
     '위치 확인 필요' 확인창이 그대로 있다). 넣은 뒤 실패면 '자동 재측위 실패' 알림(이 미확정에서는 다시 안 넣음).
  운영자가 중간에 재조정 창을 열거나 직접 넣으면(사유가 바뀜) 그 자리에서 손을 떼고 아무것도 덮어쓰지 않는다.
  넣기 전 실패(점수·모호·장마다 다름)는 같은 자리에서 60초마다 3번 → 5분 뒤 1번 → 움직이기 전까지 멈춤.
  움직였다 서면 처음부터(지도 밖에서 실패하고 돌아온 경우).
"""
from __future__ import annotations

import json
import math
import threading
import time

import app.robot_io.runtime as runtime
from app.robot_io.config import AUTO_RELOCALIZE_ENABLED
from app.logs.service import log_event
from app.user_cache import get_robot_business_id, get_robot_id, get_robot_name

AUTO_RELOC_REASON = "자동 재측위 중"
AUTO_RELOC_DONE_REASON = "위치 확인 필요(자동 재측위 완료 — '완료'로 확정)"
AUTO_RELOC_FAIL_REASON = "위치 확인 필요(자동 재측위 실패)"
_OWN_REASONS = (AUTO_RELOC_REASON, AUTO_RELOC_DONE_REASON, AUTO_RELOC_FAIL_REASON)

# 미확정이 된 뒤 이만큼은 로봇·기존 자동 해제에 맡긴다(사유별, 없으면 기본)
START_DELAY_SEC = {
    runtime.LOCALIZATION_LOST_REASON: 30.0,   # 안내 시작 전 측위 회복 대기 30초와 같음
    "위치 급변 감지": 15.0,                    # polling 의 '원래 자리 10초' 자동 해제보다 길게
}
START_DELAY_DEFAULT_SEC = 5.0
STILL_SEC = 5.0
# 상실 중에도 보고 좌표는 흔들린다(11-06 13:32~13:34 정지 중 x 범위 0.53m) — 제자리 판단은 넉넉히,
# 진짜 정지 확인은 스캔 세 장이 같은 자리를 가리키는지로 한다.
STILL_SPREAD_M = 0.6
SCAN_COUNT = 3
AGREE_M = 0.3
AGREE_DEG = 5.0
RETRY_SEC = 60.0
MAX_TRIES = 3              # 같은 자리에서 60초마다 이만큼
SLOW_RETRY_SEC = 300.0     # 그 뒤 한 번 더(보고 좌표가 굳어 움직임을 못 보는 경우), 그다음은 움직일 때까지 멈춤
RELAY_WARMUP_SEC = 3.0
SCAN_TRIES = 20

_lock = threading.Lock()
_state: dict[int, dict] = {}


def _st(rid: int) -> dict:
    return _state.setdefault(rid, {"hist": [], "episode": None, "tries": 0, "next_at": 0.0,
                                   "slow_used": False, "busy": False, "done": False})


def _reset_spot(st: dict) -> None:
    st.update(tries=0, next_at=0.0, slow_used=False)


def _pending_since(rid: int) -> float:
    with runtime._lock:
        return float((runtime._runtime.get(rid) or {}).get("_pending_since") or 0.0)


def _blocked(rid: int) -> str | None:
    """지금 자동 재측위를 하면 안 되는 이유. 없으면 None."""
    from app.robot_control import auto_init_pose as aip
    from app.robot_control.relocalize import is_relocalize_viewing
    reason = runtime.get_initpose_reason(rid)
    if reason == runtime.FLOOR_TRANSIT_REASON or reason in _OWN_REASONS:
        return "사유"
    if runtime.is_floor_transit(rid):
        return "층 이동 중"
    if runtime.is_docked(rid):
        return "도크(충전소 재주입이 맡음)"
    if aip._is_navigating() or aip._is_mapping():
        return "주행·매핑 중"
    if is_relocalize_viewing():
        return "운영자 재조정 중"
    if (aip._state.get(rid) or {}).get("in_progress"):
        return "전원 on 자동 주입 중"
    return None


def check(rid: int | None) -> None:
    """상태 heartbeat 마다 호출 — 조건이 맞으면 작업 스레드를 띄운다."""
    if not AUTO_RELOCALIZE_ENABLED or rid is None:
        return
    now = time.time()
    reason = runtime.get_initpose_reason(rid)
    with _lock:
        st = _st(rid)
        if not runtime.is_initpose_pending(rid):
            st.update(hist=[], episode=None, done=False)
            _reset_spot(st)
            return
        episode = (reason, _pending_since(rid))
        if st["episode"] != episode and reason not in _OWN_REASONS:
            st.update(episode=episode, done=False)
            _reset_spot(st)
        if st["busy"] or st["done"]:
            return
    if _blocked(rid):
        with _lock:
            _st(rid)["hist"] = []
        return
    since = _pending_since(rid)
    if since and now - since < START_DELAY_SEC.get(reason, START_DELAY_DEFAULT_SEC):
        return
    pos = runtime.get_position(rid) or {}
    ts = pos.get("timestamp", 0) or 0
    if not ts or now - ts > 5.0 or "x" not in pos:
        return
    if reason == runtime.LOCALIZATION_LOST_REASON and pos.get("location") == 0:
        return          # 로봇이 정상을 보고 중 — polling 이 곧 미확정을 푼다
    with _lock:
        st = _st(rid)
        hist = [h for h in st["hist"] if now - h[0] <= STILL_SEC + 2.0] + [(now, pos["x"], pos["y"])]
        st["hist"] = hist
        xs = [h[1] for h in hist]
        ys = [h[2] for h in hist]
        if max(xs) - min(xs) > STILL_SPREAD_M or max(ys) - min(ys) > STILL_SPREAD_M:
            st["hist"] = hist[-1:]
            if st["tries"] or st["next_at"] > now or st["slow_used"]:
                # 움직였다 = 다른 자리. 지도 밖에서 실패했어도 돌아와 서면 처음부터 다시(11-06 13:30 처럼)
                print(f"[AUTO-RELOC] robot {rid} 움직임 — 다시 서면 처음부터 탐색")
                _reset_spot(st)
            return
        if now - hist[0][0] < STILL_SEC:
            return
        if now < st["next_at"] or (st["tries"] >= MAX_TRIES and st["slow_used"]):
            return
        if st["tries"] >= MAX_TRIES:
            st["slow_used"] = True
        st["busy"] = True
        st["tries"] += 1
        st["hist"] = []
        tries = st["tries"]
    print(f"[AUTO-RELOC] robot {rid} 미확정·제자리 {STILL_SEC:.0f}초 — 지도 전체 탐색 시작({tries}번째)")
    threading.Thread(target=_worker, args=(rid,), daemon=True, name=f"auto_reloc_{rid}").start()


# ── 로봇 쪽(시뮬에서 바꿔 끼운다) ──────────────────────

def _relay_on() -> None:
    from app.map.mapping_control import start_relay_map
    start_relay_map(force=True)
    time.sleep(RELAY_WARMUP_SEC)


def _relay_off() -> None:
    """운영자가 재조정 창을 열었으면 그 창이 쓰므로 끄지 않는다."""
    from app.map.mapping_control import stop_relay_map
    from app.robot_control.relocalize import is_relocalize_viewing
    if not is_relocalize_viewing():
        stop_relay_map()


def _grab(n: int) -> list[list]:
    """켜진 중계에서 로봇 기준 스캔 n 장."""
    from app.map.ws_mapping import request_mapping_data
    out: list[list] = []
    for _ in range(SCAN_TRIES):
        if len(out) >= n:
            break
        raw = request_mapping_data("LOC_BODY")
        d = json.loads(raw) if raw else {}
        if d.get("type") == "body" and d.get("points"):
            out.append([p[:2] for p in d["points"]])
            time.sleep(1.0)
        else:
            time.sleep(0.5)
    return out


def _field(rid: int):
    from app.database.database import SessionLocal
    from app.navigation.via_route import _load_grid
    from app.robot_control import scan_match
    map_id = runtime.get_current_map_id(rid)
    if not map_id:
        return None, None
    db = SessionLocal()
    try:
        grid = _load_grid(db, int(map_id))
    finally:
        db.close()
    if grid is None:
        return None, map_id
    return scan_match.field_for_grid(int(map_id), grid), map_id


# ── 작업 ───────────────────────────────────────────

def _yaw_deg(a: float, b: float) -> float:
    return abs(math.degrees(math.atan2(math.sin(a - b), math.cos(a - b))))


def _locate(f, scans: list) -> tuple[dict | None, str]:
    """스캔 여러 장으로 위치를 정한다. (fit, 사유)."""
    from app.robot_control import scan_match
    if len(scans) < SCAN_COUNT:
        return None, f"스캔을 {len(scans)}/{SCAN_COUNT}장만 받음"
    fits = []
    for pts in scans:
        res = scan_match.global_locate(f, pts)
        if not res["ok"]:
            return None, res["reason"]
        fits.append(res)
    a = fits[0]["fit"]
    for r in fits[1:]:
        b = r["fit"]
        if math.hypot(a["x"] - b["x"], a["y"] - b["y"]) > AGREE_M or _yaw_deg(a["yaw"], b["yaw"]) > AGREE_DEG:
            return None, f"장마다 다른 자리(({a['x']:.2f},{a['y']:.2f}) ↔ ({b['x']:.2f},{b['y']:.2f})) — 움직이는 중"
    fit = dict(max(fits, key=lambda r: r["fit"]["score"])["fit"])
    fit["gap"] = min(r["gap"] for r in fits)
    return fit, "ok"


def _operator_took_over(rid: int) -> bool:
    """넣기 시작한 뒤 운영자가 재조정 창을 열었거나 직접 넣어 사유가 바뀌었는지."""
    from app.robot_control.relocalize import is_relocalize_viewing
    return (is_relocalize_viewing() or not runtime.is_initpose_pending(rid)
            or runtime.get_initpose_reason(rid) != AUTO_RELOC_REASON)


def _verify_after(rid: int, f, pose: dict) -> tuple[bool, str]:
    """넣은 뒤: 수렴(Location=0·넣은 곳 근처) + 그 자리 스캔 점수."""
    from app.robot_control import auto_init_pose as aip
    from app.robot_control import scan_match
    ok, why = aip._await_converged(rid, pose, 8.0)
    if not ok:
        return False, f"수렴 안 함({why})"
    if _operator_took_over(rid):
        return False, "운영자"
    scans = _grab(1)
    if not scans:
        return False, "넣은 뒤 스캔을 못 받음"
    p = runtime.get_position(rid) or {}
    got = scan_match.score_pose(f, scan_match.to_laser(scans[0]), p.get("x", 0.0), p.get("y", 0.0), p.get("yaw", 0.0))
    if got["score"] < scan_match.SEED_MIN_SCORE:
        return False, f"넣은 뒤 그 자리 맞음 {got['score']:.0f}%"
    return True, f"넣은 뒤 맞음 {got['score']:.0f}%"


def _log(kind: str, action: str, msg: str, detail: str) -> None:
    try:
        log_event(kind, action, msg, error_json=detail, robot_id=get_robot_id(),
                  robot_name=get_robot_name(), business_id=get_robot_business_id())
    except Exception as e:  # noqa: BLE001 — 기록 실패가 판단을 막지 않게
        print(f"[AUTO-RELOC] 기록 실패: {e}")


def _after_fail(rid: int, why: str) -> None:
    with _lock:
        st = _st(rid)
        if st["tries"] < MAX_TRIES:
            st["next_at"] = time.time() + RETRY_SEC
            nxt = f"{RETRY_SEC:.0f}초 뒤 다시"
        elif not st["slow_used"]:
            st["next_at"] = time.time() + SLOW_RETRY_SEC
            nxt = f"{SLOW_RETRY_SEC:.0f}초 뒤 한 번 더"
        else:
            nxt = "움직였다 서면 다시"
    print(f"[AUTO-RELOC] robot {rid} 위치를 정하지 못함 — {why} ({nxt})")


def _worker(rid: int) -> None:
    from app.robot_control import auto_init_pose as aip
    relay = False
    try:
        f, map_id = _field(rid)
        if f is None:
            _after_fail(rid, f"맵 {map_id} 지도 파일을 읽지 못함")
            return
        _relay_on()
        relay = True
        fit, why = _locate(f, _grab(SCAN_COUNT))
        if fit is None:
            _after_fail(rid, why)
            return
        prev = runtime.get_initpose_reason(rid)
        if not runtime.is_initpose_pending(rid) or _blocked(rid):
            print(f"[AUTO-RELOC] robot {rid} 탐색 중 상황 바뀜(회복·운영자 조작 등) — 넣지 않음")
            return
        pose = {"PosX": fit["x"], "PosY": fit["y"], "PosZ": 0.0, "Yaw": fit["yaw"]}
        where = f"({fit['x']:.2f}, {fit['y']:.2f}) 방향 {math.degrees(fit['yaw']):.0f}°"
        runtime.set_initpose_pending(rid, True, AUTO_RELOC_REASON)
        with _lock:
            _st(rid)["done"] = True        # 넣기 시작하면 이 미확정에서는 다시 하지 않는다
        print(f"[AUTO-RELOC] robot {rid} 지도 전체 탐색 결과 {where} 맞음 {fit['score']:.0f}%·점수 차 {fit['gap']:.0f}"
              f" — 위치 넣음(이전 사유: {prev})")
        sent = aip._send_init_pose_via_receiver(pose)
        if _operator_took_over(rid):
            print(f"[AUTO-RELOC] robot {rid} 운영자가 이어받음 — 자동 재측위 손 뗌")
            return
        if sent != "ok":
            runtime.set_initpose_pending(rid, True, AUTO_RELOC_FAIL_REASON)
            _log("error", "robot_initpose_manual_needed", "로봇 위치 확인 필요",
                 f"자동 재측위: 위치를 로봇에 넣지 못했습니다({sent}). 위치 재조정으로 직접 잡아 주세요. 찾은 위치={where}")
            print(f"[AUTO-RELOC] robot {rid} 위치 넣기 실패({sent})")
            return
        ok, vwhy = _verify_after(rid, f, pose)
        if _operator_took_over(rid):
            print(f"[AUTO-RELOC] robot {rid} 운영자가 이어받음 — 자동 재측위 손 뗌")
            return
        if not ok:
            runtime.set_initpose_pending(rid, True, AUTO_RELOC_FAIL_REASON)
            _log("error", "robot_initpose_manual_needed", "로봇 위치 확인 필요",
                 f"자동 재측위: {where} 에 넣었지만 확인에 실패했습니다({vwhy}). 위치 재조정으로 직접 잡아 주세요.")
            print(f"[AUTO-RELOC] robot {rid} 넣은 뒤 확인 실패 — {vwhy}")
            return
        runtime.set_initpose_pending(rid, True, AUTO_RELOC_DONE_REASON)
        _log("robot", "robot_initpose_auto_reloc", "자동 재측위 완료",
             f"{where} 맞음 {fit['score']:.0f}%·점수 차 {fit['gap']:.0f}, {vwhy}. 운영자 '완료' 대기(이전 사유: {prev})")
        print(f"[AUTO-RELOC] robot {rid} 자동 재측위 완료 {where} — {vwhy}, 운영자 '완료' 대기")
    except Exception as e:  # noqa: BLE001 — 작업 스레드 예외가 polling 을 멈추지 않게
        print(f"[AUTO-RELOC] robot {rid} 오류: {e}")
    finally:
        if relay:
            try:
                _relay_off()
            except Exception as e:  # noqa: BLE001
                print(f"[AUTO-RELOC] 스캔 중계 끄기 실패: {e}")
        with _lock:
            _st(rid)["busy"] = False
