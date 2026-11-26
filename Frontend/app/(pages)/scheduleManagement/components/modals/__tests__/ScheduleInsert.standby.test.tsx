import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ctx = vi.hoisted(() => ({
  showToast: vi.fn(),
  posted: [] as Record<string, unknown>[],
  live: [] as { id: number; currentMapId: number | null }[],
  mic: false,
  scenarios: [
    { code: "hospital", name: "병원 안내", mapId: 47 },
    { code: "public_office", name: "관공서 안내", mapId: 43 },
  ],
}));

const res = (data: unknown, ok = true) => ({ ok, json: async () => data });

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }) }));
vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("@/app/context/RobotStatusContext", () => ({ useRobotStatusContext: () => ({ robots: ctx.live }) }));
vi.mock("@/app/lib/navReach", () => ({
  usePathBlocks: () => ({ places: [], blocks: {} }),
  markBlocked: (o: unknown) => o,
  placeBlockReason: () => null,
  pathBlockReason: () => null,
}));
vi.mock("@/app/lib/api", () => ({
  apiFetch: vi.fn(async (url: string, init?: { method?: string; body?: string }) => {
    if (url.startsWith("/DB/schedule-standby-capability")) {
      return res({
        allowed: true, reason: null, hasVoice: true, hasMic: ctx.mic, hasSpeaker: true,
        destinationCount: 3, allowedTriggers: ctx.mic ? ["mic", "button"] : ["button"], scenarios: ctx.scenarios,
      });
    }
    if (url === "/DB/places") {
      return res([
        { id: 1, LacationName: "로비 대기", MapId: 47, Category: "standby" },
        { id: 2, LacationName: "약제실", MapId: 47, Category: "work" },
        { id: 3, LacationName: "로봇대기장소", MapId: 43, Category: "standby" },
        { id: 4, LacationName: "충전소", MapId: 47, Category: "charge" },
      ]);
    }
    if (url === "/DB/schedule" && init?.method === "POST") {
      ctx.posted.push(JSON.parse(init.body ?? "{}"));
      return res({ id: 99, warnings: [] });
    }
    return res([]);
  }),
}));

import InsertModal from "../ScheduleInsert";
import { WORK_TYPE_ALL } from "@/app/lib/taskType";

const robots = [
  { id: 10, no: "R296", currentMapId: 47 },
  { id: 13, no: "R669", currentMapId: 43 },
  { id: 20, no: "R000", currentMapId: null },
] as never[];

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};
const click = async (el: Element) => {
  await act(async () => {
    fireEvent.click(el);
    await Promise.resolve();
  });
  await flush();
};
// 선택 상자 — 열린 목록에도 같은 문구가 있을 수 있어 화면 순서상 첫 번째(상자 자체)를 누른다
const trigger = (text: string) => screen.getAllByText(text)[0];
const pick = async (placeholderOrValue: string, option: string) => {
  await click(trigger(placeholderOrValue));
  await click(screen.getByRole("option", { name: option }));
};
const options = async (text: string) => {
  await click(trigger(text));
  const names = screen.queryAllByRole("option").map((o) => o.textContent);
  await act(async () => {
    fireEvent.mouseDown(document.body);     // 바깥을 눌러 닫는다
  });
  return names;
};

async function openStandby() {
  render(<InsertModal isOpen onClose={vi.fn()} robots={robots} onScheduleChanged={vi.fn()} />);
  await flush();
  await click(screen.getByText("상시 대기"));
}

beforeEach(() => {
  ctx.posted = [];
  ctx.live = [];
  ctx.mic = false;
  ctx.showToast.mockClear();
});
afterEach(cleanup);

describe("작업 등록 › 상시 대기 — 대기 장소", () => {
  it("로봇을 고르기 전에는 대기 장소를 고를 수 없다", async () => {
    await openStandby();
    expect(screen.getByText("로봇을 먼저 선택하세요")).toBeTruthy();
    await click(screen.getByText("로봇을 먼저 선택하세요"));
    expect(screen.queryByRole("option", { name: "로비 대기" })).toBeNull();
  });

  it("로봇을 고르면 그 로봇이 있는 맵의 장소만 나온다(충전소 제외)", async () => {
    await openStandby();
    await pick("로봇명을 선택하세요", "R296");
    expect(await options("미지정 시 충전소에서 대기")).toEqual(["로비 대기", "약제실"]);
  });

  it("로봇을 바꾸면 그 맵 장소로 바뀌고, 맞지 않는 선택은 비워진다", async () => {
    await openStandby();
    await pick("로봇명을 선택하세요", "R296");
    await pick("미지정 시 충전소에서 대기", "로비 대기");
    await pick("R296", "R669");
    expect(screen.queryByText("로비 대기")).toBeNull();
    expect(await options("미지정 시 충전소에서 대기")).toEqual(["로봇대기장소"]);
  });

  it("실시간 로봇 상태의 맵을 먼저 쓴다(페이지를 연 뒤 맵을 바꾼 경우)", async () => {
    ctx.live = [{ id: 10, currentMapId: 43 }];
    await openStandby();
    await pick("로봇명을 선택하세요", "R296");
    expect(await options("미지정 시 충전소에서 대기")).toEqual(["로봇대기장소"]);
  });

  it("로봇의 맵을 모르면 이유를 보여 주고 막는다", async () => {
    await openStandby();
    await pick("로봇명을 선택하세요", "R000");
    expect(screen.getByText(/현재 맵을 알 수 없습니다/)).toBeTruthy();
  });
});

describe("작업 등록 › 상시 대기 — 응대 방식", () => {
  it("단발 응대 + 켜진 시나리오, 맵이 다른 시나리오는 경고, 저장 때 코드·대기 장소를 보낸다", async () => {
    await openStandby();
    await pick("로봇명을 선택하세요", "R296");
    expect(await options("단발 응대 (목적지 안내)")).toEqual([
      "단발 응대 (목적지 안내)",
      "안내 시나리오 · 병원 안내",
      "안내 시나리오 · 관공서 안내",
    ]);
    await pick("단발 응대 (목적지 안내)", "안내 시나리오 · 관공서 안내");
    expect(screen.getByText(/'관공서 안내' 시나리오의 맵이 다릅니다/)).toBeTruthy();
    await pick("안내 시나리오 · 관공서 안내", "안내 시나리오 · 병원 안내");
    expect(screen.queryByText(/시나리오의 맵이 다릅니다/)).toBeNull();
    expect(screen.getByText(/병원 안내 시나리오를 첫 단계부터/)).toBeTruthy();
    await pick("미지정 시 충전소에서 대기", "로비 대기");

    fireEvent.change(screen.getByPlaceholderText("25자(50byte) 이내로 작성하세요"), { target: { value: "로비 상시 대기" } });
    await pick(WORK_TYPE_ALL.label, "길 안내");
    await click(screen.getByText("관제 · 태블릿 버튼"));
    await click(screen.getByText("저장"));
    expect(ctx.posted).toHaveLength(1);
    expect(ctx.posted[0]).toMatchObject({ ScheduleMode: "standby", StandbyScenarioCode: "hospital", ReturnPlaceId: 1, RobotName: "R296" });
  });

  it("단발 응대로 저장하면 시나리오 코드는 비워 보낸다", async () => {
    await openStandby();
    await pick("로봇명을 선택하세요", "R296");
    fireEvent.change(screen.getByPlaceholderText("25자(50byte) 이내로 작성하세요"), { target: { value: "단발" } });
    await pick(WORK_TYPE_ALL.label, "길 안내");
    await click(screen.getByText("관제 · 태블릿 버튼"));
    await click(screen.getByText("저장"));
    expect(ctx.posted[0]).toMatchObject({ StandbyScenarioCode: null, ReturnPlaceId: null });
    expect(ctx.posted[0]).toMatchObject({ StandbyResponseMode: null });
  });

  it("마이크가 있는 로봇은 '단순 동작'을 고를 수 있고, 저장하면 mode=motion·마이크·시나리오 없음으로 보낸다", async () => {
    ctx.mic = true;
    await openStandby();
    await pick("로봇명을 선택하세요", "R296");
    expect(await options("단발 응대 (목적지 안내)")).toContain("단순 동작 (앉아·일어서)");
    await pick("단발 응대 (목적지 안내)", "단순 동작 (앉아·일어서)");
    expect(screen.getByText(/"큐리야, 앉아" 또는 "큐리야, 일어서"/)).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("25자(50byte) 이내로 작성하세요"), { target: { value: "전시 동작" } });
    await pick(WORK_TYPE_ALL.label, "길 안내");
    await click(screen.getByText("저장"));
    expect(ctx.posted).toHaveLength(1);
    expect(ctx.posted[0]).toMatchObject({
      ScheduleMode: "standby", StandbyTrigger: "mic", StandbyResponseMode: "motion", StandbyScenarioCode: null,
    });
  });

  it("'단순 동작'을 고른 뒤 호출 방식을 버튼으로 바꾸면 단발 응대로 돌아간다", async () => {
    ctx.mic = true;
    await openStandby();
    await pick("로봇명을 선택하세요", "R296");
    await pick("단발 응대 (목적지 안내)", "단순 동작 (앉아·일어서)");
    await click(screen.getByText("관제 · 태블릿 버튼"));
    expect(screen.queryByText("단순 동작 (앉아·일어서)")).toBeNull();
    expect(screen.getByText("단발 응대 (목적지 안내)")).toBeTruthy();
  });

  it("마이크가 없는 로봇에는 '단순 동작' 선택지가 없다", async () => {
    await openStandby();
    await pick("로봇명을 선택하세요", "R296");
    expect(await options("단발 응대 (목적지 안내)")).not.toContain("단순 동작 (앉아·일어서)");
  });
});
