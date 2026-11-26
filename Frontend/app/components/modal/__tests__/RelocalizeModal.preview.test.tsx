// 위치 재조정 창 — 놓으면 미리보기(주입 안 함)·자동 맞춤·적용·맞음 점수(11-05).
// 같은 파일을 현행 사본에도 넣어 돌려 현행 vs 변경을 비교한다(presim).
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

type Call = { path: string; method: string; body: Record<string, unknown> | null };
const calls: Call[] = [];
// 층 도착 시드 응답의 scan 부분(테스트마다 바꿈)
let lobbyScan: unknown = { ok: true, fit: { x: 1.0, y: 2.0, yaw: 0.0, score: 90 } };
// 가짜 중계기가 보내는 스캔 출처 — "lidar"(원본) / undefined(구판 중계기 = /LOC_BODY_POINTS)
let wsSource: string | undefined = "lidar";
// 스트림 준비(중계기 기동) 지연·결과, 스캔 순서(준비 뒤 after ms 에 그 출처의 스캔이 온다)
let streamDelay = 0;
let streamOk = true;
let wsSeq: { after: number; source: string | undefined; n: number }[] | null = null;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

vi.mock("@/app/lib/relocalizeOpen", () => ({ markRelocalizeOpen: () => () => {} }));

// 지도 캔버스 대신 드래그를 흉내 내는 버튼
vi.mock("@/app/components/map/CanvasMap", () => ({
  default: (p: {
    onPoseDragStart?: (w: { x: number; y: number }) => void;
    onPoseDrag?: (w: { x: number; y: number }) => void;
    onPoseDragEnd?: () => void;
    previewScanPoints?: number[][];
  }) => (
    <div>
      <button
        data-testid="drag-long"
        onClick={() => {
          p.onPoseDragStart?.({ x: 0.8, y: 1.6 });
          p.onPoseDrag?.({ x: 1.8, y: 1.6 });
          p.onPoseDragEnd?.();
        }}
      />
      <button
        data-testid="drag-short"
        onClick={() => {
          p.onPoseDragStart?.({ x: 0.8, y: 1.6 });
          p.onPoseDrag?.({ x: 0.9, y: 1.6 });
          p.onPoseDragEnd?.();
        }}
      />
      <span data-testid="preview-count">{p.previewScanPoints?.length ?? 0}</span>
    </div>
  ),
}));

vi.mock("@/app/lib/api", () => ({
  apiFetch: vi.fn(async (path: string, init?: { method?: string; body?: string }) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(init.body) : null;
    calls.push({ path, method, body });
    const json = (d: unknown) => ({ ok: true, json: async () => d });
    if (path.startsWith("/robot/position")) {
      return json({ x: 0, y: 0, yaw: 0, timestamp: Date.now() / 1000, location: 0 });
    }
    if (path.startsWith("/robot/relocalize/match")) {
      return json(
        body?.refine
          ? {
              status: "ok",
              n: 300,
              at: { score: 55, mean_err: 0.2 },
              refined: { x: 1.0, y: 2.0, yaw: 0.5, score: 88, mean_err: 0.03, ambiguous: true,
                         alt: { x: 3.0, y: 2.0, yaw: 0.5, score: 87 } },
            }
          : { status: "ok", n: 300, at: { score: 55, mean_err: 0.2 } }
      );
    }
    if (path.startsWith("/robot/relocalize/stream/start")) {
      await sleep(streamDelay);
      return json(streamOk ? { status: "ok" } : { status: "error", msg: "relay_map 기동 실패" });
    }
    if (path.startsWith("/robot/relocalize/stream")) return json({ status: "ok" });
    if (path.startsWith("/robot/initpose/elevator-lobby")) {
      return json({ status: "ok", transport_ok: true, seed: { name: "1F_엘리베이터입구", x: 5.4, y: -10.3, yaw: -1.2, scan: lobbyScan } });
    }
    if (path.startsWith("/robot/initpose") && method === "POST") return json({ status: "ok", converged: true });
    if (path.startsWith("/robot/initpose")) return json(null);
    return json({});
  }),
}));

class FakeWS {
  static last: FakeWS | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    FakeWS.last = this;
    setTimeout(() => this.onopen?.(), 0);
    const seq = wsSeq ?? [{ after: 0, source: wsSource, n: 120 }];
    if (!streamOk) return;                       // 중계기가 안 떴으면 스캔도 없다
    for (const it of seq) {
      setTimeout(() => {
        const pts = Array.from({ length: it.n }, (_, i) => [Math.cos(i / 19) * 3, Math.sin(i / 19) * 3]);
        this.onmessage?.({ data: JSON.stringify({ type: "body", points: pts, source: it.source }) });
      }, streamDelay + it.after);
    }
  }
  close() {}
}

import RelocalizeModal from "@/app/components/modal/RelocalizeModal";

const mapConfig = { imageSrc: "x.png", resolution: 0.1, originX: 0, originY: 0, pixelWidth: 100, pixelHeight: 100 };
const injects = () => calls.filter((c) => c.path.split("?")[0] === "/robot/initpose" && c.method === "POST");
const lobbyCalls = () => calls.filter((c) => c.path.startsWith("/robot/initpose/elevator-lobby"));
const matches = (refine: boolean) => calls.filter((c) => c.path.startsWith("/robot/relocalize/match") && !!c.body?.refine === refine);

async function openModal() {
  render(<RelocalizeModal open mapConfig={mapConfig} robotId={10} robotName="SIM" onClose={() => {}} />);
  // 스캔·위치가 들어와 미리보기 계산이 가능해질 때까지
  await waitFor(() => expect(calls.some((c) => c.path.startsWith("/robot/position"))).toBe(true));
  await act(async () => {
    await new Promise((r) => setTimeout(r, 50));
  });
}

beforeEach(() => {
  calls.length = 0;
  lobbyScan = { ok: true, fit: { x: 1.0, y: 2.0, yaw: 0.0, score: 90 } };
  wsSource = "lidar";
  streamDelay = 0;
  streamOk = true;
  wsSeq = null;
  (globalThis as unknown as { WebSocket: unknown }).WebSocket = FakeWS;
});

describe("RelocalizeModal 미리보기·자동 맞춤", () => {
  it("P1 길게 끌어 놓으면 주입하지 않고 미리보기·점수 요청만", async () => {
    await openModal();
    fireEvent.click(screen.getByTestId("drag-long"));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(injects()).toHaveLength(0);
    expect(screen.getByTestId("proposal-panel")).toBeTruthy();
    expect(Number(screen.getByTestId("preview-count").textContent)).toBeGreaterThan(0);
    await waitFor(() => expect(matches(false).some((c) => c.body?.x === 0.8 && c.body?.y === 1.6)).toBe(true));
  });

  it("P2 자동 맞춤 → 맞춘 위치로 미리보기가 바뀌고 모호 경고", async () => {
    await openModal();
    fireEvent.click(screen.getByTestId("drag-long"));
    fireEvent.click(await screen.findByText("자동 맞춤"));
    await waitFor(() => expect(matches(true)).toHaveLength(1));
    expect(matches(true)[0].body).toMatchObject({ x: 0.8, y: 1.6, refine: true });
    await screen.findByText(/미리보기 \(1\.00, 2\.00, 29°\)/);
    expect(screen.getByText(/비슷하게 맞는 곳이 또 있습니다/)).toBeTruthy();
    expect(injects()).toHaveLength(0);
  });

  it("P3 '이 위치로 적용' → 맞춘 위치로 한 번 주입", async () => {
    await openModal();
    fireEvent.click(screen.getByTestId("drag-long"));
    fireEvent.click(await screen.findByText("자동 맞춤"));
    await screen.findByText(/미리보기 \(1\.00, 2\.00/);
    fireEvent.click(screen.getByText("이 위치로 적용"));
    await waitFor(() => expect(injects()).toHaveLength(1));
    expect(injects()[0].body).toMatchObject({ x: 1.0, y: 2.0, yaw: 0.5, robot_id: 10 });
  });

  it("P4 지금 위치 맞음 점수 배지 + '정상'인데 낮으면 경고", async () => {
    await openModal();
    await screen.findByText(/지도와 맞음 55% · 낮음/, {}, { timeout: 3000 }); // 첫 점수는 스캔 뒤 다음 주기(1.5s)
    await screen.findByText(/지도와 맞는 정도는 55%/, {}, { timeout: 5000 });
  }, 8000);

  it("P5 (회귀) 짧게 끌면 주입도 미리보기도 없음", async () => {
    await openModal();
    fireEvent.click(screen.getByTestId("drag-short"));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(injects()).toHaveLength(0);
    expect(screen.queryByTestId("proposal-panel")).toBeNull();
    expect(screen.getByText(/방향이 정해지지 않/)).toBeTruthy();
  });

  it("P6 층 도착 — 첫 스캔을 기다렸다가 스캔을 실어 시드 요청(방향은 서버가 스캔으로 정함)", async () => {
    render(<RelocalizeModal open mapConfig={mapConfig} robotId={10} robotName="SIM" seedElevatorLobby onClose={() => {}} />);
    await waitFor(() => expect(lobbyCalls()).toHaveLength(1), { timeout: 3000 });
    const pts = lobbyCalls()[0].body?.points as number[][] | undefined;
    expect(pts?.length).toBe(120);
    expect(screen.queryByTestId("lobby-heading-note")).toBeNull();
    expect(injects()).toHaveLength(0);
  });

  it("P7 층 도착 — 서버가 방향을 못 정하면 확인 안내 + 최선 자리를 미리보기로(추가 주입 없음)", async () => {
    lobbyScan = { ok: false, reason: "비슷하게 맞는 방향이 또 있음", fit: { x: 5.5, y: -10.2, yaw: 1.57, score: 70 } };
    render(<RelocalizeModal open mapConfig={mapConfig} robotId={10} robotName="SIM" seedElevatorLobby onClose={() => {}} />);
    await screen.findByTestId("lobby-heading-note", {}, { timeout: 3000 });
    expect(screen.getByText(/비슷하게 맞는 방향이 또 있음/)).toBeTruthy();
    await screen.findByText(/미리보기 \(5\.50, -10\.20, 90°\)/);
    expect(lobbyCalls()).toHaveLength(1);
    expect(injects()).toHaveLength(0);
  });

  it("P8 원본 스캔이 끝내 안 옴(측위 스캔만) — 기한 뒤 저장 방향 + 안내, 점수·자동 맞춤 없음, 미리보기·적용은 그대로", async () => {
    wsSource = undefined;
    render(<RelocalizeModal open mapConfig={mapConfig} robotId={10} robotName="SIM" seedElevatorLobby lobbyScanWaitMs={600} onClose={() => {}} />);
    await waitFor(() => expect(lobbyCalls()).toHaveLength(1), { timeout: 3000 });
    expect(lobbyCalls()[0].body).toBeNull();                 // 스캔을 싣지 않음 → 서버는 저장 방향
    await screen.findByText(/라이다 원본 스캔이 오지 않아 저장된 방향으로/);
    fireEvent.click(screen.getByTestId("drag-long"));
    await screen.findByTestId("proposal-panel");
    expect(screen.queryByText("자동 맞춤")).toBeNull();
    expect(screen.queryByText(/지도와 맞음/)).toBeNull();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 1700));      // 점수 주기(1.5s)가 지나도
    });
    expect(calls.filter((c) => c.path.startsWith("/robot/relocalize/match"))).toHaveLength(0);
    fireEvent.click(screen.getByText("이 위치로 적용"));
    await waitFor(() => expect(injects()).toHaveLength(1));
  }, 8000);

  it("P9 [재현] 측위 스캔이 먼저, 원본이 0.5초 뒤 → 원본을 기다려 실어 보냄", async () => {
    wsSeq = [{ after: 0, source: undefined, n: 120 }, { after: 500, source: "lidar", n: 90 }];
    render(<RelocalizeModal open mapConfig={mapConfig} robotId={10} robotName="SIM" seedElevatorLobby onClose={() => {}} />);
    await waitFor(() => expect(lobbyCalls()).toHaveLength(1), { timeout: 4000 });
    expect((lobbyCalls()[0].body?.points as number[][] | undefined)?.length).toBe(90);
    expect(screen.queryByTestId("lobby-heading-note")).toBeNull();
  }, 8000);

  it("P10 [재현] 스트림 준비가 8.5초 걸림(11-05 실기 약 8초) → 준비 뒤 원본을 받아 실어 보냄", async () => {
    streamDelay = 8500;
    render(<RelocalizeModal open mapConfig={mapConfig} robotId={10} robotName="SIM" seedElevatorLobby onClose={() => {}} />);
    await screen.findByText(/로봇 스캔을 기다리는 중/, {}, { timeout: 2000 });
    await waitFor(() => expect(lobbyCalls()).toHaveLength(1), { timeout: 12000 });
    expect((lobbyCalls()[0].body?.points as number[][] | undefined)?.length).toBe(120);
  }, 16000);

  it("P11 스트림 준비 실패 → 기다리지 않고 저장 방향 + '스캔이 오지 않아' 안내", async () => {
    streamOk = false;
    render(<RelocalizeModal open mapConfig={mapConfig} robotId={10} robotName="SIM" seedElevatorLobby onClose={() => {}} />);
    await waitFor(() => expect(lobbyCalls()).toHaveLength(1), { timeout: 3000 });
    expect(lobbyCalls()[0].body).toBeNull();
    await screen.findByText(/스캔이 오지 않아 저장된 방향으로/);
  }, 8000);
});
