from sqlalchemy.orm import Session


def get_floor_name(db: Session, floor_id: int | None) -> str:
    """FloorId로 FloorName 조회"""
    if not floor_id:
        return ""
    from app.database.models import FloorInfo
    fi = db.query(FloorInfo).filter(FloorInfo.id == floor_id).first()
    return fi.FloorName if fi else ""


def can_read_map(db: Session, user, map_id: int) -> bool:
    """이 사용자가 map_id 맵의 장소·구간을 읽어도 되는가.

    맵 id 만 알면 다른 사업장 맵의 장소까지 읽혔다(2026-10-16 점검). 관리자와 사업장 미지정
    사용자는 지금처럼 전부 허용하고, 사업장 사용자는 자기 사업장 맵만 허용한다.
    """
    from app.auth.dependencies import is_admin, get_business_map_ids
    if is_admin(user) or not user.BusinessId:
        return True
    return map_id in get_business_map_ids(db, user.BusinessId)
