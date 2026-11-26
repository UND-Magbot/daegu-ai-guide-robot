"""안내 이동 중 동행 확인 간격 — 구간(목적지 장소)별 과거 이동 시간으로 정한다.

로봇이 안내 이동 도중 "잘 따라오고 계신가요?" 를 묻는 간격은 고정값이면 짧은 구간에서는
도착 직전에 묻고, 긴 구간에서는 너무 드물다. 그래서 장소별 평균 이동 시간을 이벤트 로그
(`place_move_start` → 바로 다음 `nav_complete`)에서 구하고 **구간의 절반쯤 한 번** 묻게 한다.

- 평균이 FOLLOW_MIN_LEG_SEC 보다 짧은 구간은 묻지 않는다(간격 0) — 금방 도착한다
- 기록이 없는 구간은 시나리오의 기록 있는 구간 평균으로, 그것도 없으면 DEFAULT_INTERVAL_SEC
- 운영자가 시나리오에 간격을 직접 넣으면(FollowIntervalSec) 모든 구간에 그 값을 쓴다
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.database.models import LocationInfo, LogDataInfo

LOOKBACK_DAYS = 60
MIN_SAMPLES = 2             # 한 번뿐인 기록은 우연(재전송·막힘)일 수 있어 믿지 않는다
MAX_LEG_SEC = 600           # 이보다 긴 건 중간에 멈췄다 간 것 — 평균을 망친다
FOLLOW_MIN_LEG_SEC = 20     # 이보다 짧은 구간은 묻지 않는다
INTERVAL_MIN_SEC = 12
INTERVAL_MAX_SEC = 60
DEFAULT_INTERVAL_SEC = 30

_START = "place_move_start"
# 출발 뒤 이 중 하나가 먼저 오면 구간이 끝난 것이다. nav_complete 만 정상 도착으로 센다.
_TERMINAL = ("nav_complete", "nav_error", "nav_comm_lost", _START, "path_move_start", "nav_start")
_PREFIX = "장소 이동: "
# log_event 쿨다운이 붙이는 꼬리표 — 이름 비교 전에 뗀다
_SUPPRESSED = re.compile(r"\s*\(이전 \d+건 동일 로그 생략됨\)\s*$")


def place_name(message: str) -> Optional[str]:
    """'장소 이동: 안과 (이전 1건 동일 로그 생략됨)' → '안과'."""
    if not message or not message.startswith(_PREFIX):
        return None
    return _SUPPRESSED.sub("", message[len(_PREFIX):]).strip() or None


def leg_durations(events: Iterable[tuple[str, str, datetime]]) -> dict[str, list[float]]:
    """(Action, Message, CreatedAt) 를 시간순으로 받아 장소별 이동 시간(초) 목록을 만든다."""
    out: dict[str, list[float]] = {}
    pending: Optional[tuple[str, datetime]] = None
    for action, message, at in events:
        if action not in _TERMINAL:
            continue
        if pending and action == "nav_complete":
            sec = (at - pending[1]).total_seconds()
            if 0 < sec <= MAX_LEG_SEC:
                out.setdefault(pending[0], []).append(sec)
        pending = None
        if action == _START:
            name = place_name(message)
            pending = (name, at) if name else None
    return out


def interval_for(avg_sec: float) -> int:
    """구간 평균 이동 시간 → 묻는 간격. 0 이면 묻지 않는다."""
    if avg_sec < FOLLOW_MIN_LEG_SEC:
        return 0
    return int(min(INTERVAL_MAX_SEC, max(INTERVAL_MIN_SEC, round(avg_sec / 2))))


def follow_plan(durations: dict[str, list[float]], places: dict[int, str],
                manual_interval: Optional[int]) -> dict:
    """번들용 동행 확인 계획.

    Args:
        durations: leg_durations() 결과 (장소 이름 → 초 목록)
        places: 이 시나리오가 갈 수 있는 장소 {LocationInfo.id: 이름}
        manual_interval: 시나리오에 운영자가 넣은 간격. None/0 이면 자동

    Returns:
        {"intervalSec", "source", "legs": [{"locationId", "avgSec", "samples", "intervalSec"}]}
        intervalSec 는 기록 없는 구간에 쓰는 기본 간격이다.
    """
    legs = []
    for loc_id, name in sorted(places.items()):
        samples = durations.get(name) or []
        if len(samples) < MIN_SAMPLES:
            continue
        avg = sum(samples) / len(samples)
        legs.append({"locationId": loc_id, "avgSec": round(avg, 1), "samples": len(samples),
                     "intervalSec": interval_for(avg)})

    if manual_interval and manual_interval > 0:
        for leg in legs:
            leg["intervalSec"] = manual_interval
        return {"intervalSec": manual_interval, "source": "manual", "legs": legs}
    if legs:
        mean = sum(leg["avgSec"] for leg in legs) / len(legs)
        return {"intervalSec": interval_for(mean) or INTERVAL_MIN_SEC, "source": "history", "legs": legs}
    return {"intervalSec": DEFAULT_INTERVAL_SEC, "source": "default", "legs": []}


def load_durations(db: Session, robot_id: int) -> dict[str, list[float]]:
    """이 로봇의 최근 장소 이동 기록에서 장소별 이동 시간을 읽는다."""
    since = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    rows = (db.query(LogDataInfo.Action, LogDataInfo.Message, LogDataInfo.CreatedAt)
            .filter(LogDataInfo.RobotId == robot_id,
                    LogDataInfo.Action.in_(_TERMINAL),
                    LogDataInfo.CreatedAt >= since)
            .order_by(LogDataInfo.id).all())
    return leg_durations(rows)


def place_names(db: Session, ids: Iterable[int]) -> dict[int, str]:
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    return {lo.id: lo.LacationName
            for lo in db.query(LocationInfo).filter(LocationInfo.id.in_(ids)).all()
            if lo.LacationName}
