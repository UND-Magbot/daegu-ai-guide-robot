import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ctx = vi.hoisted(() => ({
  showToast: vi.fn(),
  api: { currentSession: vi.fn(), closeSession: vi.fn(), operatorInput: vi.fn() },
}));

vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("../guideScenarioApi", () => ({ guideScenarioApi: ctx.api }));

import GuideSessionCard from "../GuideSessionCard";
import { fixedInputFor } from "../guideFixedInput";

function session(operatorInput: "TICKET" | "COUNTER") {
  const stepCode = operatorInput === "TICKET" ? "ticket_wait" : "seat_wait";
  return {
    id: 12,
    scenarioCode: "public_office",
    scenarioName: "관공서 안내",
    currentStepCode: stepCode,
    destinationName: "기초연금",
    navTargetName: null,
    ticketNo: operatorInput === "COUNTER" ? "10" : null,
    counterNo: null,
    startLocationId: null,
    startLocationName: null,
    heard: null,
    followState: null,
    holdState: null,
    isTest: false,
    testEndStepCode: null,
    result: "running",
    startedAt: "2026-10-28T11:38:37",
    step: { stepCode, sayText: null, waitFor: "operator", expectedIntent: operatorInput },
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

describe("GuideSessionCard — 대기번호·창구 고정값", () => {
  it("대기번호 입력 단계면 고정값 10 이 채워져 있고 그대로 보낼 수 있다", async () => {
    ctx.api.currentSession.mockResolvedValue({ session: session("TICKET"), lastEnded: null });
    ctx.api.operatorInput.mockResolvedValue({ session: session("COUNTER") });
    await renderCard();
    const input = screen.getByPlaceholderText("대기번호") as HTMLInputElement;
    expect(input.value).toBe("10");

    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(ctx.api.operatorInput).toHaveBeenCalledWith(12, { ticketNo: "10" });
  });

  it("창구 입력 단계로 넘어가면 창구 고정값 1 로 다시 채운다(대기번호가 남지 않음)", async () => {
    ctx.api.currentSession.mockResolvedValue({ session: session("COUNTER"), lastEnded: null });
    await renderCard();
    expect((screen.getByPlaceholderText("창구 번호") as HTMLInputElement).value).toBe("1");
  });

  it("고친 값을 보낼 수 있다", async () => {
    ctx.api.currentSession.mockResolvedValue({ session: session("TICKET"), lastEnded: null });
    ctx.api.operatorInput.mockResolvedValue({ session: session("COUNTER") });
    await renderCard();
    const input = screen.getByPlaceholderText("대기번호");
    fireEvent.change(input, { target: { value: "27" } });
    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(ctx.api.operatorInput).toHaveBeenCalledWith(12, { ticketNo: "27" });
  });

  it("입력 단계가 아니면 빈칸", () => {
    expect(fixedInputFor("RESUME")).toBe("");
    expect(fixedInputFor(null)).toBe("");
  });
});
