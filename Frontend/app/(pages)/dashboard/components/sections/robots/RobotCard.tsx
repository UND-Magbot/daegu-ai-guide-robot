"use client";

import React, { useState } from "react";
import styles from "./RobotCardList.module.css";
import { openBatteryResumeModal } from "@/app/components/common/BatteryResumeAlertWatcher";
import type { RobotRowData, Camera, Video } from "@/app/types";
import { useAuth } from "@/app/context/AuthContext";
import {
  ROBOT_TYPE_COLOR,
  ROBOT_TYPE_INDEX,
  getBatteryColor,
  isCriticalBattery,
  isSingleBatteryMode,
} from "@/app/constants/robotIcons";
import { isDualBatteryType } from "@/app/constants/robotCapabilities";
import ChargingIcon from "@/app/components/common/ChargingIcon";
import { apiFetch } from "@/app/lib/api";
import { useRobotStatusContext } from "@/app/context/RobotStatusContext";
import { getNetworkBadge, isPowerOnConfirmed, isRobotLive, isStatusStale } from "@/app/lib/robotLiveness";
import { useModalAlert } from "@/app/hooks/useModalAlert";
import AlertModal from "@/app/components/modal/AlertModal";
import dynamic from "next/dynamic";

const PlacePathModal = dynamic(() => import("@/app/components/modal/PlacePathModal"), { ssr: false });
const BatteryPathModal = dynamic(() => import("@/app/components/modal/BatteryChargeModal"), { ssr: false });
const FloorChangeModal = dynamic(() => import("./FloorChangeModal"), { ssr: false });
const ReturnToWorkModal = dynamic(() => import("./ReturnToWorkModal"), { ssr: false });
const RemoteModal = dynamic(() => import("@/app/components/modal/remote/RemoteModal"), { ssr: false });

// 완충 판정 임계(%) — 이 이상이면 charge_state 5(도크·전류없음)를 트리클 off 정상(도크 유지)으로
// 보고 '충전 중'으로 표시한다. 완충 아닌데 state 5면 진짜 도킹 에러('전류 없음'). (백엔드 CHARGE_FULL_PCT 와 동일)
const CHARGE_FULL_PCT = 90;

const NO_CONTROL_TITLE = "로봇 제어 권한이 없습니다(권한 관리의 '로봇 목록' 권한 필요)";

/** 로봇 명령 응답 본문. 성공·거절 모두 이 모양 중 일부다. */
type CommandResult = { status?: string; msg?: string; detail?: unknown; was_active?: boolean } | null;

/** 서버 거절 사유. detail 은 문자열(권한·사업장) 또는 {message}(다른 관제 사용 중)다. */
function rejectMessage(data: CommandResult): string | null {
  const detail = data?.detail;
  if (typeof detail === "string") return detail;
  const message = (detail as { message?: unknown } | undefined)?.message;
  return typeof message === "string" ? message : null;
}

type RobotLocation = {
  floor: string | null;
  placeName: string | null;
};

type RobotCardProps = {
  robot: RobotRowData;
  isSelected: boolean;
  onClick: () => void;
  robots: RobotRowData[];
  video: Video[];
  cameras: Camera[];
  robotLocation: RobotLocation;
  floors?: { id: number; label: string }[];
  hasActiveSchedule?: boolean;
};

// 네트워크 배지 tone → CSS 모듈 클래스
const DOT_CLASS = {
  online: styles.dotOnline,
  error: styles.dotError,
  offline: styles.dotOffline,
  unknown: styles.dotUnknown,
} as const;

const BADGE_CLASS = {
  online: styles.badgeOnline,
  error: styles.badgeError,
  offline: styles.badgeOffline,
  unknown: styles.badgeUnknown,
} as const;

const ROBOT_ICONS = [
  "/icon/robot_icon(1).png",
  "/icon/robot_icon(2).png",
  "/icon/robot_icon(3).png",
  "/icon/robot_icon(4).png",
];

export default function RobotCard({ robot, isSelected, onClick, robots, video, cameras, robotLocation, floors = [], hasActiveSchedule = false }: RobotCardProps) {
  const { modal, modalAlert, closeModal } = useModalAlert();
  const { user, hasPermission } = useAuth();
  // 작업 복귀·충전소 이동/해제·장소 이동, 원격 제어의 제어권 획득은 서버가 robot-list 권한을 요구한다(없으면 403).
  // 누른 뒤 거절당하는 대신 미리 비활성화한다. 긴급 정지는 로그인만 있으면 되므로 막지 않는다.
  const canControl = hasPermission("robot-list");
  const controlTitle = canControl ? undefined : NO_CONTROL_TITLE;
  const { serverStatus } = useRobotStatusContext();
  // 관제 서버 응답이 없으면 카드의 로봇 값은 마지막 확인 시점의 잔존값이다.
  const statusStale = isStatusStale(serverStatus);
  // 원격 제어 노출 기준 — 운영관리 "원격" 버튼과 동일하게 role 1(관리자)·2(매니저) 허용
  const canRemote = user?.role === 1 || user?.role === 2;
  // 층 변경(파괴적: NOS active 맵 교체 + localization 재시작) — Manager(2)+ 에게만 노출
  const canForceFloor = user?.role === 1 || user?.role === 2;
  const currentFloorName = floors.find((f) => f.id === robot.currentFloorId)?.label ?? "층";
  const typeIdx = ROBOT_TYPE_INDEX[robot.type] ?? 0;
  const isUnregistered = robot.power === "-";
  const isPowerOff = robot.power === "Off";
  // 서버 두절 구간엔 전원 상태도 단정할 수 없다 → 조작 불가(비활성) 취급.
  const isInactive = isUnregistered || isPowerOff || statusStale;
  const netBadge = getNetworkBadge(robot.network, serverStatus, { unregistered: isUnregistered });
  const dotClass = DOT_CLASS[netBadge.tone];
  const badgeClass = BADGE_CLASS[netBadge.tone];
  const isOnline = isRobotLive(robot.network, serverStatus);
  const canRemoteNow = isPowerOnConfirmed(robot.power, serverStatus);

  // 작업 상태 파생
  const taskStatus = (() => {
    // 관제 서버 응답 없음 — 작업 상태도 잔존값이라 단정하지 않는다.
    if (statusStale) return { label: "확인 불가", className: styles.taskIdle, tooltip: netBadge.tooltip };
    if (robot.power === "-") return { label: "미확인", className: styles.taskIdle, tooltip: "" };
    if (robot.network === "Offline") return { label: "-", className: styles.taskIdle, tooltip: "" };
    // 전환 구간의 Error 는 '통신 끊김'을 뜻한다(전원 off 는 백엔드에서 Online 유지 → 여기 안 옴).
    if (robot.network === "Error") return { label: "끊김", className: styles.taskError, tooltip: "통신 연결이 끊겼습니다." };
    // 배터리 부족 충전 후 이어 갈지 운영자 결정을 기다린다 — 배지를 눌러 확인창을 연다
    if (robot.batteryResume) return { label: "⚠ 확인 필요", className: styles.taskAttention, tooltip: "배터리 부족 충전 후 하던 일을 이어 갈지 확인해 주세요 — 눌러서 확인" };
    // 충전 관련 상태 (chargeState: 1=도크 이동, 2=충전 중, 3=나가기, 4=오류, 5=전류 없음)
    if (robot.chargeState === 4) return { label: "충전 오류", className: styles.taskError, tooltip: robot.chargeErrorMsg ?? "" };
    if (robot.chargeState === 5) {
      // state 5(도크·전류없음): 완충이면 트리클 off 정상(도크 유지) → '충전 중'.
      // 미완충이면 '전류 없음'이나, 완충 트리클 오실레이션(정상)과 시각적으로 구분되지 않아
      // 사용자가 오류로 오인하므로 색은 '충전 중'과 동일(초록)으로 두고 라벨/툴팁으로만 구분한다.
      const battPct = isDualBatteryType(robot.type)
        ? Math.min(robot.batteryLeft ?? 0, robot.batteryRight ?? 0)
        : (robot.battery ?? 0);
      if (battPct >= CHARGE_FULL_PCT) return { label: "충전 중", className: styles.taskCharging, tooltip: "완충 — 충전소 도킹 유지 중" };
      return { label: "전류 없음", className: styles.taskCharging, tooltip: "도크에 있지만 전류가 흐르지 않음" };
    }
    if (robot.chargeState === 1) return { label: "도크로 이동", className: styles.taskDocking, tooltip: "" };
    if (robot.chargeState === 2) return { label: "충전 중", className: styles.taskCharging, tooltip: "" };
    if (robot.chargeState === 3) return { label: "도크에서 나가기", className: styles.taskDocking, tooltip: "" };
    // charge_state(relay 수신)가 충전/완충/도킹 상태(2·5·1·3)를 이미 표시하므로, is_charging/atDock
    // 폴백은 제거한다. (이전엔 언도킹 직후 charge_state=0(대기)인데 is_charging 이 sticky 로 남아
    //  '충전 중'이 잠깐 잘못 표시되는 잔상이 있었다.)
    if (robot.dockingTime > 0) return { label: "도킹 중", className: styles.taskDocking, tooltip: "" };
    // 충전소로 가는 중 — 도착(도킹) 전에는 '충전 중'이 아니다
    if (robot.returningToCharge) return { label: "충전소로 이동", className: styles.taskDocking, tooltip: "" };
    if (hasActiveSchedule || robot.tasks.length > 0 || robot.isNavigating) return { label: "작업 중", className: styles.taskWorking, tooltip: "" };
    return { label: "대기 중", className: styles.taskIdle, tooltip: "" };
  })();

  const [placeModalOpen, setPlaceModalOpen] = useState(false);
  const [chargeConfirmOpen, setChargeConfirmOpen] = useState(false);
  const [undockConfirmOpen, setUndockConfirmOpen] = useState(false);
  const [emergencyConfirmOpen, setEmergencyConfirmOpen] = useState(false);
  const [floorChangeOpen, setFloorChangeOpen] = useState(false);
  const [returnModalOpen, setReturnModalOpen] = useState(false);
  const [remoteOpen, setRemoteOpen] = useState(false);

  const handleScheduleReturn = (e: React.MouseEvent) => {
    e.stopPropagation();
    setReturnModalOpen(true);
  };

  /** 로봇 명령 요청 — 서버 거절(4xx·5xx, 200 + status=error)과 전송 실패를 알린다. */
  const postRobotCommand = (path: string, label: string, onOk?: (data: CommandResult) => void) => {
    apiFetch(path, { method: "POST" })
      .then(async (res) => {
        const data: CommandResult = await res.json().catch(() => null);
        // 401 은 apiFetch 가 세션 만료 흐름(로그인 화면)으로 처리한다 — 여기서 겹쳐 알리지 않는다
        if (res.status === 401) return;
        if (!res.ok) {
          modalAlert(rejectMessage(data) ?? `${label}을(를) 하지 못했습니다(오류 ${res.status}).`);
          return;
        }
        if (data?.status === "error") {
          modalAlert(data.msg ?? `${label}을(를) 하지 못했습니다.`);
          return;
        }
        onOk?.(data);
      })
      .catch((err) => {
        console.error(`${label} 실패`, err);
        modalAlert(`${label} 요청을 보내지 못했습니다. 네트워크 상태를 확인하세요.`);
      });
  };

  const handleReturnSelect = (mode: "direct" | "retrace") => {
    postRobotCommand(`/robot/return-to-work?mode=${mode}`, "작업 복귀");
    setReturnModalOpen(false);
  };

  const handlePlaceMove = (e: React.MouseEvent) => {
    e.stopPropagation();
    setPlaceModalOpen(true);
  };

  const handleEmergencyStop = (e: React.MouseEvent) => {
    e.stopPropagation();
    setEmergencyConfirmOpen(true);
  };

  const handleEmergencyConfirm = () => {
    postRobotCommand(`/nav/stopmove`, "긴급 정지", (data) =>
      modalAlert(data?.was_active ? "작업이 중지되었습니다." : "진행 중인 작업이 없습니다."));
    setEmergencyConfirmOpen(false);
  };

  const handleChargeMove = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (robot.atDock) {
      // 도킹 점유(충전 중 + 완충 후 대기) → 도킹에서 빠져나오기(언도킹)
      setUndockConfirmOpen(true);
      return;
    }
    // 도킹 밖 → 충전소로 이동
    setChargeConfirmOpen(true);
  };

  const handleChargeConfirm = () => {
    // 서버는 거절(충전소·도킹 포인트 없음 등)도 200 + status=error 로 돌려준다 — postRobotCommand 가 알린다
    postRobotCommand(`/robot/return-to-charge`, "충전소 이동");
    setChargeConfirmOpen(false);
  };

  const handleUndockConfirm = () => {
    postRobotCommand(`/robot/undock`, "충전 해제");
    setUndockConfirmOpen(false);
  };

  return (
    <>
      <div
        className={`${styles.card} ${isSelected ? styles.cardSelected : ""} ${isInactive ? styles.cardPowerOff : ""}`}
        onClick={isInactive ? undefined : onClick}
      >
        <div className={styles.cardInfo}>
          <div className={styles.cardTop}>
            <span className={styles.robotName}>
              {robot.no}
            </span>
            {canForceFloor && (
              <button
                className={`${styles.floorChangeBtn} ${isInactive || robot.isNavigating ? styles.floorChangeBtnDisabled : ""}`}
                onClick={(e) => { e.stopPropagation(); if (!isInactive && !robot.isNavigating) setFloorChangeOpen(true); }}
                title={robot.isNavigating ? "자율 주행/작업 중에는 층 변경 불가 — 먼저 정지" : isInactive ? "로봇 전원이 꺼져있습니다" : "층 변경 (관리자)"}
                disabled={isInactive || robot.isNavigating}
              >
                층 변경
              </button>
            )}
            {/* 원격 제어 — 운영관리를 거치지 않고 카드에서 바로 진입 (role 1·2 + 전원 On + robot-list 권한) */}
            {canRemote && (
              <button
                className={`${styles.floorChangeBtn} ${!canRemoteNow || !canControl ? styles.floorChangeBtnDisabled : ""}`}
                onClick={(e) => { e.stopPropagation(); if (canRemoteNow && canControl) setRemoteOpen(true); }}
                title={!canControl ? NO_CONTROL_TITLE : canRemoteNow ? "원격 제어" : statusStale ? netBadge.tooltip : "로봇 전원이 꺼져있습니다"}
                disabled={!canRemoteNow || !canControl}
              >
                원격 제어
              </button>
            )}
            <span className={`${styles.networkBadge} ${badgeClass}`} title={netBadge.tooltip || undefined}>
              <span className={`${styles.dot} ${dotClass}`} />
              {netBadge.label}
            </span>
          </div>

          <div className={styles.cardBottom}>
            <span className={isCriticalBattery(robot) ? styles.criticalBattery : ""}>
              {isDualBatteryType(robot.type) ? (
                isSingleBatteryMode(robot) ? (
                  (() => {
                    const left = robot.batteryLeft ?? 0;
                    const right = robot.batteryRight ?? 0;
                    const isLeft = left >= right;
                    const single = isLeft ? robot.batteryLeft : robot.batteryRight;
                    return single != null ? (
                      <>
                        <span style={{ color: "var(--text-primary)" }}>{isLeft ? "L" : "R"} </span>
                        <span style={{ color: getBatteryColor(single, robot.return, isOnline) }}>{single}%</span>
                        <span style={{ color: "var(--text-muted)" }}> ({robot.return}%)</span>
                      </>
                    ) : (
                      <span>- ({robot.return}%)</span>
                    );
                  })()
                ) : (
                  (() => {
                    const hasLeft = robot.batteryLeft != null;
                    const hasRight = robot.batteryRight != null;
                    if (hasLeft && hasRight) {
                      return (
                        <>
                          <span style={{ color: "var(--text-primary)" }}>L </span>
                          <span style={{ color: getBatteryColor(robot.batteryLeft!, robot.return, isOnline) }}>{robot.batteryLeft}%</span>
                          <span style={{ color: "var(--text-muted)" }}> / </span>
                          <span style={{ color: "var(--text-primary)" }}>R </span>
                          <span style={{ color: getBatteryColor(robot.batteryRight!, robot.return, isOnline) }}>{robot.batteryRight}%</span>
                          <span style={{ color: "var(--text-muted)" }}> ({robot.return}%)</span>
                        </>
                      );
                    }
                    const active = hasLeft ? robot.batteryLeft! : hasRight ? robot.batteryRight! : null;
                    const label = hasLeft ? "L" : "R";
                    return active != null ? (
                      <>
                        <span style={{ color: "var(--text-primary)" }}>{label} </span>
                        <span style={{ color: getBatteryColor(active, robot.return, isOnline) }}>{active}%</span>
                        <span style={{ color: "var(--text-muted)" }}> ({robot.return}%)</span>
                      </>
                    ) : (
                      <span>- ({robot.return}%)</span>
                    );
                  })()
                )
              ) : (
                <span style={{ color: getBatteryColor(robot.battery, robot.return, isOnline) }}>
                  {robot.battery}% ({robot.return}%)
                </span>
              )}
            </span>
            <span
              className={styles.floorLabel}
              title={!isUnregistered && robotLocation.floor == null ? "층 정보를 확인할 수 없습니다." : undefined}
            >
              {isUnregistered || robotLocation.floor == null ? "-" : (
                <>{robotLocation.floor}{robotLocation.placeName ? ` · ${robotLocation.placeName}` : ""}</>
              )}
            </span>
            {(statusStale || (!isPowerOff && !isUnregistered)) && (robot.batteryResume && !statusStale ? (
              <button
                type="button"
                className={`${styles.taskBadge} ${taskStatus.className}`}
                title={taskStatus.tooltip || undefined}
                onClick={(e) => { e.stopPropagation(); openBatteryResumeModal(robot.no); }}
              >
                {taskStatus.label}
              </button>
            ) : (
              <span className={`${styles.taskBadge} ${taskStatus.className}`} title={taskStatus.tooltip || undefined}>
                {taskStatus.label === "충전 중" && isOnline && <ChargingIcon size={12} style={{ marginLeft: 0, marginRight: 3 }} />}
                {taskStatus.label}
              </span>
            ))}
          </div>

          {/* 선택된 카드에만 액션 버튼 표시 */}
          {isSelected && (
            <div className={styles.actionRow}>
              <button className={styles.actionBtn} onClick={handleScheduleReturn} disabled={!isOnline || !canControl} title={controlTitle}>작업 복귀</button>
              <button className={styles.actionBtn} onClick={handleChargeMove} disabled={!isOnline || !canControl} title={controlTitle}>{robot.atDock ? "충전 해제" : "충전소 이동"}</button>
              <button className={styles.actionBtn} onClick={handlePlaceMove} disabled={!isOnline || !canControl} title={controlTitle}>장소 이동</button>
              <button className={`${styles.actionBtn} ${styles.actionBtnDanger}`} onClick={handleEmergencyStop} disabled={!isOnline || !(robot.isNavigating || hasActiveSchedule)}>긴급 정지</button>
            </div>
          )}
        </div>
      </div>

      {placeModalOpen && (
        <PlacePathModal
          isOpen={placeModalOpen}
          onClose={() => setPlaceModalOpen(false)}
          selectedRobotIds={[robot.id]}
          robotFloorId={robot.currentFloorId}
          robotMapId={robot.currentMapId}
        />
      )}

      {chargeConfirmOpen && (
        <BatteryPathModal
          isOpen={chargeConfirmOpen}
          message={(robot.isNavigating || hasActiveSchedule)
            ? "현재 진행 중인 작업을 중단하고, 충전소로 이동하시겠습니까?"
            : "충전소로 이동하시겠습니까?"}
          onConfirm={handleChargeConfirm}
          onCancel={() => setChargeConfirmOpen(false)}
        />
      )}

      {undockConfirmOpen && (
        <BatteryPathModal
          isOpen={undockConfirmOpen}
          message={robot.isCharging
            ? "현재 로봇이 충전 중입니다.\n충전을 해제하시겠습니까?"
            : "충전을 해제하고 도킹에서 나옵니다.\n이동하시겠습니까?"}
          onConfirm={handleUndockConfirm}
          onCancel={() => setUndockConfirmOpen(false)}
        />
      )}

      {emergencyConfirmOpen && (
        <BatteryPathModal
          isOpen={emergencyConfirmOpen}
          message="긴급 정지하시겠습니까?"
          onConfirm={handleEmergencyConfirm}
          onCancel={() => setEmergencyConfirmOpen(false)}
        />
      )}

      {returnModalOpen && (
        <ReturnToWorkModal
          isOpen={returnModalOpen}
          onSelect={handleReturnSelect}
          onCancel={() => setReturnModalOpen(false)}
        />
      )}

      {floorChangeOpen && (
        <FloorChangeModal
          isOpen={floorChangeOpen}
          robotId={robot.id}
          robotName={robot.no}
          currentFloorId={robot.currentFloorId}
          currentMapId={robot.currentMapId}
          onClose={() => setFloorChangeOpen(false)}
          onComplete={() => {}}
        />
      )}

      {remoteOpen && (
        <RemoteModal
          isOpen={remoteOpen}
          onClose={() => setRemoteOpen(false)}
          selectedRobots={robot}
          robots={robots}
          video={video}
          camera={cameras}
          primaryView="map"
        />
      )}

      <AlertModal
        open={modal.open}
        message={modal.message}
        mode={modal.mode}
        onConfirm={closeModal}
        onClose={closeModal}
      />
    </>
  );
}
