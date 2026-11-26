"""경로 지점 이름 → 장소(LocationInfo) 조회 공용 규칙.

경로(WayInfo.WayPoints)는 장소를 이름으로 참조한다. 이름은 사업장 안에서만 유일하고
위험구역(폴리곤)은 중복 검사에서 빠지므로, 이름만으로 `.first()` 하면 다른 사업장 장소나
위험구역 중심점이 주행 목표가 될 수 있었다. 또 호출부마다 `.first()`(가장 앞 행)와
dict(마지막 행)로 고르는 행이 달랐다(2026-10-16). 주행 목표 조회는 여기 한 곳을 쓴다.
"""
from sqlalchemy.orm import Session

from app.auth.dependencies import get_business_map_ids
from app.database.models import LocationInfo


def _candidates(db: Session, names: list[str], business_id: int | None):
    base = db.query(LocationInfo).filter(
        LocationInfo.LacationName.in_(names),
    )
    if business_id:
        scoped = base.filter(LocationInfo.MapId.in_(get_business_map_ids(db, business_id))).all()
        found = {p.LacationName for p in scoped}
        # 사업장 맵에 없는 이름(맵 미지정 옛 데이터 등)만 범위 밖에서 찾는다 — 예전 동작 유지
        rest = [n for n in names if n not in found]
        if rest:
            scoped += base.filter(LocationInfo.LacationName.in_(rest)).all()
        return scoped
    return base.all()


def find_places_by_names(db: Session, names: list[str], business_id: int | None) -> dict[str, LocationInfo]:
    """이름 → 장소. 같은 이름이 여럿이면 id 가 가장 작은(먼저 만든) 장소로 정한다."""
    uniq = list(dict.fromkeys(n for n in names if n))
    if not uniq:
        return {}
    out: dict[str, LocationInfo] = {}
    for p in sorted(_candidates(db, uniq, business_id), key=lambda r: r.id):
        out.setdefault(p.LacationName, p)
    return out


def find_place_by_name(db: Session, name: str, business_id: int | None) -> LocationInfo | None:
    return find_places_by_names(db, [name], business_id).get(name)
