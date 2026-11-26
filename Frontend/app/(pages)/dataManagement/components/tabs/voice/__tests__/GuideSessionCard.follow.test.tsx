import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ctx = vi.hoisted(() => ({
  showToast: vi.fn(),
  api: { currentSession: vi.fn(), followResume: vi.fn(), closeSession: vi.fn(), operatorInput: vi.fn() },
}));

vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("../guideScenarioApi", () => ({ guideScenarioApi: ctx.api }));

import GuideSessionCard from "../GuideSessionCard";

function session(followState: string | null, result = "running") {
  return {
    id: 12,
    scenarioCode: "hospital",
    scenarioName: "병원 안내",
    currentStepCode: "clinic_move",
    destinationName: "안과",
    navTargetName: "안과",
    ticketNo: null,
    counterNo: null,
    startLocationId: null,
    startLocationName: null,
    heard: null,
    followState,
    result,
    startedAt: "2026-10-02T10:00:00",
    step: { stepCode: "clinic_move", sayText: null, waitFor: "arrival", expectedIntent: null },
    operatorInput: null,
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

describe("GuideSessionCard — 동행 확인", () => {
  it("평소 이동 중에는 '안내 중'이고 재개 버튼이 없다", async () => {
    ctx.api.currentSession.mockResolvedValue({ session: session(null), lastEnded: null });
    await renderCard();
    expect(screen.getByText("안내 중")).toBeTruthy();
    expect(screen.queryByText("안내 재개")).toBeNull();
  });

  it("멈춰 기다리는 중이면 상태와 안내 문구를 보여 준다", async () => {
    ctx.api.currentSession.mockResolvedValue({ session: session("waiting"), lastEnded: null });
    await renderCard();
    expect(screen.getByText("방문자 기다리는 중")).toBeTruthy();
    expect(screen.getByText(/로봇이 멈춰 기다립니다/)).toBeTruthy();
  });

  it("직원 호출 상태에서 '안내 재개'를 누르면 follow-resume 을 부른다", async () => {
    ctx.api.currentSession.mockResolvedValue({ session: session("lost", "paused"), lastEnded: null });
    ctx.api.followResume.mockResolvedValue({ session: session("resume", "paused") });
    await renderCard();
    expect(screen.getByText("직원 호출됨")).toBeTruthy();

    fireEvent.click(screen.getByText("안내 재개"));
    await act(async () => {
      await Promise.resolve();
    });
    expect(ctx.api.followResume).toHaveBeenCalledWith(12);
    expect(ctx.showToast).toHaveBeenCalledWith("로봇이 다시 출발합니다", "success");
    expect(screen.getByText("재출발 중")).toBeTruthy();
    expect(screen.queryByText("안내 재개")).toBeNull();
  });
});
