"""목적지 사전의 맵별 장소 연결.

한 로봇이 병원(1F)과 관공서(3F)를 함께 다닌다. 목적지 하나가 장소 하나만 가리키면
"엘리베이터" 처럼 층마다 있는 목적지를 한쪽 층에만 걸 수 있고, 다른 맵의 장소가
걸려 있어도 알아차리지 못한다. 그래서 연결을 맵마다 따로 두고, 쓰는 쪽은 항상
'지금 어느 맵인가' 를 주고 그 맵의 장소만 받는다.
"""

from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.database.models import LocationInfo, VoiceSlotLocation, VoiceSlotValue


def links_by_slot(db: Session, slot_ids: Iterable[int]) -> dict[int, list[tuple[VoiceSlotLocation, LocationInfo]]]:
    """목적지별 (연결, 장소) 목록."""
    ids = list(set(slot_ids))
    out: dict[int, list] = {i: [] for i in ids}
    if not ids:
        return out
    rows = (db.query(VoiceSlotLocation, LocationInfo)
            .join(LocationInfo, LocationInfo.id == VoiceSlotLocation.LocationId)
            .filter(VoiceSlotLocation.SlotValueId.in_(ids))
            .order_by(VoiceSlotLocation.MapId).all())
    for link, loc in rows:
        out[link.SlotValueId].append((link, loc))
    return out


def location_on_map(db: Session, slot_id: int, map_id: Optional[int]) -> Optional[LocationInfo]:
    """이 목적지가 그 맵에서 가리키는 장소. 맵을 모르면 정할 수 없다 — None."""
    if not map_id:
        return None
    return (db.query(LocationInfo)
            .join(VoiceSlotLocation, VoiceSlotLocation.LocationId == LocationInfo.id)
            .filter(VoiceSlotLocation.SlotValueId == slot_id, VoiceSlotLocation.MapId == map_id)
            .first())


def slot_ids_on_map(db: Session, robot_id: int, map_id: Optional[int]) -> set[int]:
    """그 맵에 장소가 연결된 이 로봇의 목적지 id."""
    if not map_id:
        return set()
    rows = (db.query(VoiceSlotLocation.SlotValueId)
            .join(VoiceSlotValue, VoiceSlotValue.id == VoiceSlotLocation.SlotValueId)
            .filter(VoiceSlotValue.RobotId == robot_id, VoiceSlotLocation.MapId == map_id)
            .all())
    return {r[0] for r in rows}


def slot_ids_linked(db: Session, robot_id: int) -> set[int]:
    """어느 맵에든 장소가 연결된 이 로봇의 목적지 id."""
    rows = (db.query(VoiceSlotLocation.SlotValueId)
            .join(VoiceSlotValue, VoiceSlotValue.id == VoiceSlotLocation.SlotValueId)
            .filter(VoiceSlotValue.RobotId == robot_id)
            .distinct().all())
    return {r[0] for r in rows}


def validate_location_ids(db: Session, location_ids: list[int]) -> list[LocationInfo]:
    """연결할 장소 검증 — 존재·맵 소속·위험 구역 아님·맵마다 하나. 422 로 거절한다."""
    ids = list(dict.fromkeys(i for i in location_ids if i))
    if not ids:
        return []
    locs = {lo.id: lo for lo in db.query(LocationInfo).filter(LocationInfo.id.in_(ids)).all()}
    seen_maps: dict[int, str] = {}
    out = []
    for i in ids:
        lo = locs.get(i)
        if not lo:
            raise HTTPException(status_code=422, detail=f"장소(id={i})를 찾을 수 없습니다")
        if not lo.MapId:
            raise HTTPException(status_code=422, detail=f"'{lo.LacationName}'은 맵에 속하지 않은 장소입니다")
        if lo.MapId in seen_maps:
            raise HTTPException(
                status_code=422,
                detail=f"같은 맵에 장소를 두 개 연결할 수 없습니다 ('{seen_maps[lo.MapId]}', '{lo.LacationName}')",
            )
        seen_maps[lo.MapId] = lo.LacationName
        out.append(lo)
    return out


def replace_links(db: Session, slot: VoiceSlotValue, locations: list[LocationInfo]) -> None:
    """목적지의 연결을 통째로 바꾼다. 호출자가 commit 한다."""
    db.query(VoiceSlotLocation).filter(VoiceSlotLocation.SlotValueId == slot.id).delete()
    db.flush()
    for lo in locations:
        db.add(VoiceSlotLocation(SlotValueId=slot.id, MapId=lo.MapId, LocationId=lo.id))
