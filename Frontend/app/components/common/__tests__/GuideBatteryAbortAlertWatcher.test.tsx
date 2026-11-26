import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AlertMockData } from "@/app/types";

// ── 가짜 컨텍스트·API ────────────────────────────────
const ctx = vi.hoisted(() => ({
  alerts: [] as AlertMockData[],
  refresh: vi.fn(async () => {}),
  markAlertRead: vi.fn(async () => {}),
}));

vi.mock("@/app/context/AlertContext", () => ({
  useAlertContext: () => ({ unreadAlerts: ctx.alerts, refresh: ctx.refresh }),
}));
vi.mock("@/app/lib/alertData", () => ({ markAlertRead: ctx.markAlertRead }));

import GuideBatteryAbortAlertWatcher, {
  isFresh,
  parseBatteryAbort,
  pickBatteryAbortAlert,
} from "@/app/components/common/GuideBatteryAbortAlertWatcher";

// 백엔드 scheduler/loop.on_scenario_closed 가 남기는 문구 그대로
const MESSAGE = "R296 배터리 15% — 최소 기준 이하라 안내를 멈추고 충전소로 갑니다. 방문객이 남아 있을 수 있습니다";
const detail = (where = "(12.3, -4.5)", dest = "내과 진료실", near = "접수 창구에서 1.2m") =>
  `session=77 멈춘 곳=${where} 가까운 곳=${near} step=clinic_move 못 간 목적지=${dest}`;
// 10-23 이전 알림(가까운 곳 없음)
const oldDetail = "session=77 멈춘 곳=(12.3, -4.5) step=clinic_move 못 간 목적지=내과";
const NOW = new Date("2026-10-23T10:05:00");

function abortAlert(id: number, d = detail(), date = "2026-10-23 10:00", action = "guide_battery_aborted"): AlertMockData {
  return {
    id,
    type: "Schedule",
    status: "error",
    content: "배터리 부족 — 안내 중단, 충전소 이동",
    date,
    robotName: "R296",
    isRead: false,
    log: {
      Category: "schedule",
      Action: action,
      Message: MESSAGE,
      Detail: d,
      RobotName: "R296",
      CreatedAt: `${date}:00`,       // 서버 형식 "YYYY-MM-DD HH:MM:SS"
    },
  };
}

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
  });
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  ctx.alerts = [];
  ctx.refresh.mockClear();
  ctx.markAlertRead.mockClear();
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("알림 읽기", () => {
  it("배터리·멈춘 곳·가까운 장소·못 간 목적지(띄어쓰기 포함)를 꺼낸다", () => {
    expect(parseBatteryAbort(MESSAGE, detail())).toEqual({
      battery: 15,
      stoppedAt: "(12.3, -4.5)",
      nearPlace: "접수 창구에서 1.2m",
      destination: "내과 진료실",
    });
  });

  it("위치·가까운 장소를 모르거나 목적지를 정하기 전이면 비운다", () => {
    expect(parseBatteryAbort(MESSAGE, detail("알 수 없음", "-", "-"))).toEqual({
      battery: 15,
      stoppedAt: null,
      nearPlace: null,
      destination: null,
    });
  });

  it("가까운 곳이 없는 예전 알림도 읽는다", () => {
    expect(parseBatteryAbort(MESSAGE, oldDetail)).toEqual({
      battery: 15,
      stoppedAt: "(12.3, -4.5)",
      nearPlace: null,
      destination: "내과",
    });
  });

  it("상세가 없으면 배터리만", () => {
    expect(parseBatteryAbort(MESSAGE, null)).toEqual({
      battery: 15, stoppedAt: null, nearPlace: null, destination: null,
    });
  });

  it("방금(30분 안) 생긴 알림만 창으로 — 서버 시각 형식 그대로", () => {
    expect(isFresh(abortAlert(1, detail(), "2026-10-23 09:36"), NOW.getTime())).toBe(true);
    expect(isFresh(abortAlert(1, detail(), "2026-10-23 09:34"), NOW.getTime())).toBe(false);
    expect(isFresh(abortAlert(1, detail(), "2026-10-22 10:00"), NOW.getTime())).toBe(false);
  });

  it("가장 최근의 아직 안 본 중단 알림만 고른다 — 다른 알림은 무시", () => {
    const alerts = [
      abortAlert(1, detail(), "2026-10-23 09:50"),
      abortAlert(2, detail(), "2026-10-23 10:00"),
      abortAlert(3, detail(), "2026-10-23 11:00", "battery_resume_ask"),
    ];
    expect(pickBatteryAbortAlert(alerts, new Set())?.id).toBe(2);
    expect(pickBatteryAbortAlert(alerts, new Set([2]))?.id).toBe(1);
    expect(pickBatteryAbortAlert(alerts, new Set([1, 2]))).toBeNull();
  });
});

describe("알림창", () => {
  it("중단 알림이 오면 멈춘 곳·못 간 목적지를 보여 준다", async () => {
    ctx.alerts = [abortAlert(5)];
    render(<GuideBatteryAbortAlertWatcher />);
    await flush();
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText("배터리 부족 — 안내 중단")).toBeTruthy();
    expect(screen.getByText("15%")).toBeTruthy();
    expect(screen.getByText("접수 창구에서 1.2m")).toBeTruthy();
    expect(screen.getByText("지도 좌표 (12.3, -4.5)")).toBeTruthy();
    expect(screen.getByText("내과 진료실")).toBeTruthy();
  });

  it("지난 알림(어제 등)은 페이지를 열어도 창으로 띄우지 않는다", async () => {
    ctx.alerts = [abortAlert(5, detail(), "2026-10-22 16:00")];
    render(<GuideBatteryAbortAlertWatcher />);
    await flush();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("목적지를 정하기 전에 멈췄으면 그렇게 적는다", async () => {
    ctx.alerts = [abortAlert(5, detail("알 수 없음", "-", "-"))];
    render(<GuideBatteryAbortAlertWatcher />);
    await flush();
    expect(screen.getByText("목적지를 정하기 전")).toBeTruthy();
    expect(screen.getByText("알 수 없음")).toBeTruthy();
  });

  it("'확인'은 읽음 처리하고 닫는다", async () => {
    ctx.alerts = [abortAlert(5)];
    render(<GuideBatteryAbortAlertWatcher />);
    await flush();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "확인" }));
    });
    await flush();
    expect(ctx.markAlertRead).toHaveBeenCalledWith(5);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("'나중에'는 읽음 처리 없이 닫고, 다시 띄우지 않는다", async () => {
    ctx.alerts = [abortAlert(5)];
    const { rerender } = render(<GuideBatteryAbortAlertWatcher />);
    await flush();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "나중에" }));
    });
    ctx.alerts = [abortAlert(5)];
    rerender(<GuideBatteryAbortAlertWatcher />);
    await flush();
    expect(ctx.markAlertRead).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("두 건이 쌓이면 하나를 닫은 뒤 다음 것을 띄운다", async () => {
    ctx.alerts = [abortAlert(1, detail("(1.0, 2.0)", "안과"), "2026-10-23 09:50"), abortAlert(2)];
    const { rerender } = render(<GuideBatteryAbortAlertWatcher />);
    await flush();
    expect(screen.getByText("내과 진료실")).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "확인" }));
    });
    rerender(<GuideBatteryAbortAlertWatcher />);
    await flush();
    expect(screen.getByText("안과")).toBeTruthy();
  });

  it("다른 알림에는 뜨지 않는다", async () => {
    ctx.alerts = [abortAlert(9, detail(), "2026-10-23 10:00", "guide_follow_lost")];
    render(<GuideBatteryAbortAlertWatcher />);
    await flush();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
