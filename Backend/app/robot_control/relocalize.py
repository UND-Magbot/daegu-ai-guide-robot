"""위치 재조정(스캔 정합) — rviz식 2D Pose Estimate 를 관제 화면 안에서 수행.

동작:
  - 스트림: 로봇 relay_map 을 띄우고, 측위 모드 정합 스캔 /LIO_ALIGNED_POINTS 를 receiver 의
    LIO_ALIGNED 액션으로 폴링해 WebSocket 으로 프론트에 중계.
  - 프론트는 정합 스캔을 저장맵 위에 겹쳐 보여주고(스캔이 벽과 어긋나면 측위 오차), 운영자가
    맵에서 pose 를 드래그로 지정하면 POST /robot/initpose {x,y,yaw} 로 주입한다.
    (수렴 검증·미확정 해제는 그 엔드포인트의 verify_and_clear 가 담당.)
  - 로봇 pose(화살표)는 프론트가 POSITION(ASDU) 로 폴링한다(측위 모드에서 동작).

측위 스택은 LIO 기반이라 매핑 토픽 /SLAM_* 은 측위 중 침묵하고 /LIO_* 가 나온다.
그래서 스캔 소스로 /LIO_ALIGNED_POINTS(map 프레임 정합 점군)를 쓴다. relay_map 은
stream/start 가 미실행 시 SSH 로 기동한다(매핑과 동일 스크립트, /LIO_ALIGNED_POINTS 구독 추가됨).
"""
from __future__ import annotations

import asyncio
import threading

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_ws, require_any_permission
from app.database.database import get_db

router = APIRouter()
# WebSocket 은 HTTP 용 로그인 잠금(Request 기반)을 쓸 수 없어 따로 묶는다.
ws_router = APIRouter()

_LOCALIZE = [Depends(require_any_permission("robot-list", "map-edit", "place-list"))]

# 정합 스캔·pose 폴링 주기(초). 로봇 부하를 감안해 0.5s(매핑 실시간 뷰와 동일).
_POLL_SEC = 0.5
_EMPTY_BACKOFF_AFTER = 6   # 빈 스캔이 이만큼(약 3초) 이어지면
_EMPTY_POLL_SEC = 3.0      # 이 간격으로 늦춘다

# 재조정 창(WS) 수. 열려 있는 동안 위치 폴링을 주행 중과 같은 주기로 당긴다 —
# 스캔은 0.5s 마다 오는데 위치가 2s 늦으면 주입 직후 스캔이 옛 위치로 그려져 어긋나 보인다.
_viewers = 0
_viewers_lock = threading.Lock()


def is_relocalize_viewing() -> bool:
    return _viewers > 0


def _add_viewer(delta: int) -> None:
    global _viewers
    with _viewers_lock:
        _viewers = max(0, _viewers + delta)


@router.post("/robot/relocalize/stream/start", dependencies=_LOCALIZE)
def relocalize_stream_start():
    """스캔 스트림 준비 — 로봇 relay_map(LIO/SLAM 브리지)을 미실행 시 SSH 로 기동한다.

    기동 후 pgrep 로 실제 실행을 확인하고, 실패 시 로봇 로그 tail 을 함께 반환해 진단을 돕는다.
    (start_relay_map 는 실패를 삼키므로, 여기서 실행 여부를 직접 검증한다.)
    """
    import time as _t
    # 점검·로그 조회도 relay 가 실제로 도는 호스트(RELAY_*)를 봐야 한다.
    # 매핑용 NOS 를 보면 항상 STOPPED 로 나와 오진한다.
    from app.map.mapping_control import (
        NOS_RELAY_LOG,
        RELAY_PASSWORD,
        get_relay_ssh_client,
        ssh_exec,
        start_relay_map,
    )
    client = None
    try:
        client = get_relay_ssh_client(retries=2)
        start_relay_map(force=True)   # 좀비 relay 를 물려받지 않도록 새로 띄운다
        _t.sleep(1.2)             # 기동/구독 시작 시간 확보
        running = ssh_exec(
            client,
            "sudo bash -c 'pgrep -f \"relay_[m]ap.py\" >/dev/null && echo RUNNING || echo STOPPED'",
            exec_timeout=10,
            sudo_password=RELAY_PASSWORD,
        )
        if "RUNNING" in running:
            return {"status": "ok", "msg": "스캔 스트림 준비 완료(relay 실행 중)."}
        # 기동 실패 — 로봇 로그 tail 로 사유를 되돌려준다.
        log_tail = ssh_exec(client, f"sudo tail -n 6 {NOS_RELAY_LOG} 2>/dev/null",
                            exec_timeout=10, sudo_password=RELAY_PASSWORD)
        return {
            "status": "error",
            "msg": f"relay_map 기동 실패. 로봇 로그: {log_tail[:400] or '(로그 없음)'}",
        }
    except Exception as e:
        return {"status": "error", "msg": f"스캔 스트림 준비 실패(SSH/relay): {e}"}
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


class MatchBody(BaseModel):
    """스캔 맞음 점수·자동 맞춤 요청. points 는 로봇 기준(body) x·y — 화면이 받은 스캔 그대로."""
    robot_id: int | None = None
    points: list[list[float]] = Field(default_factory=list, max_length=4000)
    x: float
    y: float
    yaw: float
    refine: bool = False


@router.post("/robot/relocalize/match", dependencies=_LOCALIZE)
def relocalize_match(body: MatchBody, db: Session = Depends(get_db)):
    """스캔이 지도 벽과 얼마나 맞는지(점수) — refine 이면 주변에서 가장 잘 맞는 위치·방향도 찾는다.

    로봇에는 아무것도 보내지 않는다(app.robot_control.scan_match). 지도는 로봇의 현재 맵.
    """
    from app.navigation.via_route import _load_grid
    from app.robot_control import scan_match
    from app.robot_control.status import _runtime_entry, _select_robot_id

    rid = _select_robot_id(body.robot_id)
    map_id = _runtime_entry(rid).get("current_map_id") if rid is not None else None
    if not map_id:
        raise HTTPException(status_code=409, detail="로봇의 현재 맵을 알 수 없습니다.")
    grid = _load_grid(db, int(map_id))
    if grid is None:
        raise HTTPException(status_code=409, detail=f"맵 {map_id} 파일을 읽지 못했습니다.")
    res = scan_match.match(scan_match.field_for_grid(int(map_id), grid), body.points,
                           body.x, body.y, body.yaw, body.refine)
    res["map_id"] = int(map_id)
    return res


@router.post("/robot/relocalize/stream/stop", dependencies=_LOCALIZE)
def relocalize_stream_stop():
    """스캔 스트림 종료 — 매핑 진행 중이 아니면 relay_map 을 정리한다(매핑 중이면 유지)."""
    from app.map.mapping_control import stop_relay_map, mapping_state
    if mapping_state.get("is_running"):
        return {"status": "ok", "msg": "매핑 진행 중 — relay 유지"}
    try:
        stop_relay_map()   # 내부에서 릴레이 호스트로 직접 접속한다
        return {"status": "ok", "msg": "스캔 스트림을 종료했습니다."}
    except Exception as e:
        return {"status": "error", "msg": f"스캔 스트림 종료 실패: {e}"}


@ws_router.websocket("/ws/relocalize/view", dependencies=[Depends(get_current_user_ws)])
async def relocalize_ws(ws: WebSocket):
    """정합 스캔(/SLAM_ALIGNED_POINTS)·로봇 pose(/SLAM_ODOM)를 폴링해 프론트로 중계.

    매핑 뷰(ws_mapping)의 request_mapping_data 를 재사용한다. 블로킹 UDP 는 executor 로 돌려
    이벤트 루프를 막지 않는다. 뷰어 1명 기준(위치 재조정은 단일 조작)이라 브로드캐스트 불필요.
    """
    from app.map.ws_mapping import request_mapping_data
    await ws.accept()
    _add_viewer(1)
    loop = asyncio.get_running_loop()

    # 연결 종료를 따로 감시한다. 이 루프는 스캔이 있을 때만 send 하므로, relay 가 꺼져
    # 스캔이 계속 비면 끊김을 영영 모른 채 receiver 에 0.5초마다 요청을 보낸다
    # (2026-09-29: 창을 닫은 뒤에도 좀비 루프가 receiver 를 막아 관제 통신 두절).
    closed = asyncio.Event()

    async def _watch_disconnect():
        try:
            while True:
                if (await ws.receive()).get("type") == "websocket.disconnect":
                    break
        except Exception:
            pass
        finally:
            closed.set()

    watcher = asyncio.create_task(_watch_disconnect())
    empty_polls = 0
    try:
        while not closed.is_set():
            # 1순위 = LOC_BODY(/LOC_BODY_POINTS, body 프레임). 프론트가 로봇 pose 로
            # map 변환해 얹는다(rviz+TF 방식) — 어긋남이 그대로 보여 재조정에 가장 정확하다.
            scan = await loop.run_in_executor(None, request_mapping_data, "LOC_BODY")

            # 폴백 = LIO_ALIGNED(map 프레임 정합 점군, type=aligned).
            # 일부 펌웨어는 /LOC_BODY_POINTS 를 발행하지 않아(publisher 는 잡히는데 무음)
            # 1순위가 영영 비어 오버레이가 안 뜬다. 그 장비에서도 스캔이 벽과 맞는지는
            # 봐야 하므로, 릴레이가 /ALIGNED_POINTS 로 채워 둔 이 캐시로 대체한다.
            if not scan:
                scan = await loop.run_in_executor(None, request_mapping_data, "LIO_ALIGNED")

            if scan:
                await ws.send_text(scan)
                empty_polls = 0
            else:
                empty_polls += 1
            # 스캔이 계속 비면(relay 미기동 등) 간격을 늘려 receiver 부담을 줄인다
            delay = _POLL_SEC if empty_polls < _EMPTY_BACKOFF_AFTER else _EMPTY_POLL_SEC
            try:
                await asyncio.wait_for(closed.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[RELOCALIZE-WS] {e}")
    finally:
        _add_viewer(-1)
        watcher.cancel()
