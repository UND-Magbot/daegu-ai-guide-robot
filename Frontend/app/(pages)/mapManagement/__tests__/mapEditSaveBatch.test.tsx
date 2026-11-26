/**
 * 맵 편집 저장 회귀 테스트 2 (2026-10-16): 저장 중 재클릭, 이미 지워진 항목(404), 중복 구간, 되돌리기.
 * 실제 page.tsx 를 jsdom 에 렌더하고 서버(apiFetch)만 메모리 가짜로 바꿔 서버 요청·알림을 검사한다.
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
type Route = { id: number; MapId: number; StartPlaceName: string; EndPlaceName: string; Direction: string };
const srv = {
  places: [] as Place[],
  routes: [] as Route[],
  puts: [] as { id: number; body: Record<string, unknown>; status: number }[],
  calls: [] as string[],
  failPut: new Map<number, number[]>(),
  delayMs: 0,
  nextId: 1000,
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
  srv.calls = [];
  srv.failPut = new Map();
  srv.delayMs = 0;
  srv.nextId = 1000;
}
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function fakeApi(path: string, init?: RequestInit): Promise<Response> {
  const method = (init?.method ?? "GET").toUpperCase();
  const [p, q = ""] = path.split("?");
  const qs = new URLSearchParams(q);
  if (method !== "GET") {
    srv.calls.push(`${method} ${p}`);
    if (srv.delayMs) await sleep(srv.delayMs);
  }
  if (p === "/DB/places" && method === "GET") {
    const mapId = qs.get("map_id");
    return json(srv.places.filter((x) => !mapId || x.MapId === Number(mapId)).map((x) => ({ ...x })));
  }
  if (p === "/DB/places" && method === "POST") {
    const body = JSON.parse(String(init?.body));
    if (srv.places.some((x) => x.LacationName === body.LacationName)) return json({ detail: "dup" }, 409);
    const row = { ...body, id: srv.nextId++ };
    srv.places.push(row);
    return json(row);
  }
  const placeM = p.match(/^\/DB\/places\/(\d+)$/);
  if (placeM && method === "PUT") {
    const id = Number(placeM[1]);
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
  if (placeM && method === "DELETE") {
    const id = Number(placeM[1]);
    const i = srv.places.findIndex((x) => x.id === id);
    if (i < 0) return json({ detail: "Place not found" }, 404);
    srv.places.splice(i, 1);
    return json({ status: "deleted" });
  }
  if (p === "/DB/routes" && method === "GET") return json(srv.routes.filter((r) => r.MapId === Number(qs.get("map_id"))));
  if (p === "/DB/routes" && method === "POST") {
    const body = JSON.parse(String(init?.body));
    const row = { ...body, id: srv.nextId++ };
    srv.routes.push(row);
    return json(row);
  }
  const routeM = p.match(/^\/DB\/routes\/(\d+)$/);
  if (routeM && method === "DELETE") {
    const id = Number(routeM[1]);
    const i = srv.routes.findIndex((x) => x.id === id);
    if (i < 0) return json({ detail: "Route not found" }, 404);
    srv.routes.splice(i, 1);
    return json({ status: "deleted" });
  }
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
const seenAlerts: string[] = [];
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
  default: (p: { open: boolean; message: string; mode: string; onConfirm: () => void; onClose: () => void }) => {
    if (p.open && seenAlerts[seenAlerts.length - 1] !== p.message) seenAlerts.push(p.message);
    return p.open ? (
      <div data-testid="alert" data-mode={p.mode}>
        {p.message}
        <button onClick={p.onConfirm}>알림-예</button>
        <button onClick={p.onClose}>알림-아니오</button>
      </div>
    ) : null;
  },
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
  default: (p: { onSaveAll: () => void }) => (
    <div><button onClick={p.onSaveAll}>상단저장</button></div>
  ),
}));
vi.mock("../hooks/useRobotPolling", () => ({ useRobotPolling: () => {} }));
vi.mock("../hooks/useMappingWebSocket", () => ({ useMappingWebSocket: () => ({ mappingCanvasRef: { current: null } }) }));
vi.mock("../hooks/useFloorMapConfig", () => ({ useFloorMapConfig: () => null }));
vi.mock("../utils/processMapImage", () => ({ processMapImage: () => ({ url: "img", w: 400, h: 400 }) }));

import MapManagementPage from "../page";

class FakeImage {
  onload: (() => void) | null = null;
  crossOrigin = "";
  set src(_v: string) { setTimeout(() => this.onload?.(), 0); }
}

const tick = (ms = 50) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });

beforeEach(() => {
  resetServer();
  seenAlerts.length = 0;
  vi.stubGlobal("Image", FakeImage);
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => setTimeout(() => cb(0), 0));
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 800, width: 800, height: 800, toJSON: () => ({}),
  } as DOMRect);
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function openPage(strict = false) {
  render(strict ? <React.StrictMode><MapManagementPage /></React.StrictMode> : <MapManagementPage />);
  await waitFor(() => screen.getByText("D"), { timeout: 3000 });
  await tick(30);
}
const marker = (name: string) => screen.getByText(name, { selector: "text" });
const tool = (label: string) => screen.getByText(label, { selector: "span" }).closest("button")!;
const pendingRouteLines = () =>
  document.querySelectorAll('line[stroke="#FFD700"]').length;
const routesOnServer = (s: string, e: string) =>
  srv.routes.filter((r) => r.StartPlaceName === s && r.EndPlaceName === e).length;

function addRoute(from: string, to: string) {
  fireEvent.click(tool("구간"));
  fireEvent.click(marker(from));
  fireEvent.click(marker(to));
  fireEvent.click(screen.getByRole("button", { name: "확인" }));
}
async function deleteRoute(from: string, to: string) {
  fireEvent.click(tool("삭제"));
  const lines = Array.from(document.querySelectorAll('line[stroke="transparent"]'));
  for (const l of lines) {
    fireEvent.click(l);
    const pop = screen.queryByText(`${from} → ${to}`);
    if (pop) {
      const btns = screen.getAllByRole("button", { name: "삭제" });
      fireEvent.click(btns[btns.length - 1]);
      return true;
    }
    fireEvent.click(screen.getByRole("button", { name: "취소" }));
  }
  return false;
}
async function saveAndClose(): Promise<string> {
  fireEvent.click(screen.getByText("상단저장"));
  const el = await waitFor(() => screen.getByTestId("alert"), { timeout: 3000 });
  const msg = el.textContent!.replace(/알림-예알림-아니오$/, "");
  fireEvent.click(screen.getByText("알림-예"));
  await tick(30);
  return msg;
}

async function deletePlace(name: string) {
  fireEvent.click(tool("삭제"));
  fireEvent.click(marker(name));
  const btns = screen.getAllByRole("button", { name: "삭제" });
  fireEvent.click(btns[btns.length - 1]);
  fireEvent.click(tool("삭제")); // 삭제 모드 끄기
}
const tabWarning = async () => {
  fireEvent.click(screen.getByText("장소 목록"));
  await tick(30);
  const t = screen.queryByTestId("alert")?.textContent?.replace(/알림-예알림-아니오$/, "") ?? null;
  if (t) fireEvent.click(screen.getByText("알림-아니오"));
  return t;
};
const GONE = "다른 곳에서 이미 삭제된 항목이 1건 있습니다";

describe("맵 편집 저장 — 중복 저장·중복 구간·되돌리기", () => {
  it("저장 중에 저장을 다시 눌러도 요청은 한 번만 나간다", async () => {
    await openPage();
    addRoute("A", "C");
    await deleteRoute("C", "B");
    srv.delayMs = 60; // 요청마다 60ms — 저장이 끝나기 전에 두 번째 클릭
    fireEvent.click(screen.getByText("상단저장"));
    await tick(20);
    fireEvent.click(screen.getByText("상단저장"));
    await tick(600);
    expect(srv.calls).toEqual(["DELETE /DB/routes/2", "POST /DB/routes"]);
    expect(routesOnServer("A", "C")).toBe(1);
    expect(seenAlerts).toEqual(["저장되었습니다."]);
  });

  it("이미 있는 구간은 만들 때 막는다", async () => {
    await openPage();
    addRoute("A", "B");
    expect(seenAlerts.at(-1)).toContain("\"A → B\" 구간이 이미 있습니다");
    fireEvent.click(screen.getByText("알림-예"));
    expect(pendingRouteLines()).toBe(0);
    expect(await saveAndClose()).toBe("저장할 변경사항이 없습니다.");
    expect(routesOnServer("A", "B")).toBe(1);
  });

  it("되돌리기는 마지막 장소 수정부터 취소한다", async () => {
    await openPage();
    addRoute("A", "C");
    fireEvent.doubleClick(marker("D"));
    await waitFor(() => screen.getByPlaceholderText("0~360"));
    fireEvent.change(screen.getByPlaceholderText("100자 이내로 작성하세요"), { target: { value: "d-수정" } });
    fireEvent.click(screen.getByRole("button", { name: "확인" }));
    await tick(30);
    fireEvent.click(tool("되돌리기"));
    await tick(30);
    expect(pendingRouteLines()).toBe(1); // 구간 추가는 그대로
    await saveAndClose();
    expect(routesOnServer("A", "C")).toBe(1);
    expect(srv.puts).toHaveLength(0); // 설명 수정은 취소돼 PUT 없음
    expect(srv.places.find((x) => x.id === 4)?.Imformation).toBe("d");
  });

  it.each([true, false])("구간 추가 → 삭제 → 되돌리기는 구간 1개로 돌아온다 (StrictMode=%s)", async (strict) => {
    await openPage(strict);
    addRoute("A", "C");
    await deleteRoute("A", "C");
    expect(pendingRouteLines()).toBe(0);
    fireEvent.click(tool("되돌리기"));
    await tick(30);
    expect(pendingRouteLines()).toBe(1);
    await saveAndClose();
    expect(routesOnServer("A", "C")).toBe(1);
  });
});

describe("맵 편집 저장 — 다른 곳에서 이미 지워진 항목", () => {
  it("구간이 먼저 지워졌으면 따로 알리고 다시 시도하지 않는다", async () => {
    await openPage();
    await deleteRoute("C", "B");
    srv.routes = srv.routes.filter((r) => r.id !== 2);
    const msg = await saveAndClose();
    expect(msg).toContain("저장을 마쳤습니다");
    expect(msg).toContain(GONE);
    expect(msg).toContain("구간 \"C→B\"");
    expect(await saveAndClose()).toBe("저장할 변경사항이 없습니다.");
    expect(await tabWarning()).toBeNull();
  });

  it("섞인 경우에도 나머지는 저장되고 이미 없는 것만 알린다", async () => {
    await openPage();
    await deleteRoute("C", "B");
    fireEvent.click(tool("삭제")); // deleteRoute 가 켜 둔 삭제 모드 끄기
    await deletePlace("D");
    addRoute("A", "C");
    srv.routes = srv.routes.filter((r) => r.id !== 2);
    expect(await saveAndClose()).toContain(GONE);
    expect(srv.places.some((p) => p.id === 4)).toBe(false);
    expect(routesOnServer("A", "C")).toBe(1);
    expect(await saveAndClose()).toBe("저장할 변경사항이 없습니다.");
  });

  it("장소가 먼저 지워졌을 때도 같다", async () => {
    await openPage();
    await deletePlace("D");
    srv.places = srv.places.filter((p) => p.id !== 4);
    const msg = await saveAndClose();
    expect(msg).toContain(GONE);
    expect(msg).toContain("장소 \"D\"");
    expect(await saveAndClose()).toBe("저장할 변경사항이 없습니다.");
    expect(await tabWarning()).toBeNull();
  });

  it("진짜 실패는 남겨 재시도하고, 이미 없는 것은 함께 알린다", async () => {
    srv.failPut.set(1, [500]);
    await openPage();
    fireEvent.doubleClick(marker("A"));
    await waitFor(() => screen.getByPlaceholderText("0~360"));
    fireEvent.change(screen.getByPlaceholderText("100자 이내로 작성하세요"), { target: { value: "a2" } });
    fireEvent.click(screen.getByRole("button", { name: "확인" }));
    await tick(30);
    await deleteRoute("C", "B");
    srv.routes = srv.routes.filter((r) => r.id !== 2);
    const msg1 = await saveAndClose();
    expect(msg1).toContain("1건을 저장하지 못했습니다");
    expect(msg1).toContain(GONE);
    expect(await saveAndClose()).toBe("저장되었습니다.");
    expect(srv.places[0].Imformation).toBe("a2");
  });

  it("아무 일도 없으면 그냥 저장되었습니다", async () => {
    await openPage();
    await deleteRoute("C", "B");
    expect(await saveAndClose()).toBe("저장되었습니다.");
    expect(routesOnServer("C", "B")).toBe(0);
  });
});
