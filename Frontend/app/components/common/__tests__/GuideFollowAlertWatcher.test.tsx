import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AlertMockData } from "@/app/types";

// ── 가짜 컨텍스트·API ────────────────────────────────
const ctx = vi.hoisted(() => ({
  alerts: [] as AlertMockData[],
  refresh: vi.fn(async () => {}),
  showToast: vi.fn(),
  markAlertRead: vi.fn(async () => {}),
  api: {
    currentSession: vi.fn(),
    followResume: vi.fn(),
    closeSession: vi.fn(),
  },
}));

vi.mock("@/app/context/AlertContext", () => ({
  useAlertContext: () => ({ unreadAlerts: ctx.alerts, refresh: ctx.refresh }),
}));
vi.mock("@/app/context/RobotStatusContext", () => ({
  useRobotStatusContext: () => ({ robots: [{ id: 10, no: "R296" }] }),
}));
vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("@/app/lib/alertData", () => ({ markAlertRead: ctx.markAlertRead }));
vi.mock("@/app/(pages)/dataManagement/components/tabs/voice/guideScenarioApi", () => ({
  guideScenarioApi: ctx.api,
}));

import GuideFollowAlertWatcher, {
  parseSessionId,
  pickFollowAlert,
  stillWaiting,
} from "@/app/components/common/GuideFollowAlertWatcher";

function followAlert(id: number, sessionId: number, date = "2026-10-02 10:00"): AlertMockData {
  return {
    id,
    type: "Schedule",
    status: "error",
    content: "안내 동행 이탈 — 운영자 확인 필요",
    date,
    robotName: "R296",
    isRead: false,
    log: {
      Category: "schedule",
      Action: "guide_follow_lost",
      Message: "R296 안내 중 방문자가 따라오지 않습니다 (안과 가는 길)",
      Detail: `session=${sessionId} step=clinic_move target=7`,
      RobotName: "R296",
      CreatedAt: "2026-10-02T10:00:00",
    },
  };
}

const lostSession = (id: number, followState: string | null = "lost") => ({
  session: { id, followState, result: "paused" },
  lastEnded: null,
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  sessionStorage.clear();
  ctx.alerts = [];
  Object.values(ctx.api).forEach((f) => f.mockReset());
  ctx.markAlertRead.mockClear();
  ctx.showToast.mockClear();
  ctx.api.currentSession.mockResolvedValue(lostSession(12));
  ctx.api.followResume.mockResolvedValue({ session: null });
  ctx.api.closeSession.mockResolvedValue({ session: null });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("동행 이탈 알림 판정", () => {
  it("상세에서 세션 id 를 꺼낸다", () => {
    expect(parseSessionId("session=12 step=clinic_move target=7")).toBe(12);
    expect(parseSessionId(null)).toBeNull();
    expect(parseSessionId("step=x")).toBeNull();
  });

  it("다른 종류·이미 본 알림은 고르지 않고, 가장 최근 것을 고른다", () => {
    const other = { ...followAlert(1, 5), log: { ...followAlert(1, 5).log!, Action: "nav_error" } };
    const older = followAlert(2, 6, "2026-10-02 09:00");
    const newer = followAlert(3, 7, "2026-10-02 11:00");
    expect(pickFollowAlert([other, older, newer], new Set())?.id).toBe(3);
    expect(pickFollowAlert([other, older, newer], new Set([3]))?.id).toBe(2);
    expect(pickFollowAlert([other], new Set())).toBeNull();
  });

  it("세션이 여전히 기다리는 중인지 판정한다", () => {
    expect(stillWaiting({ id: 12, followState: "lost" }, 12)).toBe(true);
    expect(stillWaiting({ id: 12, followState: "waiting" }, 12)).toBe(true);
    expect(stillWaiting({ id: 12, followState: null }, 12)).toBe(false);      // 방문자가 '네' 해서 출발
    expect(stillWaiting({ id: 12, followState: "resume" }, 12)).toBe(false);  // 다른 관제가 재개
    expect(stillWaiting({ id: 13, followState: "lost" }, 12)).toBe(false);    // 다른 세션
    expect(stillWaiting(null, 12)).toBe(false);                               // 종료됨
  });
});

describe("GuideFollowAlertWatcher", () => {
  it("알림이 없으면 아무것도 띄우지 않는다", () => {
    render(<GuideFollowAlertWatcher />);
    expect(screen.queryByText("안내 동행 이탈")).toBeNull();
  });

  it("알림이 오면 모달을 띄우고, '안내 재개'로 follow-resume 을 호출한 뒤 닫는다", async () => {
    ctx.alerts = [followAlert(101, 12)];
    render(<GuideFollowAlertWatcher />);
    await flush();
    expect(screen.getByText("안내 동행 이탈")).toBeTruthy();
    expect(screen.getByText("R296 안내 중 방문자가 따라오지 않습니다 (안과 가는 길)")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "안내 재개" }));
    await flush();
    expect(ctx.api.followResume).toHaveBeenCalledWith(12);
    expect(ctx.markAlertRead).toHaveBeenCalledWith(101);
    expect(ctx.showToast).toHaveBeenCalledWith("로봇이 다시 출발합니다", "success");
    expect(screen.queryByText("안내 동행 이탈")).toBeNull();
  });

  it("'안내 종료'는 세션을 닫는다", async () => {
    ctx.alerts = [followAlert(101, 12)];
    render(<GuideFollowAlertWatcher />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "안내 종료" }));
    await flush();
    expect(ctx.api.closeSession).toHaveBeenCalledWith(12, "동행 이탈 — 운영자 종료");
    expect(screen.queryByText("안내 동행 이탈")).toBeNull();
  });

  it("'나중에'는 API 를 부르지 않고 닫으며, 같은 알림으로 다시 뜨지 않는다", async () => {
    ctx.alerts = [followAlert(101, 12)];
    const { rerender } = render(<GuideFollowAlertWatcher />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "나중에" }));
    await flush();
    expect(ctx.api.followResume).not.toHaveBeenCalled();
    expect(ctx.markAlertRead).not.toHaveBeenCalled();
    ctx.alerts = [...ctx.alerts];
    rerender(<GuideFollowAlertWatcher />);
    await flush();
    expect(screen.queryByText("안내 동행 이탈")).toBeNull();
  });

  it("방문자가 '네' 해서 로봇이 먼저 출발하면 모달이 스스로 닫힌다", async () => {
    ctx.alerts = [followAlert(101, 12)];
    render(<GuideFollowAlertWatcher />);
    await flush();
    expect(screen.getByText("안내 동행 이탈")).toBeTruthy();

    ctx.api.currentSession.mockResolvedValue(lostSession(12, null));
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    await flush();
    expect(screen.queryByText("안내 동행 이탈")).toBeNull();
    expect(ctx.markAlertRead).toHaveBeenCalledWith(101);
  });

  it("재개 요청이 실패하면 오류를 알리고 모달을 유지한다", async () => {
    ctx.alerts = [followAlert(101, 12)];
    ctx.api.followResume.mockRejectedValue(new Error("동행 확인으로 멈춘 안내가 아닙니다."));
    render(<GuideFollowAlertWatcher />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "안내 재개" }));
    await flush();
    expect(ctx.showToast).toHaveBeenCalledWith("동행 확인으로 멈춘 안내가 아닙니다.", "error");
    expect(screen.getByText("안내 동행 이탈")).toBeTruthy();
  });
});
