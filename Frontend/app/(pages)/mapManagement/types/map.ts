import type { PendingPlace } from "../components/tabs/map/MapPlaceCreateModal";

// facility = 시설 장치(엘리베이터 + 자동문). 기존 딥링크 ?tab=elevator 는 facility 로 해석한다.
export type MapTab = "map" | "place" | "path" | "facility";

export type MappingState =
  | "idle"
  | "startModal"
  | "mappingModal"
  | "success"
  | "saveModal";

export type Business = {
  id: number;
  BusinessName: string;
};

export type FloorItem = {
  id: number;
  BusinessId: number;
  FloorName: string;
};

export type RobotMap = {
  id: number;
  BusinessId: number;
  FloorId: number;
  MapName: string;
  PgmFilePath: string;
  ImgFilePath: string;
};

export type Robot = {
  id: number;
  RobotName: string;
  ModelName: string;
  SerialNumber: string;
  CurrentFloorId: number | null;
  CurrentMapId?: number | null;
};

export type RouteDirection = "forward" | "reverse" | "bidirectional";

/** 맵 편집 화면의 DB 장소 행 (`GET /DB/places?map_id=`). */
export type MapPlaceRow = {
  id: number; LacationName: string; LocationX: number; LocationY: number; Yaw: number;
  RobotName?: string; Floor?: string; FloorId?: number; MapId?: number; Category?: string; Imformation?: string;
};

export type RouteSegment = {
  tempId: string;
  startName: string;
  endName: string;
  direction: RouteDirection;
};

export type DbRoute = {
  id: number;
  MapId: number;
  StartPlaceName: string;
  EndPlaceName: string;
  Direction: string;
};

export type UndoAction =
  | { type: "addPlace"; tempId: string }
  | {
      type: "deletePendingPlace";
      place: PendingPlace;
      cascadedDbRoutes: number[];
      cascadedPendingRoutes: RouteSegment[];
    }
  | {
      type: "deleteDbPlace";
      id: number;
      cascadedDbRoutes: number[];
      cascadedPendingRoutes: RouteSegment[];
    }
  | {
      // 장소 수정 모달. 이름 변경이 장소·구간·이동 기록 이름에 두루 전파되므로 관련 상태를 통째로 기록한다.
      type: "editPlace";
      prevMapPlaces: MapPlaceRow[];
      prevPendingPlaces: PendingPlace[];
      prevPendingRoutes: RouteSegment[];
      prevDbRoutes: DbRoute[];
      prevMovedPlaces: Map<string, { x: number; y: number }>;
      prevModifiedDbIds: Set<number>;
      prevYawPinnedPlaces: Set<string>;
    }
  | { type: "addRoute"; tempId: string }
  | { type: "deletePendingRoute"; route: RouteSegment }
  | { type: "deleteDbRoute"; id: number }
  | {
      type: "mapReset";
      prevPendingPlaces: PendingPlace[];
      prevPendingRoutes: RouteSegment[];
      prevDeletedDbIds: Set<number>;
      prevDeletedRouteDbIds: Set<number>;
      prevMovedPlaces: Map<string, { x: number; y: number }>;
      prevModifiedDbIds: Set<number>;
    };
