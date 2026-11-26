'use client';

import React, { useState } from 'react';
import { useToast } from '@/app/components/common/Toast';
import {
  actionLabel, autoActionLabel, fmtMinSec, frontLabel, holdChipLabel, navHoldApi, retryBlockedReason,
  type NavHold, type NavHoldAction,
} from '@/app/lib/navHold';
import styles from './NavHoldPanel.module.css';

type Props = {
  hold: NavHold;
  /** 근무 칩이 이미 같은 문구를 보여 주면 칩은 빼고 버튼만 */
  hideChip?: boolean;
  canControl: boolean;
  onDone: () => void;
};

/** 로봇 상세 '현재 작업' 안 — 이동이 멈췄을 때 상태와 운영자 선택 */
export default function NavHoldPanel({ hold, hideChip = false, canControl, onDone }: Props) {
  const { showToast } = useToast();
  const [busy, setBusy] = useState(false);
  const auto = autoActionLabel(hold.kind);
  const where = hold.pos ? ` (${hold.pos[0].toFixed(1)}, ${hold.pos[1].toFixed(1)})` : '';
  const front = frontLabel(hold);
  const blocked = retryBlockedReason(hold);

  const act = async (action: NavHoldAction) => {
    setBusy(true);
    try {
      await navHoldApi.act(action);
      showToast(`${actionLabel(hold.kind, action)} — 요청했습니다`, 'success');
      onDone();
    } catch (e) {
      showToast(e instanceof Error ? e.message : '요청에 실패했습니다', 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.panel}>
      <div className={styles.line}>
        {!hideChip && <span className={styles.chip}>{holdChipLabel(hold)}</span>}
        <span className={styles.reason}>
          &apos;{hold.wpName}&apos;(으)로 가다{where} {fmtMinSec(hold.heldSec)} 째 멈춰 있습니다 · {hold.reason}
          {front && <> · 지금 앞: {front}</>}
        </span>
      </div>
      {canControl && (
        <div className={styles.actions}>
          {hold.actions.map((a, i) => (
            <button
              key={a}
              type="button"
              className={i === 0 ? styles.btnPrimary : a === 'abort' ? styles.btnGhost : styles.btnSecondary}
              onClick={() => act(a)}
              disabled={busy || (a === 'retry' && blocked !== null)}
              title={a === 'retry' && blocked ? blocked : undefined}
            >
              {actionLabel(hold.kind, a)}
            </button>
          ))}
          {auto && hold.autoActionInSec !== null && (
            <span className={styles.auto}>
              응답 없으면 {fmtMinSec(hold.autoActionInSec)} 뒤 {auto}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
