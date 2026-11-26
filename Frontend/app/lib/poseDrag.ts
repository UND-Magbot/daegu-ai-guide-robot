// 위치 재조정 드래그 → 주입할 pose. 누른 지점 = 위치, 끈 방향 = yaw(rviz 2D Pose Estimate 와 같은 규약).
//
// 놓는 순간 바로 주입하므로, 끈 길이가 너무 짧으면 방향이 손떨림 수준(클릭만 하면 yaw=0)이라
// 주입하지 않는다. 틀린 방향 시드는 측위 폭주로 이어질 수 있다(2026-10-08 분석).

/** 주입을 허용하는 최소 드래그 길이(m). 로봇 몸 길이(~0.7m)보다 짧게, 기본 배율에서 약 10px. */
export const MIN_POSE_DRAG_M = 0.4;

type Pt = { x: number; y: number };

/** 드래그 시작·끝(월드 좌표) → {x, y, yaw}. 끈 길이가 minLenM 미만이면 null. */
export function poseFromDrag(
  start: Pt,
  end: Pt,
  minLenM: number = MIN_POSE_DRAG_M
): { x: number; y: number; yaw: number } | null {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  if (Math.hypot(dx, dy) < minLenM) return null;
  return { x: start.x, y: start.y, yaw: Math.atan2(dy, dx) };
}
