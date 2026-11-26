import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiFetch = vi.fn();
vi.mock("@/app/lib/api", () => ({ apiFetch: (...a: unknown[]) => apiFetch(...a) }));

import { useNavNodes } from "../hooks/useNavNodes";

const json = (body: unknown, ok = true) => Promise.resolve({ ok, json: () => Promise.resolve(body) });
const EMPTY = { nodes: [], edges: [], check: { ready: false, reason: "노드가 없습니다.", unlinkedPlaceIds: [] }, enabled: true };

describe("노드 자리 검사(checkSpot)", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    apiFetch.mockImplementation((url: string, init?: { body?: string }) => {
      if (url.endsWith("/spot-check")) {
        const { x } = JSON.parse(init?.body ?? "{}");
        if (x === 1) return json({ ok: true, reason: null });
        if (x === 2) return json({ ok: false, reason: "벽에 너무 가깝습니다(벽까지 0.12 m — 0.35 m 이상 떨어져야 합니다)." });
        if (x === 3) return json({ detail: "서버 오류" }, false);
        return Promise.reject(new Error("network"));
      }
      return json(EMPTY);
    });
  });

  it("흰 바닥이면 null — 노드를 둔다", async () => {
    const { result } = renderHook(() => useNavNodes(47));
    expect(await result.current.checkSpot(1, 0)).toBeNull();
    const call = apiFetch.mock.calls.find((c) => String(c[0]).endsWith("/spot-check"));
    expect(call?.[0]).toBe("/map/nav-nodes/47/spot-check");
    expect(JSON.parse(call?.[1].body)).toEqual({ x: 1, y: 0 });
  });

  it("벽·회색·맵 바깥이면 서버가 준 이유를 그대로 돌려준다", async () => {
    const { result } = renderHook(() => useNavNodes(47));
    expect(await result.current.checkSpot(2, 0)).toContain("벽에 너무 가깝습니다");
  });

  it("확인 실패(서버 오류·통신 실패)는 막지 않는다 — 저장 때 서버가 다시 검사한다", async () => {
    const { result } = renderHook(() => useNavNodes(47));
    expect(await result.current.checkSpot(3, 0)).toBeNull();
    expect(await result.current.checkSpot(4, 0)).toBeNull();
  });

  it("맵을 고르지 않았으면 묻지 않는다", async () => {
    const { result } = renderHook(() => useNavNodes(""));
    expect(await result.current.checkSpot(2, 0)).toBeNull();
    expect(apiFetch.mock.calls.some((c) => String(c[0]).endsWith("/spot-check"))).toBe(false);
  });
});
