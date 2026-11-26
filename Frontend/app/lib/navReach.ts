import { useEffect, useState } from "react";
import { apiFetch } from "@/app/lib/api";
import type { SelectOption } from "@/app/components/select/CustomSelect";

/** 노드망 기준 이동 가능 여부 — 서버의 노드망 경로가 꺼져 있으면 null(막지 않는다). */
export type MoveBlock = {
  /** 맵 전체가 막힘(노드망 미완성) — 이유. 막히지 않았으면 null */
  map: string | null;
  /** 장소 id → 갈 수 없는 이유 */
  places: Record<number, string>;
};

type NavNodesRes = {
  enabled?: boolean;
  check?: {
    ready: boolean;
    reason: string;
    unlinkedPlaceIds: number[];
    issues?: { level: string; placeId?: number; text: string }[];
  };
};

/** 서버 응답 → 막힘 정보. 테스트하려고 따로 둔다. */
export function toMoveBlock(d: NavNodesRes | null): MoveBlock | null {
  if (!d?.enabled || !d.check) return null;
  const places: Record<number, string> = {};
  for (const id of d.check.unlinkedPlaceIds ?? []) {
    places[id] = "노드망에 연결되지 않은 장소입니다 — 맵 편집에서 근처에 노드를 추가하면 이동할 수 있습니다.";
  }
  for (const i of d.check.issues ?? []) {
    if (i.level === "block" && i.placeId != null && !(i.placeId in places)) places[i.placeId] = i.text;
  }
  return { map: d.check.ready ? null : `이 맵은 노드망이 완성되지 않아 자율 이동을 할 수 없습니다. ${d.check.reason}`, places };
}

export async function fetchMoveBlock(mapId: number): Promise<MoveBlock | null> {
  const res = await apiFetch(`/map/nav-nodes/${mapId}`);
  return res.ok ? toMoveBlock(await res.json()) : null;
}

// 선택 창이 여러 개 열려도 같은 맵을 매번 묻지 않게 잠깐(30초) 기억한다 — 노드망은 자주 바뀌지 않는다
const CACHE_MS = 30_000;
const cache = new Map<number, { at: number; p: Promise<MoveBlock | null> }>();

function fetchMoveBlockCached(mapId: number): Promise<MoveBlock | null> {
  const hit = cache.get(mapId);
  if (hit && Date.now() - hit.at < CACHE_MS) return hit.p;
  const p = fetchMoveBlock(mapId).catch(() => null);
  cache.set(mapId, { at: Date.now(), p });
  return p;
}

/** 테스트에서 캐시를 비운다 */
export function clearMoveBlockCache() {
  cache.clear();
}

/** 맵 id → 막힘 정보(노드망 경로가 꺼져 있거나 못 읽으면 null) */
export type BlockMap = Record<number, MoveBlock | null>;

/** 여러 맵의 막힘 정보를 읽는다. 확인 실패는 막지 않는다 — 서버가 출발 전에 다시 확인한다. */
export function useMoveBlocks(mapIds: (number | null | undefined)[]): BlockMap {
  const key = Array.from(new Set(mapIds.filter((m): m is number => m != null))).sort((a, b) => a - b).join(",");
  const [blocks, setBlocks] = useState<BlockMap>({});
  useEffect(() => {
    if (!key) {
      setBlocks({});
      return;
    }
    let cancelled = false;
    const ids = key.split(",").map(Number);
    Promise.all(ids.map((id) => fetchMoveBlockCached(id))).then((list) => {
      if (cancelled) return;
      const out: BlockMap = {};
      ids.forEach((id, i) => { out[id] = list[i]; });
      setBlocks(out);
    });
    return () => { cancelled = true; };
  }, [key]);
  return blocks;
}

/** 이 장소로 갈 수 없는 이유 — 맵 전체 막힘이 먼저다. 갈 수 있으면 null. */
export function placeBlockReason(blocks: BlockMap, mapId: number | null | undefined, placeId: number): string | null {
  if (mapId == null) return null;
  const b = blocks[mapId];
  if (!b) return null;
  return b.map ?? b.places[placeId] ?? null;
}

export type NamedPlace = { id: number; name: string; mapId?: number | null; floorId?: number | null };

/** 전체 장소(이름 → 맵 찾기용). 경로는 장소를 이름으로 저장하므로 경로 막힘 판정에 쓴다. 창이 열렸을 때만 읽는다. */
export function usePlaceIndex(active: boolean): NamedPlace[] {
  const [places, setPlaces] = useState<NamedPlace[]>([]);
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    apiFetch(`/DB/places`)
      .then((res) => (res.ok ? res.json() : []))
      .then((data) => {
        if (cancelled) return;
        const list = Array.isArray(data) ? data : (data?.places ?? []);
        setPlaces(list.map((p: { id: number; LacationName?: string; MapId?: number | null; FloorId?: number | null }) =>
          ({ id: p.id, name: p.LacationName ?? "", mapId: p.MapId ?? null, floorId: p.FloorId ?? null })));
      })
      .catch(() => { if (!cancelled) setPlaces([]); });
    return () => { cancelled = true; };
  }, [active]);
  return places;
}

/** 경로 목록 막힘 판정에 필요한 것 한 번에 — 장소 목록 + 그 장소들이 있는 맵의 막힘 정보. */
export function usePathBlocks(active: boolean): { places: NamedPlace[]; blocks: BlockMap } {
  const places = usePlaceIndex(active);
  const blocks = useMoveBlocks(places.map((p) => p.mapId));
  return { places, blocks };
}

/**
 * 경로(WayPoints "A - B - C")에 갈 수 없는 장소가 있으면 그 이유. 경로는 장소를 이름으로 저장한다 —
 * 같은 이름이 여러 층에 있으면 경로의 층 것을 먼저 본다(층 이동 경로라 그 층에 없으면 이름이 같은 것 전부).
 */
export function pathBlockReason(
  blocks: BlockMap,
  wayPoints: string | null | undefined,
  places: NamedPlace[],
  floorId?: number | null,
): string | null {
  if (!wayPoints) return null;
  for (const raw of wayPoints.split(" - ")) {
    const name = raw.trim();
    const all = places.filter((p) => p.name === name);
    const onFloor = floorId == null ? all : all.filter((p) => p.floorId === floorId);
    for (const p of onFloor.length ? onFloor : all) {
      const r = placeBlockReason(blocks, p.mapId, p.id);
      // 점검 문구는 이미 장소 이름으로 시작한다("'수납' — 한쪽 방향 …") — 이름을 두 번 붙이지 않는다
      if (r) return r.startsWith(`'${name}'`) ? r : `'${name}' — ${r}`;
    }
  }
  return null;
}

/** 선택지에 막힘 표시를 단다(CustomSelect 가 흐리게·선택 불가·사유 툴팁으로 보인다). */
export function markBlocked<T extends SelectOption>(opt: T, reason: string | null): T {
  return reason ? { ...opt, disabled: true, disabledReason: reason } : opt;
}
