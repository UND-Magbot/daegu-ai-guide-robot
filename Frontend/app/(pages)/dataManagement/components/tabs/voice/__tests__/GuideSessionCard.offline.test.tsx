import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// 메모 11-03: 로봇이 오프라인이면 안내 시나리오의 안내 시작 버튼을 막는다
const ctx = vi.hoisted(() => ({
  showToast: vi.fn(),
  status: { robots: [] as unknown[], loaded: false, serverStatus: "ok" as "ok" | "unreachable" },
  api: { currentSession: vi.fn(), prerunCurrent: vi.fn() },
}));

vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("@/app/context/RobotStatusContext", () => ({ useRobotStatusContext: () => ctx.status }));
vi.mock("../guideScenarioApi", () => ({ guideScenarioApi: ctx.api }));

import GuideSessionCard from "../GuideSessionCard";

const SCENARIO = {
  id: 1,
  code: "hospital",
  displayName: "병원 안내",
  floorId: 1,
  mapId: 47,
  startLocationId: 1,
  isActive: true,
  steps: [],
} as never;

async function renderCard() {
  render(<GuideSessionCard robotId={13} scenario={SCENARIO} startPlaces={[{ id: 1, label: "로봇대기장소" }]} />);
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

const startBtn = () => screen.getByText("병원 안내 시작").closest("button") as HTMLButtonElement;

beforeEach(() => {
  ctx.api.currentSession.mockResolvedValue({ session: null, lastEnded: null });
  ctx.api.prerunCurrent.mockResolvedValue({ prerun: null });
});
afterEach(cleanup);

describe("GuideSessionCard — 로봇 연결 상태", () => {
  it("로봇이 Online 이면 안내 시작을 누를 수 있다", async () => {
    ctx.status = { robots: [{ id: 13, network: "Online" }], loaded: true, serverStatus: "ok" };
    await renderCard();
    expect(startBtn().disabled).toBe(false);
    expect(screen.queryByText(/로봇이 오프라인입니다/)).toBeNull();
  });

  it("로봇이 Offline 이면 안내 시작이 막히고 이유를 보여 준다", async () => {
    ctx.status = { robots: [{ id: 13, network: "Offline" }], loaded: true, serverStatus: "ok" };
    await renderCard();
    expect(startBtn().disabled).toBe(true);
    expect(startBtn().title).toBe("로봇이 오프라인입니다");
    expect(screen.getByText(/로봇이 오프라인입니다 — 연결된 뒤/)).toBeTruthy();
  });

  it("관제 서버 응답이 없으면(상태 확인 불가) 막는다 — 명령이 나가지 않는다", async () => {
    ctx.status = { robots: [{ id: 13, network: "Online" }], loaded: true, serverStatus: "unreachable" };
    await renderCard();
    expect(startBtn().disabled).toBe(true);
  });

  it("상태 목록에 없는 로봇도 막는다", async () => {
    ctx.status = { robots: [{ id: 10, network: "Online" }], loaded: true, serverStatus: "ok" };
    await renderCard();
    expect(startBtn().disabled).toBe(true);
  });

  it("상태를 아직 못 받았으면 막지 않는다(판단 근거 없음)", async () => {
    ctx.status = { robots: [], loaded: false, serverStatus: "ok" };
    await renderCard();
    expect(startBtn().disabled).toBe(false);
  });
});
