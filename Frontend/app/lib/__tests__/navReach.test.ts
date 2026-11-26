import { describe, expect, it } from "vitest";
import { markBlocked, pathBlockReason, placeBlockReason, toMoveBlock, type BlockMap } from "@/app/lib/navReach";

type Issue = { level: string; kind?: string; placeId?: number; nodeId?: number; text: string };
const check = (over: Partial<{ ready: boolean; reason: string; unlinkedPlaceIds: number[]; issues: Issue[] }> = {}) => ({
  ready: true,
  reason: "",
  unlinkedPlaceIds: [],
  issues: [] as Issue[],
  ...over,
});

describe("노드망 이동 막힘 판정", () => {
  it("서버의 노드망 경로가 꺼져 있으면 아무것도 막지 않는다(노드망 반영 전 운영)", () => {
    expect(toMoveBlock({ enabled: false, check: check({ ready: false, reason: "노드가 없습니다." }) })).toBeNull();
    expect(toMoveBlock(null)).toBeNull();
  });

  it("노드망 미완성 맵이면 맵 전체를 막고 이유를 준다", () => {
    const b = toMoveBlock({ enabled: true, check: check({ ready: false, reason: "노드가 없습니다." }) });
    expect(b?.map).toContain("노드가 없습니다.");
  });

  it("미연결 장소와 갈 길 없는 장소(점검 block)만 막는다 — 경고(warn)는 막지 않는다", () => {
    const b = toMoveBlock({
      enabled: true,
      check: check({
        unlinkedPlaceIds: [3],
        issues: [
          { level: "block", kind: "oneway_trap", placeId: 7, text: "'수납' — 한쪽 방향 연결 때문에 충전소에서 갈 수 없습니다." },
          { level: "warn", kind: "isolated", nodeId: 2, text: "N02 — 다른 노드와 연결되지 않았습니다." },
        ],
      }),
    });
    expect(b?.map).toBeNull();
    expect(Object.keys(b!.places).map(Number).sort()).toEqual([3, 7]);
    expect(b!.places[3]).toContain("노드망에 연결되지 않은");
    expect(b!.places[7]).toContain("한쪽 방향");
  });
});

describe("장소·경로 막힘 판정", () => {
  const blocks: BlockMap = {
    47: { map: null, places: { 7: "'수납' — 한쪽 방향 연결 때문에 충전소에서 갈 수 없습니다." } },
    43: { map: "이 맵은 노드망이 완성되지 않아 자율 이동을 할 수 없습니다. 노드가 없습니다.", places: {} },
    46: null, // 노드망 경로 꺼짐 또는 못 읽음 — 막지 않는다
  };

  it("맵 전체 막힘이 장소보다 먼저, 정보 없는 맵·맵 모름은 막지 않는다", () => {
    expect(placeBlockReason(blocks, 47, 7)).toContain("한쪽 방향");
    expect(placeBlockReason(blocks, 47, 8)).toBeNull();
    expect(placeBlockReason(blocks, 43, 1)).toContain("노드망이 완성되지 않아");
    expect(placeBlockReason(blocks, 46, 1)).toBeNull();
    expect(placeBlockReason(blocks, null, 1)).toBeNull();
  });

  it("경로는 이름으로 장소를 찾고, 같은 이름이면 경로의 층 것을 본다(없으면 이름 같은 것 전부)", () => {
    const places = [
      { id: 7, name: "수납", mapId: 47, floorId: 1 },
      { id: 70, name: "수납", mapId: 46, floorId: 2 },
      { id: 8, name: "간호실", mapId: 47, floorId: 1 },
      { id: 9, name: "3층 로비", mapId: 43, floorId: 3 },
    ];
    expect(pathBlockReason(blocks, "간호실 - 수납", places, 1)).toBe("'수납' — 한쪽 방향 연결 때문에 충전소에서 갈 수 없습니다.");
    expect(pathBlockReason(blocks, "간호실 - 수납", places, 2)).toBeNull();           // 2층 수납은 막히지 않음
    expect(pathBlockReason(blocks, "간호실 - 3층 로비", places, 1)).toContain("'3층 로비' —"); // 다른 층 장소(층 이동 경로)
    expect(pathBlockReason(blocks, "", places)).toBeNull();
  });

  it("선택지 표시 — 막혔을 때만 disabled", () => {
    expect(markBlocked({ id: 1, label: "A" }, null)).toEqual({ id: 1, label: "A" });
    expect(markBlocked({ id: 1, label: "A" }, "이유")).toEqual({ id: 1, label: "A", disabled: true, disabledReason: "이유" });
  });
});

