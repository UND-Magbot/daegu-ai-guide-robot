import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ctx = vi.hoisted(() => ({ calls: [] as { url: string; body: unknown }[], fail: null as string | null }));

vi.mock("@/app/lib/api", () => ({
  apiFetch: vi.fn(async (url: string, init?: { method?: string; body?: string }) => {
    ctx.calls.push({ url, body: init?.body ? JSON.parse(init.body) : null });
    if (url === "/DB/standby-call" && ctx.fail) return { ok: false, json: async () => ({ detail: ctx.fail }) };
    if (url.startsWith("/DB/standby-destinations")) return { ok: true, json: async () => [{ id: 5, name: "내과", linked: true }] };
    return { ok: true, json: async () => ({ ok: true, sessionId: 77, mode: "scenario" }) };
  }),
}));

import StandbyLiveDetail, { StandbyStatusChip } from "../StandbyLiveChips";
import type { StandbyLive } from "../useStandbyLive";

const live = (patch: Partial<StandbyLive> = {}): StandbyLive => ({
  active: true,
  scheduleId: 3,
  robotName: "R296",
  phase: "standby",
  phaseLabel: "대기 중(응대 가능)",
  step: 0,
  steps: ["대기 중", "안내 중", "대기장소 복귀"],
  session: null,
  todaySummary: { total: 4, completed: 3 },
  mode: "scenario",
  scenarioName: "병원 안내",
  guide: null,
  ...patch,
});

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

beforeEach(() => {
  ctx.calls = [];
  ctx.fail = null;
});
afterEach(cleanup);

describe("근무 칩 — 시나리오 근무", () => {
  it("작업명 줄에 시나리오 이름 꼬리표와 3단계 점", () => {
    render(<StandbyStatusChip live={live()} />);
    expect(screen.getByTitle("응대 방식 — 안내 시나리오").textContent).toBe("병원 안내");
    expect(screen.getByText("대기 중(응대 가능)")).toBeTruthy();
    expect(screen.getByTitle("대기 중 › 안내 중 › 대기장소 복귀")).toBeTruthy();
  });

  it("단발 응대 근무에는 꼬리표가 없다", () => {
    render(<StandbyStatusChip live={live({ mode: "single", scenarioName: null })} />);
    expect(screen.queryByTitle("응대 방식 — 안내 시나리오")).toBeNull();
  });

  it("[관제 개입 ▾] 대신 [안내 시작] — 목적지 없이 근무의 시나리오를 연다", async () => {
    const onCalled = vi.fn();
    render(<StandbyLiveDetail live={live()} canControl onCalled={onCalled} />);
    await flush();
    expect(screen.queryByText(/관제 개입/)).toBeNull();
    expect(ctx.calls.some((c) => c.url.startsWith("/DB/standby-destinations"))).toBe(false);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "안내 시작" }));
    });
    await flush();
    expect(ctx.calls.at(-1)).toEqual({ url: "/DB/standby-call", body: { scheduleId: 3 } });
    expect(onCalled).toHaveBeenCalled();
    expect(screen.getByText("병원 안내을(를) 시작했습니다.")).toBeTruthy();
  });

  it("안내 중이면 [안내 시작]이 꺼지고 [안내 종료]가 있다", async () => {
    render(
      <StandbyLiveDetail
        live={live({ phase: "serving", phaseLabel: "안내 중", step: 1,
          guide: { id: 77, stepCode: "purpose_ask", destination: "내과", startedAt: null, elapsedSec: 30, abortRequest: null } })}
        canControl
      />
    );
    await flush();
    expect((screen.getByRole("button", { name: "안내 시작" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("button", { name: "안내 종료" })).toBeTruthy();
  });

  it("대기 장소로 가는 중이면 그 자리에서 시작한다고 알려 준다", async () => {
    render(<StandbyLiveDetail live={live({ phase: "moving", phaseLabel: "대기 장소로 이동 중" })} canControl />);
    await flush();
    expect(screen.getByRole("button", { name: "안내 시작" }).getAttribute("title")).toContain("그 자리에서");
  });

  it("서버가 거절하면 그 이유를 보여 준다", async () => {
    ctx.fail = "로봇이 1F 에 있습니다. '병원 안내' 시나리오는 3F 기준입니다.";
    render(<StandbyLiveDetail live={live()} canControl />);
    await flush();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "안내 시작" }));
    });
    await flush();
    expect(screen.getByText(/시나리오는 3F 기준입니다/)).toBeTruthy();
  });

  it("관제 권한이 없으면 버튼이 없다", async () => {
    render(<StandbyLiveDetail live={live()} canControl={false} />);
    await flush();
    expect(screen.queryByRole("button", { name: "안내 시작" })).toBeNull();
  });
});

describe("근무 칩 — 단발 응대 근무 (회귀)", () => {
  it("그대로 [관제 개입 ▾] 목적지 목록", async () => {
    render(<StandbyLiveDetail live={live({ mode: "single", scenarioName: null })} canControl />);
    await flush();
    expect(screen.queryByRole("button", { name: "안내 시작" })).toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /관제 개입/ }));
    });
    expect(screen.getByRole("button", { name: "내과" })).toBeTruthy();
  });
});
