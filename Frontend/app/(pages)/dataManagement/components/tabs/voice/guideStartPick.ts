/** 안내 시작 창의 호출 위치 선택 규칙. id 0 = 현재 위치에서 바로 시작. */

export type StartOption = { id: number; label: string; disabled?: boolean; disabledReason?: string | null };

export const IN_PLACE_START: StartOption = { id: 0, label: "현재 위치에서 바로 시작" };

/**
 * 시작 창을 열 때 미리 고를 위치 — 시나리오의 기본 시작 장소가 운영 맵 장소 목록에 있으면 그 장소.
 * 맵이 바뀌었거나 장소가 지워져 목록에 없으면 현재 위치로 둔다(엉뚱한 곳으로 보내지 않게).
 */
export function defaultStartPick(startLocationId: number | null | undefined, places: StartOption[]): number {
  // 노드망으로 갈 수 없게 된 장소(disabled)는 기본으로 고르지 않는다
  return startLocationId && places.some((p) => p.id === startLocationId && !p.disabled) ? startLocationId : 0;
}

/** 시작 창 목록 — 기본 시작 장소가 있으면 맨 위에 '(기본)'으로, 그다음 현재 위치, 나머지 장소. */
export function buildStartOptions(defaultId: number, places: StartOption[]): StartOption[] {
  const def = places.find((p) => p.id === defaultId);
  if (!def) return [IN_PLACE_START, ...places];
  return [{ id: def.id, label: `${def.label} (기본)` }, IN_PLACE_START, ...places.filter((p) => p.id !== def.id)];
}

/** 시작 버튼 문구 — 누르면 어디로 가는지 버튼에서 바로 보이게 한다. */
export function startButtonLabel(pick: number, places: StartOption[]): string {
  const place = pick ? places.find((p) => p.id === pick) : undefined;
  return place ? `${place.label}(으)로 이동 후 시작` : "현재 위치에서 시작";
}
