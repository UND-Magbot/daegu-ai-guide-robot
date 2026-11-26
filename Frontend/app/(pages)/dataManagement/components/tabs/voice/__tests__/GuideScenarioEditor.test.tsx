import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ctx = vi.hoisted(() => ({
  showToast: vi.fn(),
  api: {
    validateSteps: vi.fn(),
    saveSteps: vi.fn(),
    updateScenario: vi.fn(),
    deleteScenario: vi.fn(),
    versions: vi.fn(),
    restoreVersion: vi.fn(),
  },
}));

vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("@/app/lib/navReach", () => ({
  useMoveBlocks: () => ({}),
  placeBlockReason: () => null,
  markBlocked: (o: unknown) => o,
}));
vi.mock("../guideScenarioApi", async (orig) => {
  const real = await orig<typeof import("../guideScenarioApi")>();
  return { ...real, guideScenarioApi: ctx.api };
});
// 안내 시작·층 이동·구간 시험은 따로 시험한다 — 여기서는 자리만
vi.mock("../GuideSessionCard", () => ({ default: () => null }));
vi.mock("../GuideFloorMoveBar", () => ({ default: () => null }));
vi.mock("../GuidePrerunTest", () => ({ default: () => null }));
vi.mock("../GuideSegmentTest", () => ({ default: () => null }));
vi.mock("../KeepGaitToggle", () => ({ default: () => null }));

import GuideScenarioEditor from "../GuideScenarioEditor";
import { GuideApiError, type GuideScenario, type GuideStep } from "../guideScenarioApi";

const gstep = (code: string, patch: Partial<GuideStep> = {}): GuideStep => ({
  id: Math.floor(Math.random() * 1e6),
  stepCode: code,
  stepLabel: null,
  stepOrder: 0,
  sayText: "멘트",
  waitFor: "none",
  expectedIntent: null,
  retryCount: 2,
  responseTimeoutSec: 10,
  actionType: "none",
  slotName: null,
  targetLocationId: null,
  targetName: null,
  needsTarget: false,
  targetMissing: false,
  targetOffMap: false,
  nextStepCode: null,
  onNoStepCode: null,
  onTimeoutStepCode: null,
  autoAdvanceSec: null,
  isActive: true,
  ...patch,
});

const scenario = (patch: Partial<GuideScenario> = {}): GuideScenario => ({
  id: 6,
  code: "scenario_6",
  displayName: "약국 안내",
  floorId: 1,
  mapId: 47,
  startLocationId: null,
  startLocationName: null,
  guideSpeed: "slow",
  followCheckEnabled: true,
  followIntervalSec: null,
  followMaxWaitSec: 60,
  turnCuesEnabled: true,
  isActive: false,
  hasDefault: false,
  missingTargets: 0,
  offMapTargets: 0,
  destinationsOnMap: 0,
  destinationsOffMap: [],
  version: 1,
  ready: true,
  checkErrors: [],
  checkWarnings: [],
  standbyTasks: [],
  steps: [
    gstep("greet", { stepLabel: "인사", nextStepCode: "finish" }),
    gstep("finish", { stepLabel: "종료" }),
  ],
  ...patch,
});

const props = (sc: GuideScenario, over: Record<string, unknown> = {}) => ({
  robotId: 10,
  robotName: "R296",
  scenario: sc,
  floors: [{ id: 1, name: "1F" }],
  maps: [{ id: 47, name: "UND_1F", floorId: 1 }],
  places: [{ id: 7, name: "원무과_접수", floorId: 1, mapId: 47 }],
  robotMapId: 47,
  mapWarning: null,
  userIntents: null,
  operatorInputs: null,
  onBack: vi.fn(),
  onReload: vi.fn().mockResolvedValue(undefined),
  onDeleted: vi.fn(),
  onResetDefault: vi.fn().mockResolvedValue(undefined),
  ...over,
});

beforeEach(() => {
  Object.values(ctx.api).forEach((f) => f.mockReset());
  ctx.showToast.mockClear();
});
afterEach(cleanup);

const click = async (el: HTMLElement) => {
  await act(async () => {
    fireEvent.click(el);
    await Promise.resolve();
  });
};

describe("저장", () => {
  it("바뀐 것이 없으면 [저장]이 꺼져 있고, 단계를 추가하면 켜진다 — 단계 전체와 화면이 들고 있던 버전을 보낸다", async () => {
    ctx.api.saveSteps.mockResolvedValue({ version: 2, warnings: [], stepCount: 3 });
    const p = props(scenario());
    render(<GuideScenarioEditor {...p} />);
    const save = screen.getByRole("button", { name: "저장" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    await click(screen.getByRole("button", { name: "+ 단계 추가" }));
    expect(save.disabled).toBe(false);
    expect(screen.getAllByText("저장 안 한 변경 있음").length).toBeGreaterThan(0);
    await click(save);
    const body = ctx.api.saveSteps.mock.calls[0][1];
    expect(ctx.api.saveSteps.mock.calls[0][0]).toBe(6);
    expect(body.baseVersion).toBe(1);
    expect(body.mapId).toBeNull();
    expect(body.steps.map((s: { stepCode: string }) => s.stepCode)).toEqual(["greet", "s_001", "finish"]);
    expect(body.steps[0].nextStepCode).toBe("s_001");
    expect(p.onReload).toHaveBeenCalled();
  });

  it("서버가 422로 거절하면 오류 목록을 보이고 그 단계를 고른다", async () => {
    ctx.api.saveSteps.mockRejectedValue(
      new GuideApiError("고칠 곳이 1건 있어 저장하지 않았습니다.", 422, {
        errors: [{ stepCode: "s_001", field: "expectedIntent", message: "'새 단계' 의 인식 종류를 고르세요" }],
        warnings: [],
      })
    );
    render(<GuideScenarioEditor {...props(scenario())} />);
    await click(screen.getByRole("button", { name: "+ 단계 추가" }));
    await click(screen.getByRole("radio", { name: "대답" }));
    await click(screen.getByRole("button", { name: /^1\s*인사/ }));       // 다른 단계로 갔다가
    await click(screen.getByRole("button", { name: "저장" }));
    expect(screen.getByRole("list", { name: "검사 결과" }).textContent).toContain("인식 종류를 고르세요");
    expect(screen.getByLabelText("단계 편집").textContent).toContain("새 단계");   // 오류 단계로 돌아옴
    expect(ctx.showToast).toHaveBeenCalledWith("고칠 곳이 1건 있어 저장하지 않았습니다.", "error");
  });

  it("[검사]는 저장하지 않고 결과만 보인다", async () => {
    ctx.api.validateSteps.mockResolvedValue({ errors: [], warnings: [{ stepCode: null, field: null, message: "'종료(finish)' 단계가 없어" }] });
    render(<GuideScenarioEditor {...props(scenario())} />);
    await click(screen.getByRole("button", { name: "검사" }));
    expect(ctx.api.saveSteps).not.toHaveBeenCalled();
    expect(screen.getByRole("list", { name: "검사 결과" }).textContent).toContain("finish");
  });

  it("실행되지 않는 단계 경고는 목록 줄에 짧게, 검사 결과·단계 편집에 전체 문장으로 — 코드는 화면 이름표로 바꾼다", async () => {
    const message =
      "'종료' 은(는) 실제 안내에서 실행되지 않습니다 — 이 단계로 넘어오는 단계 'turn_confirm' 이(가) 꺼져 있습니다. 그 단계를 켜거나, 다른 단계에서 연결하세요.";
    ctx.api.validateSteps.mockResolvedValue({
      errors: [],
      warnings: [{ stepCode: "finish", field: null, message, short: "실제 안내에서 실행 안 됨" }],
    });
    render(<GuideScenarioEditor {...props(scenario())} />);
    await click(screen.getByRole("button", { name: "검사" }));
    const list = screen.getByRole("list", { name: "검사 결과" }).textContent ?? "";
    expect(list).toContain("'순번 도래' 이(가) 꺼져 있습니다");
    expect(list).not.toContain("turn_confirm");
    expect(screen.getByText("⚠ 실제 안내에서 실행 안 됨")).toBeTruthy();
    await click(screen.getByRole("button", { name: /실제 안내에서 실행되지 않습니다/ }));
    expect(screen.getByLabelText("단계 편집").textContent).toContain("그 단계를 켜거나, 다른 단계에서 연결하세요");
  });
});

describe("나가기 · 삭제 · 사용", () => {
  it("저장 안 한 변경이 있으면 목록으로 나갈 때 묻는다", async () => {
    const p = props(scenario());
    render(<GuideScenarioEditor {...p} />);
    await click(screen.getByRole("button", { name: "+ 단계 추가" }));
    await click(screen.getByRole("button", { name: "안내 시나리오 목록으로" }));
    expect(p.onBack).not.toHaveBeenCalled();
    expect(screen.getByText("저장하지 않은 변경이 있습니다")).toBeTruthy();
    await click(screen.getByRole("button", { name: "버리고 나가기" }));
    expect(p.onBack).toHaveBeenCalled();
  });

  it("'아니요' 갈래가 가리키는 단계는 지우지 못하게 막는다", async () => {
    const sc = scenario({
      steps: [
        gstep("greet", { nextStepCode: "s_001" }),
        gstep("s_001", { stepLabel: "준비 확인", waitFor: "user", expectedIntent: "YES_NO", nextStepCode: "s_002", onNoStepCode: "s_002" }),
        gstep("s_002", { stepLabel: "작별", nextStepCode: "finish" }),
        gstep("finish"),
      ],
    });
    render(<GuideScenarioEditor {...props(sc)} />);
    await click(screen.getByRole("button", { name: /^3\s*작별/ }));
    const panel = screen.getByLabelText("단계 편집");
    await click(Array.from(panel.querySelectorAll("button")).find((b) => b.textContent === "삭제") as HTMLElement);
    expect(ctx.showToast.mock.calls.at(-1)?.[0]).toContain("'준비 확인'의 아니요 갈래");
    expect(screen.queryByText(/단계를 지울까요/)).toBeNull();
  });

  it("준비 점검을 통과하지 못한 꺼진 시나리오는 '사용 켜기'가 꺼져 있다", () => {
    render(<GuideScenarioEditor {...props(scenario({ ready: false, checkErrors: [{ stepCode: "greet", field: null, message: "x" }] }))} />);
    expect((screen.getByRole("button", { name: "사용 켜기" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("저장된 단계에 고칠 곳 1건")).toBeTruthy();
  });

  it("상시 대기 작업이 쓰는 켜진 시나리오를 끄려면 확인을 받는다", async () => {
    ctx.api.updateScenario.mockResolvedValue({ status: "ok" });
    render(<GuideScenarioEditor {...props(scenario({ isActive: true, standbyTasks: [{ id: 1, name: "로비 상시 대기" }] }))} />);
    await click(screen.getByRole("button", { name: "사용 끄기" }));
    expect(ctx.api.updateScenario).not.toHaveBeenCalled();
    expect(screen.getByText(/로비 상시 대기/, { selector: "p" })).toBeTruthy();
    await click(screen.getByRole("button", { name: "끄기" }));
    expect(ctx.api.updateScenario).toHaveBeenCalledWith(6, { isActive: false });
  });
});

describe("운영 맵", () => {
  it("설정에서 맵을 바꾸면 이동 장소 연결을 비우고, 저장 때 맵을 단계와 함께 보낸다", async () => {
    ctx.api.saveSteps.mockResolvedValue({ version: 2, warnings: [], stepCount: 3 });
    const sc = scenario({
      steps: [
        gstep("greet", { nextStepCode: "s_001" }),
        gstep("s_001", { stepLabel: "접수 이동", waitFor: "arrival", actionType: "navigate", targetLocationId: 7, nextStepCode: "finish" }),
        gstep("finish"),
      ],
    });
    const p = props(sc, { maps: [{ id: 47, name: "UND_1F", floorId: 1 }, { id: 48, name: "UND_1F_야간", floorId: 1 }] });
    const { container } = render(<GuideScenarioEditor {...p} />);
    await click(screen.getByRole("tab", { name: "설정" }));
    // CustomSelect 를 열어 두 번째 맵을 고른다
    expect(container).toBeTruthy();
    await click(screen.getByText("UND_1F (로봇 현재 맵)"));
    await click(screen.getByRole("option", { name: "UND_1F_야간" }));
    expect(ctx.showToast.mock.calls.at(-1)?.[0]).toContain("이동 장소 연결 1곳을 비웠습니다");
    await click(screen.getByRole("button", { name: "저장" }));
    const body = ctx.api.saveSteps.mock.calls[0][1];
    expect(body.mapId).toBe(48);
    expect(body.steps[1].targetLocationId).toBeNull();
  });
});
