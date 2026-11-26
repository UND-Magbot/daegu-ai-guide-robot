import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ── 가짜 API ────────────────────────────────
const ctx = vi.hoisted(() => ({
  places: [] as unknown[],
  block: null as unknown,
  apiFetch: vi.fn(),
  showToast: vi.fn(),
}));

vi.mock("@/app/lib/api", () => ({ apiFetch: ctx.apiFetch }));
vi.mock("@/app/lib/navReach", () => ({ fetchMoveBlock: vi.fn(async () => ctx.block) }));
vi.mock("@/app/components/common/Toast", () => ({ useToast: () => ({ showToast: ctx.showToast }) }));
vi.mock("@/app/hooks/useCustomScrollbar", () => ({ useCustomScrollbar: () => {} }));
vi.mock("@/app/hooks/useModalBehavior", () => ({ useModalBehavior: () => {} }));

import PlacePathModal from "@/app/components/modal/PlacePathModal";

const place = (id: number, name: string) => ({
  id, LacationName: name, Floor: "1F", FloorId: 1, MapId: 47, Category: "work",
  LocationX: id, LocationY: 0, Yaw: 0, RobotName: "R296",
});

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  ctx.places = [place(1, "간호실"), place(2, "수납")];
  ctx.block = null;
  ctx.apiFetch.mockReset();
  ctx.apiFetch.mockImplementation(async (url: string) => ({
    ok: true,
    json: async () => (url === "/DB/places" ? ctx.places : { status: "ok" }),
  }));
});

afterEach(() => cleanup());

function open() {
  return render(<PlacePathModal isOpen onClose={() => {}} selectedRobotIds={[10]} robotFloorId={1} robotMapId={47} />);
}

describe("장소 이동 창 — 노드망으로 갈 수 없는 장소", () => {
  it("노드망 경로가 꺼져 있으면(막힘 정보 null) 모든 장소를 고를 수 있다", async () => {
    open();
    await flush();
    const opts = screen.getAllByRole("option");
    expect(opts).toHaveLength(2);
    expect(opts.every((o) => o.getAttribute("aria-disabled") === "false")).toBe(true);
  });

  it("갈 수 없는 장소는 흐리게·선택 불가·사유 툴팁", async () => {
    ctx.block = { map: null, places: { 2: "'수납' — 한쪽 방향 연결 때문에 충전소에서 갈 수 없습니다." } };
    open();
    await flush();
    const blocked = screen.getByText("수납").closest("[role=option]")!;
    expect(blocked.getAttribute("aria-disabled")).toBe("true");
    expect(blocked.getAttribute("title")).toContain("한쪽 방향");
    fireEvent.click(blocked);
    expect(blocked.getAttribute("aria-selected")).toBe("false");
    const ok = screen.getByText("간호실").closest("[role=option]")!;
    fireEvent.click(ok);
    expect(ok.getAttribute("aria-selected")).toBe("true");
  });

  it("노드망 미완성 맵이면 목록 대신 이유를 보인다", async () => {
    ctx.block = { map: "이 맵은 노드망이 완성되지 않아 자율 이동을 할 수 없습니다. 노드가 없습니다.", places: {} };
    open();
    await flush();
    expect(screen.getByText(/노드망이 완성되지 않아/)).toBeTruthy();
    expect(screen.queryAllByRole("option")).toHaveLength(0);
  });
});
