/**
 * 맵 편집 저장 회귀 테스트 (2026-10-16 점검·수정분).
 * 실제 page.tsx / MapPlaceCreateModal 을 jsdom 에 렌더하고 서버(apiFetch)만 메모리 가짜로 바꿔,
 * 수정 → 드래그 → 맵 전환 → 저장 실패 → 재저장 흐름에서 서버로 나간 PUT 본문을 검사한다.
 * 근거: docs/작업보고/2026-10-16/작업보고_2026-10-16_맵편집_저장누락·되돌아감_점검.md
 */
import React from "react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, act } from "@testing-library/react";

// ── 가짜 서버 ──
type Place = {
  id: number; LacationName: string; LocationX: number; LocationY: number; Yaw: number;
  RobotName: string; FloorId: number; MapId: number; Category: string; Imformation: string | null;
};
const srv = {
  places: [] as Place[],
  routes: [] as { id: number; MapId: number; StartPlaceName: string; EndPlaceName: string; Direction: string }[],
  puts: [] as { id: number; body: Record<string, unknown>; status: number }[],
  failPut: new Map<number, number[]>(), // id → 차례로 돌려줄 오류 상태(소진되면 성공)
};
function resetServer() {
  const base = { RobotName: "R1", FloorId: 10, Category: "waypoint" };
  srv.places = [
    { ...base, id: 1, LacationName: "A", LocationX: 0, LocationY: 0, Yaw: 1.2345, MapId: 100, Imformation: "a" },
    { ...base, id: 2, LacationName: "B", LocationX: 5, LocationY: 0, Yaw: 0, MapId: 100, Imformation: "b" },
    { ...base, id: 3, LacationName: "C", LocationX: 0, LocationY: 5, Yaw: 0.5, MapId: 100, Imformation: "c" },
    { ...base, id: 4, LacationName: "D", LocationX: -5, LocationY: -5, Yaw: 2.2222, MapId: 100, Imformation: "d" },
    { ...base, id: 5, LacationName: "E", LocationX: 1, LocationY: 1, Yaw: 0, MapId: 101, Imformation: "e" },
  ];
  srv.routes = [
    { id: 1, MapId: 100, StartPlaceName: "A", EndPlaceName: "B", Direction: "forward" },
    { id: 2, MapId: 100, StartPlaceName: "C", EndPlaceName: "B", Direction: "forward" },
  ];
  srv.puts = [];
  srv.failPut = new Map();
}
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

async function fakeApi(path: string, init?: RequestInit): Promise<Response> {
  const method = (init?.method ?? "GET").toUpperCase();
  const [p, q = ""] = path.split("?");
  const qs = new URLSearchParams(q);
  if (p === "/DB/places" && method === "GET") {
    const mapId = qs.get("map_id");
    return json(srv.places.filter((x) => !mapId || x.MapId === Number(mapId)).map((x) => ({ ...x })));
  }
  const putM = p.match(/^\/DB\/places\/(\d+)$/);
  if (putM && method === "PUT") {
    const id = Number(putM[1]);
    const body = JSON.parse(String(init?.body));
    const queue = srv.failPut.get(id);
    if (queue && queue.length > 0) {
      const st = queue.shift()!;
      srv.puts.push({ id, body, status: st });
      return json({ detail: "fail" }, st);
    }
    srv.puts.push({ id, body, status: 200 });
    const row = srv.places.find((x) => x.id === id)!;
    Object.assign(row, body);
    return json(row);
  }
  if (p === "/DB/routes" && method === "GET") return json(srv.routes.filter((r) => r.MapId === Number(qs.get("map_id"))));
  if (p === "/map/businesses") return json([{ id: 1, BusinessName: "사업장" }]);
  if (p === "/map/floors") return json([{ id: 10, FloorName: "1F", BusinessId: 1 }]);
  if (p === "/map/maps") {
    return json([
      { id: 100, MapName: "M100", FloorId: 10, ImgFilePath: "./m100.png", PgmFilePath: "./m100.pgm" },
      { id: 101, MapName: "M101", FloorId: 10, ImgFilePath: "./m101.png", PgmFilePath: "./m101.pgm" },
    ]);
  }
  if (/^\/map\/maps\/\d+\/meta$/.test(p)) return json({ resolution: 0.05, originX: -10, originY: -10 });
  if (p === "/DB/robots") return json([{ id: 1, RobotName: "R1" }]);
  if (p === "/robot/status" || p === "/DB/getpath") return json([]);
  return json({}, 404);
}

// ── 모듈 대체 ──
vi.mock("@/app/lib/api", () => ({ apiFetch: (p: string, i?: RequestInit) => fakeApi(p, i) }));
const searchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({ useSearchParams: () => searchParams }));
const auth = { hasPermission: () => true, isMenuVisible: () => true, menuIndex: new Map() };
vi.mock("@/app/context/AuthContext", () => ({ useAuth: () => auth }));
const noop = () => {};
vi.mock("@/app/context/PageLoadingContext", () => ({ usePageReady: () => noop }));
vi.mock("@/app/components/common/PermissionGuard", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/app/components/modal/AlertModal", () => ({
  default: (p: { open: boolean; message: string; mode: string; onConfirm: () => void; onClose: () => void }) =>
    p.open ? (
      <div data-testid="alert" data-mode={p.mode}>
        {p.message}
        <button onClick={p.onConfirm}>알림-예</button>
        <button onClick={p.onClose}>알림-아니오</button>
      </div>
    ) : null,
}));

vi.mock("@/app/components/modal/InitPoseConfirmModal", () => ({ default: () => null }));
vi.mock("@/app/components/modal/RelocalizeModal", () => ({ default: () => null }));
vi.mock("../components/tabs/place/PlaceManageTab", () => ({ default: () => null }));
vi.mock("../components/tabs/path/PathManageTab", () => ({ default: () => null }));
vi.mock("../components/tabs/facility/FacilityTab", () => ({ default: () => null }));
vi.mock("../components/tabs/map/RobotConnectModal", () => ({ default: () => null }));
vi.mock("../components/tabs/map/MapSyncModal", () => ({ default: () => null }));
vi.mock("../components/tabs/map/MapSyncProgressModal", () => ({ default: () => null }));
vi.mock("../components/tabs/map/MappingStartModal", () => ({ default: () => null }));
vi.mock("../components/tabs/map/MappingProgressModal", () => ({ default: () => null }));
vi.mock("../components/tabs/map/MappingSuccessModal", () => ({ default: () => null }));
vi.mock("../components/tabs/map/PathBuildPanel", () => ({ default: () => null }));
vi.mock("../components/tabs/map/ImportMapModal", () => ({ default: () => null }));
vi.mock("../components/tabs/map/MapRightPanel", () => ({ default: () => null }));
vi.mock("../components/tabs/map/MapToolbar", () => ({
  default: (p: { onSaveAll: () => void; onMapChange: (id: number) => void; selectedMap: number | "" }) => (
    <div>
      <span data-testid="cur-map">{String(p.selectedMap)}</span>
      <button onClick={p.onSaveAll}>상단저장</button>
      <button onClick={() => p.onMapChange(100)}>맵100</button>
      <button onClick={() => p.onMapChange(101)}>맵101</button>
    </div>
  ),
}));
vi.mock("../hooks/useRobotPolling", () => ({ useRobotPolling: () => {} }));
vi.mock("../hooks/useMappingWebSocket", () => ({ useMappingWebSocket: () => ({ mappingCanvasRef: { current: null } }) }));
vi.mock("../hooks/useFloorMapConfig", () => ({ useFloorMapConfig: () => null }));
vi.mock("../utils/processMapImage", () => ({ processMapImage: () => ({ url: "img", w: 400, h: 400 }) }));

import MapManagementPage from "../page";
import MapPlaceCreateModal from "../components/tabs/map/MapPlaceCreateModal";

// ── 화면 좌표 ↔ 월드 좌표 (svg 800x800, 이미지 400px, zoom 1.5 로 중앙 배치됨) ──
const toClient = (wx: number, wy: number) => ({
  clientX: (((wx + 10) / 0.05) - 200) * 1.5 + 400,
  clientY: ((400 - (wy + 10) / 0.05) - 200) * 1.5 + 400,
});

class FakeImage {
  onload: (() => void) | null = null;
  crossOrigin = "";
  set src(_v: string) { setTimeout(() => this.onload?.(), 0); }
}

const putsOf = (id: number) => srv.puts.filter((x) => x.id === id);
const lastPut = (id: number) => putsOf(id).at(-1)!;
// 저장 시 Yaw 를 소수 3자리로 보내므로 그 범위 안에서 비교한다.
const expectYaw = (actual: unknown, expected: number) =>
  expect(Math.abs(Number(actual) - expected)).toBeLessThanOrEqual(0.001);

beforeEach(() => {
  resetServer();
  vi.stubGlobal("Image", FakeImage);
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => setTimeout(() => cb(0), 0));
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 800, width: 800, height: 800, toJSON: () => ({}),
  } as DOMRect);
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

// ── 조작 도우미 ──
async function openPage() {
  render(<MapManagementPage />);
  await waitFor(() => screen.getByText("D"), { timeout: 3000 });
  await act(async () => { await new Promise((r) => setTimeout(r, 30)); }); // 중앙 배치 완료 대기
}
const marker = (name: string) => screen.getByText(name, { selector: "text" });
async function openEdit(name: string) {
  fireEvent.doubleClick(marker(name));
  await waitFor(() => screen.getByPlaceholderText("0~360"));
}
const dirInput = () => screen.getByPlaceholderText("0~360") as HTMLInputElement;
async function editPlace(name: string, o: { dir?: string; desc?: string; newName?: string }) {
  await openEdit(name);
  if (o.dir !== undefined) fireEvent.change(dirInput(), { target: { value: o.dir } });
  if (o.desc !== undefined) fireEvent.change(screen.getByPlaceholderText("100자 이내로 작성하세요"), { target: { value: o.desc } });
  if (o.newName !== undefined) fireEvent.change(screen.getByDisplayValue(name), { target: { value: o.newName } });
  fireEvent.click(screen.getByRole("button", { name: "확인" }));
  await waitFor(() => { if (screen.queryByPlaceholderText("0~360")) throw new Error("modal open"); });
}
function drag(name: string, from: [number, number], to: [number, number]) {
  const svg = document.querySelector("svg")!;
  const a = toClient(...from);
  const b = toClient(...to);
  fireEvent.mouseDown(marker(name), a);
  fireEvent.mouseMove(svg, { clientX: a.clientX + 20, clientY: a.clientY });
  fireEvent.mouseMove(svg, b);
  fireEvent.mouseUp(svg, b);
}
async function save(): Promise<string> {
  fireEvent.click(screen.getByText("상단저장"));
  const el = await waitFor(() => screen.getByTestId("alert"), { timeout: 3000 });
  const msg = el.textContent!.replace(/알림-예알림-아니오$/, "");
  fireEvent.click(screen.getByText("알림-예"));
  await act(async () => { await new Promise((r) => setTimeout(r, 30)); }); // 재조회 반영
  return msg;
}

const tick = (ms = 50) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });

describe("맵 편집 저장 — 방향(Yaw)·수정 내용 보존", () => {
  it("설명만 고치면 원래 방향이 정수 도로 반올림되지 않는다", async () => {
    await openPage();
    await editPlace("A", { desc: "a-수정" });
    expect(await save()).toBe("저장되었습니다.");
    expect(srv.puts).toHaveLength(1);
    expectYaw(lastPut(1).body.Yaw, 1.2345); // 반올림되면 71° = 1.239
    expect(lastPut(1).body.Imformation).toBe("a-수정");
  });

  it("모달에서 바꾼 방향은 드래그해도 유지되고, 안 바꾼 장소는 자동 재계산된다", async () => {
    await openPage();
    await editPlace("A", { dir: "90" });
    drag("A", [0, 0], [0, -3]);
    drag("C", [0, 5], [0, 3]); // C→B(5,0) 구간이 있어 B 쪽을 보게 다시 계산
    expect(await save()).toBe("저장되었습니다.");
    expectYaw(lastPut(1).body.Yaw, Math.PI / 2);
    expect(Math.abs(Number(lastPut(3).body.Yaw) - Math.atan2(0 - 3, 5 - 0))).toBeLessThan(0.01);
  });

  it("미저장 수정이 있으면 맵 전환 전에 묻고, '아니오'면 남아서 저장된다", async () => {
    await openPage();
    await editPlace("D", { dir: "45" });
    fireEvent.click(screen.getByText("맵101"));
    await tick();
    expect(screen.getByTestId("alert").textContent).toContain("다른 맵으로 바꾸면 사라집니다");
    fireEvent.click(screen.getByText("알림-아니오"));
    await tick();
    expect(screen.getByTestId("cur-map").textContent).toBe("100");
    expect(await save()).toBe("저장되었습니다.");
    expectYaw(lastPut(4).body.Yaw, Math.PI / 4);
  });

  it("맵 전환 확인창에서 '예'면 수정을 버리고 전환한다", async () => {
    await openPage();
    await editPlace("D", { dir: "45" });
    fireEvent.click(screen.getByText("맵101"));
    await tick();
    fireEvent.click(screen.getByText("알림-예"));
    await waitFor(() => screen.getByText("E"), { timeout: 3000 });
    await tick(30);
    expect(screen.getByTestId("cur-map").textContent).toBe("101");
    expect(await save()).toBe("저장할 변경사항이 없습니다.");
    expect(srv.puts).toHaveLength(0);
  });

  it("PUT 실패 뒤 다시 불러와도 고친 값이 남고, 재저장 때 그 값이 나간다", async () => {
    srv.failPut.set(1, [500]);
    await openPage();
    await editPlace("A", { dir: "90", desc: "a-수정" });
    expect(await save()).toContain("1건을 저장하지 못했습니다");
    await openEdit("A");
    expect(dirInput().value).toBe("90");
    expect((screen.getByPlaceholderText("100자 이내로 작성하세요") as HTMLTextAreaElement).value).toBe("a-수정");
    fireEvent.click(screen.getByRole("button", { name: /취소/ }));
    expect(await save()).toBe("저장되었습니다.");
    expect(lastPut(1).status).toBe(200);
    expectYaw(lastPut(1).body.Yaw, Math.PI / 2);
    expect(srv.places[0].Imformation).toBe("a-수정");
  });

  it("이름 변경 PUT 이 409 로 실패해도 재저장하면 새 이름으로 다시 보낸다", async () => {
    srv.failPut.set(1, [409]);
    await openPage();
    await editPlace("A", { newName: "A2", dir: "90" });
    expect(await save()).toContain("장소 \"A2\" 수정");
    expect(await save()).toBe("저장되었습니다.");
    expect(putsOf(1).map((x) => x.status)).toEqual([409, 200]);
    expect(srv.places[0].LacationName).toBe("A2");
    expectYaw(srv.places[0].Yaw, Math.PI / 2);
  });

  it("여러 장소를 한 번에 고쳐도 각자 맞는 값으로 저장된다", async () => {
    await openPage();
    await editPlace("A", { desc: "a2" });
    await editPlace("B", { dir: "180" });
    drag("C", [0, 5], [2, 5]);
    await editPlace("D", { dir: "270", desc: "d2" });
    expect(await save()).toBe("저장되었습니다.");
    expect(srv.puts.map((x) => x.id).sort()).toEqual([1, 2, 3, 4]);
    expectYaw(lastPut(1).body.Yaw, 1.2345);
    expect(lastPut(1).body.Imformation).toBe("a2");
    expectYaw(lastPut(2).body.Yaw, Math.PI);
    expect(Math.abs(Number(lastPut(3).body.Yaw) - Math.atan2(0 - 5, 5 - 2))).toBeLessThan(0.01);
    expectYaw(lastPut(4).body.Yaw, -Math.PI / 2);
    expect(lastPut(4).body.Imformation).toBe("d2");
  });

  it("로봇 위치로 등록하면 로봇 방향을 반올림하지 않는다", () => {
    let got: number | null = null;
    render(
      <MapPlaceCreateModal
        isOpen mode="create" worldX={1} worldY={2} pixelX={0} pixelY={0} mapId={100}
        defaultRobotName="R1" defaultFloor="1F" floors={[{ id: 10, FloorName: "1F" } as never]}
        lockCoords defaultYaw={0.123456}
        onClose={() => {}} onConfirm={(p) => { got = p.Yaw; }}
      />,
    );
    fireEvent.change(screen.getByPlaceholderText("50자 이내로 작성하세요"), { target: { value: "로봇위치" } });
    fireEvent.click(screen.getByRole("button", { name: "확인" }));
    expect(got).toBe(0.123456); // 반올림되면 7° = 0.1222
  });
});
