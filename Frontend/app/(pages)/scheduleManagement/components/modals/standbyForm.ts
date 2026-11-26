/**
 * 작업 등록 › 상시 대기 — 대기 장소·응대 방식 고르기 규칙.
 * 화면(ScheduleInsert)과 테스트가 같이 쓴다. 저장 검증은 서버(schedule.py)가 한다.
 */

export type StandbyPlace = { id: number; name: string; mapId: number | null };
export type StandbyScenarioOption = { code: string; name: string; mapId: number | null };

/** 응대 방식 '단발 응대' — 시나리오 없이 목적지 하나로 안내하고 돌아온다(StandbyScenarioCode = null) */
export const SINGLE_RESPONSE = "";

/**
 * 응대 방식 '단순 동작' — "큐리야, 앉아 / 일어서"만 알아듣고 안내는 하지 않는다.
 * 화면에서만 쓰는 선택값이다. 저장할 때는 StandbyResponseMode = "motion" 으로 바뀐다(responsePayload).
 */
export const MOTION_RESPONSE = "__motion__";

/** 선택한 응대 방식을 서버 필드로 바꾼다 — 단순 동작은 시나리오 코드를 비운다 */
export function responsePayload(code: string): { StandbyScenarioCode: string | null; StandbyResponseMode: "motion" | null } {
  if (code === MOTION_RESPONSE) return { StandbyScenarioCode: null, StandbyResponseMode: "motion" };
  return { StandbyScenarioCode: code || null, StandbyResponseMode: null };
}

/**
 * 대기 장소 후보 — 로봇이 지금 있는 맵의 장소만. 로봇을 고르기 전이거나 로봇의 맵을 모르면 비운다.
 * 다른 맵의 장소로 보내면 좌표계가 달라 엉뚱한 곳에 선다.
 */
export function placesOnRobotMap(places: StandbyPlace[], robotMapId: number | null | undefined): StandbyPlace[] {
  if (!robotMapId) return [];
  return places
    .filter((p) => p.mapId === robotMapId)
    .sort((a, b) => a.name.localeCompare(b.name, "ko"));
}

/** 대기 장소 칸이 막힌 이유 — null 이면 고를 수 있다 */
export function placeBlockedReason(robotSelected: boolean, robotMapId: number | null | undefined, count: number): string | null {
  if (!robotSelected) return "로봇을 먼저 선택하세요";
  if (!robotMapId) return "로봇의 현재 맵을 알 수 없습니다 — 로봇이 연결돼 맵에 올라온 뒤 고를 수 있습니다";
  if (count === 0) return "로봇이 있는 맵에 대기 장소로 쓸 장소가 없습니다 — 맵 관리에서 장소를 먼저 등록하세요";
  return null;
}

/**
 * 응대 방식 선택지 — 단발 응대 + 단순 동작 + 켜진 안내 시나리오(서버가 켜진 것만 준다).
 * 단순 동작은 말로만 시키므로 이 로봇에 마이크가 있을 때만 고를 수 있다.
 */
export function responseOptions(scenarios: StandbyScenarioOption[], micAllowed = true): { id: string; label: string }[] {
  return [
    { id: SINGLE_RESPONSE, label: "단발 응대 (목적지 안내)" },
    ...(micAllowed ? [{ id: MOTION_RESPONSE, label: "단순 동작 (앉아·일어서)" }] : []),
    ...scenarios.map((s) => ({ id: s.code, label: `안내 시나리오 · ${s.name}` })),
  ];
}

/** 마이크 근무 — 손님은 호출어를 모르므로 로봇이 대기 장소에서 부르는 방법을 말한다(robot_voice STANDBY_INVITE_*) */
const MIC_INVITE_NOTE = " 로봇이 대기 장소에서 \"큐리야\"라고 부르는 방법을 주기적으로 안내합니다.";

/** 응대 방식 설명 — 호출 방식을 고르면 손님·운영자가 어떻게 시작하는지까지 적는다 */
export function responseHint(code: string, scenarios: StandbyScenarioOption[], trigger?: string | null): string {
  return describeResponse(code, scenarios, trigger) + (trigger === "mic" && code !== MOTION_RESPONSE ? MIC_INVITE_NOTE : "");
}

function describeResponse(code: string, scenarios: StandbyScenarioOption[], trigger?: string | null): string {
  if (code === MOTION_RESPONSE) {
    return "대기 장소에서 \"큐리야, 앉아\" 또는 \"큐리야, 일어서\"라고 하면 그 자세로 바꿉니다. 목적지 안내는 하지 않고 응대 기록도 남기지 않습니다. 호출 방식은 마이크여야 합니다.";
  }
  if (code === SINGLE_RESPONSE) {
    if (trigger === "mic") {
      return "대기 장소에서 \"큐리야, 원무과 안내해 줘\"처럼 부르면 목적지 한 곳으로 안내한 뒤 대기 장소로 돌아옵니다. \"큐리야\"만 부르면 어디로 갈지 묻습니다.";
    }
    return `${callPhrase(trigger)} \"어디로 안내해 드릴까요?\"라고 묻고, 목적지 한 곳으로 안내한 뒤 대기 장소로 돌아옵니다.`;
  }
  const sc = scenarios.find((s) => s.code === code);
  return `${callPhrase(trigger)} ${sc?.name ?? "안내"} 시나리오를 첫 단계부터 진행하고, 끝나면 대기 장소로 돌아옵니다.`;
}

function callPhrase(trigger?: string | null): string {
  if (trigger === "mic") return "대기 장소에서 \"큐리야\"라고 부르면";
  if (trigger === "button") return "관제·태블릿에서 [안내 시작]을 누르면";
  return "호출되면";
}

/** 시나리오의 운영 맵이 로봇의 지금 맵과 다르면 알린다(막지는 않는다 — 서버도 경고만) */
export function scenarioMapWarning(code: string, scenarios: StandbyScenarioOption[], robotMapId: number | null | undefined): string | null {
  if (code === SINGLE_RESPONSE || code === MOTION_RESPONSE || !robotMapId) return null;
  const sc = scenarios.find((s) => s.code === code);
  if (!sc?.mapId || sc.mapId === robotMapId) return null;
  return `대기 장소(로봇이 있는 맵)와 '${sc.name}' 시나리오의 맵이 다릅니다 — 호출되면 시나리오의 안내 장소로 이동하지 못할 수 있습니다.`;
}

/** 로봇을 바꿨을 때 이전 선택이 새 로봇에서 맞지 않으면 비운다 */
export function keepPlace(placeId: number | null, onMap: StandbyPlace[]): number | null {
  return placeId !== null && onMap.some((p) => p.id === placeId) ? placeId : null;
}

export function keepResponse(code: string, scenarios: StandbyScenarioOption[], micAllowed = true): string {
  if (code === MOTION_RESPONSE) return micAllowed ? code : SINGLE_RESPONSE;
  return code !== SINGLE_RESPONSE && !scenarios.some((s) => s.code === code) ? SINGLE_RESPONSE : code;
}
