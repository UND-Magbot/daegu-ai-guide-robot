import { describe, expect, it } from "vitest";
import {
  appendIndex,
  blankStep,
  canMoveDown,
  canMoveUp,
  clearTargets,
  deleteBlockers,
  deleteStep,
  duplicateStep,
  flowParts,
  insertStep,
  moveStep,
  nameOf,
  nextCode,
  quickWarning,
  sameSteps,
  setWait,
  type DraftStep,
} from "../scenarioDraft";

const step = (code: string, patch: Partial<DraftStep> = {}): DraftStep => ({
  ...blankStep(code),
  stepLabel: null,
  sayText: "멘트",
  ...patch,
});

/** 인사 → 준비 확인(예/아니요, 아니요 → 종료) → 접수 이동 → 종료 */
const flow = (): DraftStep[] => [
  step("greet", { nextStepCode: "s_001" }),
  step("s_001", { stepLabel: "준비 확인", waitFor: "user", expectedIntent: "YES_NO", nextStepCode: "s_002", onNoStepCode: "finish" }),
  step("s_002", { stepLabel: "접수 이동", waitFor: "arrival", targetLocationId: 7, nextStepCode: "finish" }),
  step("finish"),
];

describe("단계 코드", () => {
  it("s_ 번호 다음 칸을 붙이고 겹치지 않는다", () => {
    expect(nextCode(flow())).toBe("s_003");
    expect(nextCode([step("greet")])).toBe("s_001");
  });
  it("표시 이름 → 코드 이름표 → 코드 순으로 보인다", () => {
    expect(nameOf(step("greet"))).toBe("인사");
    expect(nameOf(step("s_009", { stepLabel: "약 받기" }))).toBe("약 받기");
    expect(nameOf(step("s_009"))).toBe("s_009");
  });
});

describe("추가·복제", () => {
  it("아래에 추가하면 앞 단계와 뒤 단계 사이에 끼워 흐름이 끊기지 않는다", () => {
    const r = insertStep(flow(), 3);          // 접수 이동(2) 과 종료(3) 사이
    expect(r.steps.map((s) => s.stepCode)).toEqual(["greet", "s_001", "s_002", "s_003", "finish"]);
    expect(r.steps[2].nextStepCode).toBe("s_003");
    expect(r.steps[3].nextStepCode).toBe("finish");
    expect(r.index).toBe(3);
  });
  it("앞 단계가 다른 곳으로 이어져 있으면 앞 단계는 건드리지 않는다", () => {
    const st = flow();
    st[0].nextStepCode = "s_002";              // 인사가 준비 확인을 건너뛰도록 고친 경우
    const r = insertStep(st, 1);
    expect(r.steps[0].nextStepCode).toBe("s_002");
    expect(r.steps[1].nextStepCode).toBe("s_001");
  });
  it("끝 단계 뒤에 붙이면 끝 단계는 그대로 끝난다(다음을 잇지 않음)", () => {
    const r = insertStep(flow(), 4);
    expect(r.steps[4].nextStepCode).toBeNull();
    expect(r.steps[3].nextStepCode).toBeNull();
  });
  it("[+ 단계 추가]는 종료 단계 앞에 넣어 종료로 이어지게 한다", () => {
    const st = flow();
    expect(appendIndex(st)).toBe(3);
    const r = insertStep(st, appendIndex(st));
    expect(r.steps.map((s) => s.stepCode)).toEqual(["greet", "s_001", "s_002", "s_003", "finish"]);
    expect(r.steps[3].nextStepCode).toBe("finish");
    expect(appendIndex([step("greet"), step("s_001")])).toBe(2);
  });
  it("복제는 멘트·종류·장소를 가져오고 갈래는 비운다", () => {
    const r = duplicateStep(flow(), 1);
    const c = r.steps[2];
    expect(c.stepCode).toBe("s_003");
    expect(c.waitFor).toBe("user");
    expect(c.expectedIntent).toBe("YES_NO");
    expect([c.nextStepCode, c.onNoStepCode, c.onTimeoutStepCode]).toEqual([null, null, null]);
    expect(c.stepLabel).toBe("준비 확인 복제");
  });
});

describe("삭제", () => {
  it("'다음'으로만 이어진 단계는 지우면 앞 단계가 그 다음으로 바로 이어진다", () => {
    const out = deleteStep(flow(), 2);       // 접수 이동
    expect(out.map((s) => s.stepCode)).toEqual(["greet", "s_001", "finish"]);
    expect(out[1].nextStepCode).toBe("finish");
  });
  it("'아니요'·'무응답' 갈래로 가리키는 단계는 막는다", () => {
    const st = flow();
    const i = st.findIndex((s) => s.stepCode === "finish");
    const b = deleteBlockers(st, i);
    expect(b.map((x) => [x.from.stepCode, x.field])).toEqual([["s_001", "onNoStepCode"]]);
    expect(deleteBlockers(st, 2)).toEqual([]);
  });
});

describe("순서", () => {
  it("시작 단계는 옮기지 않고, 다른 단계를 맨 위로 올리지 않는다", () => {
    const st = flow();
    expect(canMoveUp(0)).toBe(false);
    expect(canMoveUp(1)).toBe(false);
    expect(canMoveUp(2)).toBe(true);
    expect(canMoveDown(st, 0)).toBe(false);
    expect(canMoveDown(st, 3)).toBe(false);
    expect(moveStep(st, 2, -1).map((s) => s.stepCode)).toEqual(["greet", "s_002", "s_001", "finish"]);
  });
});

describe("기다림 바꾸기", () => {
  it("이동 → 말만: 장소·부르는 이름이 비워진다", () => {
    const s = setWait({ ...flow()[2], spokenPlaceName: "원무과", progressText: "원무과 방향으로 이동하고 있습니다." }, "none");
    expect(s.targetLocationId).toBeNull();
    expect(s.slotName).toBeNull();
    expect(s.spokenPlaceName).toBeNull();
    expect(s.progressText).toBeNull();
  });
  it("대답 → 운영자: 인식 종류·갈래가 비워진다(입력 종류와 다른 값)", () => {
    const s = setWait(flow()[1], "operator");
    expect([s.expectedIntent, s.onNoStepCode, s.onTimeoutStepCode]).toEqual([null, null, null]);
  });
  it("운영자 → 말만: 입력 종류·자동 진행이 비워진다", () => {
    const s = setWait(step("x", { waitFor: "operator", expectedIntent: "TICKET", autoAdvanceSec: 20 }), "none");
    expect([s.expectedIntent, s.autoAdvanceSec]).toEqual([null, null]);
  });
});

describe("목록 표시", () => {
  it("흐름 요약 — 예/아니요는 '아니요', 목적지는 '없어요'", () => {
    const st = flow();
    expect(flowParts(st, st[1])).toEqual(["→ 접수 이동", "아니요 → 종료", "무응답 → 안내 종료"]);
    st[1].expectedIntent = "DESTINATION";
    expect(flowParts(st, st[1])[1]).toBe("없어요 → 종료");
    expect(flowParts(st, st[3])).toEqual(["→ 안내 종료"]);
  });
  it("빠른 표시 — 장소 미연결·인식 종류 없음·없는 단계·빈 멘트(이동 단계 제외)", () => {
    const st = flow();
    const places = new Set([7]);
    expect(quickWarning(st, st[2], 47, places)).toBeNull();
    expect(quickWarning(st, { ...st[2], targetLocationId: null }, 47, places)).toBe("장소 미연결");
    expect(quickWarning(st, { ...st[2], targetLocationId: 9 }, 47, places)).toBe("다른 맵 장소");
    expect(quickWarning(st, { ...st[1], expectedIntent: null }, 47, places)).toBe("인식 종류 없음");
    expect(quickWarning(st, { ...st[0], nextStepCode: "nope" }, 47, places)).toBe("없는·꺼진 단계로 이어짐");
    expect(quickWarning(st, { ...st[0], sayText: " " }, 47, places)).toBe("멘트 없음");
    expect(quickWarning(st, { ...st[2], sayText: "" }, 47, places)).toBeNull();
  });
  it("운영 맵을 바꾸면 장소 연결만 비운다", () => {
    const r = clearTargets(flow());
    expect(r.cleared).toBe(1);
    expect(r.steps[2].targetLocationId).toBeNull();
    expect(r.steps[1].expectedIntent).toBe("YES_NO");
  });
  it("멘트 앞뒤 공백만 다르면 바뀌지 않은 것으로 본다", () => {
    const a = flow();
    const b = flow();
    b[0].sayText = " 멘트 ";
    expect(sameSteps(a, b)).toBe(true);
    b[0].sayText = "다른 멘트";
    expect(sameSteps(a, b)).toBe(false);
  });
});

describe("운영자 단계 음성 출구(11-02)", () => {
  /** 편의점 앞 대기(운영자) → 순번(다음) / "대기 자리로 안내해 줘" → 대기 자리 이동 */
  const store = (): DraftStep[] => [
    step("greet", { nextStepCode: "store_wait" }),
    step("store_wait", {
      stepLabel: "편의점 앞 대기",
      waitFor: "operator",
      expectedIntent: "RESUME",
      nextStepCode: "turn_ready",
      voiceExitKeywords: "대기 자리, 돌아갈",
      voiceExitStepCode: "store_return_move",
    }),
    step("store_return_move", { stepLabel: "대기자리 이동", waitFor: "arrival", targetLocationId: 7, nextStepCode: "turn_ready" }),
    step("turn_ready", { stepLabel: "순번", waitFor: "user", expectedIntent: "YES_NO", nextStepCode: null }),
  ];

  it("흐름 요약에 '말하면 →' 갈래를 보여 준다", () => {
    const s = store();
    expect(flowParts(s, s[1])).toEqual(["→ 순번", "말하면 → 대기자리 이동"]);
  });

  it("음성 출구로 가리키는 단계는 지울 수 없다(어디로 보낼지 사람이 정한다)", () => {
    const blockers = deleteBlockers(store(), 2);
    expect(blockers.map((b) => b.field)).toEqual(["voiceExitStepCode"]);
  });

  it("운영자가 아닌 기다림으로 바꾸면 음성 출구 칸을 비운다", () => {
    const s = setWait(store()[1], "user");
    expect(s.voiceExitKeywords).toBeNull();
    expect(s.voiceExitStepCode).toBeNull();
  });

  it("복제하면 음성 출구 갈래는 비운다(말 목록은 남긴다)", () => {
    const { steps, index } = duplicateStep(store(), 1);
    expect(steps[index].voiceExitStepCode).toBeNull();
    expect(steps[index].voiceExitKeywords).toBe("대기 자리, 돌아갈");
  });

  it("음성 출구가 꺼진·없는 단계로 이어지면 빠른 표시", () => {
    const s = store();
    s[1] = { ...s[1], voiceExitStepCode: "nope" };
    expect(quickWarning(s, s[1], 1, new Set([7]))).toBe("없는·꺼진 단계로 이어짐");
  });
});
