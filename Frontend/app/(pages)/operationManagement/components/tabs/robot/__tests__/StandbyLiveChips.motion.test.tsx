import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ctx = vi.hoisted(() => ({ calls: [] as string[] }));

vi.mock("@/app/lib/api", () => ({
  apiFetch: vi.fn(async (url: string) => {
    ctx.calls.push(url);
    return { ok: true, json: async () => [] };
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
  steps: [],
  session: null,
  todaySummary: { total: 0, completed: 0 },
  mode: "motion",
  scenarioName: null,
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
});
afterEach(cleanup);

describe("근무 칩 — 단순 동작 근무", () => {
  it("작업명 줄에 '단순 동작' 꼬리표가 붙고 단계 점은 없다", () => {
    render(<StandbyStatusChip live={live()} />);
    expect(screen.getByTitle("응대 방식 — 앉아·일어서 명령만 받습니다").textContent).toBe("단순 동작");
    expect(screen.getByText("대기 중(응대 가능)")).toBeTruthy();
    expect(screen.queryByText("—")).toBeNull();
  });

  it("단발·시나리오 근무에는 '단순 동작' 꼬리표가 없다", () => {
    render(<StandbyStatusChip live={live({ mode: "single", steps: ["대기 중", "응대 중"] })} />);
    expect(screen.queryByTitle("응대 방식 — 앉아·일어서 명령만 받습니다")).toBeNull();
  });

  it("관제 개입(목적지 지정)·안내 시작 버튼이 없고 목적지 목록도 받지 않는다", async () => {
    render(<StandbyLiveDetail live={live()} canControl />);
    await flush();
    expect(screen.queryByText(/관제 개입/)).toBeNull();
    expect(screen.queryByRole("button", { name: "안내 시작" })).toBeNull();
    expect(screen.getByText(/앉아 \/ 일어서/).textContent).toContain("명령을 기다리는 중");
    expect(ctx.calls.some((u) => u.startsWith("/DB/standby-destinations"))).toBe(false);
  });

  it("회귀 — 단발 근무는 관제 개입 버튼이 그대로 있다", async () => {
    render(<StandbyLiveDetail live={live({ mode: "single", steps: ["대기 중"] })} canControl />);
    await flush();
    expect(screen.getByText(/관제 개입/)).toBeTruthy();
    expect(ctx.calls.some((u) => u.startsWith("/DB/standby-destinations"))).toBe(true);
  });
});
