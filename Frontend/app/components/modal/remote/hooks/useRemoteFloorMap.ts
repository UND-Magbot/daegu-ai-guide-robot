"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "@/app/lib/api";
import { getApiBase } from "@/app/config";
import type { MapConfig } from "@/app/components/map/types";

type MapRow = { id: number; ImgFilePath?: string | null; PgmFilePath?: string | null };

/**
 * 원격 화면에 쓸 맵. 로봇의 CurrentMapId(층 변경 모달에서 고른 맵)가 그 층 목록에 있으면 그 맵,
 * 아니면 최신 맵(서버 id 내림차순 첫 번째).
 * 한 층에 맵이 여럿이라 최신 맵만 쓰면 로봇 좌표가 다른 맵 위에 찍혔다(2026-10-16).
 * useRemoteFloorPois 도 같은 규칙을 써야 맵과 POI 좌표계가 맞는다.
 */
export function pickRemoteMap<T extends MapRow>(maps: T[], mapId: number | null): T | null {
  return maps.find((m) => m.id === mapId) ?? maps[0] ?? null;
}

/**
 * 로봇의 현재 맵을 로드해 CanvasMap용 MapConfig를 반환한다.
 * 맵이 없거나 로딩 실패 시 config=null. 대시보드 MapSection과 동일한 방식
 * (맵 row → yaml meta(origin/resolution) → 이미지 자연 크기)으로 구성한다.
 *
 * 원격 화면 맵을 고정 데모 맵 대신 실제 층 맵으로 표시하기 위함. origin/resolution이
 * 실제 맵 기준이 되므로 로봇 마커 좌표도 정확히 찍힌다.
 * 층·맵이 바뀌면 이전 맵을 바로 내리고 loading=true 로 알린다 — 새 맵이 오기 전까지
 * 이전 맵 위에 새 맵 좌표의 로봇을 그리지 않게 하려는 것이다.
 */
export function useRemoteFloorMap(
  floorId: number | null,
  mapId: number | null,
): { config: MapConfig | null; loading: boolean } {
  const [config, setConfig] = useState<MapConfig | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setConfig(null);
    if (floorId == null) {
      setLoading(false);
      return;
    }
    setLoading(true);
    let cancelled = false;

    (async () => {
      try {
        const mapsRes = await apiFetch(`/map/maps?floor_id=${floorId}`);
        const maps = await mapsRes.json();
        if (cancelled) return;
        const map = Array.isArray(maps) ? pickRemoteMap<MapRow>(maps, mapId) : null;
        if (!map) {
          setConfig(null);
          return;
        }

        const imgPath =
          map.ImgFilePath?.replace("./", "/") || map.PgmFilePath?.replace("./", "/");
        if (!imgPath) {
          setConfig(null);
          return;
        }
        const imageSrc = `${getApiBase()}${imgPath}`;

        const metaRes = await apiFetch(`/map/maps/${map.id}/meta`);
        const meta = await metaRes.json();
        if (cancelled) return;

        const img = new Image();
        img.crossOrigin = "anonymous";
        img.src = imageSrc;
        await new Promise<void>((resolve) => {
          img.onload = () => resolve();
          img.onerror = () => resolve();
        });
        if (cancelled) return;

        setConfig({
          imageSrc,
          resolution: meta.resolution ?? 0.1,
          originX: meta.originX ?? 0,
          originY: meta.originY ?? 0,
          pixelWidth: img.naturalWidth || 335,
          pixelHeight: img.naturalHeight || 450,
        });
      } catch {
        if (!cancelled) setConfig(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [floorId, mapId]);

  return { config, loading };
}
