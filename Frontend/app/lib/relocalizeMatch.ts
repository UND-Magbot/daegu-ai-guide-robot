// 위치 재조정 — 스캔이 지도와 맞는 정도(점수)와 주입 전 미리보기 좌표 계산(11-05).
//
// 점수는 서버가 잰다(POST /robot/relocalize/match — 방향마다 가장 먼 점이 지도 벽 0.15m 안에 드는 비율 %).
// 기준값은 시뮬(sim/scenarios/reloc_match_eval.py, 1F 맵47·3F 맵49): 맞는 자리 중앙 약 80%,
// 0.3m 어긋난 자리 중앙 약 57% — 그래서 75% 이상 '잘 맞음', 60% 미만 '낮음'.
// 맞는 자리도 14~18% 는 60% 미만으로 나온다(시뮬) — 그래서 경고가 아니라 '다시 확인' 안내로만 쓴다.

export type Pose2D = { x: number; y: number; yaw: number };
export type MatchLevel = "good" | "check" | "low";

export const MATCH_GOOD = 75;
export const MATCH_CHECK = 60;

export function matchLevel(score: number): MatchLevel {
  if (score >= MATCH_GOOD) return "good";
  if (score >= MATCH_CHECK) return "check";
  return "low";
}

export const MATCH_LABEL: Record<MatchLevel, string> = {
  good: "잘 맞음",
  check: "확인 필요",
  low: "낮음",
};

export const MATCH_COLOR: Record<MatchLevel, string> = {
  good: "#22c55e",
  check: "#f59e0b",
  low: "#ef4444",
};

/** 로봇 기준 점 → 지도 좌표(pose 로 회전·이동). rviz /tf 와 같은 변환. */
export function bodyToMap(body: number[][], pose: Pose2D): number[][] {
  const c = Math.cos(pose.yaw);
  const s = Math.sin(pose.yaw);
  return body.map(([bx, by]) => [pose.x + bx * c - by * s, pose.y + bx * s + by * c]);
}

/**
 * 받은 스캔 → 로봇 기준 점. body(/LOC_BODY_POINTS)면 그대로, map(/LIO_ALIGNED_POINTS, 지도 좌표)이면
 * 그 스캔을 그린 로봇 위치(odom)로 되돌린다. 위치를 모르면 null.
 */
export function scanToBody(raw: number[][], frame: "body" | "map", odom: Pose2D | null): number[][] | null {
  if (raw.length === 0) return null;
  if (frame === "body") return raw;
  if (!odom) return null;
  const c = Math.cos(odom.yaw);
  const s = Math.sin(odom.yaw);
  return raw.map(([mx, my]) => {
    const dx = mx - odom.x;
    const dy = my - odom.y;
    return [dx * c + dy * s, -dx * s + dy * c];
  });
}

export type MatchResponse = {
  status: "ok" | "few_points";
  n: number;
  msg?: string;
  map_id?: number;
  at?: { score: number; mean_err: number };
  refined?: Pose2D & {
    score: number;
    mean_err: number;
    ambiguous: boolean;
    alt?: Pose2D & { score: number };
  };
};
