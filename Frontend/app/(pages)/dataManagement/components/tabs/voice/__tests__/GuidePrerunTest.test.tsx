import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ctx = vi.hoisted(() => ({
  showToast: vi.fn(),
  api: { prerunPlan: vi.fn(), prerunStart: vi.fn() },
}));

vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("../guideScenarioApi", () => ({ guideScenarioApi: ctx.api }));

import GuidePrerunTest from "../GuidePrerunTest";

// 장소 목록 펼침의 사용자 스크롤바가 쓴다 — jsdom 에는 없다
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as never;

const SCENARIO = { id: 1, code: "hospital", displayName: "병원 안내", mapId: 47, startLocationId: 1, steps: [] } as never;
const PLACES = [
  { id: 1, label: "로봇대기장소" },
  { id: 2, label: "원무과 접수" },
];
const LEGS = ["로봇대기장소", "원무과 접수", "간호실데스크", "로봇대기장소"];
const plan = (names: string[]) => ({
  legs: names.map((name, i) => ({ id: i + 1, name, role: "step", step: null })),
  warnings: [],
  destinations: 1,
});
const ALL4 = { legIndexes: [0, 1, 2, 3], legCount: 4 };
// 실제 동선 꼴 — 출발·복귀는 start, 편의점은 대답에 따라(branch)
const PICK_PLAN = {
  legs: [
    { id: 1, name: "로봇대기장소", role: "start", step: null },
    { id: 2, name: "원무과 접수", role: "step", step: "ticket_move" },
    { id: 3, name: "진료대기장소", role: "step", step: "seat_move" },
    { id: 4, name: "편의점입구", role: "step", step: "store_move", branch: true },
    { id: 3, name: "진료대기장소", role: "step", step: "store_return_move", branch: true },
    { id: 5, name: "간호실데스크", role: "destination", step: "clinic_move" },
    { id: 1, name: "로봇대기장소", role: "start", step: null },
  ],
  warnings: [],
  destinations: 1,
};

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function renderTest(blockedReason: string | null = null) {
  render(<GuidePrerunTest robotId={10} scenario={SCENARIO} startPlaces={PLACES} blockedReason={blockedReason} />);
  await flush();
}

async function click(text: string) {
  await act(async () => {
    fireEvent.click(screen.getByText(text));
    await Promise.resolve();
    await Promise.resolve();
  });
}

const startBtn = () => screen.getByText("1회 주행 시작") as HTMLButtonElement;
const goBtn = () => screen.getByText("주행 시작") as HTMLButtonElement;

/** 1회 주행 시작 → 갈 곳 고르기 창 → 주행 시작 */
async function startPicked() {
  await click("1회 주행 시작");
  await click("주행 시작");
}

beforeEach(() => {
  Object.values(ctx.api).forEach((f) => f.mockReset());
  ctx.showToast.mockClear();
  ctx.api.prerunPlan.mockResolvedValue(plan(LEGS));
  ctx.api.prerunStart.mockResolvedValue({ prerun: null });
});
afterEach(cleanup);

describe("GuidePrerunTest — 1회 주행 테스트", () => {
  it("기본 출발 장소로 동선을 보여 주고, 누르면 안내 없이 사전 주행만 시작한다", async () => {
    await renderTest();
    expect(ctx.api.prerunPlan).toHaveBeenCalledWith(10, "hospital", 1);
    expect(screen.getByText(LEGS.join(" → "))).toBeTruthy();

    await startPicked();
    expect(ctx.api.prerunStart).toHaveBeenCalledWith(10, "hospital", 1, "fast", ALL4);
    expect(ctx.showToast).toHaveBeenCalledWith(expect.stringContaining("고속 1회 주행을 시작했습니다"), "success");
  });

  it("속도를 저속·보통으로 바꾸면 그 속도로 시작한다", async () => {
    await renderTest();
    await click("고속");
    await click("저속 (따라오는 속도)");
    await startPicked();
    expect(ctx.api.prerunStart).toHaveBeenLastCalledWith(10, "hospital", 1, "slow", ALL4);
    expect(ctx.showToast).toHaveBeenLastCalledWith(expect.stringContaining("저속 1회 주행을 시작했습니다"), "success");

    await click("저속 (따라오는 속도)");
    await click("보통");
    await startPicked();
    expect(ctx.api.prerunStart).toHaveBeenLastCalledWith(10, "hospital", 1, "normal", ALL4);
  });

  it("출발 장소를 현재 위치로 바꾸면 동선을 다시 불러오고 장소 없이 시작한다", async () => {
    await renderTest();
    await click("로봇대기장소 (기본)");
    await click("현재 위치에서 바로 시작");
    expect(ctx.api.prerunPlan).toHaveBeenLastCalledWith(10, "hospital", null);
    await startPicked();
    expect(ctx.api.prerunStart).toHaveBeenCalledWith(10, "hospital", null, "fast", ALL4);
  });

  it("막힘 사유가 있으면 버튼이 꺼지고 사유를 보여 준다", async () => {
    await renderTest("진행 중인 안내·사전 주행이 끝난 뒤 시작할 수 있습니다.");
    expect(startBtn().disabled).toBe(true);
    expect(screen.getByText("진행 중인 안내·사전 주행이 끝난 뒤 시작할 수 있습니다.")).toBeTruthy();
  });

  it("갈 장소가 없으면 시작할 수 없다", async () => {
    ctx.api.prerunPlan.mockResolvedValue(plan([]));
    await renderTest();
    expect(screen.getByText("갈 장소가 없습니다")).toBeTruthy();
    expect(startBtn().disabled).toBe(true);
  });

  it("서버가 거절하면 그 사유를 알린다", async () => {
    ctx.api.prerunStart.mockRejectedValue(new Error("로봇이 다른 이동 중입니다."));
    await renderTest();
    await startPicked();
    expect(ctx.showToast).toHaveBeenCalledWith("로봇이 다른 이동 중입니다.", "error");
  });

  it("동선 미리 보기가 실패하면 그 사유를 보여 준다", async () => {
    ctx.api.prerunPlan.mockRejectedValue(new Error("시나리오의 운영 맵을 먼저 지정하세요"));
    await renderTest();
    expect(screen.getByText("시나리오의 운영 맵을 먼저 지정하세요")).toBeTruthy();
  });

  it("누르면 바로 출발하지 않고 갈 곳 고르기 창을 띄우고, 취소하면 아무것도 보내지 않는다", async () => {
    ctx.api.prerunPlan.mockResolvedValue(PICK_PLAN);
    await renderTest();
    await click("1회 주행 시작");
    expect(ctx.api.prerunStart).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "갈 곳 고르기" })).toBeTruthy();
    await click("취소");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(ctx.api.prerunStart).not.toHaveBeenCalled();
  });

  it("대답에 따라 가는 곳은 처음에 꺼져 있어 빼고 보내고, 출발·복귀는 늘 들어간다", async () => {
    ctx.api.prerunPlan.mockResolvedValue(PICK_PLAN);
    await renderTest();
    await click("1회 주행 시작");
    const boxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    expect(boxes.map((b) => b.checked)).toEqual([true, true, true, false, false, true, true]);
    expect(boxes[0].disabled && boxes[6].disabled).toBe(true);
    expect(screen.getAllByText("대답에 따라")).toHaveLength(2);
    expect(screen.getByText("로봇대기장소 → 원무과 접수 → 진료대기장소 → 간호실데스크 → 로봇대기장소")).toBeTruthy();
    await click("주행 시작");
    expect(ctx.api.prerunStart).toHaveBeenCalledWith(10, "hospital", 1, "fast", {
      legIndexes: [0, 1, 2, 5, 6],
      legCount: 7,
    });
  });

  it("편의점을 켜면 그 구간이 들어가고, 전체 선택은 모두 넣는다", async () => {
    ctx.api.prerunPlan.mockResolvedValue(PICK_PLAN);
    await renderTest();
    await click("1회 주행 시작");
    await act(async () => {
      fireEvent.click(screen.getAllByRole("checkbox")[3]);
    });
    expect(screen.getByText(/진료대기장소 → 편의점입구 → 간호실데스크/)).toBeTruthy();
    await click("전체 선택");
    await click("주행 시작");
    expect(ctx.api.prerunStart).toHaveBeenCalledWith(10, "hospital", 1, "fast", {
      legIndexes: [0, 1, 2, 3, 4, 5, 6],
      legCount: 7,
    });
  });

  it("출발·복귀만 남기면 주행 시작이 꺼진다", async () => {
    ctx.api.prerunPlan.mockResolvedValue(PICK_PLAN);
    await renderTest();
    await click("1회 주행 시작");
    const boxes = screen.getAllByRole("checkbox");
    for (const i of [1, 2, 5]) {
      await act(async () => {
        fireEvent.click(boxes[i]);
      });
    }
    expect(goBtn().disabled).toBe(true);
  });

  it("서버가 거절하면 창을 닫지 않고 사유를 알린다(동선이 바뀜 등)", async () => {
    ctx.api.prerunPlan.mockResolvedValue(PICK_PLAN);
    ctx.api.prerunStart.mockRejectedValue(new Error("동선이 바뀌었습니다. 갈 곳 고르기 창을 다시 열어 주세요."));
    await renderTest();
    await startPicked();
    expect(ctx.showToast).toHaveBeenCalledWith("동선이 바뀌었습니다. 갈 곳 고르기 창을 다시 열어 주세요.", "error");
    expect(screen.getByRole("dialog", { name: "갈 곳 고르기" })).toBeTruthy();
  });
});
