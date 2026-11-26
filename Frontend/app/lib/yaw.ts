// 방향(yaw) — 프론트는 로봇/백엔드와 '동일한 ROS yaw'를 그대로 다룬다.
//
// ROS yaw: 0 = 지도 +X축(동), 반시계(CCW) 양수, 라디안(−π~π).
//   (waypoints.py 의 atan2(ΔY,ΔX), RobotMarker 의 rotate(-yaw) 와 동일 규약)
//
// 방향 입력 필드는 이 yaw 를 '도(0~360)'로 표시/입력한다 → 숫자가 로봇 텔레메트리와 그대로 일치.
// 방향 휠/화살표는 '지도상 실제 방향'을 가리키도록 화면 회전만 따로 계산한다.
//   (지도 화면은 위=+Y(북), 오른쪽=+X(동). heading 화면각 = (90 − deg), 위 기준 시계)

const TAU = Math.PI * 2;

/** ROS yaw(rad) → 방향 도(0~360). 로봇 yaw 와 동일 규약(0=동, 반시계). */
export function degFromYaw(yawRad: number): number {
  const d = (yawRad * 180) / Math.PI;
  return Math.round(((d % 360) + 360) % 360);
}

/** 방향 도(0~360) → ROS yaw(rad, −π~π). */
export function yawFromDeg(deg: number): number {
  let rad = (deg * Math.PI) / 180;
  rad = ((rad % TAU) + TAU) % TAU;
  if (rad > Math.PI) rad -= TAU;
  return Number(rad.toFixed(4));
}

/** 방향 도 → 화살표 화면 회전(도). 화살표 base=위(북)라 지도 heading=(90−deg) 시계방향. */
export function arrowRotationDeg(deg: number): number {
  return 90 - deg;
}

/** 방향 휠 클릭각(atan2(dx,−dy)=화면 위 기준 시계 도) → 방향 도(0~360). */
export function degFromWheelAngle(cwFromUpDeg: number): number {
  const d = 90 - cwFromUpDeg;
  return Math.round(((d % 360) + 360) % 360);
}

/**
 * 장소 (x, y) 에서 방향 yaw 로 lengthM 만큼 간 점을 화면 좌표로 바꿔, 화면에서 그 방향의 각도(도, 오른쪽=0·시계 양수)를 구한다.
 * 지도 화면이 회전·뒤집혀 있어도 지도 좌표 변환(toScreen)을 그대로 따르므로 로봇이 실제로 볼 방향을 가리킨다.
 * 두 점이 화면에서 겹치면 null.
 */
export function screenHeadingDeg(
  toScreen: (wx: number, wy: number) => { x: number; y: number },
  x: number,
  y: number,
  yawRad: number,
  lengthM = 0.6,
): number | null {
  const a = toScreen(x, y);
  const b = toScreen(x + lengthM * Math.cos(yawRad), y + lengthM * Math.sin(yawRad));
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  if (Math.hypot(dx, dy) < 1e-6) return null;
  return (Math.atan2(dy, dx) * 180) / Math.PI;
}
