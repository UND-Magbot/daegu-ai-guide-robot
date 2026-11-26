import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiFetch = vi.fn();
vi.mock("@/app/lib/api", () => ({ apiFetch: (...a: unknown[]) => apiFetch(...a) }));

import { useNavNodes } from "../hooks/useNavNodes";

type E = { a: string; b: string; kind: string; direction?: string; door_id?: number | null };
const json = (body: unknown) => Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
const CHECK = { ready: true, reason: "", unlinkedPlaceIds: [], issues: [], placeAttach: {} };
// 일직선 노드 A(0,0)·B(1,0)·C(2,0) — B 가 사이에 있어 A–C 자동 연결은 없다. B 를 지우면 서버가 A–C 를 자동으로 잇는다.
const NODES = [{ id: "A", x: 0, y: 0 }, { id: "B", x: 1, y: 0 }, { id: "C", x: 2, y: 0 }];
const EDGES = [
  { a: "A", b: "B", kind: "auto", direction: "both", doorId: null },
  { a: "B", b: "C", kind: "auto", direction: "both", doorId: null },
];
const validateBodies: { nodes: { key: string }[]; edges: E[] }[] = [];

describe("노드 삭제 — 노드와 그 선만 사라진다", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    validateBodies.length = 0;
    apiFetch.mockImplementation((url: string, init?: { body?: string }) => {
      if (url.endsWith("/validate")) {
        const body = JSON.parse(init?.body ?? "{}");
        validateBodies.push(body);
        const keys = body.nodes.map((n: { key: string }) => n.key);
        const blocked = body.edges.some((e: E) => e.kind === "excluded" && [e.a, e.b].sort().join() === "A,C");
        const edges = keys.includes("B")
          ? EDGES
          : [...(blocked ? [] : [{ a: "A", b: "C", kind: "auto", direction: "both", doorId: null }]),
             ...body.edges.map((e: E) => ({ ...e, doorId: null }))];
        return json({ nodes: body.nodes, edges, check: CHECK });
      }
      return json({ nodes: NODES, edges: EDGES, check: CHECK, enabled: true });
    });
  });

  it("지운 자리 양옆이 새로 자동 연결되면 그 쌍을 막은 연결로 함께 넣는다", async () => {
    const { result } = renderHook(() => useNavNodes(43));
    await waitFor(() => expect(result.current.nodes).toHaveLength(3));

    const res = await result.current.checkRemove("B");
    expect(res?.block).toEqual([["A", "C"]]);
    // 점검은 막은 연결까지 넣은 상태로 다시 받는다(끊기는 장소를 정확히 알리려고)
    expect(validateBodies).toHaveLength(2);
    expect(validateBodies[1].edges).toContainEqual(expect.objectContaining({ a: "A", b: "C", kind: "excluded" }));

    act(() => result.current.removeNode("B", res!.block));
    await waitFor(() => expect(validateBodies.length).toBeGreaterThan(2));
    const last = validateBodies[validateBodies.length - 1];
    expect(last.nodes.map((n) => n.key)).toEqual(["A", "C"]);
    expect(last.edges).toContainEqual(expect.objectContaining({ a: "A", b: "C", kind: "excluded" }));
    await waitFor(() => expect(result.current.edges.filter((e) => e.kind !== "excluded")).toHaveLength(0));
  });

  it("새로 생길 자동 연결이 없으면 막은 연결을 넣지 않는다", async () => {
    apiFetch.mockImplementation((url: string, init?: { body?: string }) => {
      if (url.endsWith("/validate")) {
        validateBodies.push(JSON.parse(init?.body ?? "{}"));
        return json({ nodes: [], edges: [], check: CHECK });
      }
      return json({ nodes: NODES, edges: EDGES, check: CHECK, enabled: true });
    });
    const { result } = renderHook(() => useNavNodes(43));
    await waitFor(() => expect(result.current.nodes).toHaveLength(3));
    const res = await result.current.checkRemove("B");
    expect(res?.block).toEqual([]);
    expect(validateBodies).toHaveLength(1);
  });
});
