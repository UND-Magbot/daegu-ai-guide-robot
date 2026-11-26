// 장애물 2분 직원 호출 확인창(10-30) — 창이 열리고, '지금 앞' 을 보이고, 앞이 막혀 있으면 '다시 출발' 을 막는다.
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AlertMockData } from "@/app/types";

const ctx = vi.hoisted(() => ({
  alerts: [] as AlertMockData[],
  refresh: vi.fn(async () => {}),
  showToast: vi.fn(),
  markAlertRead: vi.fn(async () => {}),
  hold: null as unknown,
}));
vi.mock("@/app/context/AlertContext", () => ({
  useAlertContext: () => ({ unreadAlerts: ctx.alerts, refresh: ctx.refresh }),
}));
vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("@/app/lib/alertData", () => ({ markAlertRead: ctx.markAlertRead }));
vi.mock("@/app/lib/api", () => ({
  apiFetch: vi.fn(async () => ({ ok: true, json: async () => ({ robotName: "R669", hold: ctx.hold }) })),
}));

import NavHoldAlertWatcher from "@/app/components/common/NavHoldAlertWatcher";

const OB = {
  kind: "guide", wpNo: 1, wpName: "간호실데스크", since: 0, pos: [1.0, 2.0], reason: "장애물 정지",
  heldSec: 125, autoActionInSec: null, actions: ["retry", "abort"], front: "blocked",
};
const alert = (id: number, action: string): AlertMockData =>
  ({ id, type: "Schedule", status: "error", content: "", date: "2026-10-30 10:02", robotName: "R669", isRead: false,
     log: { Action: action, Message: "" } }) as unknown as AlertMockData;
const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};
const retryBtn = () => screen.getByText("다시 출발").closest("button") as HTMLButtonElement;

beforeEach(() => {
  vi.useFakeTimers();
  ctx.alerts = [];
  ctx.hold = OB;
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("장애물 직원 호출 확인창", () => {
  it("P1 2분 직원 호출 알림 → 확인창이 열리고 '지금 앞: 막혀 있음', '다시 출발' 은 눌리지 않는다", async () => {
    ctx.alerts = [alert(11, "guide_obstacle_staff")];
    render(<NavHoldAlertWatcher />);
    await flush();
    expect(screen.queryByText("안내 이동 막힘")).not.toBeNull();
    expect(screen.queryByText("막혀 있음")).not.toBeNull();
    expect(retryBtn().disabled).toBe(true);
    expect((screen.getByText("안내 종료").closest("button") as HTMLButtonElement).disabled).toBe(false);
  });

  it("P2 앞이 비면(3초 뒤 다시 읽음) '비어 있음' 으로 바뀌고 '다시 출발' 이 눌린다 — 창은 그대로(자동 출발 없음)", async () => {
    ctx.alerts = [alert(12, "guide_obstacle_staff")];
    render(<NavHoldAlertWatcher />);
    await flush();
    ctx.hold = { ...OB, front: "clear" };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3100);
    });
    await flush();
    expect(screen.queryByText("비어 있음")).not.toBeNull();
    expect(retryBtn().disabled).toBe(false);
  });

  it("P3 (회귀) 60초 '안내 이동 막힘'(앞 정보 없음)은 지금처럼 '다시 출발' 이 눌리고 앞 줄이 없다", async () => {
    ctx.hold = { ...OB, reason: "60초 제자리", front: undefined };
    ctx.alerts = [alert(13, "guide_nav_blocked")];
    render(<NavHoldAlertWatcher />);
    await flush();
    expect(screen.queryByText("지금 앞")).toBeNull();
    expect(retryBtn().disabled).toBe(false);
  });
});
