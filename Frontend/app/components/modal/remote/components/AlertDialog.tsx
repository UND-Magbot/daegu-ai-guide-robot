'use client';

import React from 'react';
import styles from './AlertDialog.module.css';

type AlertDialogProps = {
  message: string;
  onClose: () => void;
  /** 주면 [확인]/[취소] 두 버튼짜리 확인 대화상자가 된다 (자율 작업 선점 등). */
  onConfirm?: () => void;
  confirmLabel?: string;
  cancelLabel?: string;
};

export default function AlertDialog({
  message,
  onClose,
  onConfirm,
  confirmLabel = '확인',
  cancelLabel = '취소',
}: AlertDialogProps) {
  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.box} onClick={(e) => e.stopPropagation()}>
        <div className={styles.message}>{message}</div>
        {onConfirm ? (
          <div className={styles.actions}>
            <button type="button" className={styles.cancelBtn} onClick={onClose}>
              {cancelLabel}
            </button>
            <button type="button" className={styles.confirmBtn} onClick={onConfirm}>
              {confirmLabel}
            </button>
          </div>
        ) : (
          <button type="button" className={styles.confirmBtn} onClick={onClose}>
            확인
          </button>
        )}
      </div>
    </div>
  );
}
