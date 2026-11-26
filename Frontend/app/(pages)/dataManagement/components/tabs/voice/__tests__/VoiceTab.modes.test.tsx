import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// 메모 11-03: 단발성 / 안내 시나리오를 탭으로 나누고, 버튼마다 어디에 쓰이는지 묶음 이름을 붙인다
const ctx = vi.hoisted(() => ({
  showToast: vi.fn(),
  api: { listIntents: vi.fn(), bulkDelete: vi.fn() },
}));

vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("../voiceApi", () => ({ voiceApi: ctx.api }));
vi.mock("@/app/components/select/CustomSelect", () => ({ default: () => <div data-testid="robot-select" /> }));
vi.mock("../VoiceScenarioList", () => ({ default: () => <div data-testid="single-list" /> }));
vi.mock("../VoiceGuidePanel", () => ({
  default: ({ onClose }: { onClose?: () => void }) => (
    <div data-testid="guide-panel" data-has-back={onClose ? "yes" : "no"} />
  ),
}));

import VoiceTab from "../VoiceTab";

const ROBOTS = [{ id: 13, no: "M20-13" }] as never;

async function renderTab() {
  render(<VoiceTab robots={ROBOTS} />);
  await act(async () => {
    await new Promise((r) => setTimeout(r, 350)); // 검색 디바운스(300ms)
  });
}

beforeEach(() => {
  ctx.api.listIntents.mockResolvedValue({ items: [], pendingCount: 0 });
});
afterEach(cleanup);

describe("VoiceTab — 단발성 / 안내 시나리오 탭", () => {
  it("처음에는 단발성 탭 — 단발성 전용·경로 경유지용·공통 묶음이 보인다", async () => {
    await renderTab();
    expect(screen.getByRole("tab", { name: "단발성 시나리오" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByTestId("single-list")).toBeTruthy();
    for (const label of ["공통", "경로 경유지용", "단발성 전용"]) expect(screen.getByText(label)).toBeTruthy();
    for (const btn of ["목적지 사전", "음성 설정", "안내 멘트", "도착 인식어", "종료 인식어", "일괄 등록", "시나리오 추가", "로봇 적용"])
      expect(screen.getByText(btn)).toBeTruthy();
  });

  it("안내 시나리오 탭 — 버튼 안을 거치지 않고 바로 열리고, 단발성 전용 버튼은 숨고 공통 버튼은 남는다", async () => {
    await renderTab();
    await act(async () => {
      fireEvent.click(screen.getByRole("tab", { name: "안내 시나리오" }));
    });
    const panel = screen.getByTestId("guide-panel");
    expect(panel.getAttribute("data-has-back")).toBe("no"); // 탭에 붙어 있으니 뒤로 가기 없음
    expect(screen.queryByTestId("single-list")).toBeNull();
    for (const btn of ["안내 멘트", "도착 인식어", "종료 인식어", "일괄 등록", "시나리오 추가", "로봇 적용"])
      expect(screen.queryByText(btn)).toBeNull();
    expect(screen.getByText("목적지 사전")).toBeTruthy();
    expect(screen.getByText("음성 설정")).toBeTruthy();
  });

  it("다시 단발성 탭으로 돌아오면 목록이 보인다", async () => {
    await renderTab();
    await act(async () => {
      fireEvent.click(screen.getByRole("tab", { name: "안내 시나리오" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("tab", { name: "단발성 시나리오" }));
    });
    expect(screen.getByTestId("single-list")).toBeTruthy();
  });
});
