'use client';

import React, { useState, useEffect, useRef } from 'react';
import type { RobotRowData } from '@/app/types';
import { isDualBatteryType } from '@/app/constants/robotCapabilities';
import ChargingIcon from '@/app/components/common/ChargingIcon';
import styles from './StatusBar.module.css';

type StatusBarProps = {
  selectedRobot: RobotRowData | null;
  /** 선택 로봇의 현재 층 이름(예: "2F"). null이면 "-" 표시. */
  floorName?: string | null;
  onClose: () => void;
  controlledBy?: string | null;
  // recording
  isRecording?: boolean;
  recordType?: 'auto' | 'manual' | null;
  isNavigating?: boolean;
  onToggleRecording?: () => void;
  recordingDisabled?: boolean;
  // 제어권 — 조작하면 자동으로 잡히므로 시작 버튼은 없다. 표시와 해제만 한다.
  controlPhase?: 'observing' | 'starting' | 'controlling' | 'lost';
  onStopControl?: () => void;
  /** 다른 사람이 쥐고 있어 조작할 수 없는 상태 */
  heldByOther?: boolean;
};

/** 배터리 값 → 색상 (로봇 목록과 동일 기준) */
function batColor(level: number): string {
  if (level > 25) return '#22c55e';
  if (level > 10) return '#f59e0b';
  return '#ef4444';
}

/** 초 → MM:SS 포맷 */
function formatElapsed(sec: number): string {
  const m = String(Math.floor(sec / 60)).padStart(2, '0');
  const s = String(sec % 60).padStart(2, '0');
  return `${m}:${s}`;
}

export default function StatusBar({
  selectedRobot,
  floorName = null,
  onClose,
  controlledBy,
  isRecording = false,
  recordType = null,
  isNavigating = false,
  onToggleRecording,
  recordingDisabled = false,
  controlPhase = 'observing',
  onStopControl,
  heldByOther = false,
}: StatusBarProps) {
  const robot = selectedRobot;
  const power = robot?.power ?? '-';
  const network = robot?.network ?? '-';
  const isDual = isDualBatteryType(robot?.type ?? '');

  // 녹화 경과 시간 카운터
  const [elapsed, setElapsed] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (isRecording) {
      setElapsed(0);
      timerRef.current = setInterval(() => setElapsed((p) => p + 1), 1000);
    } else {
      setElapsed(0);
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [isRecording]);

  return (
    <div className={styles.statusBar}>
      <div className={styles.left}>
        <span className={styles.title}>{robot?.no ?? 'Robot'} 원격제어</span>
        <span className={styles.divider} />
        <span className={styles.floorBadge}>{floorName ?? '-'}</span>

        {/* 전원 */}
        <span className={styles.statusBadge}>
          <span className={styles.badgeLabel}>전원</span>
          <span>{power}</span>
        </span>

        {/* 상태 */}
        <span className={styles.statusBadge}>
          <span className={styles.badgeLabel}>상태</span>
          <span>
            {(robot?.chargeState === 2 || robot?.chargeState === 5) ? (
              <><ChargingIcon size={12} style={{ marginLeft: 0, marginRight: 3 }} />충전 중</>
            ) : robot?.isNavigating ? '작업 중'
            : network}
          </span>
        </span>

        {/* 배터리 */}
        <span className={styles.statusBadge}>
          <span className={styles.badgeLabel}>배터리</span>
          {isDual ? (
            <>
              <span className={styles.batLabel}>L</span>
              {robot?.batteryLeft != null ? (
                <span style={{ color: batColor(robot.batteryLeft) }}>{robot.batteryLeft}%</span>
              ) : <span>-</span>}
              <span className={styles.batSep}>/</span>
              <span className={styles.batLabel}>R</span>
              {robot?.batteryRight != null ? (
                <span style={{ color: batColor(robot.batteryRight) }}>{robot.batteryRight}%</span>
              ) : <span>-</span>}
            </>
          ) : (
            <span style={{ color: batColor(robot?.battery ?? 0) }}>{robot?.battery ?? '-'}%</span>
          )}
        </span>

        {/* 녹화 영역 */}
        {onToggleRecording && (
          <>
            <span className={styles.divider} />
            {isRecording && recordType === 'auto' ? (
              /* ── 자동 녹화 중 (표시 전용) ── */
              <span className={styles.recAutoIndicator}>
                <span className={styles.recDotAuto} />
                <span className={styles.recAutoLabel}>자동 녹화</span>
                <span className={styles.recTimer}>{formatElapsed(elapsed)}</span>
              </span>
            ) : isRecording && recordType === 'manual' ? (
              /* ── 수동 녹화 중 → 중지 버튼 ── */
              <button
                type="button"
                className={styles.recStopBtn}
                onClick={onToggleRecording}
                disabled={recordingDisabled}
              >
                <span className={styles.recDotLive} />
                <span className={styles.recStopLabel}>녹화 중지</span>
                <span className={styles.recTimer}>{formatElapsed(elapsed)}</span>
              </button>
            ) : (
              /* ── 대기: 수동 녹화 시작 버튼 ── */
              <button
                type="button"
                className={styles.recStartBtn}
                onClick={onToggleRecording}
                disabled={recordingDisabled || isNavigating}
              >
                <svg className={styles.recIcon} width="14" height="14" viewBox="0 0 14 14" fill="none">
                  <circle cx="7" cy="7" r="6" stroke="currentColor" strokeWidth="1.5"/>
                  <circle cx="7" cy="7" r="3" fill="currentColor"/>
                </svg>
                <span>수동 녹화</span>
              </button>
            )}
          </>
        )}
      </div>

      <div className={styles.controlBar}>
        {controlPhase === 'controlling' ? (
          <>
            <span className={styles.controlOn}>● 제어 중</span>
            <button
              type="button"
              className={styles.controlBtn}
              onClick={onStopControl}
              title="제어권을 반납합니다. 다시 조작하면 자동으로 잡습니다."
            >
              제어 해제
            </button>
          </>
        ) : controlPhase === 'starting' ? (
          <span className={styles.controlOff}>제어권 확보 중…</span>
        ) : heldByOther || controlledBy ? (
          <span className={styles.controlHeld}>
            {controlledBy ? `${controlledBy}님이 제어 중` : '다른 세션이 제어 중'}
          </span>
        ) : (
          <span className={styles.controlOff} title="방향키(WASD·화살표)나 패드를 조작하면 제어가 시작됩니다">
            대기 — 조작 시 자동 제어
          </span>
        )}
      </div>

      <button type="button" className={styles.closeBtn} onClick={onClose} aria-label="닫기">
        ✕
      </button>
    </div>
  );
}