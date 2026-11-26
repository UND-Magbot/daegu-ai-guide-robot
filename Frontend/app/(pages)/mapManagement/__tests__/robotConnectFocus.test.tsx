/**
 * 메모 11-03: 맵 편집 › 로봇 연결 → 연결한 로봇이 있는 층·맵으로 상단 선택을 맞춘다.
 * 로봇의 층·맵을 모르면 지금 층의 활성 맵(로봇이 쓰는 맵, 없으면 최근 맵)으로 맞춘다.
 * 실제 page.tsx 를 렌더하고 서버(apiFetch)만 메모리 가짜로 바꾼다(mapEditSaveBatch.test.tsx 와 같은 방식).
 */
import React from "react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup, act } from "@testing-library/react";

type R = { id: number; RobotName: string; CurrentFloorId: number | null; CurrentMapId: number | null };
const srv = { robots: [] as R[] };
const FLOORS = [
  { id: 10, FloorName: "1F", BusinessId: 1 },
  { id: 20, FloorName: "2F", BusinessId: 1 },
];
const MAPS = [
  { id: 100, MapName: "M100", FloorId: 10, BusinessId: 1, ImgFilePath: "./m100.png", PgmFilePath: "./m100.pgm" },
  { id: 101, MapName: "M101", FloorId: 10, BusinessId: 1, ImgFilePath: "./m101.png", PgmFilePath: "./m101.pgm" },
  { id: 200, MapName: "M200", FloorId: 20, BusinessId: 1, ImgFilePath: "./m200.png", PgmFilePath: "./m200.pgm" },
];
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

let maps = [...MAPS];
async function fakeApi(path: string, init?: RequestInit): Promise<Response> {
  const [p, q = ""] = path.split("?");
  const qs = new URLSearchParams(q);
  const method = (init?.method ?? "GET").toUpperCase();
  if (p === "/map/mapping/start" && method === "POST") return json({ status: "ok" });
  if (p === "/map/mapping/end" && method === "POST") {
    const body = JSON.parse(String(init?.body));
    const row = { id: 201, MapName: body.MapName, FloorId: body.FloorId, BusinessId: body.BusinessId,
                  ImgFilePath: "./m201.png", PgmFilePath: "./m201.pgm" };
    maps.push(row);
    return json({ status: "ok", map_id: 201 });
  }
  if (p === "/map/businesses") return json([{ id: 1, BusinessName: "사업장" }]);
  if (p === "/map/floors") {
    const biz = qs.get("business_id");
    return json(FLOORS.filter((f) => !biz || f.BusinessId === Number(biz)));
  }
  if (p === "/map/maps") {
    const fl = qs.get("floor_id");
    return json(maps.filter((m) => !fl || m.FloorId === Number(fl)));
  }
  if (/^\/map\/maps\/\d+\/meta$/.test(p)) return json({ resolution: 0.05, originX: -10, originY: -10 });
  if (p === "/DB/places" || p === "/DB/routes") return json([]);
  if (p === "/DB/robots") return json(srv.robots.map((r) => ({ ...r })));
  if (p === "/robot/status") return json(srv.robots.map((r) => ({ robot_id: r.id, robot_name: r.RobotName, network: "Online" })));
  if (p === "/DB/getpath") return json([]);
  return json({}, 404);
}

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
  default: (p: { open: boolean; message: string; onConfirm: () => void }) =>
    p.open ? <div data-testid="alert">{p.message}<button onClick={p.onConfirm}>알림-예</button></div> : null,
}));
vi.mock("@/app/components/modal/InitPoseConfirmModal", () => ({ default: () => null }));
vi.mock("@/app/components/modal/RelocalizeModal", () => ({ default: () => null }));
vi.mock("../components/tabs/place/PlaceManageTab", () => ({ default: () => null }));
vi.mock("../components/tabs/path/PathManageTab", () => ({ default: () => null }));
vi.mock("../components/tabs/facility/FacilityTab", () => ({ default: () => null }));
vi.mock("../components/tabs/map/RobotConnectModal", () => ({
  default: (p: { isOpen: boolean; robots: R[]; onConnect: (ids: number[]) => void }) =>
    p.isOpen ? (
      <div data-testid="connect-modal">
        {p.robots.map((r) => (
          <button key={r.id} onClick={() => p.onConnect([r.id])}>연결-{r.RobotName}</button>
        ))}
      </div>
    ) : null,
}));
vi.mock("../components/tabs/map/MapSyncModal", () => ({ default: () => null }));
vi.mock("../components/tabs/map/MapSyncProgressModal", () => ({ default: () => null }));
type StartP = {
  isOpen: boolean; onStartBizChange: (id: number) => void; setStartFloorId: (id: number) => void;
  setStartMapName: (n: string) => void; onCheckMapName: () => void; onConfirm: () => void;
};
vi.mock("../components/tabs/map/MappingStartModal", () => ({
  default: (p: StartP) =>
    p.isOpen ? (
      <div>
        <button onClick={() => p.onStartBizChange(1)}>시작-사업장</button>
        <button onClick={() => p.setStartFloorId(20)}>시작-2F</button>
        <button onClick={() => p.setStartMapName("새맵")}>시작-이름</button>
        <button onClick={p.onCheckMapName}>시작-중복확인</button>
        <button onClick={p.onConfirm}>시작-확인</button>
      </div>
    ) : null,
}));
vi.mock("../components/tabs/map/MappingProgressModal", () => ({
  default: (p: { isOpen: boolean; onStart: () => void; onEnd: () => void }) =>
    p.isOpen ? (<div><button onClick={p.onStart}>매핑-시작</button><button onClick={p.onEnd}>매핑-종료</button></div>) : null,
}));
vi.mock("../components/tabs/map/MappingSuccessModal", () => ({
  default: (p: { isOpen: boolean; onConfirm: () => void }) =>
    p.isOpen ? <button onClick={p.onConfirm}>매핑-성공확인</button> : null,
}));
vi.mock("../components/tabs/map/PathBuildPanel", () => ({ default: () => null }));
vi.mock("../components/tabs/map/ImportMapModal", () => ({ default: () => null }));
vi.mock("../components/tabs/map/MapRightPanel", () => ({
  default: (p: { onMappingStart: () => void }) => <button onClick={p.onMappingStart}>맵핑시작열기</button>,
}));
vi.mock("../components/tabs/map/MapToolbar", () => ({
  default: (p: { selectedFloor: number | ""; selectedMap: number | ""; connectedRobots: R[]; onOpenRobotModal: () => void }) => (
    <div>
      <span data-testid="sel">{`floor=${p.selectedFloor} map=${p.selectedMap} conn=${p.connectedRobots.length}`}</span>
      <button onClick={p.onOpenRobotModal}>로봇연결열기</button>
    </div>
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
const sel = () => screen.getByTestId("sel").textContent;

beforeEach(() => {
  maps = [...MAPS];
  vi.stubGlobal("Image", FakeImage);
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => setTimeout(() => cb(0), 0));
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function connect(name: string) {
  render(<MapManagementPage />);
  await waitFor(() => expect(sel()).toBe("floor=10 map=100 conn=0"), { timeout: 3000 });
  await act(async () => { fireEvent.click(screen.getByText("로봇연결열기")); });
  await waitFor(() => screen.getByText(`연결-${name}`));
  await act(async () => { fireEvent.click(screen.getByText(`연결-${name}`)); });
  await tick(80);
}

describe("맵 편집 › 로봇 연결 → 층·맵 자동 맞춤", () => {
  it("다른 층(2F/M200)에 있는 로봇도 목록에 나오고, 연결하면 그 층·맵으로 바뀐다(연결은 유지)", async () => {
    srv.robots = [{ id: 2, RobotName: "R2", CurrentFloorId: 20, CurrentMapId: 200 }];
    await connect("R2");
    await waitFor(() => expect(sel()).toBe("floor=20 map=200 conn=1"));
  });

  it("이미 그 맵이면 그대로 둔다", async () => {
    srv.robots = [{ id: 1, RobotName: "R1", CurrentFloorId: 10, CurrentMapId: 100 }];
    await connect("R1");
    expect(sel()).toBe("floor=10 map=100 conn=1");
  });

  it("로봇 층·맵을 모르면 지금 층의 활성 맵(다른 로봇이 쓰는 M101)으로 맞춘다", async () => {
    srv.robots = [
      { id: 3, RobotName: "R3", CurrentFloorId: null, CurrentMapId: null },
      { id: 4, RobotName: "R4", CurrentFloorId: 10, CurrentMapId: 101 },
    ];
    await connect("R3");
    await waitFor(() => expect(sel()).toBe("floor=10 map=101 conn=1"));
  });

  it("층·맵을 모르고 그 층을 쓰는 로봇도 없으면 그 층의 최근 맵(M101)", async () => {
    srv.robots = [{ id: 3, RobotName: "R3", CurrentFloorId: null, CurrentMapId: null }];
    await connect("R3");
    await waitFor(() => expect(sel()).toBe("floor=10 map=101 conn=1"));
  });
});

describe("맵 편집 › 매핑 후 기본값", () => {
  it("매핑을 마치고 확인하면 상단 층·맵이 새로 만든 맵(2F / id 201)으로 맞춰진다", async () => {
    srv.robots = [{ id: 1, RobotName: "R1", CurrentFloorId: 10, CurrentMapId: 100 }];
    await connect("R1");
    const click = async (t: string) => {
      await act(async () => { fireEvent.click(screen.getByText(t)); });
      await tick(30);
    };
    await click("맵핑시작열기");
    for (const t of ["시작-사업장", "시작-2F", "시작-이름", "시작-중복확인", "시작-확인", "매핑-시작", "매핑-종료", "매핑-성공확인"])
      await click(t);
    await waitFor(() => expect(sel()).toBe("floor=20 map=201 conn=1"));
  });
});
