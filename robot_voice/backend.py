"""관제 백엔드 클라이언트 — 번들 pull + 세션 보고.

**판정은 Jetson 이 한다.** 관제에서 번들(목적지 사전·시나리오 단계·튜닝값)을 받아
로컬 캐시에 두고, STT/의도/목적지 판정을 여기서 끝낸 뒤 결과만 보고한다.
관제가 끊겨도 마지막 캐시로 계속 동작한다 — `WaitFor=operator` 단계만 예외다.

인증은 `X-Agent-Token` 하나다(관제 .env 의 AGENT_TOKEN). 만료가 없어 재로그인 로직이 없다.
"""

import json
import logging
import os
import time
from typing import Any, Optional

import requests

from robot_voice import config

log = logging.getLogger("robot_voice.backend")


class BackendError(RuntimeError):
    """관제 API 호출 실패."""


NAV_ARRIVED = "arrived"
NAV_FAILED = "failed"
NAV_MOVING = "moving"
NAV_IDLE = "idle"          # 아직 출발 전


def nav_phase(st: dict, started: bool) -> str:
    """/robot/nav 응답 하나로 주행 단계를 가른다.

    Args:
        started: 이번 이동에서 주행 중인 걸 한 번이라도 봤는가
    """
    if st.get("arrived"):
        return NAV_ARRIVED
    navigating = bool(st.get("navigating") or st.get("is_navigating"))
    # 관제가 목표에 못 가고(재전송 한도 초과) 주행을 끝냈다 — 제자리에서 도착 멘트를 하지 않는다
    if st.get("unreached") and not navigating:
        return NAV_FAILED
    if navigating:
        return NAV_MOVING
    # 시작됐다가 멈췄는데 도착이 아니면 취소/실패
    return NAV_FAILED if started else NAV_IDLE


def obstacle_holding(st: dict) -> bool:
    """관제가 안내 중 장애물로 멈춰 세워 두고 있는가(풀리면 관제가 다시 출발시킨다)."""
    return bool((st.get("obstacle") or {}).get("holding"))


class BackendClient:
    def __init__(self) -> None:
        self._s = requests.Session()
        self._s.headers.update({"X-Agent-Token": config.AGENT_TOKEN})
        self._base = config.BACKEND_BASE.rstrip("/")
        self._bundle: Optional[dict] = None
        self._bundle_at = 0.0

    # ── 저수준 ────────────────────────────────────────
    def _call(self, method: str, path: str, timeout: Optional[float] = None, **kw: Any) -> Any:
        url = f"{self._base}{path}"
        try:
            r = self._s.request(method, url, timeout=timeout or config.BACKEND_TIMEOUT, **kw)
        except requests.RequestException as e:
            raise BackendError(f"{method} {path} 연결 실패: {e}") from e

        if r.status_code == 401:
            raise BackendError("에이전트 토큰이 거부됐다. 관제 .env 의 AGENT_TOKEN 과 맞출 것.")
        if r.status_code == 503:
            raise BackendError("관제에 AGENT_TOKEN 이 설정되지 않았다(채널 비활성).")
        if r.status_code == 409:
            raise BackendError(f"충돌: {r.json().get('detail', r.text)[:120]}")
        if not r.ok:
            raise BackendError(f"{method} {path} 실패: HTTP {r.status_code} {r.text[:200]}")
        if not r.content:
            return None
        return r.json()

    # ── 번들 ──────────────────────────────────────────
    def _cache_path(self) -> str:
        return config.BUNDLE_CACHE

    def _save_cache(self, bundle: dict) -> None:
        try:
            path = self._cache_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(bundle, f, ensure_ascii=False)
        except Exception as e:
            log.warning("번들 캐시 저장 실패: %s", e)

    def _load_cache(self) -> Optional[dict]:
        try:
            with open(self._cache_path(), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def bundle(self, force: bool = False) -> dict:
        """목적지·시나리오·설정 번들.

        주기 안이면 메모리 캐시를 쓰고, 관제가 죽어 있으면 디스크 캐시로 떨어진다.
        캐시조차 없으면 예외 — 아무것도 모르는 채 동작하면 엉뚱한 곳으로 간다.
        """
        fresh = time.time() - self._bundle_at < config.BUNDLE_REFRESH_SEC
        if self._bundle and fresh and not force:
            return self._bundle

        try:
            data = self._call("GET", f"/voice/bundle/{config.ROBOT_ID}")
            self._bundle = data
            self._bundle_at = time.time()
            self._save_cache(data)
            return data
        except BackendError as e:
            if self._bundle:
                log.warning("번들 갱신 실패, 메모리 캐시 사용: %s", e)
                return self._bundle
            cached = self._load_cache()
            if cached:
                log.warning("번들 갱신 실패, 디스크 캐시 사용: %s", e)
                self._bundle = cached
                return cached
            raise

    def scenario(self, code: str) -> Optional[dict]:
        """번들에서 시나리오 하나를 꺼낸다."""
        for sc in self.bundle().get("scenarios", []):
            if sc.get("code") == code:
                return sc
        return None

    def slots(self) -> list[dict]:
        return self.bundle().get("slots", [])

    def voice_config(self) -> dict:
        return self.bundle().get("config") or {}

    # ── 세션 ──────────────────────────────────────────
    def start_session(self, scenario_code: str, floor_id: Optional[int] = None) -> dict:
        body = {"robotId": config.ROBOT_ID, "scenarioCode": scenario_code}
        if floor_id is not None:
            body["floorId"] = floor_id
        return self._call("POST", "/voice/session", json=body)

    def current_session(self) -> Optional[dict]:
        return self._call("GET", "/voice/session/current",
                          params={"robot_id": config.ROBOT_ID})

    def standby_context(self) -> dict:
        """상시대기 근무 중 지금 손님 말을 들어도 되는가(관제가 판정) — main._standby_context"""
        return self._call("GET", "/DB/agent/standby-context") or {}

    def standby_call(self, heard: str) -> dict:
        """들은 말을 올린다. 시나리오 세션을 열지·어디로 안내할지는 관제가 정한다."""
        return self._call("POST", "/DB/agent/standby-call", json={"heard": heard}) or {}

    def patch_session(self, session_id: int, **fields: Any) -> dict:
        body = {k: v for k, v in fields.items() if v is not None}
        return self._call("PATCH", f"/voice/session/{session_id}", json=body)

    def move(self, session_id: int, place_id: int) -> dict:
        return self._call("POST", f"/voice/session/{session_id}/move",
                          timeout=config.MOVE_TIMEOUT, params={"place_id": place_id})

    def move_check(self, session_id: int, place_id: int) -> dict:
        """이동 전 확인 — {"ok": bool, "reason": str|None}. 로봇을 움직이지 않는다."""
        return self._call("GET", f"/voice/session/{session_id}/move/check",
                          params={"place_id": place_id}) or {"ok": True}

    def report_tts_failed(self, session_id: int, text: str, critical: bool) -> None:
        """발화 실패 보고. 실패해도 시나리오를 멈추지 않는다."""
        try:
            self._call("POST", f"/voice/session/{session_id}/tts-failed",
                       json={"text": text, "critical": critical})
        except BackendError as e:
            log.warning("발화 실패 보고 실패(무시): %s", e)

    def follow(self, session_id: int, state: str) -> dict:
        """동행 확인 결과 — waiting(멈춰 기다림) | lost(운영자 호출) | ok(다시 따라옴)."""
        return self._call("POST", f"/voice/session/{session_id}/follow", json={"state": state})

    def obstacle_answer(self, session_id: int, ask_id: int, answer: str) -> dict:
        """장애물 긴 정지 뒤 '준비됐어요' 질문의 대답 — yes | no | none."""
        return self._call("POST", f"/voice/session/{session_id}/obstacle-answer",
                          json={"askId": ask_id, "answer": answer})

    def hold(self, session_id: int, state: str) -> dict:
        """운영자 일시 정지 — holding(다음 이동 단계 앞에서 멈췄다)."""
        return self._call("POST", f"/voice/session/{session_id}/hold", json={"state": state})

    def close_session(self, session_id: int, result: str, closed_by: str,
                      reason: Optional[str] = None) -> dict:
        return self._call("POST", f"/voice/session/{session_id}/close",
                          json={"result": result, "closedBy": closed_by, "reason": reason})

    def log_dialog(self, heard: str, slot_value: Optional[str] = None,
                   score: Optional[float] = None, acted: bool = False) -> None:
        """대화 로그. 실패해도 시나리오를 멈추지 않는다."""
        try:
            self._call("POST", f"/DB/robots/{config.ROBOT_ID}/voice-dialog-logs",
                       json={"heard": heard, "slotValue": slot_value,
                             "score": score, "acted": 1 if acted else 0})
        except BackendError as e:
            log.debug("대화 로그 실패(무시): %s", e)

    # ── 도착 대기 ─────────────────────────────────────
    def robot_state(self) -> dict:
        """NAV 상태. 도착 판정에 쓴다."""
        return self._call("GET", "/robot/nav") or {}

    def wait_arrival(self, timeout_sec: float = 300.0,
                     on_tick=None, should_stop=None) -> bool:
        """도착까지 폴링한다.

        Args:
            should_stop: 참이 되면 기다리지 않고 False 를 돌려준다(운영자 안내 종료).

        Returns:
            True = 도착, False = 타임아웃 또는 이동 중단.
        """
        deadline = time.time() + timeout_sec
        started = False

        while time.time() < deadline:
            if should_stop and should_stop():
                return False
            try:
                st = self.robot_state()
            except BackendError:
                time.sleep(1.0)
                continue

            if obstacle_holding(st):
                deadline += 1.0          # 장애물 앞에서 멈춰 기다린 시간은 도착 대기에서 뺀다
            phase = nav_phase(st, started)
            if phase == NAV_ARRIVED:
                return True
            if phase == NAV_FAILED:
                return False
            if phase == NAV_MOVING:
                started = True

            if on_tick:
                on_tick(st)
            time.sleep(1.0)

        return False
