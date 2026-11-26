from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.database.models import (
    LocationInfo, UserInfo, FloorInfo, MapInitPose, RobotInfo, RobotMapInfo, RouteInfo, WayInfo,
)
from app.auth.dependencies import (
    require_any_permission, get_current_user, is_admin, get_business_map_ids, get_business_robot_names,
)
from app.auth.audit import write_audit, get_client_ip

from app.database.routes import database, get_db
from app.database.routes._helpers import get_floor_name, can_read_map


def _sync_map_init_pose_to_charge(db: Session, place: LocationInfo) -> str | None:
    """충전소 저장 시 같은 맵의 초기 위치(map_init_pose)를 충전소 좌표로 맞춘다.

    맵 활성화는 map_init_pose 를 시드로 주입하는데, 충전소를 다시 세팅해도 이 값은 그대로라
    옛 충전소 자리로 측위가 시작돼 상실됐다(2026-10-13). 로봇은 맵 활성화를 보통 충전소에서
    하므로 충전소 좌표를 초기 위치로 쓴다.
    - 그 맵에 초기 위치가 있는 로봇은 모두 갱신, 없으면 장소의 로봇(RobotName)으로 새로 만든다.
    반환: 감사 로그용 변경 요약(변경 없으면 None).
    """
    if place.Category != "charge" or place.MapId is None:
        return None
    if place.LocationX is None or place.LocationY is None:
        return None
    x, y, yaw = float(place.LocationX), float(place.LocationY), float(place.Yaw or 0.0)

    rows = db.query(MapInitPose).filter(MapInitPose.MapId == place.MapId).all()
    if not rows and place.RobotName:
        robot = db.query(RobotInfo).filter(RobotInfo.RobotName == place.RobotName).first()
        if robot:
            row = MapInitPose(MapId=place.MapId, RobotId=robot.id, PosX=x, PosY=y, Yaw=yaw)
            db.add(row)
            db.commit()
            return f"맵 {place.MapId} 초기 위치 생성(로봇 {robot.id}): ({x:.3f}, {y:.3f}, {yaw:.3f})"
        return None

    changes = []
    for row in rows:
        if (row.PosX, row.PosY, row.Yaw) == (x, y, yaw):
            continue
        changes.append(
            f"로봇 {row.RobotId} ({row.PosX:.3f}, {row.PosY:.3f}, {row.Yaw:.3f}) → ({x:.3f}, {y:.3f}, {yaw:.3f})"
        )
        row.PosX, row.PosY, row.Yaw = x, y, yaw
    if not changes:
        return None
    db.commit()
    return f"맵 {place.MapId} 초기 위치 갱신: " + ", ".join(changes)


def _apply_charge_init_pose_sync(db: Session, place: LocationInfo, user_id: int, request: Request) -> None:
    """충전소면 맵 초기 위치를 동기화하고 감사 로그를 남긴다. 실패해도 장소 저장은 유지한다."""
    try:
        summary = _sync_map_init_pose_to_charge(db, place)
    except Exception as e:
        db.rollback()
        print(f"[INIT_POSE] 충전소 {place.id} 맵 초기 위치 동기화 실패: {e}")
        return
    if summary:
        write_audit(db, user_id, "map_init_pose_saved", "map", place.MapId,
                    detail=f"충전소 '{place.LacationName}' 저장 → {summary}",
                    ip_address=get_client_ip(request))
        print(f"[INIT_POSE] 충전소 '{place.LacationName}' 저장 → {summary}")


class RobotPlaceInsertReq(BaseModel):
    RobotName: str
    LacationName: str
    FloorId: int | None = None
    LocationX: float
    LocationY: float
    Yaw: float = 0.0
    MapId: int | None = None
    Category: str = "waypoint"
    Imformation: str | None = None


def _assert_unique_place_name(db: Session, name: str, category: str | None,
                              current_user: UserInfo, exclude_id: int | None = None) -> None:
    """경로(WayInfo.WayPoints)가 장소를 '이름'으로 참조·조회(.first())하므로,
    waypoint 계열 장소명은 사업장 내에서 유일해야 한다. 중복이면 경로가 항상 첫 번째
    장소로만 해석돼 다층 경로·전환 등이 깨진다."""
    name = (name or "").strip()
    if not name:
        return
    q = db.query(LocationInfo.id).filter(LocationInfo.LacationName == name)
    if exclude_id is not None:
        q = q.filter(LocationInfo.id != exclude_id)
    # 사업장 스코프(경로가 참조하는 범위와 동일). 관리자/무사업장은 전역 검사.
    if not is_admin(current_user) and current_user.BusinessId:
        biz_map_ids = get_business_map_ids(db, current_user.BusinessId)
        q = q.filter(LocationInfo.MapId.in_(biz_map_ids))
    if q.first():
        raise HTTPException(
            status_code=409,
            detail=f"이미 '{name}' 이름의 장소가 있습니다. 경로가 이름으로 장소를 찾으므로 층이 달라도 이름은 다르게 지정하세요.",
        )


def _rename_place_references(db: Session, place: LocationInfo, old_name: str, new_name: str,
                              current_user: UserInfo) -> str | None:
    """장소 이름 변경을 구간(RouteInfo)·경로(WayInfo.WayPoints)에 전파한다. 커밋은 호출자가 한다.

    둘 다 장소를 id 가 아니라 '이름'으로 참조해, 장소만 바꾸면 구간 그래프와 경로가 옛 이름에
    묶여 끊긴다. 이름은 사업장 안에서 유일하므로 그 장소가 속한 사업장 범위에서만 바꾼다.
    반환: 감사 로그용 변경 요약(변경 없으면 None).
    """
    if not old_name or old_name == new_name:
        return None

    business_id = None
    if place.MapId is not None:
        row = db.query(RobotMapInfo.BusinessId).filter(RobotMapInfo.id == place.MapId).first()
        business_id = row.BusinessId if row else None
    if business_id is None:
        business_id = current_user.BusinessId

    route_q = db.query(RouteInfo).filter(
        (RouteInfo.StartPlaceName == old_name) | (RouteInfo.EndPlaceName == old_name)
    )
    way_q = db.query(WayInfo).filter(WayInfo.WayPoints.contains(old_name))
    if business_id is not None:
        route_q = route_q.filter(RouteInfo.MapId.in_(get_business_map_ids(db, business_id)))
        biz_names = get_business_robot_names(db, business_id)
        way_q = way_q.filter(
            WayInfo.RobotName.in_(biz_names) | (WayInfo.RobotName == "") | (WayInfo.RobotName.is_(None))
        )

    route_count = 0
    for r in route_q.all():
        if r.StartPlaceName == old_name:
            r.StartPlaceName = new_name
        if r.EndPlaceName == old_name:
            r.EndPlaceName = new_name
        route_count += 1

    way_names = []
    for w in way_q.all():
        parts = [n.strip() for n in (w.WayPoints or "").split(" - ")]
        if old_name not in parts:  # contains 는 부분 일치라 정확히 한 번 더 거른다
            continue
        w.WayPoints = " - ".join(new_name if n == old_name else n for n in parts)
        way_names.append(w.WayName or str(w.id))

    if not route_count and not way_names:
        return None
    summary = f"이름 변경 전파 '{old_name}' → '{new_name}': 구간 {route_count}건"
    if way_names:
        summary += f", 경로 {len(way_names)}건({', '.join(way_names)})"
    return summary


def _charge_approach_points(db: Session, station: LocationInfo) -> list[tuple[LocationInfo, int]]:
    """충전소의 접근점 "<충전소>-N"(N=1 이 도킹 포인트)과 번호.

    충전 로직(robot_control/charge.py)은 접근점을 이 이름 규칙으로만 찾는다. 충전소 이름만
    바꾸면 도킹 포인트를 못 찾아 접근 통로가 빠지므로 이름을 같이 바꿔야 한다.
    범위는 충전 로직과 같게 충전소의 맵(없으면 층)으로 좁힌다.
    """
    prefix = f"{station.LacationName}-"
    q = db.query(LocationInfo).filter(
        LocationInfo.LacationName.like(f"{prefix}%"), LocationInfo.id != station.id,
    )
    if station.MapId is not None:
        q = q.filter(LocationInfo.MapId == station.MapId)
    elif station.FloorId is not None:
        q = q.filter(LocationInfo.FloorId == station.FloorId)
    points = []
    for p in q.all():
        # like 의 '_'·'%' 와일드카드에 걸린 것과 "ch-1a" 같은 이름은 정확히 다시 거른다.
        suffix = (p.LacationName or "")[len(prefix):]
        if p.LacationName.startswith(prefix) and suffix.isdigit():
            points.append((p, int(suffix)))
    return points


@database.post("/places")
def insert_robot_place(
    req: RobotPlaceInsertReq,
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(require_any_permission("place-list", "map-edit")),
):
    _assert_unique_place_name(db, req.LacationName, req.Category, current_user)

    place = LocationInfo(
        UserId=current_user.id,
        RobotName=req.RobotName,
        LacationName=req.LacationName,
        FloorId=req.FloorId,
        LocationX=req.LocationX,
        LocationY=req.LocationY,
        Yaw=req.Yaw,
        MapId=req.MapId,
        Category=req.Category,
        Imformation=req.Imformation,
    )

    db.add(place)
    db.commit()
    db.refresh(place)

    floor_name = get_floor_name(db, req.FloorId)
    write_audit(db, current_user.id, "place_created", "place", place.id,
                detail=f"장소명: {req.LacationName}, 로봇: {req.RobotName}, 층: {floor_name}",
                ip_address=get_client_ip(request))
    _apply_charge_init_pose_sync(db, place, current_user.id, request)

    return place


@database.get("/places")
def get_places(
    map_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: UserInfo = Depends(get_current_user),  # 맵 뷰는 어느 탭에서든 표시 가능
):
    q = db.query(LocationInfo)
    if map_id is not None:
        if not can_read_map(db, current_user, map_id):
            return []
        q = q.filter(LocationInfo.MapId == map_id)
    elif not is_admin(current_user) and current_user.BusinessId:
        biz_map_ids = get_business_map_ids(db, current_user.BusinessId)
        q = q.filter(LocationInfo.MapId.in_(biz_map_ids))
    places = q.order_by(LocationInfo.id.desc()).all()

    floor_ids = {p.FloorId for p in places if p.FloorId}
    floor_map = {}
    if floor_ids:
        rows = db.query(FloorInfo).filter(FloorInfo.id.in_(floor_ids)).all()
        floor_map = {f.id: f.FloorName for f in rows}

    return [
        {
            "id": p.id,
            "UserId": p.UserId,
            "RobotName": p.RobotName,
            "LacationName": p.LacationName,
            "FloorId": p.FloorId,
            "Floor": floor_map.get(p.FloorId, ""),
            "LocationX": p.LocationX,
            "LocationY": p.LocationY,
            "Yaw": p.Yaw,
            "MapId": p.MapId,
            "Category": p.Category,
            "Imformation": p.Imformation,
        }
        for p in places
    ]


@database.put("/places/{place_id}")
def update_place(place_id: int, req: RobotPlaceInsertReq, request: Request, db: Session = Depends(get_db), current_user: UserInfo = Depends(require_any_permission("place-list", "map-edit"))):
    place = db.query(LocationInfo).filter(LocationInfo.id == place_id).first()

    if not place:
        raise HTTPException(status_code=404, detail="Place not found")

    # 이름 변경 시 사업장 내 중복 차단(경로가 이름으로 참조하므로). 자기 자신 제외.
    _assert_unique_place_name(db, req.LacationName, req.Category, current_user, exclude_id=place_id)

    # 충전소면 접근점 "<충전소>-N" 도 함께 바꾼다. 새 이름이 막히면 아무것도 바꾸기 전에 409.
    old_name = place.LacationName
    approach_renames = []
    if place.Category == "charge" and req.LacationName != old_name:
        approach_renames = [(p, f"{req.LacationName}-{n}") for p, n in _charge_approach_points(db, place)]
        for p, new_name in approach_renames:
            _assert_unique_place_name(db, new_name, p.Category, current_user, exclude_id=p.id)

    # 전파 범위·대상은 바뀌기 전 장소(맵·카테고리) 기준으로 정한다.
    summaries = [_rename_place_references(db, place, old_name, req.LacationName, current_user)]
    for p, new_name in approach_renames:
        summaries.append(_rename_place_references(db, p, p.LacationName, new_name, current_user))
        summaries.append(f"접근점: {p.LacationName} → {new_name}")
        p.LacationName = new_name
    rename_summary = ", ".join(s for s in summaries if s) or None

    changes = []
    field_map = {
        "로봇": ("RobotName", req.RobotName),
        "장소명": ("LacationName", req.LacationName),
        "층ID": ("FloorId", req.FloorId),
        "X좌표": ("LocationX", req.LocationX),
        "Y좌표": ("LocationY", req.LocationY),
        "방향": ("Yaw", req.Yaw),
        "맵ID": ("MapId", req.MapId),
        "카테고리": ("Category", req.Category),
        "정보": ("Imformation", req.Imformation),
    }

    for label, (attr, new_val) in field_map.items():
        old_val = getattr(place, attr)
        if old_val != new_val:
            if attr == "FloorId":
                old_name = get_floor_name(db, old_val)
                new_name = get_floor_name(db, new_val)
                changes.append(f"층: {old_name} → {new_name}")
            else:
                changes.append(f"{label}: {old_val or ''} → {new_val or ''}")
            setattr(place, attr, new_val)

    db.commit()
    db.refresh(place)

    if rename_summary:
        changes.append(rename_summary)
    detail = ", ".join(changes) if changes else None
    write_audit(db, current_user.id, "place_updated", "place", place_id, detail=detail,
                ip_address=get_client_ip(request))
    _apply_charge_init_pose_sync(db, place, current_user.id, request)

    return place


@database.delete("/places/{place_id}")
def delete_place(place_id: int, request: Request, db: Session = Depends(get_db), current_user: UserInfo = Depends(require_any_permission("place-list", "map-edit"))):
    place = (
        db.query(LocationInfo)
        .filter(LocationInfo.id == place_id)
        .first()
    )

    if not place:
        raise HTTPException(status_code=404, detail="Place not found")

    place_name = place.LacationName
    db.delete(place)
    db.commit()

    write_audit(db, current_user.id, "place_deleted", "place", place_id,
                detail=f"장소명: {place_name}",
                ip_address=get_client_ip(request))

    return {"status": "deleted"}
