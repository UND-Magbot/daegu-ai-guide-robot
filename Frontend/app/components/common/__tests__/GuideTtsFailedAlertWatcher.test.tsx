import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AlertMockData } from "@/app/types";

// ── 가짜 컨텍스트·API ────────────────────────────────
const ctx = vi.hoisted(() => ({
  alerts: [] as AlertMockData[],
  refresh: vi.fn(async () => {}),
  showToast: vi.fn(),
  markAlertRead: vi.fn(async () => {}),
  closeSession: vi.fn(async () => ({})),
}));

vi.mock("@/app/context/AlertContext", () => ({
  useAlertContext: () => ({ unreadAlerts: ctx.alerts, refresh: ctx.refresh }),
}));
vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("@/app/lib/alertData", () => ({ markAlertRead: ctx.markAlertRead }));
vi.mock("@/app/(pages)/dataManagement/components/tabs/voice/guideScenarioApi", () => ({
  guideScenarioApi: { closeSession: ctx.closeSession },
}));

import GuideTtsFailedAlertWatcher, {
  parseTtsFailed,
  pickTtsFailedAlert,
  ttsFailedKind,
} from "@/app/components/common/GuideTtsFailedAlertWatcher";

// 백엔드 voice_scenario.session_tts_failed 가 남기는 형식 그대로(11-02 세션 522 실제 상세)
const DETAIL = "session=522 step=greet text='안녕하세요. 병원에 오신 것을 환영합니다. 오늘 안내를 도와드릴 로봇입니다.' error=";
const MESSAGE = "CA9B_NO.669_5G 안내 음성 출력 실패(안내 멘트)";
const NOW = new Date("2026-11-02T11:30:00");

function ttsAlert(id: number, detail: string | null = DETAIL, date = "2026-11-02 11:27", action = "guide_tts_failed"): AlertMockData {
  return {
    id,
    type: "Schedule",
    status: "error",
    content: "안내 음성 출력 실패 — 운영자 확인 필요",
    date,
    robotName: "CA9B_NO.669_5G",
    isRead: false,
    log: {
      Category: "schedule",
      Action: action,
      Message: MESSAGE,
      Detail: detail,
      RobotName: "CA9B_NO.669_5G",
      CreatedAt: `${date}:31`,
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
  ctx.refresh.mockClear();
  ctx.showToast.mockClear();
  ctx.markAlertRead.mockClear();
  ctx.closeSession.mockClear();
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("알림 읽기", () => {
  it("상세에서 세션·단계·못 한 말을 꺼낸다", () => {
    expect(parseTtsFailed(DETAIL)).toEqual({
      sessionId: 522,
      step: "greet",
      text: "안녕하세요. 병원에 오신 것을 환영합니다. 오늘 안내를 도와드릴 로봇입니다.",
    });
  });

  it("말 안에 따옴표가 있어도 error= 앞까지 읽는다 / 상세가 없으면 빈 값", () => {
    expect(parseTtsFailed("session=3 step=ready text=\"'준비됐어요'라고 말씀해 주세요.\" error=x").text).toBe(
      "'준비됐어요'라고 말씀해 주세요."
    );
    expect(parseTtsFailed(null)).toEqual({ sessionId: null, step: null, text: null });
  });

  it("본문 괄호에서 질문·안내 멘트를 구분한다", () => {
    expect(ttsFailedKind(MESSAGE)).toBe("안내 멘트");
    expect(ttsFailedKind("R 안내 음성 출력 실패(질문)")).toBe("질문");
    expect(ttsFailedKind("다른 글")).toBeNull();
  });

  it("방금(30분 안) 생긴, 안 본 음성 실패 알림만 — 반복 로그·다른 알림은 무시", () => {
    const alerts = [
      ttsAlert(1, DETAIL, "2026-11-02 11:20"),
      ttsAlert(2, DETAIL, "2026-11-02 11:27"),
      ttsAlert(3, DETAIL, "2026-11-02 10:40"),
      ttsAlert(4, DETAIL, "2026-11-02 11:28", "guide_tts_failed_repeat"),
      ttsAlert(5, DETAIL, "2026-11-02 11:29", "guide_follow_lost"),
    ];
    expect(pickTtsFailedAlert(alerts, new Set(), NOW.getTime())?.id).toBe(2);
    expect(pickTtsFailedAlert(alerts, new Set([2]), NOW.getTime())?.id).toBe(1);
    expect(pickTtsFailedAlert(alerts, new Set([1, 2]), NOW.getTime())).toBeNull();
  });
});

describe("알림창", () => {
  it("로봇·못 한 말·안내 종료 버튼을 보여 준다", async () => {
    ctx.alerts = [ttsAlert(7)];
    render(<GuideTtsFailedAlertWatcher />);
    await flush();
    expect(screen.getByText("안내 음성 출력 실패")).toBeTruthy();
    expect(screen.getByText("CA9B_NO.669_5G")).toBeTruthy();
    expect(screen.getByText(/병원에 오신 것을 환영합니다/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "안내 종료" })).toBeTruthy();
  });

  it("'확인' → 읽음 처리하고 닫는다", async () => {
    ctx.alerts = [ttsAlert(7)];
    render(<GuideTtsFailedAlertWatcher />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "확인" }));
    await flush();
    expect(ctx.markAlertRead).toHaveBeenCalledWith(7);
    expect(screen.queryByText("안내 음성 출력 실패")).toBeNull();
  });

  it("'나중에' → 읽음 처리 없이 닫고 다시 띄우지 않는다", async () => {
    ctx.alerts = [ttsAlert(7)];
    const { rerender } = render(<GuideTtsFailedAlertWatcher />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "나중에" }));
    await flush();
    expect(ctx.markAlertRead).not.toHaveBeenCalled();
    ctx.alerts = [ttsAlert(7)];
    rerender(<GuideTtsFailedAlertWatcher />);
    await flush();
    expect(screen.queryByText("안내 음성 출력 실패")).toBeNull();
  });

  it("'안내 종료' → 그 세션을 닫고 읽음 처리", async () => {
    ctx.alerts = [ttsAlert(7)];
    render(<GuideTtsFailedAlertWatcher />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "안내 종료" }));
    await flush();
    expect(ctx.closeSession).toHaveBeenCalledWith(522, "음성 출력 실패 — 운영자 종료");
    expect(ctx.markAlertRead).toHaveBeenCalledWith(7);
  });

  it("세션 번호가 없는 예전 알림은 본문을 보여 주고 '안내 종료'를 숨긴다", async () => {
    ctx.alerts = [ttsAlert(8, null)];
    render(<GuideTtsFailedAlertWatcher />);
    await flush();
    expect(screen.getByText(MESSAGE)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "안내 종료" })).toBeNull();
  });
});
