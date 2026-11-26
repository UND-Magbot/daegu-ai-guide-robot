'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from '@/app/lib/api';

/**
 * 로봇 제어권 — 한 로봇은 동시에 한 세션만 조작한다.
 *
 * 제어권은 **첫 조작이 곧 획득 신호**다(`ensure`). 예전에는 [제어 시작]을 먼저 눌러야
 * 방향키·패드가 살아났는데, 개입이 급할 때 그 한 단계가 손을 늦춘다.
 * 관찰(상태·영상)은 여전히 제어권 없이 된다.
 * (설계: 06_Planning/PM-Remote-Intervention-Concept.md §6.3, ARCH §4)
 */

export type ControlOwner = {
  ownerType: 'user' | 'system';
  ownerId: string;
  ownerLabel: string;
  expiresIn: number;
};

export type ControlPhase =
  | 'observing'   // 제어권 없음 (기본)
  | 'starting'    // 획득 시도 중
  | 'controlling' // 내가 쥐고 있음
  | 'lost';       // 쥐고 있다가 잃음 (만료·회수)

type Options = {
  robotId: number | null;
  enabled: boolean;
  /** 제어권을 잃었을 때 사용자에게 알릴 콜백 */
  onLost?: (reason: string) => void;
  /**
   * 자율(system) 작업이 쥐고 있어 선점해야 할 때 허락을 묻는 콜백.
   * 주지 않으면 확인 없이 선점한다 — 운영자가 스스로 시작한 작업(매핑)처럼
   * 매번 묻는 것이 방해가 되는 경우를 위해서다.
   */
  onPreempt?: (message: string, owner: ControlOwner | null) => Promise<boolean> | boolean;
};

const OWNER_POLL_MS = 5000;
/** 선점을 거절한 뒤 다시 묻기까지의 간격. 키를 누를 때마다 확인창이 뜨는 걸 막는다. */
const PREEMPT_COOLDOWN_MS = 10_000;

type AcquireResult =
  | { ok: true }
  | { ok: false; preemptible: boolean; message: string; owner: ControlOwner | null };

export function useControlLock({ robotId, enabled, onLost, onPreempt }: Options) {
  const [phase, setPhase] = useState<ControlPhase>('observing');
  const [owner, setOwner] = useState<ControlOwner | null>(null);
  const [error, setError] = useState<string | null>(null);

  const tokenRef = useRef<string | null>(null);
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const ensureRef = useRef<Promise<boolean> | null>(null);
  const preemptBlockedUntilRef = useRef(0);
  const onLostRef = useRef(onLost);
  onLostRef.current = onLost;
  const onPreemptRef = useRef(onPreempt);
  onPreemptRef.current = onPreempt;

  const stopHeartbeat = useCallback(() => {
    if (heartbeatRef.current) {
      clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }
  }, []);

  /** 조작 명령에 실어 보낼 헤더. 제어권이 없으면 빈 객체 — 서버가 409로 막는다. */
  const controlHeaders = useCallback((): Record<string, string> => {
    return tokenRef.current ? { 'X-Control-Token': tokenRef.current } : {};
  }, []);

  /** 누가 쥐고 있는지 조회 (관찰 모드 표시용) */
  const refreshOwner = useCallback(async () => {
    if (robotId == null) return;
    try {
      const res = await apiFetch(`/robot/${robotId}/control`);
      if (!res.ok) return;
      const body = await res.json();
      setOwner(body.controlled ? body.owner : null);
    } catch {
      /* 조회 실패는 조용히 넘긴다 — 조작을 막는 정보가 아니다 */
    }
  }, [robotId]);

  /** 획득 요청 1회. 선점 여부 판단은 호출자가 한다. */
  const request = useCallback(
    async (force: boolean): Promise<AcquireResult> => {
      if (robotId == null) {
        return { ok: false, preemptible: false, message: '대상 로봇이 없습니다', owner: null };
      }
      const path = `/robot/${robotId}/control/acquire${force ? '?force=true' : ''}`;
      const res = await apiFetch(path, { method: 'POST' });

      if (res.status === 409) {
        const body = await res.json().catch(() => null);
        const detail = body?.detail;
        const conflictOwner: ControlOwner | null = detail?.owner ?? null;
        setOwner(conflictOwner);
        return {
          ok: false,
          preemptible: Boolean(detail?.preemptible),
          message: detail?.message ?? '다른 세션이 제어 중입니다',
          owner: conflictOwner,
        };
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const body = await res.json();
      tokenRef.current = body.token;
      setOwner(body.owner ?? null);
      return { ok: true };
    },
    [robotId],
  );

  /**
   * 제어권 획득. 자율 작업이 쥐고 있으면(preemptible) 허락을 받아 선점한다.
   * 사람이 쥔 제어권은 force 로도 못 뺏는다 — 그건 관리자 강제 회수의 몫이다.
   */
  const start = useCallback(async (): Promise<boolean> => {
    if (robotId == null) return false;
    setPhase('starting');
    setError(null);
    try {
      let result = await request(false);

      if (!result.ok && result.preemptible) {
        if (Date.now() < preemptBlockedUntilRef.current) {
          setPhase('observing');
          setError(result.message);
          return false;
        }
        const ask = onPreemptRef.current;
        const approved = ask ? await ask(result.message, result.owner) : true;
        if (!approved) {
          preemptBlockedUntilRef.current = Date.now() + PREEMPT_COOLDOWN_MS;
          setPhase('observing');
          setError(result.message);
          return false;
        }
        result = await request(true);
      }

      if (!result.ok) {
        setError(result.message);
        setPhase('observing');
        return false;
      }

      preemptBlockedUntilRef.current = 0;
      setPhase('controlling');
      return true;
    } catch {
      setError('제어를 시작하지 못했습니다');
      setPhase('observing');
      return false;
    }
  }, [robotId, request]);

  /**
   * 조작 직전에 부른다. 이미 쥐고 있으면 즉시 통과, 아니면 획득을 시도한다.
   * 방향키를 연타해도 요청이 겹치지 않도록 진행 중인 시도를 공유한다.
   */
  const ensure = useCallback(async (): Promise<boolean> => {
    if (tokenRef.current) return true;
    if (robotId == null || !enabled) return false;
    if (ensureRef.current) return ensureRef.current;

    const pending = start().finally(() => {
      ensureRef.current = null;
    });
    ensureRef.current = pending;
    return pending;
  }, [robotId, enabled, start]);

  const stop = useCallback(async () => {
    stopHeartbeat();
    const token = tokenRef.current;
    tokenRef.current = null;
    setPhase('observing');
    if (robotId == null || !token) return;
    try {
      await apiFetch(`/robot/${robotId}/control/release`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
      });
    } catch {
      /* 반환 실패해도 서버가 TTL 로 회수한다 */
    }
    refreshOwner();
  }, [robotId, stopHeartbeat, refreshOwner]);

  // 제어 중에는 heartbeat 로 살아있음을 알린다. 끊기면 서버가 TTL 후 자동 해제한다.
  useEffect(() => {
    if (phase !== 'controlling' || robotId == null) return;

    const beat = async () => {
      const token = tokenRef.current;
      if (!token) return;
      try {
        const res = await apiFetch(`/robot/${robotId}/control/heartbeat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token }),
        });
        if (res.status === 409) {
          // 만료됐거나 관리자가 회수했다
          tokenRef.current = null;
          stopHeartbeat();
          setPhase('lost');
          setError('제어권이 해제되었습니다');
          onLostRef.current?.('제어권이 해제되었습니다');
          refreshOwner();
        }
      } catch {
        /* 일시적 네트워크 실패는 다음 주기에 다시 시도 */
      }
    };

    heartbeatRef.current = setInterval(beat, 5000);
    return stopHeartbeat;
  }, [phase, robotId, stopHeartbeat, refreshOwner]);

  // 관찰 중에는 소유자만 주기적으로 확인한다 (제어 중에는 heartbeat 가 대신한다)
  useEffect(() => {
    if (!enabled || robotId == null || phase === 'controlling') return;
    refreshOwner();
    const id = setInterval(refreshOwner, OWNER_POLL_MS);
    return () => clearInterval(id);
  }, [enabled, robotId, phase, refreshOwner]);

  // 화면을 벗어나면 제어권을 돌려준다. 유령 점유를 남기지 않는다.
  useEffect(() => {
    if (enabled) return;
    preemptBlockedUntilRef.current = 0;
    if (tokenRef.current) void stop();
    else setPhase('observing');
  }, [enabled, stop]);

  useEffect(() => {
    return () => {
      const token = tokenRef.current;
      if (!token || robotId == null) return;
      // 언마운트 시에는 응답을 기다릴 수 없다 — 실패해도 서버 TTL 이 회수한다
      void apiFetch(`/robot/${robotId}/control/release`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
      }).catch(() => {});
    };
  }, [robotId]);

  return {
    phase,
    owner,
    error,
    isControlling: phase === 'controlling',
    /** 다른 **사람**이 쥐고 있어 내가 못 잡는 상태 (자율 작업은 선점할 수 있다) */
    heldByOther: phase !== 'controlling' && owner?.ownerType === 'user',
    start,
    ensure,
    stop,
    controlHeaders,
    refreshOwner,
  };
}
