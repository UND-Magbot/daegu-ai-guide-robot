'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import { apiFetch } from '@/app/lib/api';

const RECORDING_POLL_INTERVAL = 2000;

type ActiveSession = {
  robot_id: number;
  module_id: number;
  record_type: 'auto' | 'manual';
  group_id: string;
  started_at: string;
};

type UseRecordingReturn = {
  isRecording: boolean;
  recordType: 'auto' | 'manual' | null;
  isNavigating: boolean;
  isPending: boolean;
  toggleRecording: (moduleId: number) => void;
};

type RecordingApiResult = { status?: string; msg?: string };

// 서버가 { status: 'error', msg } 로 거절하면(FFmpeg 없음·이미 녹화 중 등) 사유를 알린다.
async function reportFailure(
  res: Response,
  action: string,
  onError?: (message: string) => void,
): Promise<void> {
  if (!onError) return;
  if (!res.ok) {
    onError(`녹화 ${action} 실패 (HTTP ${res.status})`);
    return;
  }
  const data: RecordingApiResult = await res.json().catch(() => ({}));
  if (data.status === 'error') onError(data.msg || `녹화 ${action} 실패`);
}

export function useRecording(
  isOpen: boolean,
  robotId?: number,
  onError?: (message: string) => void,
): UseRecordingReturn {
  const [sessions, setSessions] = useState<ActiveSession[]>([]);
  const [isNavigating, setIsNavigating] = useState(false);
  const [isPending, setIsPending] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 폴링: 녹화 활성 세션 조회
  useEffect(() => {
    if (!isOpen || !robotId) {
      setSessions([]);
      setIsNavigating(false);
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
      return;
    }

    const poll = async () => {
      try {
        const res = await apiFetch(`/api/recordings/active?robot_id=${robotId}`);
        if (!res.ok) return;
        const data = await res.json();
        setSessions(data.sessions || []);
        setIsNavigating(data.is_navigating ?? false);
      } catch {
        // 무시
      }
    };

    poll();
    pollRef.current = setInterval(poll, RECORDING_POLL_INTERVAL);
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [isOpen, robotId]);

  const isRecording = sessions.length > 0;
  const recordType = sessions.length > 0 ? sessions[0].record_type : null;

  const toggleRecording = useCallback(
    async (moduleId: number) => {
      if (!robotId || isPending) return;
      setIsPending(true);
      try {
        const post = (path: string) =>
          apiFetch(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ robot_id: robotId, module_id: moduleId }),
          });
        if (isRecording && recordType === 'manual') {
          await reportFailure(await post('/api/recordings/stop'), '중지', onError);
        } else if (!isRecording && !isNavigating) {
          await reportFailure(await post('/api/recordings/start'), '시작', onError);
        }
      } catch {
        onError?.('녹화 요청 실패 - 서버에 연결할 수 없습니다.');
      } finally {
        setIsPending(false);
      }
    },
    [robotId, isPending, isRecording, recordType, isNavigating, onError],
  );

  return { isRecording, recordType, isNavigating, isPending, toggleRecording };
}
