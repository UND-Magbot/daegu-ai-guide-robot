"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "@/app/lib/api";
import type { POIItem } from "@/app/components/map/types";
import { toPoiItem, type PlaceRow } from "@/app/lib/placeFilter";
import { pickRemoteMap } from "./useRemoteFloorMap";

/**
 * 로봇의 현재 맵에 등록된 장소를 원격 화면 맵용 POI 로 반환한다.
 * 맵이 없거나 조회 실패면 빈 배열.
 *
 * useRemoteFloorMap 과 층→맵 조회가 겹치지만, 맵 렌더(이미 검증된 경로)를 건드리지 않으려고
 * 별도 훅으로 둔다. 층·맵이 바뀔 때만 도는 저빈도 요청이라 비용도 작다.
 *
 * ⚠ 맵 선택은 반드시 pickRemoteMap 으로 useRemoteFloorMap 과 같게 한다 — 어긋나면 POI 가
 *   다른 맵 좌표계로 찍힌다. 층·맵이 바뀌면 이전 POI 를 바로 비운다(새 맵 위에 남지 않게).
 */
export function useRemoteFloorPois(floorId: number | null, mapId: number | null): POIItem[] {
  const [pois, setPois] = useState<POIItem[]>([]);

  useEffect(() => {
    setPois([]);
    if (floorId == null) return;
    let cancelled = false;

    (async () => {
      try {
        const mapsRes = await apiFetch(`/map/maps?floor_id=${floorId}`);
        const maps = await mapsRes.json();
        if (cancelled) return;
        const map = Array.isArray(maps) ? pickRemoteMap<{ id: number }>(maps, mapId) : null;
        if (!map) return;

        const res = await apiFetch(`/DB/places?map_id=${map.id}`);
        const rows: PlaceRow[] = res.ok ? await res.json() : [];
        if (cancelled) return;

        setPois(
          (Array.isArray(rows) ? rows : []).map(toPoiItem)
        );
      } catch {
        if (!cancelled) setPois([]);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [floorId, mapId]);

  return pois;
}
