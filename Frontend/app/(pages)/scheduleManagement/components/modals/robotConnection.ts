import type { RobotNetwork, ServerStatus } from '@/app/lib/robotLiveness';

/**
 * 작업 등록 창의 로봇 연결 상태 줄(10-23).
 *
 * 서버는 '지금 바로 실행될 작업'을 끊긴 로봇에 등록하면 409 로 거절하고,
 * 나중에 실행될 작업은 받되 실행 시각에 끊겨 있으면 알림을 보낸다. 이 줄은 그 규칙을 미리 보여 준다.
 * 화면의 연결 상태는 5초마다 갱신되므로 서버 판정(35초 무응답 = 끊김)과 잠깐 어긋날 수 있다.
 */
export type RobotConnectionTone = 'online' | 'error' | 'offline' | 'unknown';

export interface RobotConnectionNote {
  tone: RobotConnectionTone;
  label: string;
  note: string | null;
}

export function robotConnectionNote(
  network: RobotNetwork | null,
  serverStatus: ServerStatus,
): RobotConnectionNote | null {
  if (serverStatus !== 'ok') {
    return {
      tone: 'unknown',
      label: '확인 불가',
      note: '관제 서버 응답이 없어 로봇 연결 상태를 확인할 수 없습니다.',
    };
  }
  if (network === null) return null;
  if (network === 'Online') return { tone: 'online', label: '연결됨', note: null };
  if (network === 'Error') {
    return {
      tone: 'error',
      label: '연결 불안정',
      note: '등록은 됩니다. 실행 시각에도 끊겨 있으면 알림을 보내고 연결될 때까지 기다립니다.',
    };
  }
  return {
    tone: 'offline',
    label: '연결 안 됨',
    note: '지금 바로 실행되는 작업은 등록할 수 없습니다. 나중에 실행할 작업은 등록되고, 실행 시각에도 끊겨 있으면 알림을 보냅니다.',
  };
}
