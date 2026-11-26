'use client';

import React, { useState, useEffect, useCallback, useRef } from 'react';
import type { RobotRowData, Video, Camera, PrimaryViewType } from '@/app/types';
import { getCamerasForRobot } from '@/app/lib/cameraView';
import { useModalBehavior } from '@/app/hooks/useModalBehavior';
import { useRobotPosition } from '@/app/hooks/useRobotPosition';
import { apiFetch } from '@/app/lib/api';

import { useCameraStream } from './hooks/useCameraStream';
import { useWorkAutomation } from './hooks/useWorkAutomation';
import { useRemoteCommand } from './hooks/useRemoteCommand';
import { useControlLock } from './hooks/useControlLock';
import { useKeyboardControls } from './hooks/useKeyboardControls';
import { useRecording } from './hooks/useRecording';

import StatusBar from './components/StatusBar';
import ViewportArea from './components/ViewportArea';
import { useRemoteFloorMap } from './hooks/useRemoteFloorMap';
import { useRemoteFloorPois } from './hooks/useRemoteFloorPois';
import MovementPad from './components/MovementPad';
import ControlPanel from './components/ControlPanel';
import AlertDialog from './components/AlertDialog';
import { RemoteSessionProvider } from './RemoteSessionContext';
import styles from './RemoteModal.module.css';

export type RemoteModalProps = {
  isOpen: boolean;
  onClose: () => void;
  selectedRobots: RobotRowData | null;
  robots: RobotRowData[];
  video: Video[];
  camera: Camera[];
  initialCam?: Camera | null;
  initialCamIndex?: number;
  primaryView: PrimaryViewType;
  readOnly?: boolean;
  controlledBy?: string | null;
};

export default function RemoteModal({
  isOpen,
  onClose,
  selectedRobots,
  robots: _robots,
  video: _video,
  camera,
  initialCam,
  initialCamIndex,
  primaryView: _primaryView,
  readOnly = false,
  controlledBy,
}: RemoteModalProps) {
  // --- 선택된 로봇 ---
  const [selectedRobot, setSelectedRobot] = useState<RobotRowData | null>(null);

  useEffect(() => {
    setSelectedRobot(selectedRobots);
  }, [selectedRobots]);

  // --- 층 id → 이름 매핑 (현재 층 배지용, 모달 열릴 때 1회 로드) ---
  const [floorNameById, setFloorNameById] = useState<Record<number, string>>({});
  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    apiFetch(`/map/floors`)
      .then((res) => (res.ok ? res.json() : []))
      .then((data: { id: number; FloorName: string }[]) => {
        if (cancelled || !Array.isArray(data)) return;
        const map: Record<number, string> = {};
        for (const f of data) map[f.id] = f.FloorName;
        setFloorNameById(map);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [isOpen]);

  const floorName =
    selectedRobot?.currentFloorId != null
      ? (floorNameById[selectedRobot.currentFloorId] ?? null)
      : null;

  // --- 로봇별 카메라 동적 로드 ---
  const [robotCameras, setRobotCameras] = useState<Camera[]>(camera);
  const [camerasReady, setCamerasReady] = useState(false);

  useEffect(() => {
    if (!isOpen || !selectedRobot) {
      setRobotCameras(camera);
      setCamerasReady(camera.length > 0);
      return;
    }
    setCamerasReady(false);
    let cancelled = false;
    getCamerasForRobot(selectedRobot.id).then((cams) => {
      if (cancelled) return;
      const result = cams.length > 0 ? cams : camera;
      setRobotCameras(result);
      setCamerasReady(true);
    });
    return () => { cancelled = true; };
  }, [isOpen, selectedRobot?.id, camera]);

  // --- 커스텀 알림 ---
  const [alertMsg, setAlertMsg] = useState<string | null>(null);
  const showAlert = useCallback((msg: string) => setAlertMsg(msg), []);

  // --- 확인 대화상자 (자율 작업 선점) ---
  // 조작하면 제어권이 자동으로 잡히지만, 자율 작업을 뺏는 것만은 조용히 하지 않는다.
  const [confirmMsg, setConfirmMsg] = useState<string | null>(null);
  const confirmResolveRef = useRef<((ok: boolean) => void) | null>(null);

  const askConfirm = useCallback(
    (msg: string) =>
      new Promise<boolean>((resolve) => {
        confirmResolveRef.current = resolve;
        setConfirmMsg(msg);
      }),
    [],
  );

  const settleConfirm = useCallback((ok: boolean) => {
    setConfirmMsg(null);
    confirmResolveRef.current?.(ok);
    confirmResolveRef.current = null;
  }, []);

  // 모달이 닫히는데 확인이 떠 있으면 대기 중인 Promise 가 영영 안 풀린다.
  useEffect(() => {
    if (isOpen) return;
    confirmResolveRef.current?.(false);
    confirmResolveRef.current = null;
    setConfirmMsg(null);
  }, [isOpen]);

  // --- 로봇 위치/연결 상태 (연결 끊김 감지 → 카메라 재연결 게이트에 사용) ---
  const { position: robotPos, isReady: robotConnected, hasError: positionError } = useRobotPosition(isOpen);

  // 연결 끊김 감지
  const errorCountRef = useRef(0);
  const [isDisconnected, setIsDisconnected] = useState(false);

  useEffect(() => {
    if (positionError) {
      errorCountRef.current += 1;
      if (errorCountRef.current >= 3) setIsDisconnected(true);
    } else {
      errorCountRef.current = 0;
      setIsDisconnected(false);
    }
  }, [positionError]);

  // --- 커스텀 훅 (카메라 준비 후에만 활성화) ---
  // 로봇 통신이 끊기면(isDisconnected) 카메라 연결·재연결을 멈춘다.
  const cam = useCameraStream({
    isOpen: isOpen && camerasReady,
    camera: robotCameras,
    initialCam,
    initialCamIndex,
    enabled: !isDisconnected,
  });
  const work = useWorkAutomation(isOpen, {
    onAlert: showAlert,
    currentFloorId: selectedRobot?.currentFloorId ?? null,
  });
  // 로봇 현재 맵(층 변경 때 고른 맵)의 실제 이미지 (없으면 null → ViewportArea가 고정 맵으로 폴백)
  const { config: floorMapConfig, loading: floorMapLoading } = useRemoteFloorMap(
    selectedRobot?.currentFloorId ?? null,
    selectedRobot?.currentMapId ?? null,
  );
  // 같은 맵의 장소 — 로봇이 지금 어디쯤인지 판단하려면 주변 지형지물이 필요하다
  const floorPois = useRemoteFloorPois(
    selectedRobot?.currentFloorId ?? null,
    selectedRobot?.currentMapId ?? null,
  );
  // --- 제어권 (관찰이 기본, 첫 조작이 곧 제어 시작) ---
  const control = useControlLock({
    robotId: selectedRobot?.id ?? null,
    enabled: isOpen,
    onLost: showAlert,
    onPreempt: askConfirm,
  });

  const moveCmd = useRemoteCommand({
    robotId: selectedRobot?.id ?? null,
    debounceMs: 100,
    onError: showAlert,
    headers: control.controlHeaders,
    ensureControl: control.ensure,
    onDenied: showAlert,
  });
  const recording = useRecording(isOpen, selectedRobot?.id, showAlert);

  // --- 키보드 이동 제어 ---
  const handleMove = useCallback(
    (endpoint: string) => { moveCmd.execute(endpoint); },
    [moveCmd],
  );

  const handleStop = useCallback(() => {
    const id = selectedRobot?.id;
    apiFetch(id != null ? `/robot/${id}/stop` : '/robot/stop', { method: 'POST' }).catch(
      () => {},
    );
  }, [selectedRobot?.id]);

  // 제어권은 첫 명령이 알아서 잡는다 — 여기서는 "조작해도 되는 상황인가"만 본다.
  const keyboardEnabled =
    isOpen && !readOnly && !work.isWorking && !isDisconnected && !control.heldByOther;
  const { activeKey: activeKeyRef } = useKeyboardControls({
    enabled: keyboardEnabled,
    onMove: handleMove,
    onStop: handleStop,
  });

  // --- 모달 닫기 ---
  const handleClose = useCallback(() => {
    onClose();
  }, [onClose]);

  useModalBehavior({ isOpen, onClose: handleClose });

  // --- auth 만료 처리 ---
  useEffect(() => {
    if (!isOpen) return;
    const handleSessionExpired = () => {
      const id = selectedRobot?.id;
      apiFetch(id != null ? `/robot/${id}/stop` : '/robot/stop', { method: 'POST' }).catch(
        () => {},
      );
      handleClose();
    };
    window.addEventListener('auth:session-expired', handleSessionExpired);
    return () => window.removeEventListener('auth:session-expired', handleSessionExpired);
  }, [isOpen, handleClose, selectedRobot?.id]);

  if (!isOpen) return null;

  // 조작을 막는 것은 "다른 사람이 쥐고 있을 때"뿐이다. 내 제어권은 조작 시 자동으로 잡힌다.
  const controlsDisabled = readOnly || isDisconnected || control.heldByOther;

  return (
    <RemoteSessionProvider
      robotId={selectedRobot?.id ?? null}
      controlHeaders={control.controlHeaders}
      ensureControl={control.ensure}
    >
    <div className={styles.overlay} onClick={handleClose}>
      <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
        <StatusBar
          selectedRobot={selectedRobot}
          floorName={floorName}
          onClose={handleClose}
          controlledBy={
            control.isControlling
              ? undefined
              : (control.owner?.ownerLabel ?? (readOnly ? controlledBy : undefined))
          }
          controlPhase={control.phase}
          onStopControl={control.stop}
          heldByOther={control.heldByOther}
          isRecording={recording.isRecording}
          recordType={recording.recordType}
          isNavigating={recording.isNavigating}
          onToggleRecording={() => {
            const camId = robotCameras[cam.cameraTabActiveIndex]?.id;
            if (camId) recording.toggleRecording(camId);
          }}
          recordingDisabled={recording.isPending || isDisconnected}
        />

        <div className={styles.mainLayout}>
          <ViewportArea
            isCamLoading={cam.isCamLoading}
            camError={cam.camError}
            cameraStream={cam.cameraStream}
            thermalUrl={cam.thermalUrl}
            retryKey={cam.retryKey}
            cameraTabActiveIndex={cam.cameraTabActiveIndex}
            camera={robotCameras}
            camerasReady={camerasReady}
            onRetryCamera={cam.handleRetryCamera}
            onCameraTab={cam.handleCameraTab}
            onCamImgLoad={cam.handleCamImgLoad}
            onCamImgError={cam.handleCamImgError}
            robotPos={robotPos}
            robotConnected={robotConnected}
            isDisconnected={isDisconnected}
            mapConfig={floorMapConfig}
            mapLoading={floorMapLoading}
            pois={floorPois}
          />

          <div className={styles.sidebar}>
            <MovementPad
              onCommand={handleMove}
              onStop={handleStop}
              disabled={controlsDisabled || work.isWorking}
              activeKeyRef={activeKeyRef}
            />

            <ControlPanel
              robotType={selectedRobot?.type ?? ''}
              motionState={selectedRobot?.motionState ?? null}
              gait={selectedRobot?.gait ?? null}
              isCharging={selectedRobot?.isCharging ?? false}
              isWorking={work.isWorking}
              isWorkPending={work.isPending}
              loopCount={work.loopCount}
              loopCurrent={work.loopCurrent}
              loopTotal={work.loopTotal}
              loopInfinite={work.loopInfinite}
              isDisconnected={controlsDisabled}
              onStartWork={work.startWork}
              onStopWork={work.stopWork}
              onLoopCountChange={work.handleLoopCountChange}
              onLoopCountBlur={work.handleLoopCountBlur}
              paths={work.paths}
              selectedPath={work.selectedPath}
              onSelectPath={work.setSelectedPath}
              taskTypeFilter={work.taskTypeFilter}
              onTaskTypeFilterChange={work.setTaskTypeFilter}
              isCreating={work.isCreating}
              createdPoints={work.createdPoints}
              onStartCreating={work.startCreating}
              onSavePoint={work.savePoint}
              onSetPointWait={work.setPointWait}
              onClearPoints={work.clearPoints}
              onFinishCreating={work.finishCreating}
              onCancelCreating={work.cancelCreating}
              controlHeaders={control.controlHeaders}
            />
          </div>
        </div>

        {alertMsg && (
          <AlertDialog message={alertMsg} onClose={() => setAlertMsg(null)} />
        )}

        {confirmMsg && (
          <AlertDialog
            message={confirmMsg}
            confirmLabel="개입"
            cancelLabel="취소"
            onConfirm={() => settleConfirm(true)}
            onClose={() => settleConfirm(false)}
          />
        )}

      </div>
    </div>
    </RemoteSessionProvider>
  );
}
