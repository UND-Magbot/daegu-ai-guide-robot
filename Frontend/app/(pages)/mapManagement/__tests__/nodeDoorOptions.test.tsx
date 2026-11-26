import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiFetch = vi.fn();
vi.mock("@/app/lib/api", () => ({ apiFetch: (...a: unknown[]) => apiFetch(...a) }));

import NodeEditPanel from "../components/tabs/map/NodeEditPanel";

// 서버 /door 응답 모양(map/door.py _door_dict) — 이름은 소문자 name
const DOORS = [
  { id: 1, name: "본관", mapId: 48 },
  { id: 2, name: "별관", mapId: 47 },
  { id: 3, name: "맵 미지정", mapId: null },
];

const panel = (extra: Partial<React.ComponentProps<typeof NodeEditPanel>> = {}) => render(
  <NodeEditPanel isOpen mapId={48} businessId={3} tool="door" setTool={() => {}}
    nodes={[]} edges={[]} check={null} error={null} dirty={false} saving={false}
    selectedKey={null} linkFromKey={null} doorPair={["A", "B"]} places={[]} robotPos={null}
    selectedEdge={null} chargerNodes={[]} migration={null}
    onSelectEdge={() => {}} onSetDirection={() => {}} onBlockEdge={() => {}}
    onConfirmDoor={() => {}} onCancelDoor={() => {}}
    onPreview={() => Promise.reject(new Error("unused"))} onPreviewShown={() => {}}
    onReset={() => {}} onSave={() => {}} onClose={() => {}}
    {...extra} />,
);

describe("문 간선 — 연결할 자동문 목록", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    apiFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve(DOORS) });
  });

  it("편집 중인 맵의 사업장으로 불러오고, 자동문 이름이 보인다", async () => {
    panel();
    await waitFor(() => expect(screen.getByRole("option", { name: "본관" })).toBeTruthy());
    expect(apiFetch).toHaveBeenCalledWith("/door?business_id=3");
  });

  it("다른 맵의 자동문은 빼고, 맵을 안 정한 자동문은 남긴다", async () => {
    panel();
    await waitFor(() => expect(screen.getByRole("option", { name: "본관" })).toBeTruthy());
    expect(screen.queryByRole("option", { name: "별관" })).toBeNull();
    expect(screen.getByRole("option", { name: "맵 미지정" })).toBeTruthy();
  });

  it("사업장을 모르면 부르지 않고 '등록된 자동문 없음'을 보인다", async () => {
    panel({ businessId: null });
    expect(await screen.findByText(/이 맵에 등록된 자동문이 없습니다/)).toBeTruthy();
    expect(apiFetch).not.toHaveBeenCalledWith(expect.stringContaining("/door"));
  });
});
