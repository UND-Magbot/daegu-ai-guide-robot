"use client";

import React, { useEffect, useState } from "react";
import { useAuth } from "@/app/context/AuthContext";
import { useRobotStatusContext } from "@/app/context/RobotStatusContext";
import { useToast } from "@/app/components/common/Toast";
import RelocalizeModal from "@/app/components/modal/RelocalizeModal";
import { apiFetch } from "@/app/lib/api";
import {
  useFloorMapConfig,
  type FloorMapRow,
} from "@/app/(pages)/mapManagement/hooks/useFloorMapConfig";
import styles from "./voice.module.css";
import g from "./guideScenario.module.css";
import type { GuideFloor, GuideMap, GuideScenario } from "./guideScenarioApi";

type Props = {
  robotId: number;
  robotName: string;
  scenario: GuideScenario | null;
  floors: GuideFloor[];
  maps: GuideMap[];
  /** 진행 중인 안내가 있으면 맵을 바꾸지 않는다 */
  sessionActive: boolean;
  /** 맵 전환·위치 확정 뒤 부모가 로봇 위치를 다시 읽는다 */
  onChanged: () => void;
};

type Pending = "transit" | "arrived" | null;

const nameOf = (list: { id: number; name: string }[], id: number | null | undefined, fallback: string) =>
  list.find((x) => x.id === id)?.name ?? fallback;

/**
 * 안내 시나리오 화면에서 바로 하는 층 이동 — 대시보드 '층 변경'·'위치 재조정'과 같은 API 를 쓴다.
 *
 * 1층 안내가 엘리베이터 입구에서 끝나면 여기서 도착층 맵으로 '미리 전환'해 두고(측위 재시작을
 * 엘리베이터 이동 시간에 숨긴다), 도착층에서 '도착 · 위치 재조정'으로 위치를 맞춘 뒤 시나리오를 시작한다.
 */
export default function GuideFloorMoveBar({
  robotId,
  robotName,
  scenario,
  floors,
  maps,
  sessionActive,
  onChanged,
}: Props) {
  const { user } = useAuth();
  const { robots, refresh: refreshRobots } = useRobotStatusContext();
  const { showToast } = useToast();
  const [confirm, setConfirm] = useState<Pending>(null);
  const [busy, setBusy] = useState(false);
  const [showRelocalize, setShowRelocalize] = useState(false);
  // '도착 · 위치 재조정'으로 열었는지 — 그때만 엘리베이터 입구를 첫 시드로 넣는다
  const [seedLobby, setSeedLobby] = useState(false);
  const [mapRows, setMapRows] = useState<FloorMapRow[]>([]);

  const robot = robots.find((r) => r.id === robotId);
  const canManage = user?.role === 1 || user?.role === 2;
  const targetMapId = scenario?.mapId ?? null;
  const relocalizeMapConfig = useFloorMapConfig(showRelocalize ? targetMapId : null, mapRows);

  useEffect(() => {
    if (!showRelocalize || scenario?.floorId == null) return;
    let cancelled = false;
    apiFetch(`/map/maps?floor_id=${scenario.floorId}`)
      .then((r) => (r.ok ? r.json() : []))
      .then((rows: FloorMapRow[]) => {
        if (!cancelled) setMapRows(Array.isArray(rows) ? rows : []);
      })
      .catch(() => {
        if (!cancelled) setMapRows([]);
      });
    return () => {
      cancelled = true;
    };
  }, [showRelocalize, scenario?.floorId]);

  // 시나리오를 바꾸면 열려 있던 확인을 닫는다
  useEffect(() => setConfirm(null), [scenario?.code]);

  if (!scenario || !targetMapId || !robot) return null;

  const onTargetMap = robot.currentMapId === targetMapId;
  const pending = !!robot.initposePending;
  // 맵도 맞고 위치도 확정이면 할 일이 없다
  if (onTargetMap && !pending) return null;

  const targetName = `${nameOf(floors, scenario.floorId, "")} ${nameOf(maps, targetMapId, "")}`.trim();
  const hereName = `${nameOf(floors, robot.currentFloorId, "층 미지정")} / ${nameOf(
    maps,
    robot.currentMapId,
    "맵 미지정"
  )}`;

  const activate = async (transit: boolean) => {
    setBusy(true);
    try {
      const res = await apiFetch(`/map/maps/activate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          map_id: targetMapId,
          robot_id: robotId,
          reason: `안내 시나리오 '${scenario.displayName}' ${transit ? "층 이동(엘리베이터 탑승 전 사전 전환)" : "도착층 맵 지정"}`,
          transit,
        }),
        // 측위 재시작 대기 포함 약 15~20초, SSH 재시도까지 감안
        timeoutMs: 90000,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "맵 전환에 실패했습니다");
      showToast(
        transit
          ? `${targetName}(으)로 미리 전환했습니다. 이제 엘리베이터에 타세요.`
          : `${targetName}(으)로 전환했습니다. 위치를 맞춰 주세요.`,
        "success"
      );
      setConfirm(null);
      await refreshRobots().catch(() => {});
      onChanged();
      // 이미 도착한 경우엔 바로 위치를 맞춘다
      if (!transit) {
        setSeedLobby(false);
        setShowRelocalize(true);
      }
    } catch (e) {
      showToast(e instanceof Error ? e.message : "맵 전환에 실패했습니다", "error");
    } finally {
      setBusy(false);
    }
  };

  const closeRelocalize = () => {
    setShowRelocalize(false);
    setSeedLobby(false);
    setMapRows([]);
  };

  return (
    <>
      <div className={`${g.sessionCard} ${g.floorMove}`}>
        <div className={g.sessionInfo}>
          {onTargetMap ? (
            <>
              <span className={g.sessionTitle}>
                {robot.floorTransit ? "층 이동 중" : "로봇 위치 미확정"}
              </span>
              <span className={g.sessionMeta}>
                {robot.floorTransit
                  ? `${targetName} 맵으로 전환을 마쳤습니다. 엘리베이터로 이동하고, 도착해 Wi-Fi가 다시 연결되면 로봇을 세운 뒤 위치를 맞추세요.`
                  : "위치를 맞춰야 안내를 시작할 수 있습니다."}
              </span>
            </>
          ) : (
            <>
              <span className={g.sessionTitle}>층 이동 — {targetName}</span>
              <span className={g.sessionMeta}>
                로봇은 {hereName}에 있습니다.
                {confirm === "transit" &&
                  " 측위가 재시작되고, 도착해 위치를 맞출 때까지 자율주행이 막힙니다. 전환이 끝난 뒤 엘리베이터에 타세요."}
                {confirm === "arrived" && " 측위가 재시작됩니다. 로봇이 이미 도착층에 있을 때만 쓰세요."}
              </span>
            </>
          )}
        </div>

        <div className={g.sessionActions}>
          {!canManage ? (
            <span className={g.mapWarn}>층 이동은 관리자 권한이 필요합니다.</span>
          ) : onTargetMap ? (
            <button className={styles.primaryBtn} onClick={() => { setSeedLobby(true); setShowRelocalize(true); }}>
              도착 · 위치 재조정
            </button>
          ) : busy ? (
            <span className={g.mapWarn}>맵 전환 중(약 15초) — 끝날 때까지 엘리베이터에 타지 마세요</span>
          ) : confirm ? (
            <>
              <button className={`${styles.workBtn} ${styles.ghost}`} onClick={() => setConfirm(null)}>
                취소
              </button>
              <button className={styles.primaryBtn} onClick={() => activate(confirm === "transit")}>
                {confirm === "transit" ? "미리 전환" : "층 변경"}
              </button>
            </>
          ) : (
            <>
              <button
                className={`${styles.workBtn} ${styles.ghost}`}
                onClick={() => setConfirm("arrived")}
                disabled={sessionActive}
                title={sessionActive ? "진행 중인 안내가 끝난 뒤 바꿀 수 있습니다" : "로봇이 이미 도착층에 있을 때"}
              >
                이미 도착함 · 층 변경
              </button>
              <button
                className={styles.primaryBtn}
                onClick={() => setConfirm("transit")}
                disabled={sessionActive}
                title={sessionActive ? "진행 중인 안내가 끝난 뒤 바꿀 수 있습니다" : undefined}
              >
                엘리베이터 타기 전 미리 전환
              </button>
            </>
          )}
        </div>
      </div>

      {showRelocalize && (
        <RelocalizeModal
          open
          mapConfig={relocalizeMapConfig}
          robotId={robotId}
          robotName={robotName}
          seedElevatorLobby={seedLobby}
          onClose={closeRelocalize}
          onConfirmed={() => {
            refreshRobots().catch(() => {});
            onChanged();
          }}
        />
      )}
    </>
  );
}
