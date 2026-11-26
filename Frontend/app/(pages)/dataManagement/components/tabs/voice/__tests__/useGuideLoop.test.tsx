import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ctx = vi.hoisted(() => ({
  api: { loopCheck: vi.fn(), openSession: vi.fn() },
}));

vi.mock("../guideScenarioApi", () => ({ guideScenarioApi: ctx.api }));

import { LOOP_GAP_SEC, afterRound, useGuideLoop, type LoopRun } from "../useGuideLoop";
import type { GuideSession, LoopCheck } from "../guideScenarioApi";

const sess = (id: number) => ({ id, result: "running" }) as unknown as GuideSession;
const check = (over: Partial<LoopCheck> = {}): LoopCheck => ({
  ok: true, kind: null, reason: null, battery: 80, limit: 17, result: "completed", ...over,
});
const plan = (total: number | null) => ({ scenarioCode: "parking", scenarioName: "주차장 안내", startLocationId: null, total });

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function setup(total: number | null) {
  const notify = vi.fn();
  // 화면은 새 회차 세션을 바로 반영한다(GuideSessionCard 의 setSession)
  const onOpened = vi.fn((s: GuideSession) => hook.rerender({ session: s }));
  const hook = renderHook(
    ({ session }: { session: GuideSession | null }) =>
      useGuideLoop({ robotId: 10, session, loaded: true, onOpened, notify }),
    { initialProps: { session: sess(1) as GuideSession | null } }
  );
  act(() => hook.result.current.start(plan(total), 1));
  return { hook, notify, onOpened };
}

beforeEach(() => {
  vi.useFakeTimers();
  ctx.api.loopCheck.mockReset();
  ctx.api.openSession.mockReset();
});
afterEach(() => vi.useRealTimers());

describe("afterRound", () => {
  const base: LoopRun = {
    plan: plan(3), round: 1, sessionId: 1, countdown: null, stopRequested: false, rounds: [], ended: null,
  };

  it("정상이고 남은 회차가 있으면 카운트다운", () => {
    const r = afterRound(base, 1, check());
    expect(r.countdown).toBe(LOOP_GAP_SEC);
    expect(r.ended).toBeNull();
    expect(r.rounds).toHaveLength(1);
  });

  it("고객 대답이 없었으면 멈춘다", () => {
    const r = afterRound(base, 1, check({ ok: false, kind: "no_answer", reason: "대답 없음" }));
    expect(r.ended?.kind).toBe("no_answer");
  });

  it("배터리 기준 이하면 멈춘다", () => {
    const r = afterRound(base, 1, check({ ok: false, kind: "battery", reason: "배터리 15%", battery: 15 }));
    expect(r.ended?.kind).toBe("battery");
  });

  it("지정 횟수를 채우면 끝", () => {
    expect(afterRound({ ...base, round: 3 }, 1, check()).ended?.kind).toBe("done");
  });

  it("중지 요청이면 이번 회차로 끝", () => {
    expect(afterRound({ ...base, stopRequested: true }, 1, check()).ended?.kind).toBe("stopped");
  });

  it("무한 반복은 횟수로 끝나지 않는다", () => {
    expect(afterRound({ ...base, plan: plan(null), round: 50 }, 1, check()).ended).toBeNull();
  });
});

describe("useGuideLoop", () => {
  it("회차가 끝나면 판정 → 대기 → 같은 설정으로 다음 회차를 연다", async () => {
    ctx.api.loopCheck.mockResolvedValue(check());
    ctx.api.openSession.mockResolvedValue({ session: sess(2) });
    const { hook, onOpened } = setup(3);

    hook.rerender({ session: null });
    await flush();
    expect(ctx.api.loopCheck).toHaveBeenCalledWith(10, 1, 1);
    expect(hook.result.current.run?.countdown).toBe(LOOP_GAP_SEC);

    for (let i = 0; i < LOOP_GAP_SEC; i++) {
      act(() => vi.advanceTimersByTime(1000));
    }
    await flush();
    expect(ctx.api.openSession).toHaveBeenCalledWith(10, "parking", null);
    expect(onOpened).toHaveBeenCalled();
    expect(hook.result.current.run?.round).toBe(2);
    expect(hook.result.current.run?.sessionId).toBe(2);
  });

  it("고객 대답 없이 끝나면 다음 회차를 열지 않고 알린다", async () => {
    ctx.api.loopCheck.mockResolvedValue(check({ ok: false, kind: "no_answer", reason: "직전 회차에 고객 대답이 없었습니다." }));
    const { hook, notify } = setup(null);

    hook.rerender({ session: null });
    await flush();
    act(() => vi.advanceTimersByTime(LOOP_GAP_SEC * 1000));
    await flush();
    expect(ctx.api.openSession).not.toHaveBeenCalled();
    expect(hook.result.current.run?.ended?.kind).toBe("no_answer");
    expect(notify).toHaveBeenCalledWith(expect.stringContaining("고객 대답이 없었습니다"), "error");
  });

  it("새 회차를 폴링하기 전이라 '진행 중'이면 잠시 뒤 다시 묻는다", async () => {
    ctx.api.loopCheck
      .mockResolvedValueOnce(check({ ok: false, kind: "running", result: "running" }))
      .mockResolvedValueOnce(check());
    const { hook } = setup(3);

    hook.rerender({ session: null });
    await flush();
    expect(hook.result.current.run?.ended).toBeNull();
    act(() => vi.advanceTimersByTime(2000));
    await flush();
    expect(ctx.api.loopCheck).toHaveBeenCalledTimes(2);
    expect(hook.result.current.run?.countdown).toBe(LOOP_GAP_SEC);
  });

  it("카운트다운 중 반복 중지하면 다음 회차를 열지 않는다", async () => {
    ctx.api.loopCheck.mockResolvedValue(check());
    const { hook } = setup(null);

    hook.rerender({ session: null });
    await flush();
    act(() => hook.result.current.requestStop());
    act(() => vi.advanceTimersByTime(LOOP_GAP_SEC * 1000));
    await flush();
    expect(ctx.api.openSession).not.toHaveBeenCalled();
    expect(hook.result.current.run?.ended?.kind).toBe("stopped");
  });
});
