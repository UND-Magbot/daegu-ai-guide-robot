"use client";

import styles from "./MapSection.module.css";
import { ZoomControl } from "@/app/components/button";
import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import type { Floor, RobotRowData, Video, Camera } from "@/app/types";
import type { MapConfig, POIItem, RobotOnMap, NavPath, NavPathSegment, MapView, NavNetwork, NetworkLevel, ActiveRoute } from "@/app/components/map/types";
import { zoomIconClass } from "@/app/components/button/ZoomControl";
import type { CanvasMapHandle } from "@/app/components/map/CanvasMap";
import CanvasMap from "@/app/components/map/CanvasMap";
import { toPoiCategory } from "@/app/components/map/poiStyle";
import { apiFetch } from "@/app/lib/api";
import { getApiBase } from "@/app/config";
import { useModalAlert } from "@/app/hooks/useModalAlert";
import AlertModal from "@/app/components/modal/AlertModal";
import { useOutsideClick } from "@/app/hooks/useOutsideClick";
import { WORK_TYPES, resolveTaskTypeLabel } from "@/app/lib/taskType";
import { isStatusStale } from "@/app/lib/robotLiveness";
import { useRobotStatusContext } from "@/app/context/RobotStatusContext";
import { useElevatorOpState } from "./useElevatorOpState";
import { useDoorOpState } from "./useDoorOpState";
import { useArrivalGate } from "./useArrivalGate";
import dynamic from "next/dynamic";

const ElevatorOpOverlay = dynamic(() => import("./ElevatorOpOverlay"), { ssr: false });
const DoorOpOverlay = dynamic(() => import("./DoorOpOverlay"), { ssr: false });
const ArrivalGateOverlay = dynamic(() => import("./ArrivalGateOverlay"), { ssr: false });

const StopAllConfirmModal = dynamic(() => import("@/app/components/modal/BatteryChargeModal"), { ssr: false });

// 대시보드 맵 작업유형 필터 옵션 — 단일 출처(@/app/lib/taskType)에서 파생.
const TASK_TYPE_OPTIONS = WORK_TYPES.map((t) => t.label);

type PathRow = {
  id: number;
  taskType: string;
  wayPoints: string;
  floorId: number | null;
};

// ── 모듈 레벨 캐시 — 탭 전환해도 유지, npm run dev 재시작 시 초기화 ──
// 장소·구간·경로는 캐시로 먼저 그리고 마운트마다 다시 조회해 덮는다(stale-while-revalidate).
// 캐시만 믿으면 맵 관리에서 삭제·수정한 내용이 새로고침 전까지 대시보드에 남는다.
type FloorMapRow = { id: number; ImgFilePath?: string | null; PgmFilePath?: string | null };

const _cache: {
  floorMaps: Record<number, FloorMapRow[]>;          // floorId → 맵 목록(서버 순서 = id 내림차순)
  mapConfigs: Record<number, MapConfig | null>;      // mapId → config
  places: POIItem[] | null;
  navPaths: Record<number, NavPath | null>;  // mapId → navPath
  paths: PathRow[] | null;
  loaded: boolean;
} = { floorMaps: {}, mapConfigs: {}, places: null, navPaths: {}, paths: null, loaded: false };

/**
 * 층에서 보여줄 맵. 로봇이 그 층에 있으면 층 변경 때 고른 맵(CurrentMapId), 아니면 최신 맵.
 * 한 층에 맵이 여럿이라 최신 맵만 보여주면 로봇 좌표가 다른 맵 이미지 위에 찍혔다(2026-10-16).
 */
function pickMap(list: FloorMapRow[], preferredMapId: number | null): FloorMapRow | null {
  return list.find((m) => m.id === preferredMapId) ?? list[0] ?? null;
}

/** 캐시로 바로 그릴 수 있는 맵. undefined = 캐시 없음(불러와야 함), null = 그 층에 맵 없음. */
function getCachedMap(floorId: number, preferredMapId: number | null):
  { mapId: number; config: MapConfig | null } | null | undefined {
  const list = _cache.floorMaps[floorId];
  if (!list) return undefined;
  // 목록을 캐시한 뒤 새로 만든 맵이 활성화됐으면 목록부터 다시 받는다
  if (preferredMapId != null && !list.some((m) => m.id === preferredMapId)) return undefined;
  const map = pickMap(list, preferredMapId);
  if (!map) return null;
  const config = _cache.mapConfigs[map.id];
  return config === undefined ? undefined : { mapId: map.id, config };
}

type MapSectionProps = {
  floors: Floor[];
  robots: RobotRowData[];
  video: Video[];
  cameras: Camera[];
  selectedRobotId?: number | null;
  selectedRobotName?: string;
  robotFloorId?: number | null;
};

export default function MapSection({ floors, robots, video, cameras, selectedRobotId, selectedRobotName, robotFloorId = null }: MapSectionProps) {
  // 초기 층을 동기적으로 결정
  const getInitialFloor = (): { idx: number; floor: Floor | null } => {
    if (!floors.length) return { idx: 0, floor: null };
    if (robotFloorId != null) {
      const idx = floors.findIndex((f) => f.id === robotFloorId);
      if (idx >= 0) return { idx, floor: floors[idx] };
    }
    // 기본 층: "1F" 라벨이 있으면 해당 층, 없으면 첫 번째 층
    const f1Idx = floors.findIndex((f) => f.label === "1F");
    if (f1Idx >= 0) return { idx: f1Idx, floor: floors[f1Idx] };
    return { idx: 0, floor: floors[0] };
  };
  const initial = getInitialFloor();

  const [floorActiveIndex, setFloorActiveIndex] = useState<number>(initial.idx);
  const [selectedFloor, setSelectedFloor] = useState<Floor | null>(initial.floor);
  // 사용자가 로봇 층과 다른 층을 수동으로 보는 중인지 (뷰 override).
  // false면 선택 로봇의 currentFloorId를 자동 추종한다.
  const [userFloorOverride, setUserFloorOverride] = useState<boolean>(false);
  const [places, setPlaces] = useState<POIItem[]>(_cache.places ?? []);
  const [activeMapId, setActiveMapId] = useState<number | null>(null);
  const [navPath, setNavPath] = useState<NavPath | null>(null);
  const [mapView, setMapView] = useState<MapView>("2d");
  // ── 노드망(2026-10-19): 배경 표시 단계 + 이동 중 경로 ──
  const [networkLevel, setNetworkLevel] = useState<NetworkLevel>("faint");
  const [navNetwork, setNavNetwork] = useState<NavNetwork | null>(null);
  const [navCheck, setNavCheck] = useState<{ ready: boolean; reason: string; unlinkedPlaceIds: number[] } | null>(null);
  // 서버의 노드망 경로가 켜져 있는가 — 꺼져 있으면(노드망 반영 전) 노드망은 보여 주기만 하고 이동을 막지 않는다
  const [navEnabled, setNavEnabled] = useState(false);
  const [progress, setProgress] = useState<{
    robotName: string; isNavigating: boolean; remainM?: number; start?: { x: number; y: number };
    waypoints: { name: string; status: ActiveRoute["points"][number]["status"]; x?: number; y?: number; mapId?: number; node?: boolean }[];
    /** 지금 보낸 다지점 묶음의 웨이포인트 번호 범위(1부터, 끝 포함) — 단일 NAV 면 null */
    bundle?: { from: number; to: number } | null;
  } | null>(null);
  const [paths, setPaths] = useState<PathRow[]>(_cache.paths ?? []);
  const [taskTypeFilter, setTaskTypeFilter] = useState<string | null>(null); // null = 전체
  const [taskMenuOpen, setTaskMenuOpen] = useState(false);
  const taskMenuRef = useRef<HTMLDivElement>(null);
  useOutsideClick(taskMenuRef, useCallback(() => setTaskMenuOpen(false), []));

  // 선택 로봇이 지금 보는 층에 있으면 그 로봇의 맵을 보여준다(층 변경 모달에서 고른 맵).
  const robotMapId = robots.find((r) => r.id === selectedRobotId)?.currentMapId ?? null;
  const preferredMapId = selectedFloor != null && selectedFloor.id === robotFloorId ? robotMapId : null;

  // 캐시에서 즉시 mapConfig 복원
  const cachedEntry = selectedFloor ? getCachedMap(selectedFloor.id, preferredMapId) : undefined;
  const [mapConfig, setMapConfig] = useState<MapConfig | null>(cachedEntry?.config ?? null);
  const [mapLoading, setMapLoading] = useState(!cachedEntry);

  const mapRef = useRef<CanvasMapHandle>(null);

  // 관제 서버 응답이 없으면 로봇 상태·좌표는 갱신이 멈춘 잔존값이다.
  const { serverStatus } = useRobotStatusContext();
  const statusStale = isStatusStale(serverStatus);

  const hasFloors = floors.length > 0;
  const hasRobots = robots.length > 0;
  const hasOnlineRobot = !statusStale && robots.some(r => r.network === "Online");
  const hasWorkingRobot = !statusStale && robots.some(r => r.network === "Online" && r.isNavigating);

  const { modal, modalAlert, modalConfirm, closeModal, handleConfirm } = useModalAlert();
  const [stopAllConfirmOpen, setStopAllConfirmOpen] = useState(false);

  // 캐시된 mapId도 즉시 복원
  useEffect(() => {
    if (cachedEntry) {
      setActiveMapId(cachedEntry.mapId);
    }
  }, []);

  const handleStopAll = useCallback(() => {
    setStopAllConfirmOpen(true);
  }, []);

  const handleStopAllConfirm = useCallback(() => {
    setStopAllConfirmOpen(false);
    apiFetch(`/nav/stopmove`, { method: "POST" })
      .then(async (res) => {
        if (!res.ok) { modalAlert("전체 정지 요청이 실패했습니다."); return; }
        const data = await res.json().catch(() => null);
        modalAlert(data?.was_active ? "모든 로봇의 작업이 중지되었습니다." : "진행 중인 작업이 없습니다.");
      })
      .catch((err) => {
        console.error("전체 정지 실패", err);
        modalAlert("전체 정지 요청이 실패했습니다.");
      });
  }, [modalAlert]);

  // 엘리베이터 조작 오버레이 상태(선택 로봇 기준). 전환 중이 아니면 null → 오버레이 숨김.
  const opState = useElevatorOpState(selectedRobotId);
  // 자동문 통과 오버레이 — 같은 자리에 같은 문법으로 뜬다(둘이 동시에 진행되지는 않는다).
  const { state: doorOpState, refresh: refreshDoorOp } = useDoorOpState(selectedRobotId);
  // 게이트는 전역 1칸이라 로봇 선택과 무관하게 조회한다(응답의 robotName 으로 누구인지 보여준다)
  const { state: arrivalGate, refresh: refreshArrivalGate } = useArrivalGate();

  // 현재 선택된 층에 있는 로봇들 — 전원이 On인 경우만 표시
  const floorRobots: RobotOnMap[] = useMemo(() => {
    if (!selectedFloor) return [];
    // 관제 서버 두절 — 좌표가 갱신되지 않으므로 '지금 여기 있다'고 그리지 않는다.
    if (statusStale) return [];
    return robots
      .map((r) => {
        // 위치 미확정(localization 발산/리셋 등)이면 발산하는 라이브 좌표 대신
        // 마지막 신뢰 위치에 고정 표시한다(없으면 라이브로 폴백).
        const uncertain = !!r.initposePending;
        const src = uncertain && r.trustedPosition ? r.trustedPosition : r.position;
        return { r, src, uncertain };
      })
      .filter(({ r, src }) =>
        r.currentFloorId === selectedFloor.id
        // 같은 층이라도 다른 맵에 있는 로봇은 좌표계가 달라 이 이미지 위에 그리지 않는다(맵 모르면 층 기준)
        && (r.currentMapId == null || activeMapId == null || r.currentMapId === activeMapId)
        && (src?.timestamp ?? 0) > 0
        && r.power === "On"
        // IN_TRANSIT/MAP_SWITCH 구간엔 선택 로봇 좌표가 측위 freeze로 무의미 → 마커 숨김
        && !(opState?.inTransit && r.id === selectedRobotId)
      )
      .map(({ r, src, uncertain }) => ({
        id: r.id,
        name: r.no,
        position: { x: src.x, y: src.y, yaw: src.yaw },
        uncertain,
      }));
  }, [robots, selectedFloor?.id, activeMapId, opState?.inTransit, selectedRobotId, statusStale]);

  // 노드망 — 보고 있는 맵이 바뀔 때마다
  useEffect(() => {
    if (activeMapId == null) { setNavNetwork(null); return; }
    let cancelled = false;
    apiFetch(`/map/nav-nodes/${activeMapId}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (cancelled || !d) return;
        setNavCheck(d.check ?? null);
        setNavEnabled(!!d.enabled);
        setNavNetwork({
          nodes: d.nodes.map((n: { id: number; x: number; y: number }) => ({ id: String(n.id), x: n.x, y: n.y })),
          edges: d.edges.map((e: { a: number; b: number; kind: string; direction?: "both" | "forward" }) =>
            ({ a: String(e.a), b: String(e.b), kind: e.kind, direction: e.direction ?? "both" })),
        });
      })
      .catch(() => { if (!cancelled) setNavNetwork(null); });
    return () => { cancelled = true; };
  }, [activeMapId]);

  // 장소 카드 — 이 장소로 갈 수 없는 이유(노드망 미완성 맵 / 노드에 안 붙은 장소)
  const poiBlockedReason = useCallback((poi: POIItem): string | null => {
    if (!navEnabled || !navCheck) return null;
    if (!navCheck.ready) return `이 맵은 노드망이 완성되지 않아 자율 이동을 할 수 없습니다. ${navCheck.reason}`;
    if (navCheck.unlinkedPlaceIds.includes(poi.id)) return "노드망에 연결되지 않은 장소입니다. 맵 편집에서 근처에 노드를 추가하면 이동할 수 있습니다.";
    return null;
  }, [navCheck, navEnabled]);

  // 이동 진행 — 2초마다(도착 확인·자동문 오버레이와 같은 주기)
  useEffect(() => {
    let cancelled = false;
    const tick = () => apiFetch(`/nav/progress`).then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (!cancelled) setProgress(d); }).catch(() => {});
    tick();
    const t = setInterval(tick, 2000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  const selectedRobot = robots.find((r) => r.id === selectedRobotId) ?? null;
  const activeRoute: ActiveRoute | null = useMemo(() => {
    if (!progress?.isNavigating || !selectedRobot || progress.robotName !== selectedRobot.no || !progress.start) return null;
    const b = progress.bundle;
    const pts = progress.waypoints.map((w, i) => ({ w, no: i + 1 }))
      .filter(({ w }) => w.x != null && w.y != null && (w.mapId == null || w.mapId === activeMapId));
    if (pts.length === 0) return null;
    return {
      start: progress.start,
      points: pts.map(({ w, no }) => ({
        x: w.x!, y: w.y!, name: w.name, status: w.status, node: !!w.node,
        inBundle: !!b && no >= b.from && no <= b.to,
      })),
      robot: selectedRobot.position?.timestamp ? { x: selectedRobot.position.x, y: selectedRobot.position.y } : null,
    };
  }, [progress, selectedRobot, activeMapId]);
  const routeChip = activeRoute && progress ? (() => {
    const nodes = activeRoute.points.filter((p) => p.node);
    const passedNodes = nodes.filter((p) => p.status === "completed").length;
    return { goal: activeRoute.points[activeRoute.points.length - 1].name, passed: passedNodes, total: nodes.length, remain: progress.remainM };
  })() : null;

  // 선택 로봇의 currentFloorId를 자동 추종한다(뷰 override 중이 아닐 때).
  // 사용자가 다른 층 탭을 눌러 override하면 추종을 멈추고, "로봇 층으로"로 복귀할 수 있다.
  // (기존: robotFloorApplied ref로 최초 1회만 반영 → 로봇이 층을 옮겨도 지도가 안 따라가던 문제)
  useEffect(() => {
    if (!hasFloors || robotFloorId == null || userFloorOverride) return;
    const idx = floors.findIndex((f) => f.id === robotFloorId);
    if (idx >= 0 && floors[idx].id !== selectedFloor?.id) {
      setFloorActiveIndex(idx);
      setSelectedFloor(floors[idx]);
    }
  }, [robotFloorId, hasFloors, userFloorOverride, selectedFloor?.id]);

  // 선택 로봇이 바뀌면 뷰 override를 해제하고 그 로봇의 층을 다시 추종한다.
  useEffect(() => {
    setUserFloorOverride(false);
  }, [selectedRobotId]);

  // 선택된 층의 맵 로드 (캐시 있으면 스킵)
  useEffect(() => {
    if (!selectedFloor) { setMapLoading(false); return; }

    // 층 이동 시 이전 canvas를 완전히 언마운트하고 로딩 오버레이로 가린 뒤
    // 새 맵을 마운트한다. React의 commit/paint 타이밍상 key 리마운트만으로는
    // 이전 프레임이 몇 ms 남아 보일 수 있어, 명시적으로 mapConfig=null 경유.
    setMapConfig(null);
    setActiveMapId(null);
    setMapLoading(true);

    // 캐시 히트 → 다음 tick에 새 config 세팅 (이 사이 오버레이가 캔버스 전환을 가림)
    const floorId = selectedFloor.id;
    const cached = getCachedMap(floorId, preferredMapId);
    if (cached !== undefined) {
      const t = setTimeout(() => {
        setMapConfig(cached?.config ?? null);
        setActiveMapId(cached?.mapId ?? null);
        setMapLoading(false);
      }, 80);
      return () => clearTimeout(t);
    }

    // 캐시 미스 → fetch
    let cancelled = false;

    (async () => {
      try {
        let list = _cache.floorMaps[floorId];
        if (!list || (preferredMapId != null && !list.some((m) => m.id === preferredMapId))) {
          const mapsRes = await apiFetch(`/map/maps?floor_id=${floorId}`);
          const maps = await mapsRes.json();
          if (cancelled) return;
          list = Array.isArray(maps) ? maps : [];
          _cache.floorMaps[floorId] = list;
        }
        const map = pickMap(list, preferredMapId);
        if (!map) {
          setMapConfig(null);
          setMapLoading(false);
          return;
        }

        const imgPath = map.ImgFilePath?.replace("./", "/") || map.PgmFilePath?.replace("./", "/");

        const metaRes = await apiFetch(`/map/maps/${map.id}/meta`);
        const meta = await metaRes.json();
        if (cancelled) return;

        // 이미지 프리로드
        const img = new Image();
        img.crossOrigin = "anonymous";
        img.src = `${getApiBase()}${imgPath}`;
        await new Promise<void>((resolve) => {
          img.onload = () => resolve();
          img.onerror = () => resolve();
        });
        if (cancelled) return;

        const config: MapConfig = {
          imageSrc: `${getApiBase()}${imgPath}`,
          resolution: meta.resolution ?? 0.1,
          originX: meta.originX ?? 0,
          originY: meta.originY ?? 0,
          pixelWidth: img.naturalWidth || 335,
          pixelHeight: img.naturalHeight || 450,
        };

        // 캐시에 저장
        _cache.mapConfigs[map.id] = config;

        setActiveMapId(map.id);
        setMapConfig(config);
        setMapLoading(false);
      } catch {
        if (!cancelled) setMapLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // 같은 층에서 다른 맵으로 활성화하면(preferredMapId 변경) 다시 그린다.
  }, [selectedFloor?.id, preferredMapId]);

  // 장소 데이터 — 캐시로 먼저 그리고 매번 재조회(맵 관리의 삭제·수정 반영)
  useEffect(() => {
    apiFetch(`/DB/places`)
      .then((res) => res.ok ? res.json() : [])
      .then((data: any[]) => {
        const mapped: POIItem[] = data.map((p) => ({
          id: p.id,
          name: p.LacationName ?? "",
          x: p.LocationX ?? 0,
          y: p.LocationY ?? 0,
          floor: p.Floor ?? "",
          floorId: p.FloorId ?? null,
          mapId: p.MapId ?? null,
          // 맵관리와 동일한 카테고리 아이콘·색을 쓰도록 실제 Category 전달(미등록은 work).
          category: toPoiCategory(p.Category),
        }));
        _cache.places = mapped;
        setPlaces(mapped);
      })
      // 재조회 실패 시 캐시가 있으면 그대로 둔다(빈 화면으로 덮지 않음)
      .catch(() => { if (!_cache.places) setPlaces([]); });
  }, []);

  // 구간(경로) 데이터 — 캐시로 먼저 그리고 매번 재조회
  useEffect(() => {
    if (!activeMapId) { setNavPath(null); return; }

    // 캐시가 있으면 즉시 그리고, 없으면 이전 층의 navPath가 새 씬에 그려지지 않도록 비운다
    const cached = _cache.navPaths[activeMapId];
    setNavPath(cached !== undefined ? cached : null);

    let cancelled = false;
    (async () => {
      try {
        const res = await apiFetch(`/DB/routes?map_id=${activeMapId}`);
        const routes: { StartPlaceName: string; EndPlaceName: string; Direction: string }[] = await res.json();
        if (cancelled || !routes.length) {
          _cache.navPaths[activeMapId] = null;
          setNavPath(null);
          return;
        }

        // 구간 끝점은 이 맵의 장소에서만 찾는다(같은 이름이 다른 맵·사업장에 있을 수 있음)
        const mapPlaces = places.filter((p) => p.mapId === activeMapId);
        const segments: NavPathSegment[] = routes
          .map((r): NavPathSegment | null => {
            const from = mapPlaces.find((p) => p.name === r.StartPlaceName);
            const to = mapPlaces.find((p) => p.name === r.EndPlaceName);
            if (!from || !to) return null;
            return {
              from: { x: from.x, y: from.y, name: from.name },
              to: { x: to.x, y: to.y, name: to.name },
              direction: r.Direction === "bidirectional" ? "two-way" : "one-way",
            };
          })
          .filter((s): s is NavPathSegment => s !== null);

        const path = segments.length > 0 ? { segments } : null;
        _cache.navPaths[activeMapId] = path;
        if (!cancelled) setNavPath(path);
      } catch {
        if (!cancelled) setNavPath(null);
      }
    })();
    return () => { cancelled = true; };
  }, [activeMapId, places]);

  // 경로 목록 (작업유형 필터용). 캐시로 먼저 그리고 매번 재조회.
  useEffect(() => {
    apiFetch(`/DB/paths`)
      .then((res) => res.ok ? res.json() : [])
      .then((data: any[]) => {
        const mapped: PathRow[] = data.map((p) => ({
          id: p.id,
          taskType: resolveTaskTypeLabel(p.TaskType),
          wayPoints: p.WayPoints ?? "",
          floorId: p.FloorId ?? null,
        }));
        _cache.paths = mapped;
        setPaths(mapped);
      })
      .catch(() => { if (!_cache.paths) setPaths([]); });
  }, []);

  // 작업유형 필터가 켜진 경우, 해당 유형 경로에 등장하는 장소 이름 집합과
  // 인접 (a,b) 쌍 집합을 만들어 POI/구간 필터링에 사용.
  // 필터가 없으면 null 반환 (= 제한 없음).
  const taskFilterSets = useMemo(() => {
    if (!taskTypeFilter) return null;
    const placeNames = new Set<string>();
    const pairs = new Set<string>(); // "a||b" — 정렬된 쌍으로 양방향 매칭
    for (const p of paths) {
      if (p.taskType !== taskTypeFilter) continue;
      const names = p.wayPoints.split(" - ").map((s) => s.trim()).filter(Boolean);
      for (let i = 0; i < names.length; i++) {
        placeNames.add(names[i]);
        if (i > 0) {
          const a = names[i - 1];
          const b = names[i];
          const key = a < b ? `${a}||${b}` : `${b}||${a}`;
          pairs.add(key);
        }
      }
    }
    return { placeNames, pairs };
  }, [paths, taskTypeFilter]);

  // 선택된 층의 장소만 필터 — useMemo로 레퍼런스 안정화
  // (매 렌더마다 새 배열이 만들어지면 CanvasMap/Map3DCanvas의 POI effect가
  //  불필요하게 재실행되어 씬이 깜빡이는 원인이 됨)
  const floorPois = useMemo(() => {
    if (!selectedFloor || activeMapId == null) return [];
    // 현재 표시 중인 맵의 POI만 그린다. 한 층에 맵이 여러 개일 수 있으므로 floorId가 아닌
    // mapId(activeMapId) 기준으로 필터해야 다른 맵의 POI가 새어 표시되지 않는다.
    const base = places.filter((p) => p.mapId === activeMapId);
    if (!taskFilterSets) return base;
    // 충전소(category=charge)와 그 파생 지점은 작업유형 필터와 무관하게 항상 표시한다.
    // 파생 지점 명명 규칙(백엔드 charge.py): '<충전소>-<숫자>' — '-1'=도킹포인트, '-2,3…'=접근 경유지.
    // 파생 지점의 category 는 charge 가 아닐 수 있어(예: work/waypoint) 이름 패턴으로 매칭한다.
    const chargeNames = base.filter((p) => p.category === "charge").map((p) => p.name);
    const isChargeRelated = (name: string) =>
      chargeNames.some((cn) => {
        if (!cn || !name.startsWith(`${cn}-`)) return false;
        const suffix = name.slice(cn.length + 1);
        return suffix.length > 0 && /^\d+$/.test(suffix);
      });
    return base.filter(
      (p) =>
        p.category === "charge" ||
        isChargeRelated(p.name) ||
        taskFilterSets.placeNames.has(p.name)
    );
    // base가 activeMapId로 필터되므로 의존성에 포함해야 맵 전환이 반영된다.
  }, [places, activeMapId, selectedFloor?.id, taskFilterSets]);

  // 작업유형 필터 적용된 NavPath. 필터된 인접 쌍에 속한 segment만 통과.
  const filteredNavPath = useMemo<NavPath | null>(() => {
    if (!navPath) return null;
    if (!taskFilterSets) return navPath;
    const segments = navPath.segments.filter((s) => {
      const a = s.from.name;
      const b = s.to.name;
      if (!a || !b) return false;
      const key = a < b ? `${a}||${b}` : `${b}||${a}`;
      return taskFilterSets.pairs.has(key);
    });
    return segments.length > 0 ? { segments } : null;
  }, [navPath, taskFilterSets]);

  const handleFloorSelect = (idx: number, floor: Floor) => {
    // 층 변경 시 mapConfig/activeMapId를 즉시 null 로 만들어
    // CanvasMap을 곧바로 언마운트시킨다. 이렇게 하지 않으면
    // (setSelectedFloor만 먼저 반영되는) 한 프레임 동안
    // selectedFloor=새 층 + mapConfig=이전 층 상태가 되어,
    // floorPois는 새 층의 POI인데 좌표 변환은 이전 층 config로 계산되어
    // POI가 엉뚱한 위치에 잠깐 보이는 잔상이 생긴다.
    setMapConfig(null);
    setActiveMapId(null);
    setMapLoading(true);
    setFloorActiveIndex(idx);
    setSelectedFloor(floor);
    // 선택 로봇 층과 다른 층을 고르면 뷰 override(추종 중단). 같으면 추종 복귀.
    setUserFloorOverride(robotFloorId != null && floor.id !== robotFloorId);
    mapRef.current?.handleZoom("reset");
  };

  // 선택 로봇이 있는 층 라벨 (뷰 전용 배너 표시용)
  const robotFloorLabel = useMemo(
    () => (robotFloorId != null ? floors.find((f) => f.id === robotFloorId)?.label ?? null : null),
    [robotFloorId, floors]
  );

  const handleReturnToRobotFloor = () => {
    if (robotFloorId == null) return;
    const idx = floors.findIndex((f) => f.id === robotFloorId);
    if (idx >= 0) handleFloorSelect(idx, floors[idx]);
  };

  const handleZoomFromChild = (action: string) => {
    mapRef.current?.handleZoom(action as "in" | "out" | "reset");
  };

  const handlePoiNavigate = useCallback(async (poi: POIItem) => {
    // 노드망 경로 미리보기 — 갈 수 없으면 이유를 보이고, 직선의 2배를 넘으면 한 번 확인받는다
    const me = robots.find((r) => r.id === selectedRobotId);
    if (navEnabled && navNetwork && navNetwork.nodes.length > 0 && activeMapId != null && me?.position?.timestamp) {
      try {
        const pv = await apiFetch(`/map/nav-nodes/${activeMapId}/preview`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ from_x: me.position.x, from_y: me.position.y, to_place_id: poi.id }),
        }).then((r) => r.json());
        if (!pv.ok) { modalAlert(pv.msg ?? "노드망 경로를 만들 수 없습니다."); return; }
        if (pv.ratio != null && pv.ratio > 2) {
          modalConfirm(
            `'${poi.name}'까지 노드망 경로가 직선의 ${pv.ratio.toFixed(1)}배입니다 (${pv.lengthM.toFixed(1)} m · 통과 지점 ${pv.waypoints?.length ?? 0}개).\n벽 때문에 돌아가는 길입니다. 이 경로로 이동할까요?`,
            () => { void sendPlaceMove(poi); },
          );
          return;
        }
      } catch {
        // 미리보기 실패는 이동을 막지 않는다 — 서버가 출발 전에 다시 확인한다
      }
    }
    await sendPlaceMove(poi);
  }, [modalAlert, modalConfirm, robots, selectedRobotId, navNetwork, navEnabled, activeMapId]);

  const sendPlaceMove = async (poi: POIItem) => {
    try {
      const res = await apiFetch(`/nav/placemove/${poi.id}`, { method: "POST" });
      const data = await res.json();
      // 백엔드는 거부(위치 초기화 필요 등)도 200 + status:"error" 로 돌려준다 — 화면에 보여야 한다.
      if (!res.ok || data?.status === "error") {
        modalAlert(data?.msg ?? "장소 이동을 시작하지 못했습니다.");
        return;
      }
      console.log("장소 이동 명령 전송:", data.msg ?? data.status);
    } catch (err) {
      console.error("장소 이동 실패:", err);
      modalAlert("장소 이동 요청이 실패했습니다. 네트워크를 확인하세요.");
    }
  };

  return (
    <div className={styles["middle-div"]}>
      <div className={styles["view-div"]}>
        {!hasRobots && (
          <div className={styles.emptyOverlay}>
            <span>등록된 로봇이 없습니다.</span>
            <span className={styles.emptySubText}>로봇을 등록하면 위치를 확인할 수 있습니다.</span>
          </div>
        )}

        {hasRobots && !hasFloors && (
          <div className={styles.emptyOverlay}>
            <span>등록된 층이 없습니다.</span>
            <span className={styles.emptySubText}>층 정보를 등록하면 맵을 확인할 수 있습니다.</span>
          </div>
        )}

        {hasRobots && hasFloors && !mapConfig && !mapLoading && (
          <div className={styles.emptyOverlay}>
            <span>해당 층에 등록된 맵이 없습니다.</span>
          </div>
        )}

        {hasRobots && hasFloors && mapLoading && (
          <div className={styles.loadingOverlay}>
            <div className={styles.loadingSpinner} />
            <span>지도를 불러오는 중...</span>
          </div>
        )}

        <button
          type="button"
          className={`${styles.stopAllBtn} ${!hasWorkingRobot ? styles.stopAllBtnDisabled : ""}`}
          onClick={handleStopAll}
          disabled={!hasWorkingRobot}
        >
          로봇 전체 정지
        </button>

        {hasFloors && (
          <div className={styles.floorList}>
            {[...floors].reverse().map((floor) => {
              const originalIdx = floors.indexOf(floor);
              const isRobotFloor = floor.id === robotFloorId;
              return (
                <button
                  key={floor.id}
                  className={`${styles.floorBtn} ${originalIdx === floorActiveIndex ? styles.floorBtnActive : ""} ${isRobotFloor ? styles.floorBtnRobot : ""}`}
                  onClick={() => handleFloorSelect(originalIdx, floor)}
                  title={
                    isRobotFloor
                      ? `선택 로봇${selectedRobotName ? `(${selectedRobotName})` : ""}이 ${statusStale ? "마지막으로 확인된" : "있는"} 층`
                      : undefined
                  }
                >
                  <span className={styles.floorBtnLabel}>{floor.label}</span>
                  {isRobotFloor && (
                    <span className={styles.floorRobotName}>{selectedRobotName ? selectedRobotName.slice(-6) : "로봇"}</span>
                  )}
                </button>
              );
            })}
          </div>
        )}

        {hasFloors && robotFloorId != null && selectedFloor && selectedFloor.id !== robotFloorId && (
          <div className={styles.viewOnlyBanner}>
            <span className={styles.viewOnlyDot} />
            <span>
              보기 전용 — 선택 로봇은 <b>{robotFloorLabel ?? "다른 층"}</b>에 있습니다. 로봇은 움직이지 않습니다.
            </span>
            <button type="button" className={styles.viewOnlyBtn} onClick={handleReturnToRobotFloor}>
              로봇 층으로
            </button>
          </div>
        )}

        {opState && (
          <ElevatorOpOverlay state={opState} robotName={selectedRobotName} onStopAll={handleStopAll} />
        )}

        {!opState && doorOpState && (
          <DoorOpOverlay state={doorOpState} robotName={selectedRobotName} onChanged={refreshDoorOp} />
        )}

        {/* 층 전환·자동문 통과가 우선 — 그 둘은 로봇이 이동 중이고, 도착 확인은 서 있는 상태다.
            셋이 동시에 뜰 일은 없지만 겹치면 '움직이는 쪽'을 먼저 보여준다. */}
        {!opState && !doorOpState && arrivalGate && (
          <ArrivalGateOverlay
            state={arrivalGate}
            robotName={arrivalGate.robotName ?? selectedRobotName}
            onChanged={refreshArrivalGate}
          />
        )}

        {/* 노드망 미완성 맵 — 자율 이동 불가 */}
        {navEnabled && navCheck && !navCheck.ready && (
          <div className={styles.viewOnlyBanner} role="alert" style={{ borderColor: "#BF0E14", background: "rgba(148,22,33,0.55)" }}>
            <span className={styles.viewOnlyDot} style={{ background: "#ff4b4b" }} />
            <span>노드망 미완성 — 이 맵에서는 자율 이동을 할 수 없습니다. <b>{navCheck.reason}</b></span>
            <a href="/mapManagement?tab=map" className={styles.viewOnlyBtn}>맵 편집 열기</a>
          </div>
        )}

        {routeChip && (
          <div className={styles.routeChip} role="status">
            <span className={styles.routeChipDot} />
            <b>{selectedRobot?.no}</b>
            <span>→ {routeChip.goal}</span>
            <span className={styles.routeChipSep}>·</span>
            <span>통과 지점 {routeChip.passed}/{routeChip.total}</span>
            {routeChip.remain != null && (<><span className={styles.routeChipSep}>·</span><span>남은 {routeChip.remain.toFixed(1)} m</span></>)}
          </div>
        )}

        {mapConfig && (
          // key로 mapId + view를 합친 값을 사용 → 층 이동 또는 2D↔3D 전환 시
          // CanvasMap 전체가 언마운트/리마운트되어 이전 씬 잔상이 남지 않음
          <CanvasMap
            key={`map-${activeMapId ?? 0}-${mapView}`}
            ref={mapRef}
            config={mapConfig}
            view={mapView}
            robots={floorRobots}
            pois={floorPois}
            // 노드망이 있는 맵은 구간 선을 그리지 않는다 — 노드망이 구간을 대신한다(세 방식 통합)
            navPath={navEnabled && navNetwork && navNetwork.nodes.length > 0 ? null : filteredNavPath}
            navNetwork={navNetwork}
            networkLevel={networkLevel}
            activeRoute={activeRoute}
            poiBlockedReason={poiBlockedReason}
            showPois
            showPath
            showLabels
            onPoiNavigate={
              // 보고 있는 층이 로봇 층과 다르면 이동을 걸지 않는다 — 다층 이동은 경로·작업으로만(Q8)
              selectedFloor?.id === robotFloorId ? handlePoiNavigate : undefined
            }
          />
        )}
        {hasRobots && hasFloors && mapConfig && (
          <div ref={taskMenuRef} className={styles.taskFilter}>
            <button
              type="button"
              className={styles.taskFilterBtn}
              onClick={() => setTaskMenuOpen((v) => !v)}
              aria-haspopup="listbox"
              aria-expanded={taskMenuOpen}
            >
              <span className={styles.taskFilterLabel}>{taskTypeFilter ?? "전체"}</span>
              <span className={`${styles.taskFilterCaret} ${taskMenuOpen ? styles.taskFilterCaretOpen : ""}`} />
            </button>
            {taskMenuOpen && (
              <div className={styles.taskFilterMenu} role="listbox">
                <button
                  type="button"
                  className={`${styles.taskFilterItem} ${taskTypeFilter === null ? styles.taskFilterItemActive : ""}`}
                  onClick={() => { setTaskTypeFilter(null); setTaskMenuOpen(false); }}
                  role="option"
                  aria-selected={taskTypeFilter === null}
                >
                  전체
                </button>
                {TASK_TYPE_OPTIONS.map((t) => (
                  <button
                    key={t}
                    type="button"
                    className={`${styles.taskFilterItem} ${taskTypeFilter === t ? styles.taskFilterItemActive : ""}`}
                    onClick={() => { setTaskTypeFilter(t); setTaskMenuOpen(false); }}
                    role="option"
                    aria-selected={taskTypeFilter === t}
                  >
                    {t}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {hasRobots && hasFloors && (
          <ZoomControl
            onClick={handleZoomFromChild}
            mapView={mapView}
            onToggleView={() => setMapView((v) => (v === "2d" ? "3d" : "2d"))}
          >
            {navNetwork && navNetwork.nodes.length > 0 && (
              <button type="button" className={zoomIconClass}
                onClick={() => setNetworkLevel((l) => (l === "faint" ? "strong" : l === "strong" ? "off" : "faint"))}
                title={`노드망 표시: ${networkLevel === "off" ? "숨김" : networkLevel === "faint" ? "흐리게" : "또렷하게"} (눌러서 바꾸기)`}
                aria-label="노드망 표시 단계 바꾸기"
                style={networkLevel === "off" ? { opacity: 0.55 } : networkLevel === "strong" ? { boxShadow: "inset 0 0 0 2px #7fd0ff" } : undefined}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#ffffff" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                  <circle cx="5" cy="6" r="2" /><circle cx="19" cy="6" r="2" /><circle cx="12" cy="18" r="2" />
                  <path d="M7 6h10M6 8l5 8M18 8l-5 8" />
                </svg>
              </button>
            )}
          </ZoomControl>
        )}
      </div>

      {stopAllConfirmOpen && (
        <StopAllConfirmModal
          isOpen={stopAllConfirmOpen}
          message={"연결된 모든 로봇의 작업을 긴급 정지합니다.\n정말 진행하시겠습니까?"}
          onConfirm={handleStopAllConfirm}
          onCancel={() => setStopAllConfirmOpen(false)}
        />
      )}

      <AlertModal
        open={modal.open}
        message={modal.message}
        mode={modal.mode}
        onConfirm={modal.mode === "alert" ? closeModal : handleConfirm}
        onClose={closeModal}
      />
    </div>
  );
}
