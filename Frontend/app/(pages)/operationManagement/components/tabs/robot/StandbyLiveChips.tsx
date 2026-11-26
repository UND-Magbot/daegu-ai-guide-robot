'use client';

/**
 * 상시 대기 응대 표시 — 로봇 상세 '현재 작업'.
 *
 * 작업명과 응대 상태를 따로 떼어 놓으면 "이 작업이 지금 어떤 상태인가"를 두 줄에서 조합해야 한다.
 * 그래서 상태 칩은 **작업명과 같은 줄**(`StandbyStatusChip`)에 붙이고,
 * 요청 내용·경과 같은 상세만 아래(`StandbyLiveDetail`)에 둔다.
 *
 * 데이터는 `useStandbyLive` 하나에서 온다 — 컴포넌트마다 폴링하면 두 표시가 한 틱씩 어긋난다.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import styles from '@/app/components/modal/Modal.module.css';
import { apiFetch } from '@/app/lib/api';
import { useOutsideClick } from '@/app/hooks/useOutsideClick';
import type { StandbyLive } from './useStandbyLive';

type Destination = { id: number; name: string; linked: boolean };

function fmtDuration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return '—';
  return `${Math.floor(sec / 60)}'${String(sec % 60).padStart(2, '0')}"`;
}

function fmtTime(iso: string | null | undefined): string {
  return iso ? iso.slice(11, 16) : '—';
}

/** 작업명 옆에 붙는 응대 상태 — 단계 점까지 한 줄에 담는다 */
export function StandbyStatusChip({ live }: { live: StandbyLive | null }) {
  if (!live) return null;

  const sess = live.session ?? null;
  const steps = live.steps ?? [];
  const stepIndex = live.step ?? 0;

  return (
    <>
      {/* 응대 방식이 안내 시나리오면 그 이름 — 같은 작업이라도 무엇으로 응대하는지가 다르다 */}
      {live.mode === 'scenario' && live.scenarioName && (
        <span className={styles.standbyScenarioTag} title="응대 방식 — 안내 시나리오">
          {live.scenarioName}
        </span>
      )}
      {/* 단순 동작 근무 — 안내를 하지 않으니 응대 단계 대신 방식만 보인다 */}
      {live.mode === 'motion' && (
        <span className={styles.standbyScenarioTag} title="응대 방식 — 앉아·일어서 명령만 받습니다">
          단순 동작
        </span>
      )}
      <span className={`${styles.standbyChip} ${sess || live.guide ? styles.standbyChipBusy : styles.standbyChipWait}`}>
        {live.phaseLabel}
      </span>
      {steps.length > 0 && (
        <>
          <span className={styles.standbyDots} title={steps.join(' › ')}>
            {steps.map((label, i) => (
              <span
                key={label}
                className={`${styles.standbyDot} ${
                  i < stepIndex ? styles.standbyDotDone : i === stepIndex ? styles.standbyDotNow : ''
                }`}
              />
            ))}
          </span>
          <span className={styles.standbyStep}>{steps[stepIndex] ?? '—'}</span>
        </>
      )}
      {sess && <span className={styles.standbySub}>{fmtDuration(sess.elapsedSec)}</span>}
    </>
  );
}

type DetailProps = {
  live: StandbyLive | null;
  /** 관제 개입 호출 노출 여부 — 등급 1·2만 */
  canControl: boolean;
  onCalled?: () => void;
};

/** 요청 내용·오늘 집계·관제 개입 — 작업명 줄에 담기엔 긴 것들 */
export default function StandbyLiveDetail({ live, canControl, onCalled }: DetailProps) {
  const [destinations, setDestinations] = useState<Destination[]>([]);
  const [calling, setCalling] = useState(false);
  const [callMsg, setCallMsg] = useState<string | null>(null);
  const [callOpen, setCallOpen] = useState(false);
  const [endConfirm, setEndConfirm] = useState(false);
  const [ending, setEnding] = useState(false);

  const callRef = useRef<HTMLDivElement>(null);
  useOutsideClick(callRef, useCallback(() => setCallOpen(false), []));

  const sess = live?.session ?? null;
  const scheduleId = live?.scheduleId;
  // 시나리오 근무는 목적지를 시나리오가 묻는다 — 목적지 목록 대신 [안내 시작] 하나
  const scenarioMode = live?.mode === 'scenario';
  // 단순 동작 근무는 목적지 호출을 받지 않는다 — 관제 개입(목적지 지정)도 없다
  const motionMode = live?.mode === 'motion';

  useEffect(() => {
    if (!scheduleId || !canControl || scenarioMode || motionMode) { setDestinations([]); return; }
    apiFetch(`/DB/standby-destinations?schedule_id=${scheduleId}`)
      .then((r) => (r.ok ? r.json() : []))
      .then((d) => setDestinations(Array.isArray(d) ? d : []))
      .catch(() => setDestinations([]));
  }, [scheduleId, canControl, scenarioMode, motionMode]);

  if (!live) return null;
  const guide = live.guide ?? null;

  // 근무에 붙은 안내를 끝낸다 — 근무는 남고 로봇은 대기 장소로 돌아간다(관제 종료 API 가 근무를 유지)
  const handleEndGuide = async () => {
    if (!guide) return;
    setEnding(true);
    setCallMsg(null);
    try {
      const res = await apiFetch(`/DB/voice-sessions/${guide.id}/close`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: '운영자 종료(로봇 상세)' }),
      });
      const body = await res.json().catch(() => null);
      setCallMsg(res.ok ? '안내를 종료했습니다. 대기 장소로 돌아갑니다.' : body?.detail ?? '종료에 실패했습니다.');
      setEndConfirm(false);
      if (res.ok) onCalled?.();
    } catch {
      setCallMsg('종료 중 오류가 발생했습니다.');
    } finally {
      setEnding(false);
    }
  };

  // 근무의 안내 시나리오를 첫 단계부터 연다. 대기 장소로 가는 중이면 그 자리에서 시작한다(D9)
  const handleStartScenario = async () => {
    if (!scheduleId) return;
    setCalling(true);
    setCallMsg(null);
    try {
      const res = await apiFetch(`/DB/standby-call`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scheduleId }),
      });
      const body = await res.json().catch(() => null);
      setCallMsg(res.ok ? `${live?.scenarioName ?? '안내'}을(를) 시작했습니다.` : body?.detail ?? '안내를 시작하지 못했습니다.');
      if (res.ok) onCalled?.();
    } catch {
      setCallMsg('안내 시작 중 오류가 발생했습니다.');
    } finally {
      setCalling(false);
    }
  };

  const handleCall = async (destinationId: number) => {
    if (!scheduleId) return;
    setCalling(true);
    setCallMsg(null);
    try {
      const res = await apiFetch(`/DB/standby-call`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scheduleId, destinationId }),
      });
      const body = await res.json().catch(() => null);
      setCallMsg(body?.message ?? body?.detail ?? (res.ok ? '응대를 시작했습니다.' : '호출에 실패했습니다.'));
      if (res.ok) { setCallOpen(false); onCalled?.(); }
    } catch {
      setCallMsg('호출 중 오류가 발생했습니다.');
    } finally {
      setCalling(false);
    }
  };

  return (
    <div className={styles.standbyLiveRow}>
      {sess ? (
        <span className={styles.standbyHeard}>
          {sess.heard ? `“${sess.heard}”` : '(버튼 호출)'} → <b>{sess.destination ?? '—'}</b>
          {sess.confidence !== null && ` · 신뢰도 ${sess.confidence.toFixed(2)}`}
          {sess.retryCount > 0 && ` · 되묻기 ${sess.retryCount}회`}
        </span>
      ) : guide ? (
        <span className={styles.standbyHeard}>
          {live.scenarioName ?? '안내'} #{guide.id} · 목적지 <b>{guide.destination ?? '—'}</b> ·{' '}
          {fmtDuration(guide.elapsedSec)}
        </span>
      ) : (
        <span className={styles.standbySub}>
          {motionMode ? '"큐리야, 앉아 / 일어서" 명령을 기다리는 중입니다' : '호출을 기다리는 중입니다'}
        </span>
      )}

      {canControl && guide && (
        endConfirm ? (
          <>
            <button type="button" className={styles.standbyCallBtn} onClick={handleEndGuide} disabled={ending}>
              종료 확인
            </button>
            <button type="button" className={styles.standbyCallBtn} onClick={() => setEndConfirm(false)} disabled={ending}>
              취소
            </button>
          </>
        ) : (
          <button
            type="button"
            className={styles.standbyCallBtn}
            onClick={() => setEndConfirm(true)}
            title="안내만 끝내고 근무는 유지합니다 — 로봇은 대기 장소로 돌아갑니다"
          >
            안내 종료
          </button>
        )
      )}
      {guide && callMsg && <span className={styles.standbySub}>{callMsg}</span>}

      {live.todaySummary && (
        <span className={styles.standbySub}>
          오늘 {live.todaySummary.total}건 · 완료 {live.todaySummary.completed}
        </span>
      )}
      {live.endsAt && <span className={styles.standbySub}>운영 종료 {fmtTime(live.endsAt)}</span>}

      {canControl && scenarioMode && (
        <>
          <button
            type="button"
            className={styles.standbyCallBtn}
            onClick={handleStartScenario}
            disabled={calling || !!guide || !!sess}
            title={
              guide || sess
                ? '안내 중에는 시작할 수 없습니다'
                : live.phase === 'standby'
                  ? `${live.scenarioName ?? '안내'} 시나리오를 첫 단계부터 시작합니다`
                  : '이동을 멈추고 그 자리에서 안내를 시작합니다'
            }
          >
            {calling ? '시작 중…' : '안내 시작'}
          </button>
          {!guide && callMsg && <span className={styles.standbySub}>{callMsg}</span>}
        </>
      )}
      {canControl && !scenarioMode && !motionMode && (
        <div className={styles.standbyCallAnchor} ref={callRef}>
          <button
            type="button"
            className={styles.standbyCallBtn}
            onClick={() => setCallOpen((v) => !v)}
            disabled={!!sess}
            title={sess ? '응대 중에는 호출할 수 없습니다' : undefined}
          >
            관제 개입 {callOpen ? '▴' : '▾'}
          </button>
          {callOpen && !sess && (
            <div className={styles.standbyCallPopover}>
              <div className={styles.standbyCallLabel}>
                로봇이 못 알아들을 때 대신 목적지를 지정합니다
              </div>
              <div className={styles.standbyCallBtns}>
                {destinations.length === 0 ? (
                  <span className={styles.standbyCallEmpty}>
                    목적지 사전에 등록된 목적지가 없습니다.
                  </span>
                ) : (
                  destinations.map((d) => (
                    <button
                      key={d.id}
                      type="button"
                      className={`${styles.standbyDestBtn} ${!d.linked ? styles.standbyDestUnlinked : ''}`}
                      disabled={calling || !d.linked}
                      title={d.linked ? undefined : '장소가 연결되지 않아 안내할 수 없습니다'}
                      onClick={() => handleCall(d.id)}
                    >
                      {d.name}
                    </button>
                  ))
                )}
              </div>
              {callMsg && <div className={styles.standbyCallMsg}>{callMsg}</div>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
