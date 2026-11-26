import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AlertMockData } from "@/app/types";

// ── 가짜 컨텍스트·API ────────────────────────────────
const ctx = vi.hoisted(() => ({
  alerts: [] as AlertMockData[],
  refresh: vi.fn(async () => {}),
  showToast: vi.fn(),
  markAlertRead: vi.fn(async () => {}),
  live: { active: false } as { active: boolean; scheduleId?: number },
}));

vi.mock("@/app/context/AlertContext", () => ({
  useAlertContext: () => ({ unreadAlerts: ctx.alerts, refresh: ctx.refresh }),
}));
vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("@/app/lib/alertData", () => ({ markAlertRead: ctx.markAlertRead }));
vi.mock("@/app/lib/api", () => ({
  apiFetch: vi.fn(async () => ({ ok: true, json: async () => ctx.live })),
}));

import StandbyMapHoldAlertWatcher, {
  parseMapHold,
  pickMapHoldAlert,
  shiftStarted,
} from "@/app/components/common/StandbyMapHoldAlertWatcher";

// 백엔드 scheduler/loop._note_map_hold 가 남기는 형식 그대로
const DETAIL = JSON.stringify({
  scheduleId: 182,
  workName: "로비 상시 대기",
  place: "3F 로봇대기장소",
  placeFloor: "3F",
  robotFloor: "1F",
});
const MESSAGE =
  "로비 상시 대기 시작 보류 — 대기 장소 '3F 로봇대기장소'은(는) 3F에 있는데 로봇은 1F에 있습니다. 로봇을 그 층으로 옮기면 시작합니다";
const NOW = new Date("2026-10-23T15:25:00");

function holdAlert(id: number, detail: string | null = DETAIL, date = "2026-10-23 15:20", action = "standby_map_mismatch"): AlertMockData {
  return {
    id,
    type: "Schedule",
    status: "error",
    content: "상시 대기 시작 보류 — 대기 장소가 다른 층",
    date,
    robotName: "R296",
    isRead: false,
    log: {
      Category: "schedule",
      Action: action,
      Message: MESSAGE,
      Detail: detail,
      RobotName: "R296",
      CreatedAt: `${date}:05`,
    },
  };
}

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  ctx.alerts = [];
  ctx.live = { active: false };
  ctx.refresh.mockClear();
  ctx.showToast.mockClear();
  ctx.markAlertRead.mockClear();
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("알림 읽기", () => {
  it("상세 JSON 에서 작업·대기 장소·두 층을 꺼낸다", () => {
    expect(parseMapHold(DETAIL)).toEqual({
      scheduleId: 182,
      workName: "로비 상시 대기",
      place: "3F 로봇대기장소",
      placeFloor: "3F",
      robotFloor: "1F",
    });
  });

  it("상세가 없거나 JSON 이 아니면 빈 값(예전 알림 'scheduleId=182')", () => {
    const empty = { scheduleId: null, workName: null, place: null, placeFloor: null, robotFloor: null };
    expect(parseMapHold(null)).toEqual(empty);
    expect(parseMapHold("scheduleId=182")).toEqual(empty);
  });

  it("방금(30분 안) 생긴, 안 본 보류 알림만 — 다른 알림은 무시", () => {
    const alerts = [
      holdAlert(1, DETAIL, "2026-10-23 15:00"),
      holdAlert(2, DETAIL, "2026-10-23 15:20"),
      holdAlert(3, DETAIL, "2026-10-23 14:40"),
      holdAlert(4, DETAIL, "2026-10-23 15:22", "guide_battery_aborted"),
    ];
    expect(pickMapHoldAlert(alerts, new Set(), NOW.getTime())?.id).toBe(2);
    expect(pickMapHoldAlert(alerts, new Set([2]), NOW.getTime())?.id).toBe(1);
    expect(pickMapHoldAlert(alerts, new Set([1, 2]), NOW.getTime())).toBeNull();
  });

  it("그 근무가 실시간 상태에 잡혀야 시작으로 본다", () => {
    expect(shiftStarted({ active: true, scheduleId: 182 }, 182)).toBe(true);
    expect(shiftStarted({ active: true, scheduleId: 7 }, 182)).toBe(false);
    expect(shiftStarted({ active: false }, 182)).toBe(false);
    expect(shiftStarted(null, 182)).toBe(false);
  });
});

describe("알림창", () => {
  it("작업·로봇·대기 장소(층)·로봇 위치를 칸별로 보여 주고, 버튼은 한 줄에 나란히", async () => {
    ctx.alerts = [holdAlert(5)];
    render(<StandbyMapHoldAlertWatcher />);
    await flush();
    expect(screen.getByText("상시 대기 시작 보류")).toBeTruthy();
    expect(screen.getByText("로비 상시 대기")).toBeTruthy();
    expect(screen.getByText("R296")).toBeTruthy();
    expect(screen.getByText("3F 로봇대기장소 (3F)")).toBeTruthy();
    expect(screen.getByText("1F")).toBeTruthy();
    const ok = screen.getByRole("button", { name: "확인" });
    const later = screen.getByRole("button", { name: "나중에" });
    expect(ok.parentElement).toBe(later.parentElement);
  });

  it("상세를 못 읽는 예전 알림은 본문을 그대로 보여 준다", async () => {
    ctx.alerts = [holdAlert(5, "scheduleId=182")];
    render(<StandbyMapHoldAlertWatcher />);
    await flush();
    expect(screen.getByText(MESSAGE)).toBeTruthy();
  });

  it("지난 알림(30분 넘음)은 페이지를 열어도 띄우지 않는다", async () => {
    ctx.alerts = [holdAlert(5, DETAIL, "2026-10-23 14:40")];
    render(<StandbyMapHoldAlertWatcher />);
    await flush();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("'확인'은 읽음 처리하고 닫는다", async () => {
    ctx.alerts = [holdAlert(5)];
    render(<StandbyMapHoldAlertWatcher />);
    await flush();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "확인" }));
    });
    await flush();
    expect(ctx.markAlertRead).toHaveBeenCalledWith(5);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("'나중에'는 읽음 처리 없이 닫고 다시 띄우지 않는다", async () => {
    ctx.alerts = [holdAlert(5)];
    const { rerender } = render(<StandbyMapHoldAlertWatcher />);
    await flush();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "나중에" }));
    });
    rerender(<StandbyMapHoldAlertWatcher />);
    await flush();
    expect(ctx.markAlertRead).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("열려 있는 동안 로봇을 옮겨 근무가 시작되면 스스로 닫고 알린다", async () => {
    ctx.alerts = [holdAlert(5)];
    ctx.live = { active: true, scheduleId: 182 };
    render(<StandbyMapHoldAlertWatcher />);
    await flush();
    await flush();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(ctx.markAlertRead).toHaveBeenCalledWith(5);
    expect(ctx.showToast).toHaveBeenCalledWith("로비 상시 대기 근무를 시작했습니다", "success");
  });

  it("다른 근무가 돌고 있으면 닫지 않는다", async () => {
    ctx.alerts = [holdAlert(5)];
    ctx.live = { active: true, scheduleId: 7 };
    render(<StandbyMapHoldAlertWatcher />);
    await flush();
    await flush();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
});
