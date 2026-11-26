"use client";

/**
 * 로봇별 현재 위치("2F · 접수") 계산.
 *
 * 이전에는 `useRobotLocation()` 이 **대표 로봇 하나**(power==="On" 인 첫 로봇)의 위치를
 * 구해 모든 카드에 같은 값을 넘겼다. 그래서 M20-SIM 이 움직이면 목록의 로봇이 전부
 * 같이 움직이는 것처럼 보였다 — 화면이 자신 있게 틀린 정보를 말하는 상태였다.
 *
 * 좌표는 이미 로봇마다 `robot.position` 에 있다. 전역 `/robot/position` 을 폴링할
 * 이유가 없어 그 호출도 없앴다(로봇 수와 무관하게 상태는 이미 흐르고 있다).
 *
 * 장소·층 마스터는 로봇과 무관하므로 한 번 받아 공유하고, **매칭만 로봇별로** 돈다.
 *
 * 매칭 후보는 **로봇의 현재 맵 장소**뿐이다. 맵마다 좌표 원점이 달라 같은 층의 다른 맵 장소는 비교할 수
 * 없다(2026-10-08: 3F 관공서 맵에서 옛 시험 맵의 '테스트BB' 가 엘리베이터 입구_3F 대신 잡혔다).
 * 맵을 모를 때만 층 이름으로 대체한다.
 *
 * 장소 목록은 관제가 /robot/status 헤더로 주는 장소 지문(placesRev)이 바뀌었을 때 다시 받는다.
 * 화면이 숨겨져 있으면 받지 않고, 다시 보일 때 바뀐 게 있으면 그때 한 번 받는다.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "@/app/lib/api";
import { useRobotStatusContext } from "@/app/context/RobotStatusContext";
import type { RobotRowData } from "@/app/types";

export type Place = {
  id: number;
  name: string;
  x: number;
  y: number;
  floor: string;
  mapId: number | null;
};

export type RobotLocation = {
  /** 층 이름. 미매칭·미확정이면 null(= 표시는 "-"). 임의 기본값으로 채우지 않는다. */
  floor: string | null;
  placeName: string | null;
};

export const EMPTY_LOCATION: RobotLocation = { floor: null, placeName: null };

const NEARBY_THRESHOLD = 1.5; // m — 이 거리 이내면 그 장소에 있다고 본다
const PLACE_FALLBACK_FLOOR = "1F"; // 장소 마스터에 Floor 가 비어있을 때만 쓰는 파싱 기본값

function distance(x1: number, y1: number, x2: number, y2: number) {
  return Math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2);
}

/** 이 로봇의 위치로 믿을 수 있는 좌표. 측위 미확정이면 마지막 신뢰 위치를 쓴다. */
function usablePosition(robot: RobotRowData): { x: number; y: number } | null {
  if (robot.initposePending) {
    const t = robot.trustedPosition;
    return t ? { x: t.x, y: t.y } : null;
  }
  const p = robot.position;
  return p ? { x: p.x, y: p.y } : null;
}

export function useRobotLocations(robots: RobotRowData[]): {
  places: Place[];
  byRobotId: Record<number, RobotLocation>;
} {
  const [places, setPlaces] = useState<Place[]>([]);
  const [floorNameById, setFloorNameById] = useState<Record<number, string>>({});
  const { placesRev } = useRobotStatusContext();
  const [visible, setVisible] = useState(
    () => typeof document === "undefined" || document.visibilityState === "visible"
  );
  // 지금 들고 있는 장소 목록이 어느 지문 기준인가 / 받는 중인가
  const loadedRevRef = useRef<string | null>(null);
  const loadingRef = useRef(false);
  // 받기가 끝날 때마다 올린다 — 받는 도중 지문이 또 바뀌었으면 끝난 뒤 다시 판단하게
  const [loadSeq, setLoadSeq] = useState(0);

  // 층 목록 1회 로드 (id → 이름)
  useEffect(() => {
    apiFetch(`/map/floors`)
      .then((res) => (res.ok ? res.json() : []))
      .then((data: { id: number; FloorName: string }[]) => {
        const map: Record<number, string> = {};
        for (const f of data) map[f.id] = f.FloorName;
        setFloorNameById(map);
      })
      .catch(() => {});
  }, []);

  // 장소 마스터 — 받는 중 실패하면 기존 목록을 그대로 둔다(빈 목록으로 덮어 POI 가 사라지지 않게)
  const loadPlaces = useCallback((rev: string | null) => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    apiFetch(`/DB/places`)
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((data: { id: number; LacationName?: string; LocationX?: number; LocationY?: number; Floor?: string; MapId?: number | null }[]) => {
        setPlaces(
          data.map((p) => ({
            id: p.id,
            name: p.LacationName ?? "",
            x: p.LocationX ?? 0,
            y: p.LocationY ?? 0,
            floor: p.Floor ?? PLACE_FALLBACK_FLOOR,
            mapId: p.MapId ?? null,
          }))
        );
        loadedRevRef.current = rev;
      })
      .catch(() => {})
      .finally(() => {
        loadingRef.current = false;
        setLoadSeq((n) => n + 1);
      });
  }, []);

  // 화면 표시 여부 — 숨긴 동안은 장소를 다시 받지 않는다
  useEffect(() => {
    const onChange = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);

  // 처음 한 번 + 보이는 동안 지문이 바뀌면(숨긴 동안 바뀌었으면 다시 보일 때) 다시 받는다
  useEffect(() => {
    if (!visible || loadingRef.current) return;
    if (loadedRevRef.current === null) {
      if (places.length === 0) {
        loadPlaces(placesRev); // 처음(또는 첫 시도 실패)
      } else if (placesRev !== null) {
        loadedRevRef.current = placesRev; // 지문을 알기 전에 받은 목록 — 첫 지문을 기준으로 삼는다(중복 조회 없이)
      }
      return;
    }
    if (placesRev !== null && placesRev !== loadedRevRef.current) loadPlaces(placesRev);
  }, [visible, placesRev, places.length, loadSeq, loadPlaces]);

  const byRobotId = useMemo(() => {
    const out: Record<number, RobotLocation> = {};
    for (const robot of robots) {
      // 층을 모르면 같은 층 장소를 고를 수 없다 → 층도 장소도 미확정이다.
      const floor =
        robot.currentFloorId != null ? (floorNameById[robot.currentFloorId] ?? null) : null;
      if (floor == null) {
        out[robot.id] = EMPTY_LOCATION;
        continue;
      }

      const pos = usablePosition(robot);
      if (!pos) {
        out[robot.id] = { floor, placeName: null };
        continue;
      }

      // 맵마다 좌표 원점이 달라 현재 맵의 장소만 비교한다. 맵을 모를 때만 층 이름으로 대체.
      const mapId = robot.currentMapId;
      let nearest: Place | null = null;
      let minDist = Infinity;
      for (const p of places) {
        if (mapId != null ? p.mapId !== mapId : p.floor !== floor) continue;
        const d = distance(pos.x, pos.y, p.x, p.y);
        if (d < minDist) {
          minDist = d;
          nearest = p;
        }
      }
      out[robot.id] = {
        floor,
        placeName: nearest && minDist <= NEARBY_THRESHOLD ? nearest.name : null,
      };
    }
    return out;
  }, [robots, places, floorNameById]);

  return { places, byRobotId };
}
