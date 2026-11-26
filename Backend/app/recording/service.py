import os
from sqlalchemy.orm import Session
from sqlalchemy import desc, func as sqlfunc
from datetime import datetime, timedelta
from typing import Optional, List

from app.database.database import BACKEND_ROOT
from app.database.models import RecordingInfo, RobotModule, ModuleCameraInfo, RobotInfo, ScheduleInfo

# 녹화 파일 루트 디렉토리 (절대경로).
# manager.py / retention.py에서 공용으로 사용.
RECORDINGS_BASE = os.path.join(BACKEND_ROOT, "recordings")

# DB에는 "recordings/10/2026-04-09" 같은 상대경로를 저장하고,
# 런타임에 BACKEND_ROOT를 붙여서 절대경로로 복원한다.
RECORDINGS_REL_PREFIX = "recordings"


def to_relative_path(abs_path: str) -> str:
    """절대경로 → DB 저장용 상대경로 (recordings/... 이하)"""
    if not abs_path:
        return abs_path
    # 이미 상대경로면 그대로
    if abs_path.startswith(RECORDINGS_REL_PREFIX):
        return abs_path
    idx = abs_path.find(RECORDINGS_REL_PREFIX)
    if idx >= 0:
        return abs_path[idx:].replace("\\", "/")
    return abs_path


def to_absolute_path(rel_path: str) -> str:
    """DB 상대경로 → 현재 서버의 절대경로"""
    if not rel_path:
        return rel_path
    # 이미 절대경로면 그대로 (레거시 호환)
    if os.path.isabs(rel_path):
        return rel_path
    return os.path.join(BACKEND_ROOT, rel_path.replace("/", os.sep))


# 녹화 워커가 나누는 세그먼트 길이(초). worker.CameraRecordingWorker 기본값과 같다.
SEGMENT_SEC = 300


def numbered_segment_files(rec: RecordingInfo, names: Optional[List[str]] = None) -> List[str]:
    """레코드 번호가 박힌(_r<id>_seg) 이 레코드의 mp4 파일들(절대경로, 세그먼트 순).

    11-03 이후 녹화는 파일명에 레코드 번호가 들어가 레코드 1건 = 파일 N개(5분마다 1개)다.
    예전 파일(번호 없음)이면 빈 목록 — 호출 측이 예전 방식으로 찾는다.
    names 를 주면 그 폴더 목록을 재사용한다(목록 API 에서 폴더당 1회 조회).
    """
    if not rec.VideoPath:
        return []
    video_dir = to_absolute_path(rec.VideoPath)
    if names is None:
        if not os.path.isdir(video_dir):
            return []
        names = os.listdir(video_dir)
    head = f"{rec.RobotId}_cam{rec.ModuleId}_"
    mark = f"_r{rec.id}_seg"
    return [os.path.join(video_dir, f) for f in sorted(names)
            if f.endswith(".mp4") and f.startswith(head) and mark in f]


def create_recording(
    db: Session,
    robot_id: int,
    module_id: int,
    group_id: str,
    record_type: str,
    video_path: str,
    schedule_id: Optional[int] = None,
) -> RecordingInfo:
    rec = RecordingInfo(
        RobotId=robot_id,
        ModuleId=module_id,
        ScheduleId=schedule_id,
        GroupId=group_id,
        RecordType=record_type,
        VideoPath=to_relative_path(video_path),
        Status="recording",
        RecordStart=datetime.now(),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def complete_recording(
    db: Session,
    record_id: int,
    video_size: Optional[int] = None,
    thumbnail_path: Optional[str] = None,
):
    rec = db.query(RecordingInfo).filter(RecordingInfo.id == record_id).first()
    if not rec:
        return
    rec.Status = "completed"
    rec.RecordEnd = datetime.now()
    if video_size is not None:
        rec.VideoSize = video_size
    if thumbnail_path:
        rec.ThumbnailPath = to_relative_path(thumbnail_path)
    db.commit()


def error_recording(db: Session, record_id: int, reason: str = None):
    rec = db.query(RecordingInfo).filter(RecordingInfo.id == record_id).first()
    if not rec:
        return
    rec.Status = "error"
    rec.RecordEnd = datetime.now()
    if reason:
        rec.ErrorReason = reason[:200]  # 컬럼 길이 제한
    db.commit()


def get_earliest_recording_date(db: Session) -> dict:
    """가장 이른 녹화 날짜 반환"""
    min_dt = (
        db.query(sqlfunc.min(RecordingInfo.RecordStart))
        .filter(RecordingInfo.DeletedAt.is_(None), RecordingInfo.Status == "completed")
        .scalar()
    )
    if min_dt and isinstance(min_dt, str):
        return {"earliest_date": min_dt[:10]}
    return {"earliest_date": min_dt.strftime("%Y-%m-%d") if min_dt else None}


def get_recording_by_id(db: Session, record_id: int) -> Optional[RecordingInfo]:
    return (
        db.query(RecordingInfo)
        .filter(RecordingInfo.id == record_id, RecordingInfo.DeletedAt.is_(None))
        .first()
    )


def get_recordings_grouped(
    db: Session,
    robot_id: Optional[int] = None,
    record_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = 1,
    size: int = 20,
    robot_ids: list[int] = None,
):
    """GroupId 기준으로 묶어서 녹화 목록 반환 (error 상태 제외)"""
    # 1) 그룹별 요약 서브쿼리
    base = db.query(RecordingInfo).filter(
        RecordingInfo.DeletedAt.is_(None),
        RecordingInfo.Status == "completed",
    )
    if robot_ids is not None:
        base = base.filter(RecordingInfo.RobotId.in_(robot_ids))
    if robot_id:
        base = base.filter(RecordingInfo.RobotId == robot_id)
    if record_type:
        base = base.filter(RecordingInfo.RecordType == record_type)
    if start_date:
        base = base.filter(RecordingInfo.RecordStart >= start_date)
    if end_date:
        # "2026-04-08" → 해당 날짜 끝(23:59:59)까지 포함
        end_val = end_date.strip()
        if len(end_val) == 10:  # YYYY-MM-DD
            end_val += " 23:59:59"
        base = base.filter(RecordingInfo.RecordStart <= end_val)

    # 카메라별로 분리하여 그룹핑 (GroupId + ModuleId)
    group_query = (
        base.with_entities(RecordingInfo.GroupId, RecordingInfo.ModuleId)
        .group_by(RecordingInfo.GroupId, RecordingInfo.ModuleId)
        .order_by(desc(sqlfunc.max(RecordingInfo.RecordStart)))
    )

    # 전체 그룹 수 (총 카운트)
    all_groups = group_query.all()
    total = len(all_groups)

    # 페이지네이션
    page_groups = all_groups[(page - 1) * size : page * size]
    group_keys = [(g[0], g[1]) for g in page_groups]  # (GroupId, ModuleId)

    if not group_keys:
        return {"items": [], "total": total, "page": page, "size": size}

    # 2) 해당 페이지의 세그먼트를 한 번에 조회
    gid_set = set(gk[0] for gk in group_keys)
    all_segments = (
        db.query(RecordingInfo)
        .filter(RecordingInfo.GroupId.in_(gid_set), RecordingInfo.DeletedAt.is_(None),
                RecordingInfo.Status != "error")
        .order_by(RecordingInfo.RecordStart)
        .all()
    )

    # (GroupId, ModuleId)별로 세그먼트 분류
    segments_by_key: dict[tuple, list] = {}
    for seg in all_segments:
        k = (seg.GroupId, seg.ModuleId)
        segments_by_key.setdefault(k, []).append(seg)

    # 벌크 조회를 위한 ID 수집
    robot_ids = set()
    module_ids = set()
    schedule_ids = set()
    for k, segs in segments_by_key.items():
        if segs:
            first = segs[0]
            robot_ids.add(first.RobotId)
            module_ids.add(first.ModuleId)
            if first.ScheduleId:
                schedule_ids.add(first.ScheduleId)

    # 벌크 조회 (N+1 → 3쿼리)
    robots = {r.id: r for r in db.query(RobotInfo).filter(RobotInfo.id.in_(robot_ids)).all()} if robot_ids else {}
    modules_map = {m.id: m for m in db.query(RobotModule).filter(RobotModule.id.in_(module_ids)).all()} if module_ids else {}
    schedules = {s.id: s for s in db.query(ScheduleInfo).filter(ScheduleInfo.id.in_(schedule_ids)).all()} if schedule_ids else {}

    # 3) 카메라별 결과 조립
    results = []
    dir_cache: dict[str, list] = {}   # 같은 날짜 폴더는 한 번만 읽는다
    for gk in group_keys:
        segments = segments_by_key.get(gk, [])
        if not segments:
            continue

        first = segments[0]
        last = segments[-1]

        # 로봇 이름
        robot = robots.get(first.RobotId)
        robot_name = robot.RobotName if robot else f"Robot-{first.RobotId}"

        # 카메라 라벨
        module = modules_map.get(first.ModuleId)
        camera_label = module.Label if module else f"cam-{first.ModuleId}"

        # 스케줄 작업명
        work_name = None
        if first.ScheduleId:
            schedule = schedules.get(first.ScheduleId)
            if schedule:
                work_name = schedule.WorkName

        # 총 녹화 시간
        total_sec = 0
        for seg in segments:
            if seg.RecordEnd and seg.RecordStart:
                total_sec += int((seg.RecordEnd - seg.RecordStart).total_seconds())

        type_label = "자동" if first.RecordType == "auto" else "수동"

        thumbnail_url = None
        if first.ThumbnailPath:
            thumbnail_url = f"/api/recordings/{first.id}/thumbnail"

        seg_items = []
        for seg in segments:
            seg_items.extend(_segment_items(seg, dir_cache))

        status = first.Status
        error_reason = first.ErrorReason if status == "error" else None

        results.append({
            "group_id": f"{gk[0]}_{gk[1]}",  # GroupId_ModuleId (카메라별 고유키)
            "robot_id": first.RobotId,
            "robot_name": robot_name,
            "camera_label": camera_label,
            "record_type": type_label,
            "work_name": work_name,
            "record_start": first.RecordStart,
            "record_end": last.RecordEnd,
            "total_duration_sec": total_sec,
            "segment_count": len(seg_items),
            "thumbnail_url": thumbnail_url,
            "status": status,
            "error_reason": error_reason,
            "segments": seg_items,
        })

    return {"items": results, "total": total, "page": page, "size": size}


def _segment_items(rec: RecordingInfo, dir_cache: dict) -> list:
    """레코드 1건 → 재생 세그먼트 목록.

    5분 넘는 녹화는 파일이 여러 개인데 레코드는 1건이라 예전에는 첫 파일만 재생됐다.
    번호 박힌 파일이 2개 이상이면 파일마다 ?seg=N 으로 나눠 화면이 이어서 재생하게 한다.
    """
    total = 0
    if rec.RecordEnd and rec.RecordStart:
        total = int((rec.RecordEnd - rec.RecordStart).total_seconds())
    start = rec.RecordStart.strftime("%H:%M:%S") if rec.RecordStart else ""

    files = []
    if rec.VideoPath:
        video_dir = to_absolute_path(rec.VideoPath)
        if video_dir not in dir_cache:
            dir_cache[video_dir] = os.listdir(video_dir) if os.path.isdir(video_dir) else []
        files = numbered_segment_files(rec, dir_cache[video_dir])

    base_url = f"/api/recordings/{rec.id}/stream"
    if len(files) <= 1:
        return [{"id": rec.id, "start": start, "duration_sec": total, "stream_url": base_url}]

    items = []
    for i in range(len(files)):
        last = i == len(files) - 1
        dur = max(total - SEGMENT_SEC * i, 0) if last else SEGMENT_SEC
        seg_start = ""
        if rec.RecordStart:
            seg_start = (rec.RecordStart + timedelta(seconds=SEGMENT_SEC * i)).strftime("%H:%M:%S")
        items.append({"id": rec.id, "start": seg_start, "duration_sec": dur,
                      "stream_url": f"{base_url}?seg={i}"})
    return items


def soft_delete_recording(db: Session, record_id: int):
    rec = db.query(RecordingInfo).filter(RecordingInfo.id == record_id).first()
    if rec:
        rec.DeletedAt = datetime.now()
        db.commit()


def soft_delete_by_groups(db: Session, group_ids: List[str]) -> int:
    """영상 관리 목록의 그룹 키로 일괄 soft delete.

    목록은 카메라별로 "GroupId_ModuleId" 키를 준다(get_recordings_grouped). 그 키면 그 카메라 것만 지우고,
    예전 키(GroupId 만)면 그룹 전체를 지운다. GroupId 는 uuid4(하이픈)라 마지막 '_' 뒤가 숫자면 카메라 번호다.
    (11-03: GroupId 로만 찾아 0건 삭제 — 화면은 지운 듯 보이고 새로 고치면 다시 나왔다)
    """
    now = datetime.now()
    count = 0
    for key in group_ids:
        key = str(key)
        gid, sep, module = key.rpartition("_")
        q = db.query(RecordingInfo).filter(RecordingInfo.DeletedAt.is_(None))
        if sep and module.isdigit():
            q = q.filter(RecordingInfo.GroupId == gid, RecordingInfo.ModuleId == int(module))
        else:
            q = q.filter(RecordingInfo.GroupId == key)
        for rec in q.all():
            rec.DeletedAt = now
            count += 1
    db.commit()
    return count


def cleanup_orphaned(db: Session) -> int:
    """서버 시작 시 비정상 레코드 정리:
    0) 절대경로 → 상대경로 마이그레이션
    1) Status='recording' 고아 → 'error'
    2) Status='completed'인데 실제 파일 없음 → 'error'
    """
    now = datetime.now()
    count = 0

    # 0) 레거시 절대경로 → 상대경로로 변환
    all_recs = db.query(RecordingInfo).filter(RecordingInfo.DeletedAt.is_(None)).all()
    for rec in all_recs:
        if rec.VideoPath and os.path.isabs(rec.VideoPath):
            rec.VideoPath = to_relative_path(rec.VideoPath)
        if rec.ThumbnailPath and os.path.isabs(rec.ThumbnailPath):
            rec.ThumbnailPath = to_relative_path(rec.ThumbnailPath)
    db.flush()

    # 1) recording 상태 고아
    orphans = db.query(RecordingInfo).filter(RecordingInfo.Status == "recording").all()
    for rec in orphans:
        rec.Status = "error"
        rec.ErrorReason = "서버 재시작 시 녹화 중이던 세션 (고아)"
        rec.RecordEnd = now
        count += 1

    # 2) completed인데 파일 없는 레코드 → 다중 서버 환경에서는 다른 PC의 파일일 수 있으므로
    #    error로 변경하지 않음 (프론트에서 재생 시 404로 처리)

    if count > 0:
        db.commit()
    return count


def get_active_camera_modules(db: Session, robot_id: int):
    """로봇의 녹화 가능한 활성 카메라 모듈 (rtsp / http MJPEG)"""
    modules = (
        db.query(RobotModule)
        .join(ModuleCameraInfo, RobotModule.id == ModuleCameraInfo.ModuleId)
        .filter(
            RobotModule.RobotId == robot_id,
            RobotModule.ModuleType == "camera",
            RobotModule.IsActive == 1,
            ModuleCameraInfo.StreamType.in_(["rtsp", "http"]),
        )
        .all()
    )
    return modules


def build_stream_url(db: Session, module: RobotModule, robot: RobotInfo = None) -> Optional[str]:
    """모듈에서 녹화용 입력 URL 생성. rtsp / http MJPEG 둘 다 지원.
    robot 객체를 전달하면 DB 재조회를 생략한다.
    """
    ci = module.camera_info
    if not ci or ci.StreamType not in ("rtsp", "http"):
        return None

    if robot is None:
        robot = db.query(RobotInfo).filter(RobotInfo.id == module.RobotId).first()
    ip = ci.CameraIP or (robot.RobotIP if robot else None)
    if not ip:
        return None

    path = ci.Path or ""
    if ci.StreamType == "rtsp":
        return f"rtsp://{ip}:{ci.Port}{path}"
    return f"http://{ip}:{ci.Port}{path}"
