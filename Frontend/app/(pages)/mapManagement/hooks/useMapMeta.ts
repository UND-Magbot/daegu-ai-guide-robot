"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "@/app/lib/api";

export type MapMeta = {
  resolution: number;
  originX: number;
  originY: number;
};

/**
 * 선택된 맵의 메타데이터(해상도, 원점)를 로드.
 * selectedMap이 ""이면 null.
 */
export function useMapMeta(selectedMap: number | "") {
  const [mapMeta, setMapMeta] = useState<MapMeta | null>(null);

  useEffect(() => {
    if (selectedMap === "") {
      setMapMeta(null);
      return;
    }
    // 맵을 바꾸면 이전 맵 원점·해상도로 새 맵 좌표를 계산하지 않게 바로 비우고,
    // 늦게 온 이전 맵 응답은 버린다(2026-10-16).
    setMapMeta(null);
    let cancelled = false;
    apiFetch(`/map/maps/${selectedMap}/meta`)
      .then((res) => res.json())
      .then((data) => { if (!cancelled) setMapMeta(data); })
      .catch(() => { if (!cancelled) setMapMeta(null); });
    return () => { cancelled = true; };
  }, [selectedMap]);

  return mapMeta;
}
