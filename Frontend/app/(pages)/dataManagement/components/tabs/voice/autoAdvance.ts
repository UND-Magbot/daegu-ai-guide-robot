/**
 * 운영자 입력 단계의 자동 진행 설정 — 단계 값 AutoAdvanceSec 과 편집 화면 선택을 오간다.
 * null = 기본(관제 전역 규칙: 번호표 바로 · 창구·재개 몇 초 뒤) · -1 = 끔(운영자가 직접 입력) · 0 이상 = 그 초 뒤
 */

export type AutoMode = "default" | "off" | "sec";

export const AUTO_OFF = -1;
export const AUTO_MAX_SEC = 1800;

export const AUTO_MODE_OPTIONS: { id: AutoMode; label: string }[] = [
  { id: "default", label: "기본 규칙" },
  { id: "off", label: "끔 (운영자 입력)" },
  { id: "sec", label: "직접 입력(초)" },
];

export function toAutoMode(value: number | null | undefined): AutoMode {
  if (value === null || value === undefined) return "default";
  return value < 0 ? "off" : "sec";
}

/** 단계 저장 요청에 넣을 값. 범위를 벗어난 초는 null(저장하지 않음) — 화면이 막는다. */
export function autoAdvancePayload(
  mode: AutoMode,
  sec: number
): { clearAutoAdvance: true } | { autoAdvanceSec: number } | null {
  if (mode === "default") return { clearAutoAdvance: true };
  if (mode === "off") return { autoAdvanceSec: AUTO_OFF };
  if (!Number.isInteger(sec) || sec < 0 || sec > AUTO_MAX_SEC) return null;
  return { autoAdvanceSec: sec };
}

/** 단계 목록에 붙이는 짧은 표시. 기본 규칙이면 표시하지 않는다. */
export function autoAdvanceLabel(value: number | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  return value < 0 ? "자동 진행 끔" : `${value}초 뒤 자동 진행`;
}
