import type { GuideSpeed, OperatorInput, SegmentInput, WaitFor } from "./guideScenarioApi";

/** 호출 위치 이동 — 시나리오 단계가 아니라 안내 시작 때 세션 앞에 붙는 가상 단계(관제·Jetson 공통 코드) */
export const CALL_MOVE_STEP = "call_move";

/** 단계 코드는 엔진과 맞물린 식별자라 화면에는 사람이 읽는 이름을 붙인다 */
export const STEP_LABELS: Record<string, string> = {
  [CALL_MOVE_STEP]: "호출 위치 이동",
  greet: "인사",
  purpose_ask: "목적 질문",
  purpose_confirm: "목적 확인",
  guide_start: "안내 시작",
  ticket_move: "발급기 이동",
  ticket_wait: "번호표 발급",
  ticket_announce: "대기번호 안내",
  seat_move: "대기석 이동",
  seat_wait: "순번 대기",
  turn_confirm: "순번 도래",
  counter_move: "창구 이동",
  counter_arrive: "창구 도착",
  clinic_move: "진료실 이동",
  clinic_wait: "진료 대기",
  resume_ask: "진료 후 재개",
  payment_move: "수납 이동",
  payment_arrive: "수납 도착",
  exit_ask: "추가 안내 · 출구 확인",
  exit_move: "출구 이동",
  exit_arrive: "출구 도착",
  form_move: "신청서 이동",
  form_write: "신청서 작성",
  form_done: "작성 완료 · 대기석 이동",
  lobby_ask: "로비 확인",
  lobby_move: "로비 이동",
  lobby_arrive: "로비 도착",
  finish_ask: "추가 안내 확인",
  extra_dest: "추가 목적지",
  extra_move: "추가 이동",
  extra_arrive: "추가 도착",
  greet_move: "인사 장소 이동",
  greet_ask: "인사 · 안내 여부",
  elevator_move: "엘리베이터 이동",
  elevator_arrive: "엘리베이터 도착",
  decline_end: "안내 사양 종료",
  finish: "종료",
};

export const stepLabel = (code: string | null | undefined) =>
  code ? STEP_LABELS[code] ?? code : "-";

/** 서버 검사 문구는 표시 이름이 없는 단계를 코드로 부른다('turn_confirm') — 화면 이름표가 있으면 바꿔 보인다 */
export const withStepLabels = (message: string) =>
  message.replace(/'([a-z][a-z0-9_]*)'/g, (m, code: string) => (STEP_LABELS[code] ? `'${STEP_LABELS[code]}'` : m));

export const WAIT_LABELS: Record<WaitFor, string> = {
  none: "바로 진행",
  user: "고객 응답",
  operator: "운영자 입력",
  arrival: "도착 대기",
};

export const INTENT_LABELS: Record<string, string> = {
  PURPOSE: "목적 인식",
  DESTINATION: "목적지 인식(장소 포함)",
  YES_NO: "예/아니요",
  FORM_COMPLETE: "작성 완료",
  TICKET: "대기번호",
  COUNTER: "창구 번호",
  RESUME: "재개",
};

export const OPERATOR_ACTION: Record<OperatorInput, { title: string; button: string }> = {
  TICKET: { title: "대기번호 입력", button: "대기번호 입력" },
  COUNTER: { title: "창구 번호 입력", button: "창구 번호 입력" },
  RESUME: { title: "작업 재개", button: "안내 재개" },
};

export const GUIDE_SPEED_LABELS: Record<GuideSpeed, string> = {
  slow: "저속 (따라오는 속도)",
  normal: "보통",
  fast: "고속",
};

/** 좁은 자리(사전 주행 카드 꼬리표)용 짧은 이름 */
export const GUIDE_SPEED_SHORT: Record<GuideSpeed, string> = {
  slow: "저속",
  normal: "보통",
  fast: "고속",
};

/** 안내 종료 결과 — 세션 Result */
export const RESULT_LABELS: Record<string, string> = {
  completed: "정상 종료",
  nav_failed: "이동 실패",
  no_match: "응답 인식 실패",
  unknown_dest: "목적지 인식 실패",
  aborted_operator: "운영자 종료",
  aborted_user: "고객 취소",
  aborted_no_response: "고객 무응답",
  aborted_follow_lost: "동행 이탈 자동 종료",
  aborted_hold_timeout: "일시 정지 후 자동 종료",
  aborted_offline: "음성 엔진 끊김",
  aborted_tts: "안내 음성 출력 실패",
  aborted_bad_step: "시나리오 단계 오류",
  load_failed: "시나리오 불러오기 실패",
  test_done: "구간 테스트 완료",
};

/** 일시 정지 후 재개가 없으면 관제가 세션을 닫는 시간(분) — 백엔드 voice_scenario.HOLD_CLOSE_SEC */
export const HOLD_CLOSE_MIN = 20;

/** 멘트에 쓸 수 있는 치환자 — Jetson 단계 엔진이 채운다.
 *  {도착지}: 실제 도착 장소, {도착안내}: 목적지별 도착 문구. '{목적지}로' 는 받침에 맞춰 '으로' 로 바뀐다 */
export const SAY_TOKENS = ["{목적지}", "{도착지}", "{도착안내}", "{대기번호}", "{창구}"];

/** 구간 테스트에 미리 넣어야 하는 값 — 백엔드 voice_segment_test.required_inputs */
export const SEGMENT_INPUT_LABELS: Record<SegmentInput, string> = {
  destination: "고객 목적지",
  ticket: "대기번호",
  counter: "창구 번호",
};
