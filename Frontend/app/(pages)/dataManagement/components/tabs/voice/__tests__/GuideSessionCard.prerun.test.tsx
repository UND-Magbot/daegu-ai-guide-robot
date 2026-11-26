import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ctx = vi.hoisted(() => ({
  showToast: vi.fn(),
  calls: [] as string[],
  api: {
    currentSession: vi.fn(),
    openSession: vi.fn(),
    prerunCurrent: vi.fn(),
    prerunPlan: vi.fn(),
    prerunStart: vi.fn(),
    prerunResume: vi.fn(),
    prerunStop: vi.fn(),
    prerunDismiss: vi.fn(),
  },
}));

vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("../guideScenarioApi", () => ({ guideScenarioApi: ctx.api }));

import GuideSessionCard from "../GuideSessionCard";

const SCENARIO = {
  id: 1,
  code: "hospital",
  displayName: "병원 안내",
  floorId: 1,
  mapId: 47,
  startLocationId: 1,
  startLocationName: "로봇대기장소",
  guideSpeed: "normal",
  isActive: true,
  steps: [],
} as never;
const PLACES = [{ id: 1, label: "로봇대기장소" }];
const LEGS = ["로봇대기장소", "원무과 접수", "진료대기장소", "간호실데스크", "원무과수납", "로봇대기장소"];

function prerun(status: string, extra: Record<string, unknown> = {}) {
  const leg = (extra.leg as number) ?? 2;
  return {
    status,
    scenarioCode: "hospital",
    scenarioName: "병원 안내",
    startLocationId: 1,
    legs: LEGS.map((name, i) => ({
      id: i + 1,
      name,
      role: i === 3 ? "destination" : "step",
      step: null,
      status: status === "done" || i < leg ? "done" : i === leg ? (status === "running" ? "current" : "error") : "pending",
    })),
    leg,
    doneLegs: status === "done" ? LEGS.length : leg,
    total: LEGS.length,
    reason: null,
    locLost: 0,
    locWaitLeft: null,
    elapsedSec: 95,
    startedAt: "2026-10-22T14:00:00",
    ...extra,
  };
}

const GUIDE = {
  id: 31,
  scenarioCode: "hospital",
  scenarioName: "병원 안내",
  currentStepCode: "call_move",
  destinationName: null,
  navTargetName: null,
  ticketNo: null,
  counterNo: null,
  startLocationId: 1,
  startLocationName: "로봇대기장소",
  heard: null,
  followState: null,
  holdState: null,
  isTest: false,
  testEndStepCode: null,
  result: "running",
  startedAt: "2026-10-22T14:10:00",
  step: null,
  operatorInput: null,
};

async function renderCard() {
  render(<GuideSessionCard robotId={10} scenario={SCENARIO} startPlaces={PLACES} />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function click(text: string | RegExp) {
  await act(async () => {
    fireEvent.click(screen.getByText(text));
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  Object.values(ctx.api).forEach((f) => f.mockReset());
  ctx.showToast.mockClear();
  ctx.calls.length = 0;
  ctx.api.currentSession.mockResolvedValue({ session: null, lastEnded: null });
  ctx.api.prerunCurrent.mockResolvedValue({ prerun: null });
});
afterEach(cleanup);

describe("GuideSessionCard — 안내 사전 주행", () => {
  it("시작 창에서 '고속 1바퀴 후 안내 시작'을 고르면 동선을 보여 주고, 안내 대신 사전 주행을 시작한다", async () => {
    ctx.api.prerunPlan.mockResolvedValue({
      legs: LEGS.map((name, i) => ({ id: i + 1, name, role: "step", step: null })),
      warnings: [],
      destinations: 1,
    });
    ctx.api.prerunStart.mockResolvedValue({ prerun: prerun("running", { leg: 1 }) });
    await renderCard();

    await click("병원 안내 시작");
    await click("없음");
    await click("고속 1바퀴 후 안내 시작");

    expect(ctx.api.prerunPlan).toHaveBeenCalledWith(10, "hospital", 1);
    expect(screen.getByText(LEGS.join(" → "))).toBeTruthy();

    await click("사전 주행 시작");
    expect(ctx.api.prerunStart).toHaveBeenCalledWith(10, "hospital", 1);
    expect(ctx.api.openSession).not.toHaveBeenCalled();
    expect(screen.getByText("사전 주행 중")).toBeTruthy();
    expect(screen.getByText("사전 주행 중지")).toBeTruthy();
  });

  it("사전 주행을 고르지 않으면 지금처럼 바로 안내를 연다", async () => {
    ctx.api.openSession.mockResolvedValue({ session: GUIDE });
    await renderCard();
    await click("병원 안내 시작");
    await click("로봇대기장소(으)로 이동 후 시작");
    expect(ctx.api.openSession).toHaveBeenCalledWith(10, "hospital", 1);
    expect(ctx.api.prerunStart).not.toHaveBeenCalled();
  });

  it("완료되면 저절로 시작하지 않고, '안내 시작'을 눌러야 안내를 연 뒤 사전 주행 기록을 닫는다", async () => {
    ctx.api.prerunCurrent.mockResolvedValue({ prerun: prerun("done") });
    ctx.api.openSession.mockImplementation(async () => {
      ctx.calls.push("open");
      return { session: GUIDE };
    });
    ctx.api.prerunDismiss.mockImplementation(async () => {
      ctx.calls.push("dismiss");
      return { prerun: null };
    });
    await renderCard();

    expect(screen.getByText("사전 주행 완료")).toBeTruthy();
    expect(ctx.api.openSession).not.toHaveBeenCalled();

    await click("안내 시작");
    expect(ctx.api.openSession).toHaveBeenCalledWith(10, "hospital", 1);
    expect(ctx.calls).toEqual(["open", "dismiss"]);
  });

  it("안내 열기가 거절되면 사전 주행 기록을 닫지 않는다", async () => {
    ctx.api.prerunCurrent.mockResolvedValue({ prerun: prerun("done") });
    ctx.api.openSession.mockRejectedValue(new Error("로봇이 다른 맵에 있습니다"));
    await renderCard();
    await click("안내 시작");
    expect(ctx.api.prerunDismiss).not.toHaveBeenCalled();
    expect(ctx.showToast).toHaveBeenCalledWith("로봇이 다른 맵에 있습니다", "error");
  });

  it("멈추면 사유와 버튼 4개를 보여 주고, '실패 지점부터 다시'는 멈춘 곳부터 다시 보낸다", async () => {
    ctx.api.prerunCurrent.mockResolvedValue({
      prerun: prerun("failed", { reason: "'간호실데스크' — NAV hang — 재전송 한도 초과", leg: 3 }),
    });
    ctx.api.prerunResume.mockResolvedValue({ prerun: prerun("running", { leg: 3 }) });
    await renderCard();

    expect(screen.getByText("'간호실데스크' — NAV hang — 재전송 한도 초과")).toBeTruthy();
    for (const label of ["실패 지점부터 다시", "처음부터 다시", "사전 주행 건너뛰고 안내 시작", "취소"]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    await click("실패 지점부터 다시");
    expect(ctx.api.prerunResume).toHaveBeenCalledWith(10, true);
  });

  it("측위 회복 대기 중에는 남은 초와 위치 재조정 안내를 보여 준다", async () => {
    ctx.api.prerunCurrent.mockResolvedValue({ prerun: prerun("loc_wait", { locWaitLeft: 18 }) });
    await renderCard();
    expect(screen.getByText("측위 회복 대기 18초")).toBeTruthy();
    expect(screen.getByText(/위치 재조정으로 위치를 맞추세요/)).toBeTruthy();
    expect(screen.getByText("사전 주행 중지")).toBeTruthy();
  });

  it("사전 주행 조회가 실패해도 안내 세션 표시는 그대로다", async () => {
    ctx.api.prerunCurrent.mockRejectedValue(new Error("404"));
    ctx.api.currentSession.mockResolvedValue({ session: GUIDE, lastEnded: null });
    await renderCard();
    expect(screen.getByText("호출 위치로 이동 중")).toBeTruthy();
  });
});
