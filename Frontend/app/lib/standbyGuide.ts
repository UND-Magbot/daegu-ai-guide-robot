// 상시 대기 근무 안에서 연 안내 세션을 그 근무 카드에 붙인다 (대시보드 작업 일정).
// 안내 세션은 '길 안내' 작업 1건으로 일정 목록에 섞여 보이는데(10-20), 상시 대기 근무에서 연 세션은
// 근무 카드와 같은 일이라 따로 띄우면 두 번 보인다(11-05 메모). 근무 카드가 같은 목록에 있을 때만 접는다 —
// 근무 카드가 안 보이는 경우(오늘 회차 취소 등)에는 세션을 따로 남겨야 사라지지 않는다.

import { resolveScheduleMode, type ScheduleModeSource } from "@/app/lib/scheduleMode";

/** 접기 판정에 필요한 최소 필드. 일정 행과 안내 행이 같은 모양이다. */
export type StandbyGuideRow = ScheduleModeSource & {
  id: number;
  Kind?: "guide";
  ScheduleId?: number | null;
  TaskStatus: string;
  WorkName: string;
};

const isGuide = (r: StandbyGuideRow) => r.Kind === "guide";

/** 같은 목록에 있는 상시 대기 근무에 속한 안내 행을 뺀다. */
export function foldStandbyGuides<T extends StandbyGuideRow>(rows: T[]): T[] {
  const standbyIds = new Set(
    rows.filter((r) => !isGuide(r) && resolveScheduleMode(r) === "standby").map((r) => r.id),
  );
  return rows.filter((r) => !(isGuide(r) && r.ScheduleId != null && standbyIds.has(r.ScheduleId)));
}

/** 상시 대기 카드에 붙일 한 줄 — 응대 중이면 그 안내, 아니면 오늘 응대 건수. 없으면 null. */
export function standbyGuideSummary(guides: StandbyGuideRow[], scheduleId: number): string | null {
  const mine = guides.filter((g) => isGuide(g) && g.ScheduleId === scheduleId);
  if (mine.length === 0) return null;
  const running = mine.find((g) => g.TaskStatus === "진행중");
  if (running) return `응대 중 · ${running.WorkName}`;
  return `오늘 응대 ${mine.length}건`;
}
