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
    pending: vi.fn(),
    decide: vi.fn(),
  },
}));

vi.mock("@/app/context/AlertContext", () => ({
  useAlertContext: () => ({ unreadAlerts: ctx.alerts, refresh: ctx.refresh }),
}));
vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("@/app/lib/alertData", () => ({ markAlertRead: ctx.markAlertRead }));
vi.mock("@/app/lib/batteryResumeApi", () => ({ batteryResumeApi: ctx.api }));

import BatteryResumeAlertWatcher, {
  openBatteryResumeModal,
  parseResumeToken,
  pickResumeAlert,
  stillPending,
} from "@/app/components/common/BatteryResumeAlertWatcher";

function resumeAlert(id: number, token: number, action = "battery_resume_ask", date = "2026-10-22 10:00"): AlertMockData {
  return {
    id,
    type: "Robot",
    status: "event",
    content: "배터리 부족 — 충전 후 이어 갈까요?",
    date,
    robotName: "R296",
    isRead: false,
    log: {
      Category: "robot",
      Action: action,
      Message: "배터리 부족으로 충전소로 갑니다 — 충전 후 경로 반복 'A → B'을(를) 이어 갈까요?",
      Detail: `resume=${token} kind=route`,
      RobotName: "R296",
      CreatedAt: "2026-10-22T10:00:00",
    },
  };
}

const pending = (token: number, confirm_asked = false) => ({
  pending: { token, kind: "route", label: "A → B", confirm_asked },
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
  ctx.api.pending.mockResolvedValue(pending(7));
  ctx.api.decide.mockResolvedValue({ ok: true, msg: "충전이 끝나면 이어 갑니다" });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("이어 가기 알림 판정", () => {
  it("상세에서 요청 번호를 꺼낸다", () => {
    expect(parseResumeToken("resume=7 kind=route")).toBe(7);
    expect(parseResumeToken(null)).toBeNull();
    expect(parseResumeToken("kind=route")).toBeNull();
  });

  it("두 종류만 고르고, 이미 본 알림은 빼고, 가장 최근 것을 고른다", () => {
    const other = { ...resumeAlert(1, 5), log: { ...resumeAlert(1, 5).log!, Action: "nav_error" } };
    const older = resumeAlert(2, 6, "battery_resume_ask", "2026-10-22 09:00");
    const newer = resumeAlert(3, 6, "battery_resume_confirm", "2026-10-22 11:00");
    expect(pickResumeAlert([other, older, newer], new Set())?.id).toBe(3);
    expect(pickResumeAlert([other, older, newer], new Set([3]))?.id).toBe(2);
    expect(pickResumeAlert([other], new Set())).toBeNull();
  });

  it("요청이 살아 있는지 — 다시 묻는 단계로 넘어가면 처음 확인창은 닫는다", () => {
    expect(stillPending(pending(7).pending as never, 7, "ask")).toBe(true);
    expect(stillPending(pending(7, true).pending as never, 7, "ask")).toBe(false);
    expect(stillPending(pending(7, true).pending as never, 7, "confirm")).toBe(true);
    expect(stillPending(pending(8).pending as never, 7, "ask")).toBe(false);   // 새 요청
    expect(stillPending(null, 7, "confirm")).toBe(false);                       // 처리됨
  });
});

describe("BatteryResumeAlertWatcher", () => {
  it("알림이 없으면 아무것도 띄우지 않는다", () => {
    render(<BatteryResumeAlertWatcher />);
    expect(screen.queryByText("배터리 부족 — 충전하러 갑니다")).toBeNull();
  });

  it("충전소로 갈 때 확인창 — '충전 후 재개'는 resume 을 보내고 닫는다", async () => {
    ctx.alerts = [resumeAlert(101, 7)];
    render(<BatteryResumeAlertWatcher />);
    await flush();
    expect(screen.getByText("배터리 부족 — 충전하러 갑니다")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "충전 후 재개" }));
    await flush();
    expect(ctx.api.decide).toHaveBeenCalledWith(7, "resume");
    expect(ctx.markAlertRead).toHaveBeenCalledWith(101);
    expect(ctx.showToast).toHaveBeenCalledWith("충전이 끝나면 이어 갑니다", "success");
    expect(screen.queryByText("배터리 부족 — 충전하러 갑니다")).toBeNull();
  });

  it("'오늘은 여기서 종료'는 end 를 보낸다", async () => {
    ctx.alerts = [resumeAlert(101, 7)];
    ctx.api.decide.mockResolvedValue({ ok: true, msg: "오늘은 여기서 종료합니다" });
    render(<BatteryResumeAlertWatcher />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "오늘은 여기서 종료" }));
    await flush();
    expect(ctx.api.decide).toHaveBeenCalledWith(7, "end");
    expect(ctx.showToast).toHaveBeenCalledWith("오늘은 여기서 종료합니다", "success");
  });

  it("다시 묻는 확인창(confirm) — 문구가 다르고 '이어 가기'로 resume", async () => {
    ctx.alerts = [resumeAlert(102, 7, "battery_resume_confirm")];
    ctx.api.pending.mockResolvedValue(pending(7, true));
    render(<BatteryResumeAlertWatcher />);
    await flush();
    expect(screen.getByText("충전 완료 — 이어 갈까요?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "이어 가기" }));
    await flush();
    expect(ctx.api.decide).toHaveBeenCalledWith(7, "resume");
  });

  it("'나중에'는 API 를 부르지 않고 닫으며, 같은 알림으로 다시 뜨지 않는다", async () => {
    ctx.alerts = [resumeAlert(101, 7)];
    const { rerender } = render(<BatteryResumeAlertWatcher />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "나중에" }));
    await flush();
    expect(ctx.api.decide).not.toHaveBeenCalled();
    expect(ctx.markAlertRead).not.toHaveBeenCalled();
    ctx.alerts = [...ctx.alerts];
    rerender(<BatteryResumeAlertWatcher />);
    await flush();
    expect(screen.queryByText("배터리 부족 — 충전하러 갑니다")).toBeNull();
  });

  it("요청이 다른 데서 처리되면(자동 재개·다른 관제) 확인창이 스스로 닫힌다", async () => {
    ctx.alerts = [resumeAlert(101, 7)];
    render(<BatteryResumeAlertWatcher />);
    await flush();
    expect(screen.getByText("배터리 부족 — 충전하러 갑니다")).toBeTruthy();
    ctx.api.pending.mockResolvedValue({ pending: null });
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    await flush();
    expect(screen.queryByText("배터리 부족 — 충전하러 갑니다")).toBeNull();
    expect(ctx.markAlertRead).toHaveBeenCalledWith(101);
  });

  it("결정 요청이 실패하면(이미 처리됨 409) 오류를 알리고 확인창을 유지한다", async () => {
    ctx.alerts = [resumeAlert(101, 7)];
    ctx.api.decide.mockRejectedValue(new Error("이미 처리됐거나 지난 요청입니다"));
    render(<BatteryResumeAlertWatcher />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "충전 후 재개" }));
    await flush();
    expect(ctx.showToast).toHaveBeenCalledWith("이미 처리됐거나 지난 요청입니다", "error");
    expect(screen.getByText("배터리 부족 — 충전하러 갑니다")).toBeTruthy();
  });

  it("로봇 카드 배지로 연다 — '나중에' 뒤에도 지금 기다리는 요청으로 띄우고, 알림 읽음 처리는 하지 않는다", async () => {
    ctx.api.pending.mockResolvedValue(pending(7, true));
    render(<BatteryResumeAlertWatcher />);
    await act(async () => {
      openBatteryResumeModal("R296");
    });
    await flush();
    expect(screen.getByText("충전 완료 — 이어 갈까요?")).toBeTruthy();
    expect(screen.getByText("경로 반복 'A → B'")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "그만하기" }));
    await flush();
    expect(ctx.api.decide).toHaveBeenCalledWith(7, "end");
    expect(ctx.markAlertRead).not.toHaveBeenCalled();
    expect(screen.queryByText("충전 완료 — 이어 갈까요?")).toBeNull();
  });

  it("배지로 열었는데 기다리는 요청이 없으면(방금 처리됨) 안내만 한다", async () => {
    ctx.api.pending.mockResolvedValue({ pending: null });
    render(<BatteryResumeAlertWatcher />);
    await act(async () => {
      openBatteryResumeModal("R296");
    });
    await flush();
    expect(ctx.showToast).toHaveBeenCalledWith("확인을 기다리는 요청이 없습니다", "info");
    expect(screen.queryByText("충전 완료 — 이어 갈까요?")).toBeNull();
  });
});
