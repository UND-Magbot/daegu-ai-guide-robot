import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ctx = vi.hoisted(() => ({
  showToast: vi.fn(),
  api: {
    currentSession: vi.fn(),
    holdSession: vi.fn(),
    releaseHold: vi.fn(),
    followResume: vi.fn(),
    closeSession: vi.fn(),
    operatorInput: vi.fn(),
  },
}));

vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("../guideScenarioApi", () => ({ guideScenarioApi: ctx.api }));

import GuideSessionCard from "../GuideSessionCard";

function session(holdState: string | null, opts: { result?: string; operatorInput?: string | null } = {}) {
  const operatorInput = opts.operatorInput ?? null;
  return {
    id: 12,
    scenarioCode: "hospital",
    scenarioName: "병원 안내",
    currentStepCode: operatorInput ? "ticket_wait" : "clinic_move",
    destinationName: "안과",
    navTargetName: "안과",
    ticketNo: null,
    counterNo: null,
    startLocationId: null,
    startLocationName: null,
    heard: null,
    followState: null,
    holdState,
    isTest: false,
    testEndStepCode: null,
    result: opts.result ?? "running",
    startedAt: "2026-10-09T10:00:00",
    step: operatorInput
      ? { stepCode: "ticket_wait", sayText: null, waitFor: "operator", expectedIntent: operatorInput }
      : { stepCode: "clinic_move", sayText: null, waitFor: "arrival", expectedIntent: null },
    operatorInput,
  };
}

async function renderCard() {
  render(<GuideSessionCard robotId={10} scenario={null} startPlaces={[]} />);
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  Object.values(ctx.api).forEach((f) => f.mockReset());
  ctx.showToast.mockClear();
});
afterEach(cleanup);

describe("GuideSessionCard — 안내 일시 정지", () => {
  it("안내 중에는 일시 정지 버튼이 있고, 누르면 예약을 보낸다", async () => {
    ctx.api.currentSession.mockResolvedValue({ session: session(null), lastEnded: null });
    ctx.api.holdSession.mockResolvedValue({ session: session("requested") });
    await renderCard();
    expect(screen.getByText("안내 중")).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByText("일시 정지"));
    });
    expect(ctx.api.holdSession).toHaveBeenCalledWith(12);
    expect(screen.getByText(/일시 정지 예정/)).toBeTruthy();
    expect(screen.getByText("일시 정지 취소")).toBeTruthy();
  });

  it("예약 중에는 지금 단계를 이어 가며 취소할 수 있다", async () => {
    ctx.api.currentSession.mockResolvedValue({ session: session("requested"), lastEnded: null });
    ctx.api.releaseHold.mockResolvedValue({ session: session(null) });
    await renderCard();
    expect(screen.getByText("안내 중")).toBeTruthy();
    expect(screen.queryByText("일시 정지")).toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByText("일시 정지 취소"));
    });
    expect(ctx.api.releaseHold).toHaveBeenCalledWith(12);
    expect(ctx.showToast).toHaveBeenCalledWith("일시 정지 예약을 취소했습니다", "success");
  });

  it("멈춘 뒤에는 '일시 정지 중'과 재개 버튼, 자동 종료 안내를 보여 준다", async () => {
    ctx.api.currentSession.mockResolvedValue({ session: session("holding", { result: "paused" }), lastEnded: null });
    ctx.api.releaseHold.mockResolvedValue({ session: session(null) });
    await renderCard();
    expect(screen.getByText("일시 정지 중")).toBeTruthy();
    expect(screen.getByText(/20분 동안/)).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByText("안내 재개"));
    });
    expect(ctx.api.releaseHold).toHaveBeenCalledWith(12);
    expect(ctx.showToast).toHaveBeenCalledWith("안내를 재개합니다", "success");
  });

  it("운영자 입력 대기 중에도 일시 정지를 예약할 수 있다", async () => {
    ctx.api.currentSession.mockResolvedValue({
      session: session(null, { result: "paused", operatorInput: "TICKET" }),
      lastEnded: null,
    });
    await renderCard();
    expect(screen.getByPlaceholderText("대기번호")).toBeTruthy();
    expect(screen.getByText("일시 정지")).toBeTruthy();
  });
});
