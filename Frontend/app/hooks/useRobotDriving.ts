'use client';

import { useCallback } from 'react';
import { apiFetch } from '@/app/lib/api';
import {
  useControlLock,
  type ControlOwner,
} from '@/app/components/modal/remote/hooks/useControlLock';
import { useRemoteCommand } from '@/app/components/modal/remote/hooks/useRemoteCommand';
import { useKeyboardControls } from '@/app/components/modal/remote/hooks/useKeyboardControls';

/**
 * 로봇 수동 주행 한 벌 — 제어권 + 이동 명령 + 방향키를 묶는다.
 *
 * 원격제어 모달 밖에서도 주행이 필요해서 분리했다. 맵핑 진행 모달이 그 경우다:
 * 맵을 그리려면 로봇을 끌고 다녀야 하는데, 그러자고 다른 화면의 원격제어 모달을
 * 띄워 둘 수는 없다.
 *
 * 제어권은 첫 명령이 알아서 잡는다(`useControlLock.ensure`).
 */

type Options = {
  /** 조작 대상 로봇. null 이면 주행이 비활성된다. */
  robotId: number | null;
  /** 이 화면/모달이 주행을 받을 상태인가. false 면 제어권도 반납한다. */
  enabled: boolean;
  /** 실패·제어권 상실을 사용자에게 알릴 콜백 */
  onAlert?: (message: string) => void;
  /**
   * 자율(system) 작업을 확인 없이 선점할지 판단한다.
   * 예) 맵핑 진행 모달은 자기 매핑 세션(`mapping`)만 조용히 선점한다.
   */
  autoPreempt?: (owner: ControlOwner | null) => boolean;
};

export function useRobotDriving({ robotId, enabled, onAlert, autoPreempt }: Options) {
  const control = useControlLock({
    robotId,
    enabled: enabled && robotId != null,
    onLost: onAlert,
    onPreempt: (_message, owner) => (autoPreempt ? autoPreempt(owner) : true),
  });

  const moveCmd = useRemoteCommand({
    robotId,
    debounceMs: 100,
    onError: onAlert,
    headers: control.controlHeaders,
    ensureControl: control.ensure,
    onDenied: onAlert,
  });

  const move = useCallback(
    (endpoint: string) => {
      void moveCmd.execute(endpoint);
    },
    [moveCmd],
  );

  // 정지는 제어권보다 위다 — 토큰 없이도 서버가 받아준다(pad.robot_stop).
  const stop = useCallback(() => {
    if (robotId == null) return;
    apiFetch(`/robot/${robotId}/stop`, { method: 'POST' }).catch(() => {});
  }, [robotId]);

  const disabled = !enabled || robotId == null || control.heldByOther;

  const { activeKey } = useKeyboardControls({
    enabled: !disabled,
    onMove: move,
    onStop: stop,
  });

  return {
    move,
    stop,
    activeKey,
    disabled,
    phase: control.phase,
    heldByOther: control.heldByOther,
    owner: control.owner,
  };
}
