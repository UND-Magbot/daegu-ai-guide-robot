import { render, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiFetch = vi.fn();
vi.mock("@/app/lib/api", () => ({ apiFetch: (...a: unknown[]) => apiFetch(...a) }));

import NodeLayer from "../components/tabs/map/NodeLayer";
import { useNavNodes, type NavEdge } from "../hooks/useNavNodes";

const CHECK = { ready: true, reason: "", unlinkedPlaceIds: [7], issues: [], placeAttach: { "1": "A" } };
const NODES = [{ key: "A", x: 0, y: 0 }, { key: "B", x: 2, y: 0 }, { key: "C", x: 4, y: 0 }];
const EDGES: NavEdge[] = [
  { a: "A", b: "B", kind: "auto", direction: "both", doorId: null },
  { a: "B", b: "C", kind: "manual", direction: "forward", doorId: null },
  { a: "A", b: "C", kind: "excluded", direction: "both", doorId: null },
];
const PLACES = [{ id: 1, name: "원무과", x: 0, y: 1 }, { id: 7, name: "간호실", x: 4, y: 2 }];
const layer = (extra: Partial<React.ComponentProps<typeof NodeLayer>> = {}) => render(
  <svg>
    <NodeLayer nodes={NODES} edges={EDGES} check={CHECK as never} places={PLACES}
      meta={{ resolution: 0.05, originX: -10, originY: -10 }} img={{ w: 400, h: 400 }} zoom={1}
      active selectedKey={null} linkFromKey={null} routeKeys={[]} routeSkipped={[]} routeStart={null} routeGoal={null}
      {...extra} />
  </svg>,
);

describe("노드 편집 표시 — 길은 한 모양", () => {
  it("자동·수동 구분 없이 점선으로 그리지 않는다", () => {
    const { container } = layer();
    const dashed = Array.from(container.querySelectorAll("line")).filter((l) => {
      const d = l.getAttribute("stroke-dasharray");
      return d && d !== "none";
    });
    expect(dashed).toHaveLength(0);
  });

  it("막은 연결은 연결 도구에서만 보인다", () => {
    const hidden = layer();
    const seen = layer({ showExcluded: true });
    const count = (c: HTMLElement) => c.querySelectorAll('line[stroke="#90A4AE"]').length;
    expect(count(hidden.container)).toBe(0);
    expect(count(seen.container)).toBe(1);
  });

  it("연결 안 된 장소에 따로 표시(점선 원)를 두지 않는다 — 장소↔노드 선이 없는 것으로 알 수 있다", () => {
    const { container } = layer();
    expect(container.querySelectorAll('circle[stroke-dasharray]')).toHaveLength(0);
    expect(container.querySelectorAll('line[stroke="#546E7A"]')).toHaveLength(1);   // 원무과 → A 만
  });
});

describe("벽을 지나는 연결은 이을 때 막는다", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    apiFetch.mockImplementation((url: string, init?: { body?: string }) => {
      if (url.endsWith("/validate")) {
        const body = JSON.parse(init?.body ?? "{}");
        const wall = body.edges.some((e: { a: string; b: string; kind: string }) => e.kind === "manual" && [e.a, e.b].sort().join() === "A,C");
        return Promise.resolve(wall
          ? { ok: false, json: () => Promise.resolve({ detail: "두 노드 사이에 벽이 있어 이을 수 없습니다." }) }
          : { ok: true, json: () => Promise.resolve({ nodes: body.nodes, edges: EDGES, check: CHECK }) });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ nodes: NODES.map((n) => ({ id: n.key, x: n.x, y: n.y })), edges: EDGES.filter((e) => e.kind !== "excluded"), check: CHECK, enabled: true }) });
    });
  });

  it("벽 너머 두 노드를 이으려 하면 이유를 돌려준다", async () => {
    const { result } = renderHook(() => useNavNodes(47));
    await waitFor(() => expect(result.current.nodes).toHaveLength(3));
    expect(await result.current.checkLink("A", "C")).toContain("벽이 있어");
  });

  it("이미 이어진 연결을 끊는 것은 묻지 않는다", async () => {
    const { result } = renderHook(() => useNavNodes(47));
    await waitFor(() => expect(result.current.nodes).toHaveLength(3));
    const n = apiFetch.mock.calls.length;
    expect(await result.current.checkLink("A", "B")).toBeNull();
    expect(apiFetch.mock.calls.length).toBe(n);
  });

  it("노드를 옮겨 연결이 벽을 지나게 되면 거절 이유를 돌려준다", async () => {
    apiFetch.mockImplementation((url: string) => url.endsWith("/validate")
      ? Promise.resolve({ ok: false, json: () => Promise.resolve({ detail: "두 노드 사이에 벽이 있어 이을 수 없습니다." }) })
      : Promise.resolve({ ok: true, json: () => Promise.resolve({ nodes: NODES.map((n) => ({ id: n.key, x: n.x, y: n.y })), edges: EDGES, check: CHECK, enabled: true }) }));
    const { result } = renderHook(() => useNavNodes(47));
    await waitFor(() => expect(result.current.nodes).toHaveLength(3));
    expect(await result.current.checkMove("B", 2, 3)).toContain("벽이 있어");
  });
});
