import { apiFetch } from "@/app/lib/api";

/**
 * 다단계 안내 시나리오(병원·관공서) — 관제 운영자 경로.
 * 단발 FAQ(voiceApi 의 voice-intents)와 다른 테이블이다. 화면에서도 '안내 시나리오'로 구분한다.
 */

export type WaitFor = "none" | "user" | "operator" | "arrival";
export type OperatorInput = "TICKET" | "COUNTER" | "RESUME";
/** 안내 이동 속도 — slow 는 사람이 따라오는 속도. 호출 위치로 가는 이동에는 쓰지 않는다 */
export type GuideSpeed = "slow" | "normal" | "fast";
export type FollowState = "waiting" | "lost" | "resume";
/** 운영자 일시 정지 — requested(다음 이동 단계 앞에서 멈출 예정) | holding(멈춰서 재개 대기) */
export type HoldState = "requested" | "holding";

export type GuideStep = {
  id: number;
  stepCode: string;
  /** 화면에 보이는 단계 이름. 비면 코드 이름표(guideScenarioLabels.stepLabel) */
  stepLabel?: string | null;
  stepOrder: number;
  sayText: string | null;
  waitFor: WaitFor;
  expectedIntent: string | null;
  retryCount: number;
  responseTimeoutSec: number;
  actionType: string;
  slotName: string | null;
  targetLocationId: number | null;
  targetName: string | null;
  needsTarget: boolean;
  targetMissing: boolean;
  targetOffMap: boolean;
  nextStepCode: string | null;
  onNoStepCode: string | null;
  onTimeoutStepCode: string | null;
  /** 운영자 단계 자동 진행(초). null = 기본 규칙 · -1 = 끔 — autoAdvance.ts */
  autoAdvanceSec: number | null;
  /** 이동 단계의 부르는 이름 — 도착 직전 "곧 ○○에 도착합니다." null = "곧 도착합니다." */
  spokenPlaceName?: string | null;
  /** 이동 단계의 진행 멘트 — 걷기 시작하고 조금 뒤 한 번. null = 말하지 않음 */
  progressText?: string | null;
  /** 운영자 단계 음성 출구 — 기다리는 동안 이 말(쉼표 구분) 중 하나가 들리면 voiceExitStepCode 로 넘어간다 */
  voiceExitKeywords?: string | null;
  voiceExitStepCode?: string | null;
  isActive: boolean;
};

export type GuideScenario = {
  id: number;
  code: string;
  displayName: string;
  floorId: number | null;
  mapId: number | null;
  /** 초기 대기(호출) 위치 기본값 — 안내 시작 창에 미리 골라 둔다 */
  startLocationId: number | null;
  startLocationName: string | null;
  guideSpeed: GuideSpeed;
  /** 이동 중 동행 확인("잘 따라오고 계신가요?") */
  followCheckEnabled: boolean;
  /** 묻는 간격(초). null = 구간(장소)별 과거 평균 이동 시간으로 자동 */
  followIntervalSec: number | null;
  /** 멈춰 기다리다 운영자를 호출하기까지(초) */
  followMaxWaitSec: number;
  /** 경유지 방향 안내("잠시 후 왼쪽으로 돕니다"·"곧 도착합니다") — 보이지 않는 방문객용 */
  turnCuesEnabled: boolean;
  isActive: boolean;
  hasDefault: boolean;
  missingTargets: number;
  offMapTargets: number;
  /** 운영 맵에 장소가 연결된 목적지 수 — 이 시나리오에서 알아듣는 목적지 */
  destinationsOnMap: number;
  /** 운영 맵에 연결이 없어 이 시나리오에서 알아듣지 않는 목적지 */
  destinationsOffMap: string[];
  /** 편집기 저장 버전 — 저장할 때 그대로 돌려보내 다른 사람이 먼저 저장했는지 본다 */
  version: number;
  /** 준비 점검(서버 검사) — 오류가 없고 운영 맵이 있어야 '사용' 을 켤 수 있다 */
  ready: boolean;
  checkErrors: StepIssue[];
  checkWarnings: StepIssue[];
  /** 이 시나리오를 응대 방식으로 쓰는 상시 대기 작업 */
  standbyTasks: { id: number; name: string }[];
  steps: GuideStep[];
};

/** 서버 검사 한 건 — stepCode 가 없으면 시나리오 전체에 대한 것. short 는 단계 목록 줄에 보일 짧은 문구 */
export type StepIssue = { stepCode: string | null; field: string | null; message: string; short?: string };
export type CheckResult = { errors: StepIssue[]; warnings: StepIssue[] };

export type ScenarioVersion = {
  version: number;
  note: string | null;
  createdAt: string;
  createdBy: string | null;
  stepCount: number;
};

/** 엔진이 지원하는 값 — 화면은 이 안에서만 고르게 한다(백엔드 voice_scenario_editor.capabilities) */
export type EngineCapabilities = {
  waits: WaitFor[];
  userIntents: string[];
  operatorInputs: OperatorInput[];
  slots: string[];
  placeholders: string[];
  finishCode: string;
};

/** 저장 거절 — 422 면 issues 에 단계별 오류·경고가 온다 */
export class GuideApiError extends Error {
  status: number;
  issues: CheckResult | null;
  constructor(message: string, status: number, issues: CheckResult | null) {
    super(message);
    this.status = status;
    this.issues = issues;
  }
}

export type GuidePlace = { id: number; name: string; floorId: number | null; mapId: number | null };
export type GuideFloor = { id: number; name: string };
export type GuideMap = { id: number; name: string; floorId: number };
export type GuideRobotPos = { currentFloorId: number | null; currentMapId: number | null };

export type GuideSession = {
  id: number;
  scenarioCode: string | null;
  scenarioName: string | null;
  currentStepCode: string | null;
  /** 고객 목적지 — 고객이 말해 확정한 곳({목적지}). 이동으로 바뀌지 않는다 */
  destinationName: string | null;
  /** 현재 이동 목표 — 마지막 이동 명령의 장소(발급기·대기석 …) */
  navTargetName: string | null;
  ticketNo: string | null;
  counterNo: string | null;
  /** 안내 시작 때 먼저 이동한 호출 위치 */
  startLocationId: number | null;
  startLocationName: string | null;
  heard: string | null;
  /** 동행 확인 — null(정상) | waiting(멈춰 기다림) | lost(운영자 호출됨) | resume(재개 지시됨) */
  followState: FollowState | null;
  /** 운영자 일시 정지 — null(없음) | requested | holding */
  holdState?: HoldState | null;
  /** 구간 테스트 세션 — 종료 단계를 마치면 관제가 닫는다. 응대 통계에서 빠진다 */
  isTest: boolean;
  testEndStepCode: string | null;
  result: string;
  startedAt: string;
  step: { stepCode: string; sayText: string | null; waitFor: WaitFor; expectedIntent: string | null } | null;
  operatorInput: OperatorInput | null;
  /** 상시 대기 근무에 붙은 안내면 그 작업 — 이 화면(일회성 시연용)은 시작·종료를 숨긴다 */
  scheduleId?: number | null;
  scheduleName?: string | null;
};

/** 로봇이 상시 대기 근무 중이면 그 작업 — 근무 중 안내는 로봇 상세에서 시작한다 */
export type GuideStandbyWork = { scheduleId: number; workName: string | null };

/** 직전에 끝난 안내 — 진행 중 세션이 없을 때만 온다 */
export type GuideLastEnded = {
  id: number;
  scenarioName: string | null;
  stepCode: string | null;
  result: string;
  closedBy: string | null;
  reason: string | null;
  endedAt: string;
};

async function unwrap<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `요청에 실패했습니다 (${res.status})`;
    let issues: CheckResult | null = null;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
      // 편집기 저장 거절 — {message, errors, warnings}
      else if (body?.detail && typeof body.detail.message === "string") {
        detail = body.detail.message;
        issues = { errors: body.detail.errors ?? [], warnings: body.detail.warnings ?? [] };
      }
    } catch {
      /* 본문이 JSON이 아니면 기본 메시지를 쓴다 */
    }
    throw new GuideApiError(detail, res.status, issues);
  }
  return res.json() as Promise<T>;
}

function send<T>(path: string, method: string, body: unknown): Promise<T> {
  return apiFetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(unwrap<T>);
}

type SessionRes = { session: GuideSession | null };

/** 반복을 멈춘 종류 — 백엔드 voice_loop.judge_next_round */
export type LoopStopKind = "failed" | "no_answer" | "battery" | "battery_unknown" | "position";

export type LoopCheck = {
  ok: boolean;
  /** running — 회차가 아직 안 끝났다(화면이 새 회차를 폴링하기 전). 잠시 뒤 다시 묻는다 */
  kind: LoopStopKind | "running" | null;
  reason: string | null;
  battery: number | null;
  limit: number | null;
  result: string;
};

/** 안내 사전 주행 — 안내 전에 시나리오 동선을 멘트 없이 한 바퀴(기본 고속). 백엔드 voice_prerun */
export type PrerunStatus = "running" | "loc_wait" | "failed" | "stopped" | "done";
export type PrerunLegStatus = "done" | "current" | "wait" | "error" | "pending";

export type PrerunPlanLeg = {
  id: number;
  name: string;
  /** start — 호출 위치(출발·복귀) · step — 이동 단계 장소 · destination — 고객 목적지 장소 */
  role: "start" | "step" | "destination";
  step: string | null;
  /** 대답에 따라 가는 곳 — 응답이 없을 때 가는 기본 흐름 밖(편의점 '예' 쪽 등). 갈 곳 고르기에서 처음엔 꺼 둔다 */
  branch?: boolean;
};

export type PrerunPlan = { legs: PrerunPlanLeg[]; warnings: string[]; destinations: number };

/** 1회 주행 '갈 곳 고르기' — 동선(plan.legs) 구간 번호와, 고를 때 본 구간 수(그 사이 동선이 바뀌면 서버가 거절) */
export type PrerunPick = { legIndexes: number[]; legCount: number };

export type GuidePrerun = {
  status: PrerunStatus;
  scenarioCode: string;
  scenarioName: string;
  /** 고른 속도 — 다시 돌릴 때도 같은 속도 */
  speed: GuideSpeed;
  startLocationId: number | null;
  legs: (PrerunPlanLeg & { status: PrerunLegStatus })[];
  /** 지금(또는 멈춘) 구간 번호(0부터) */
  leg: number;
  doneLegs: number;
  total: number;
  reason: string | null;
  locLost: number;
  /** 측위 회복 대기 남은 초 — loc_wait 일 때만 */
  locWaitLeft: number | null;
  elapsedSec: number;
  startedAt: string;
};

type PrerunRes = { prerun: GuidePrerun | null };

export type SegmentInput = "destination" | "ticket" | "counter";

export type SegmentTestBody = {
  scenarioCode: string;
  startStepCode: string;
  endStepCode: string;
  destinationSlotId?: number | null;
  ticketNo?: string | null;
  counterNo?: string | null;
};

export const guideScenarioApi = {
  list: (robotId: number) =>
    apiFetch(`/DB/robots/${robotId}/voice-scenarios`).then(
      unwrap<{
        scenarios: GuideScenario[];
        places: GuidePlace[];
        floors: GuideFloor[];
        maps: GuideMap[];
        robot: GuideRobotPos;
        missingDefaults: string[];
      }>
    ),

  loadDefaults: (robotId: number, reset: boolean, codes?: string[]) =>
    send<{ created: string[]; reset: string[] }>(
      `/DB/robots/${robotId}/voice-scenarios/defaults`,
      "POST",
      { reset, codes: codes ?? null }
    ),

  updateScenario: (scenarioId: number, body: unknown) =>
    send<{ status: string }>(`/DB/voice-scenarios/${scenarioId}`, "PUT", body),

  updateStep: (stepId: number, body: unknown) =>
    send<{ status: string }>(`/DB/voice-scenario-steps/${stepId}`, "PUT", body),

  /** 빈 시나리오(인사 → 종료) 만들기 — 코드는 서버가 붙이고 '사용' 은 꺼진 채로 시작한다 */
  createScenario: (robotId: number, displayName: string, mapId: number) =>
    send<{ id: number; code: string; version: number }>(`/DB/robots/${robotId}/voice-scenarios`, "POST", {
      displayName,
      mapId,
    }),

  /** 저장하지 않고 검사만. mapId: 저장 전 바꾼 운영 맵 기준으로 */
  validateSteps: (scenarioId: number, steps: unknown[], mapId?: number | null) =>
    send<CheckResult>(`/DB/voice-scenarios/${scenarioId}/validate`, "POST", { steps, mapId: mapId ?? null }),

  /** 단계 전체 저장 — baseVersion 이 지금 버전과 다르면 409(다른 사람이 먼저 저장) */
  saveSteps: (scenarioId: number, body: { steps: unknown[]; baseVersion: number; mapId?: number | null; note?: string }) =>
    send<{ version: number; warnings: StepIssue[]; stepCount: number }>(
      `/DB/voice-scenarios/${scenarioId}/steps`,
      "PUT",
      body
    ),

  versions: (scenarioId: number) =>
    apiFetch(`/DB/voice-scenarios/${scenarioId}/versions`).then(
      unwrap<{ current: number; items: ScenarioVersion[] }>
    ),

  restoreVersion: (scenarioId: number, version: number, baseVersion: number) =>
    send<{ version: number; warnings: StepIssue[] }>(
      `/DB/voice-scenarios/${scenarioId}/versions/${version}/restore`,
      "POST",
      { baseVersion }
    ),

  deleteScenario: (scenarioId: number) =>
    apiFetch(`/DB/voice-scenarios/${scenarioId}`, { method: "DELETE" }).then(unwrap<{ status: string }>),

  capabilities: () => apiFetch(`/DB/voice-engine/capabilities`).then(unwrap<EngineCapabilities>),

  currentSession: (robotId: number) =>
    apiFetch(`/DB/robots/${robotId}/voice-sessions/current`).then(
      unwrap<SessionRes & { lastEnded: GuideLastEnded | null; standbyWork?: GuideStandbyWork | null }>
    ),

  openSession: (robotId: number, scenarioCode: string, startLocationId: number | null) =>
    send<SessionRes>(`/DB/robots/${robotId}/voice-sessions`, "POST", { scenarioCode, startLocationId }),

  /** 구간 테스트 미리 보기 — 시작→종료가 이어지는가, 미리 넣어야 할 값 */
  checkSegment: (scenarioId: number, startStepCode: string, endStepCode: string) =>
    send<{ reachable: boolean; requiredInputs: SegmentInput[] }>(
      `/DB/voice-scenarios/${scenarioId}/segment-check`,
      "POST",
      { startStepCode, endStepCode }
    ),

  /** 구간 테스트 시작 — 시작 단계부터 돌고 종료 단계를 마치면 관제가 닫는다 */
  openTestSession: (robotId: number, body: SegmentTestBody) =>
    send<SessionRes>(`/DB/robots/${robotId}/voice-sessions/test`, "POST", body),

  operatorInput: (sessionId: number, body: { ticketNo?: string; counterNo?: string }) =>
    send<SessionRes>(`/DB/voice-sessions/${sessionId}/operator`, "POST", body),

  /** 일시 정지 예약 — 로봇이 다음 이동 단계 앞에서 멈춘다(지금 단계는 마저 진행) */
  holdSession: (sessionId: number) =>
    send<SessionRes>(`/DB/voice-sessions/${sessionId}/hold`, "POST", {}),

  /** 일시 정지 재개 — 아직 멈추기 전이면 예약 취소 */
  releaseHold: (sessionId: number) =>
    send<SessionRes>(`/DB/voice-sessions/${sessionId}/hold-release`, "POST", {}),

  /** 동행 이탈로 멈춘 안내를 다시 출발시킨다 — 로봇이 세션을 폴링하다 보고 출발한다 */
  followResume: (sessionId: number) =>
    send<SessionRes>(`/DB/voice-sessions/${sessionId}/follow-resume`, "POST", {}),

  /** 안내 반복 — 방금 끝난 회차를 보고 다음 회차를 열어도 되는지 묻는다(배터리 부족이면 관제 알림) */
  loopCheck: (robotId: number, sessionId: number, round: number) =>
    send<LoopCheck>(`/DB/robots/${robotId}/voice-sessions/loop-check`, "POST", { sessionId, round }),

  closeSession: (sessionId: number, reason?: string) =>
    send<SessionRes>(`/DB/voice-sessions/${sessionId}/close`, "POST", { reason: reason ?? null }),

  /** 사전 주행 동선 미리 보기 — 안내 시작 창의 경로 한 줄 */
  prerunPlan: (robotId: number, scenarioCode: string, startLocationId: number | null) =>
    send<PrerunPlan>(`/DB/robots/${robotId}/voice-prerun/plan`, "POST", { scenarioCode, startLocationId }),

  prerunCurrent: (robotId: number) =>
    apiFetch(`/DB/robots/${robotId}/voice-prerun`).then(unwrap<PrerunRes>),

  /** speed 를 빼면 서버가 고속으로 돈다(안내 시작 창의 '고속 1바퀴'). pick 을 빼면 동선 전체 */
  prerunStart: (
    robotId: number,
    scenarioCode: string,
    startLocationId: number | null,
    speed?: GuideSpeed,
    pick?: PrerunPick,
  ) =>
    send<PrerunRes>(`/DB/robots/${robotId}/voice-prerun`, "POST", {
      scenarioCode,
      startLocationId,
      speed,
      ...(pick ? { legIndexes: pick.legIndexes, legCount: pick.legCount } : {}),
    }),

  /** 멈춘 사전 주행 다시 — fromFailed: 멈춘 구간부터 · false: 처음부터 */
  prerunResume: (robotId: number, fromFailed: boolean) =>
    send<PrerunRes>(`/DB/robots/${robotId}/voice-prerun/resume`, "POST", { fromFailed }),

  prerunStop: (robotId: number) => send<PrerunRes>(`/DB/robots/${robotId}/voice-prerun/stop`, "POST", {}),

  /** 끝난 사전 주행 기록 닫기 */
  prerunDismiss: (robotId: number) => send<PrerunRes>(`/DB/robots/${robotId}/voice-prerun/dismiss`, "POST", {}),

  keepGait: (robotId: number) =>
    apiFetch(`/DB/robots/${robotId}/keep-gait`).then(unwrap<KeepGaitState>),

  /** 재배포 없이 켜고 끈다. 백엔드를 재시작하면 .env 기본값으로 돌아간다 */
  setKeepGait: (robotId: number, enabled: boolean) =>
    send<KeepGaitState>(`/DB/robots/${robotId}/keep-gait`, "PUT", { enabled }),
};

/** 안내 도착 후 내비 보행 유지 — 도착·출발마다 다리를 다시 딛는 소음을 줄인다 */
export type KeepGaitState = {
  enabled: boolean;
  /** .env(NAV_KEEP_GAIT_ON_ARRIVAL) 값 — 재시작하면 이 값으로 돌아간다 */
  envDefault: boolean;
  /** 이번 안내에서 출발 확인에 실패해 기존 방식으로 돌아갔다(다음 안내에서 다시 시도) */
  sessionOff: boolean;
  /** 지금 내비 보행으로 서 있다 */
  gaitKept: boolean;
};
