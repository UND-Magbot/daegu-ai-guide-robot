"""경유지 방향 안내 문구(경유지 1~2m 앞에서 알린다) — 따라오는 사람(시각장애인 포함)에게 꺾는 방향을 그때 알려 준다.

출발 전에 "통로를 따라 가다가 오른쪽" 처럼 길 전체를 설명하면 보이지 않는 사람은 알 수 없고,
방문객이 로봇을 마주 보고 있으면 좌우가 뒤집힌다. 걷는 중에는 방문객이 로봇 뒤에서 같은 쪽을
보고 있으므로, 꺾기 직전에 "잠시 후 오른쪽으로 방향을 바꾸겠습니다" 라고 하면 누구에게나 같은 오른쪽이다.
문구는 본사 시연 대본 개정안(2026-10-19, E3)에 맞췄다(2026-10-27).

좌표는 맵(ROS) 기준 오른손 좌표계라 반시계 회전(+)이 왼쪽이다. 2026-10-11 병원 맵(42)에서
원무과_01 → 경유지_01 → 대기의자 경로가 +90°(왼쪽)로 나와 현장 설명("대기자리는 왼쪽")과 맞았다.
"""

import math
import os
import re
from typing import Optional, Sequence

# 이보다 덜 꺾으면 직진으로 보고 말하지 않는다. 30° 였을 때 1F 수납→엘리베이터 입구에서 6번 나와(N236~N245
# 비스듬한 통로의 31°·36° 지그재그까지) 너무 많다는 지적으로 50° 로 올렸다(2026-11-09 메모, 6 → 4번).
# 영향 표: docs/분석보고/2026-11-09/분석보고_2026-11-09_방향안내기준각도영향.md
TURN_MIN_DEG = 50.0
TURN_BACK_DEG = 135.0     # 이보다 많이 꺾으면 돌아서 간다
# "곧 도착합니다" 는 목적지까지 이 거리 안에 들어왔을 때 말한다(send_move.check_approach_cue).
# 예전에는 '마지막 경유지에서 목적지까지 6m 이하' 면 그 경유지 2m 앞에서 말해, 실제로는 3~7.5m ·
# 13~27초 전에 들렸다(2026-10-11 실기 13건, 안내 저속 약 0.3m/s). 2.5m 면 도착 판정까지 약 8~10초.
ARRIVE_SOON_M = float(os.getenv("NAV_ARRIVE_SOON_M", "2.5"))
# 마지막 꺾는 안내가 들리는 지점(경유지 lead_m 앞)에서 목적지까지 이 안이면 두 말을 한 문장으로 붙인다.
# 따로 내면 1~2초 간격으로 겹쳐 엔진이 앞 문장을 건너뛸 수 있다(엔진은 1초마다 마지막 안내만 본다).
ARRIVE_SOON_MERGE_M = 3.5
ARRIVE_SOON = "곧 도착합니다."
# 시나리오 단계의 '부르는 이름'(VoiceScenarioStep.SpokenPlaceName)이 있으면 "곧 원무과에 도착합니다."
# 장소 DB 이름(원무과 접수 등)은 사람이 부르는 이름과 달라 쓰지 않는다.
ARRIVE_SOON_NAMED = "곧 {name}에 도착합니다."
TURN_PHRASES = {
    "left": "잠시 후 왼쪽으로 방향을 바꾸겠습니다.",
    "right": "잠시 후 오른쪽으로 방향을 바꾸겠습니다.",
    "back": "잠시 후 뒤로 방향을 바꾸겠습니다.",
}
# 출발 제자리 회전(send_move._depart_turn) 직전에 엔진이 출발 멘트 뒤에 말한다 — 바로 도니 "잠시 후" 를 붙이지 않는다(11-02)
DEPART_TURN_PHRASES = {
    "left": "왼쪽으로 방향을 바꾸겠습니다.",
    "right": "오른쪽으로 방향을 바꾸겠습니다.",
    "back": "뒤로 방향을 바꾸겠습니다.",
}
MIN_SEGMENT_M = 0.3       # 이보다 짧은 구간은 방향이 불안정해 판정하지 않는다
# 이동 중 진행 멘트(send_move._check_progress_cue). 단계 칸이 비면 이 문장에 부르는 이름(없으면 장소 이름)을 넣는다.
# 칸에 PROGRESS_OFF("-")를 넣으면 그 단계는 말하지 않는다(2026-10-29).
PROGRESS_DEFAULT = "{name} 방향으로 이동하고 있습니다."
PROGRESS_OFF = "-"

Point = tuple[float, float]


def _heading(a: Point, b: Point) -> float:
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def _dist(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def turn_deg(prev: Point, at: Point, nxt: Point) -> float:
    """at 에서 꺾는 각도(도). + 는 왼쪽(반시계), - 는 오른쪽. 범위 -180~180."""
    d = _heading(at, nxt) - _heading(prev, at)
    return (d + 180.0) % 360.0 - 180.0


def path_length(points: Sequence[Point]) -> float:
    """점들을 차례로 이은 길이(m)."""
    return sum(_dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def turn_phrase(deg: float) -> Optional[str]:
    """꺾는 각도 → 말. 직진이면 None."""
    a = abs(deg)
    if a < TURN_MIN_DEG:
        return None
    # 1~2m 앞에서 미리 알리므로(send_move.NAV_CUE_LEAD_M) "잠시 후" 를 붙인다
    if a >= TURN_BACK_DEG:
        return TURN_PHRASES["back"]
    return TURN_PHRASES["left"] if deg > 0 else TURN_PHRASES["right"]


def depart_turn_phrase(deg: float) -> str:
    """출발 제자리 회전 각도(+ 왼쪽) → 말. 회전 점을 넣는 기준(45°)을 넘은 경우에만 부른다."""
    if abs(deg) >= TURN_BACK_DEG:
        return DEPART_TURN_PHRASES["back"]
    return DEPART_TURN_PHRASES["left"] if deg > 0 else DEPART_TURN_PHRASES["right"]


def arrive_soon_phrase(name: Optional[str]) -> str:
    """도착 접근 안내. 부르는 이름이 있으면 넣는다."""
    name = (name or "").strip()
    return ARRIVE_SOON_NAMED.format(name=name) if name else ARRIVE_SOON


def spoken_place_label(name: Optional[str]) -> str:
    """장소 DB 이름 → 말할 이름. 밑줄은 띄어 쓰고, 앞의 층 표시(1F_)·뒤의 번호(_01)는 뗀다.

    예) "1F_엘리베이터입구" → "엘리베이터입구", "원무과_01" → "원무과", "원무과 접수"·"3층 민원실" → 그대로.
    """
    s = re.sub(r"[_\s]+", " ", name or "").strip()
    s = re.sub(r"^B?\d+F\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+\d+$", "", s)
    return s.strip()


def progress_phrase(text: Optional[str], spoken_name: Optional[str], place_name: Optional[str]) -> Optional[str]:
    """이동 단계의 진행 멘트. 칸이 있으면 그것, "-" 면 없음, 비면 기본 문장(이름을 모르면 없음)."""
    text = (text or "").strip()
    if text == PROGRESS_OFF:
        return None
    if text:
        return text
    name = (spoken_name or "").strip() or spoken_place_label(place_name)
    return PROGRESS_DEFAULT.format(name=name) if name else None


def build_cues(start: Point, waypoints: Sequence[dict], lead_m: float = 2.0) -> dict[int, str]:
    """경유지·목적지에서 할 말. 키는 웨이포인트 번호(1부터).

    경유지(마지막 앞까지)는 꺾는 방향을 그 경유지 lead_m 앞에서 말한다. 목적지(마지막 번호)는
    "곧 도착합니다"(목적지 웨이포인트에 arrive_name 이 있으면 "곧 ○○에 도착합니다") 로,
    목적지까지 ARRIVE_SOON_M 안에서 말한다 — 경유지를 거치는 이동에서만.
    마지막 꺾는 안내가 목적지 가까이에서 들리면 그 문장에 붙이고 목적지 몫은 두지 않는다.

    Args:
        start: 출발 위치(로봇 현재 위치).
        waypoints: [{"x", "y", ...}] — 경유지들과 마지막 목적지(arrive_name 은 마지막에만).
        lead_m: 경유지 안내를 그 경유지 몇 m 앞에서 내는지(send_move.NAV_CUE_LEAD_M).
    """
    pts = [start] + [(float(w["x"]), float(w["y"])) for w in waypoints]
    cues: dict[int, str] = {}
    last = len(waypoints)
    for k in range(1, last):
        prev, at, nxt = pts[k - 1], pts[k], pts[k + 1]
        if _dist(prev, at) >= MIN_SEGMENT_M and _dist(at, nxt) >= MIN_SEGMENT_M:
            phrase = turn_phrase(turn_deg(prev, at, nxt))
            if phrase:
                cues[k] = phrase
    if last >= 2:
        soon = arrive_soon_phrase(waypoints[-1].get("arrive_name"))
        k = last - 1
        if k in cues and lead_m + path_length(pts[k:]) <= ARRIVE_SOON_MERGE_M:
            cues[k] = f"{cues[k]} {soon}"
        else:
            cues[last] = soon
    return cues
