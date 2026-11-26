import type { GuideStep, WaitFor } from "./guideScenarioApi";
import { stepLabel } from "./guideScenarioLabels";

/**
 * 안내 시나리오 편집기의 초안 — 화면이 들고 있다가 [저장]에서 단계 전체를 한 번에 보낸다.
 * 검사의 기준은 서버(voice_scenario_editor.py)다. 여기 있는 표시는 목록에 바로 보이는 빠른 표시일 뿐이다.
 */

export type DraftStep = {
  stepCode: string;
  stepLabel: string | null;
  sayText: string;
  waitFor: WaitFor;
  expectedIntent: string | null;
  retryCount: number;
  responseTimeoutSec: number;
  slotName: string | null;
  targetLocationId: number | null;
  nextStepCode: string | null;
  onNoStepCode: string | null;
  onTimeoutStepCode: string | null;
  autoAdvanceSec: number | null;
  /** 이동 단계의 부르는 이름("곧 원무과에 도착합니다") */
  spokenPlaceName: string | null;
  /** 이동 단계의 진행 멘트("편의점 방향으로 이동하고 있습니다.") */
  progressText: string | null;
  /** 운영자 단계 음성 출구 — 알아들을 말(쉼표 구분)과 넘어갈 단계. 둘 다 있어야 로봇이 듣는다 */
  voiceExitKeywords: string | null;
  voiceExitStepCode: string | null;
  isActive: boolean;
};

/** 고객 목적지로 이동 — 목적지 사전의 연결 장소로 간다(엔진 슬롯 이름) */
export const SLOT_DEST = "목적지";
/** "그만" 이라고 하면 엔진이 와서 인사하고 끝내는 단계 */
export const FINISH_CODE = "finish";

/** 목록의 [+ 단계 추가] 자리 — 마지막이 종료 단계면 그 앞에 넣어 종료로 이어지게 한다 */
export const appendIndex = (steps: DraftStep[]) =>
  steps.length > 1 && steps[steps.length - 1].stepCode === FINISH_CODE ? steps.length - 1 : steps.length;
const AUTO_CODE = /^s_(\d+)$/;

export function toDraft(steps: GuideStep[]): DraftStep[] {
  return steps.map((st) => ({
    stepCode: st.stepCode,
    stepLabel: st.stepLabel ?? null,
    sayText: st.sayText ?? "",
    waitFor: st.waitFor,
    expectedIntent: st.expectedIntent,
    retryCount: st.retryCount,
    responseTimeoutSec: st.responseTimeoutSec,
    slotName: st.slotName,
    targetLocationId: st.targetLocationId,
    nextStepCode: st.nextStepCode,
    onNoStepCode: st.onNoStepCode,
    onTimeoutStepCode: st.onTimeoutStepCode,
    autoAdvanceSec: st.autoAdvanceSec,
    spokenPlaceName: st.spokenPlaceName ?? null,
    progressText: st.progressText ?? null,
    voiceExitKeywords: st.voiceExitKeywords ?? null,
    voiceExitStepCode: st.voiceExitStepCode ?? null,
    isActive: st.isActive,
  }));
}

/** 서버 저장 형식(StepIn) — 순서가 곧 진행 순서다 */
export const toPayload = (steps: DraftStep[]) => steps.map((s) => ({ ...s, sayText: s.sayText.trim() }));

/** 화면 이름 — 표시 이름 → 기본 코드 이름표 → 코드 */
export const nameOf = (s: Pick<DraftStep, "stepCode" | "stepLabel"> | undefined | null) =>
  s ? s.stepLabel?.trim() || stepLabel(s.stepCode) : "-";

/** 새 단계 코드 — s_001 부터 비어 있는 번호. 한 번 정하면 바꾸지 않는다(다른 단계가 코드로 가리킨다) */
export function nextCode(steps: DraftStep[]): string {
  const used = new Set(steps.map((s) => s.stepCode));
  let n = 1;
  for (const s of steps) {
    const m = AUTO_CODE.exec(s.stepCode);
    if (m) n = Math.max(n, Number(m[1]) + 1);
  }
  while (used.has(`s_${String(n).padStart(3, "0")}`)) n += 1;
  return `s_${String(n).padStart(3, "0")}`;
}

export function blankStep(code: string): DraftStep {
  return {
    stepCode: code,
    stepLabel: "새 단계",
    sayText: "",
    waitFor: "none",
    expectedIntent: null,
    retryCount: 2,
    responseTimeoutSec: 10,
    slotName: null,
    targetLocationId: null,
    nextStepCode: null,
    onNoStepCode: null,
    onTimeoutStepCode: null,
    autoAdvanceSec: null,
    spokenPlaceName: null,
    progressText: null,
    voiceExitKeywords: null,
    voiceExitStepCode: null,
    isActive: true,
  };
}

/**
 * 단계 추가. at 자리에 넣는다(위에 추가 = 고른 단계 자리, 아래에 추가 = 고른 단계 다음 자리).
 * 순서상 바로 앞 단계가 원래 뒤 단계로 이어져 있었으면 그 사이에 끼운다 — 흐름이 끊기지 않게.
 */
export function insertStep(steps: DraftStep[], at: number): { steps: DraftStep[]; index: number } {
  const code = nextCode(steps);
  const prev = steps[at - 1];
  const after = steps[at];
  const fresh = { ...blankStep(code), nextStepCode: after?.stepCode ?? null };
  const out = steps.map((s) => ({ ...s }));
  // 끝 단계(다음 없음) 뒤에 붙일 때는 앞 단계를 잇지 않는다 — 종료에서 안내가 끝나야 한다
  if (prev && after && prev.nextStepCode === after.stepCode) out[at - 1] = { ...out[at - 1], nextStepCode: code };
  out.splice(at, 0, fresh);
  return { steps: out, index: at };
}

/** 복제 — 멘트·종류·장소는 복사하고 갈래는 비운다(엉뚱한 곳으로 이어지지 않게). 원래 단계 바로 아래에 둔다 */
export function duplicateStep(steps: DraftStep[], index: number): { steps: DraftStep[]; index: number } {
  const src = steps[index];
  const code = nextCode(steps);
  const copy: DraftStep = {
    ...src,
    stepCode: code,
    stepLabel: `${nameOf(src)} 복제`,
    nextStepCode: null,
    onNoStepCode: null,
    onTimeoutStepCode: null,
    voiceExitStepCode: null,
  };
  const out = steps.map((s) => ({ ...s }));
  out.splice(index + 1, 0, copy);
  return { steps: out, index: index + 1 };
}

export type Reference = {
  from: DraftStep;
  field: "nextStepCode" | "onNoStepCode" | "onTimeoutStepCode" | "voiceExitStepCode";
};

/** 이 단계를 가리키는 다른 단계 */
export function referencesTo(steps: DraftStep[], code: string): Reference[] {
  const out: Reference[] = [];
  for (const s of steps) {
    if (s.stepCode === code) continue;
    for (const field of ["nextStepCode", "onNoStepCode", "onTimeoutStepCode", "voiceExitStepCode"] as const) {
      if (s[field] === code) out.push({ from: s, field });
    }
  }
  return out;
}

/**
 * 삭제 가능 여부. '다음' 으로 이어진 단계는 지운 단계의 다음으로 바로 잇는다(앞 단계가 다음 단계로 이어짐).
 * '아니요'·'무응답' 갈래로 가리키는 곳이 있으면 막는다 — 어디로 보낼지는 사람이 정해야 한다.
 */
export function deleteBlockers(steps: DraftStep[], index: number): Reference[] {
  return referencesTo(steps, steps[index].stepCode).filter((r) => r.field !== "nextStepCode");
}

export function deleteStep(steps: DraftStep[], index: number): DraftStep[] {
  const gone = steps[index];
  return steps
    .filter((_, i) => i !== index)
    .map((s) => (s.nextStepCode === gone.stepCode ? { ...s, nextStepCode: gone.nextStepCode } : { ...s }));
}

/** 순서 바꾸기 — 시작 단계(맨 위)는 옮기지 않고, 다른 단계를 맨 위로 올리지도 않는다(시작이 바뀌면 안내 전체가 바뀐다) */
export const canMoveUp = (index: number) => index > 1;
export const canMoveDown = (steps: DraftStep[], index: number) => index > 0 && index < steps.length - 1;

export function moveStep(steps: DraftStep[], index: number, delta: -1 | 1): DraftStep[] {
  const to = index + delta;
  const out = steps.map((s) => ({ ...s }));
  [out[index], out[to]] = [out[to], out[index]];
  return out;
}

/**
 * 기다림 바꾸기 — 이전 종류에만 쓰던 칸을 비운다. 남기면 엔진이 엉뚱한 값으로 움직인다(서버도 같은 규칙으로 정리).
 * 대답 단계의 '아니요(없어요)' 갈래는 대답 단계끼리 바꿀 때 유지한다.
 */
export function setWait(step: DraftStep, wait: WaitFor): DraftStep {
  const s: DraftStep = { ...step, waitFor: wait };
  if (wait !== "arrival") {
    s.targetLocationId = null;
    s.slotName = null;
    s.spokenPlaceName = null;
    s.progressText = null;
  }
  if (wait !== "user") {
    s.onNoStepCode = null;
    s.onTimeoutStepCode = null;
  }
  if (wait !== step.waitFor && (wait === "user" || wait === "operator" || step.waitFor === "user" || step.waitFor === "operator")) {
    s.expectedIntent = null;     // 대답의 인식 종류와 운영자의 입력 종류는 서로 다른 값이다
  }
  if (wait !== "operator") {
    s.autoAdvanceSec = null;
    s.voiceExitKeywords = null;
    s.voiceExitStepCode = null;
  }
  return s;
}

/** '아니요' 갈래가 뜻이 있는 인식 종류 — 예/아니요, 그리고 목적지 질문의 "없어요"·"출구요" */
export const hasNoBranch = (intent: string | null) => intent === "YES_NO" || intent === "PURPOSE" || intent === "DESTINATION";

/** 목록의 흐름 요약 한 줄 */
export function flowParts(steps: DraftStep[], s: DraftStep): string[] {
  const by = new Map(steps.map((x) => [x.stepCode, x]));
  const label = (code: string | null, empty: string) => (code ? (by.has(code) ? nameOf(by.get(code)) : `없는 단계(${code})`) : empty);
  const parts = [`→ ${label(s.nextStepCode, "안내 종료")}`];
  if (s.waitFor === "user") {
    if (s.onNoStepCode) parts.push(`${s.expectedIntent === "YES_NO" ? "아니요" : "없어요"} → ${label(s.onNoStepCode, "")}`);
    parts.push(`무응답 → ${label(s.onTimeoutStepCode, "안내 종료")}`);
  }
  if (s.waitFor === "operator" && s.voiceExitStepCode) parts.push(`말하면 → ${label(s.voiceExitStepCode, "")}`);
  return parts;
}

/** 목록에 바로 보이는 표시(서버 검사 결과가 오면 그것이 우선) */
export function quickWarning(steps: DraftStep[], s: DraftStep, mapId: number | null, placeIds: Set<number>): string | null {
  if (!s.isActive) return null;
  if (s.waitFor === "arrival" && !s.slotName) {
    if (!s.targetLocationId) return "장소 미연결";
    if (!mapId || !placeIds.has(s.targetLocationId)) return "다른 맵 장소";
  }
  if (s.waitFor === "user" && !s.expectedIntent) return "인식 종류 없음";
  if (s.waitFor === "operator" && !s.expectedIntent) return "입력 종류 없음";
  const codes = new Set(steps.filter((x) => x.isActive).map((x) => x.stepCode));
  for (const ref of [s.nextStepCode, s.onNoStepCode, s.onTimeoutStepCode, s.voiceExitStepCode]) {
    if (ref && !codes.has(ref)) return "없는·꺼진 단계로 이어짐";
  }
  if (!s.sayText.trim() && s.waitFor !== "arrival") return "멘트 없음";
  return null;
}

/** 운영 맵을 바꾸면 이동 장소 연결을 비운다 — 장소 id 는 맵마다 다르다 */
export function clearTargets(steps: DraftStep[]): { steps: DraftStep[]; cleared: number } {
  let cleared = 0;
  const out = steps.map((s) => {
    if (s.targetLocationId === null) return { ...s };
    cleared += 1;
    return { ...s, targetLocationId: null };
  });
  return { steps: out, cleared };
}

export const sameSteps = (a: DraftStep[], b: DraftStep[]) => JSON.stringify(toPayload(a)) === JSON.stringify(toPayload(b));
