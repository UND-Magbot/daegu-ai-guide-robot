"""안내 반복 실행(시연 전 반복 테스트) — 다음 회차를 열어도 되는지 판정한다.

반복 자체는 관제 화면이 이어 간다(같은 시나리오·출발 장소로 세션을 다시 연다). 화면을 닫으면 멈추므로
아무도 보지 않는 무인 주행이 계속되지 않는다. 여기서는 회차가 끝날 때마다 화면이 묻는 판정만 한다.

멈추는 조건(2026-10-13 시뮬레이션 — docs/분석보고/2026-10-13/분석보고_2026-10-13_안내반복실행_검토.md):
    failed      정상 종료가 아니다 — 이동 실패·무응답·인식 실패·운영자 종료·음성 엔진 끊김 …
    no_answer   정상 종료지만 고객 대답이 한 번도 없었다. 주차장은 대답이 없으면 "알겠습니다" 로
                정상 종료돼 안내 장소에서 인사만 되풀이한다.
    battery     배터리가 로봇의 최소 배터리 기준(robot_info.LimitBattery) 이하 — 관제 알림을 남긴다
    battery_unknown 배터리를 읽지 못했다 — 낮은지 모르는 채로 계속 돌리지 않는다
    position    위치 미확정 — 다음 회차의 첫 이동이 거부된다
"""

from typing import Optional

RESULT_COMPLETED = "completed"


def judge_next_round(result: Optional[str], heard: Optional[str], battery_pct: Optional[int],
                     limit_pct: Optional[int], position_pending: bool) -> tuple[bool, str, str]:
    """반환: (다음 회차 진행 여부, 멈춘 종류, 사람이 읽는 사유)."""
    if result != RESULT_COMPLETED:
        return False, "failed", f"직전 회차가 정상 종료되지 않았습니다({result or '알 수 없음'})."
    if not (heard or "").strip():
        return False, "no_answer", "직전 회차에 고객 대답이 없었습니다."
    if battery_pct is None:
        return False, "battery_unknown", "배터리 잔량을 확인할 수 없습니다."
    limit = limit_pct or 0
    if limit > 0 and battery_pct <= limit:
        return False, "battery", f"배터리 {battery_pct}% — 최소 배터리 기준 {limit}% 이하입니다."
    if position_pending:
        return False, "position", "로봇 위치가 확정되지 않았습니다."
    return True, "", ""
