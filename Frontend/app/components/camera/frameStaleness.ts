/**
 * 화면 멈춤 판정 — "마지막 화면 N초 전" 표시용.
 *
 * WebRTC 연결은 살아 있는데 프레임만 끊기는 경우(카메라·MediaMTX 소스 정지, 일시 끊김 유예 중)
 * <video> 는 마지막 프레임을 그대로 보여준다. 이때 LIVE 배지가 떠 있으면 운영자는 멈춘 화면을
 * 실시간으로 착각한다. 그래서 디코딩된 프레임 수(getStats framesDecoded)가 늘었는지로
 * 마지막 프레임 시각을 재고, 일정 시간 넘게 멈추면 LIVE 대신 경과 시간을 보여준다.
 */

import { STALE_COPY } from "./streamStatusCopy";

/** 이 시간 넘게 새 프레임이 없으면 멈춘 화면으로 본다(정상 프레임 간격은 0.1초 미만). */
export const STALE_AFTER_MS = 3000;

/** 프레임 수 확인 주기. */
export const FRAME_POLL_MS = 1000;

export type FrameClock = {
  /** 트랙을 받은 시각 — 프레임이 한 번도 안 온 경우의 기준점 */
  since: number | null;
  /** 마지막으로 새 프레임을 확인한 시각 */
  lastFrameAt: number | null;
  /** 지금까지 디코딩된 프레임 수 */
  frames: number;
  /** 브라우저가 framesDecoded 를 알려주는지. false 면 판단 불가 → 표시하지 않는다 */
  supported: boolean;
};

export const EMPTY_CLOCK: FrameClock = { since: null, lastFrameAt: null, frames: 0, supported: true };

/** 트랙을 새로 받았을 때의 시계. */
export function startFrameClock(now: number): FrameClock {
  return { since: now, lastFrameAt: null, frames: 0, supported: true };
}

/** getStats 결과 하나를 반영한다. framesDecoded 를 못 읽으면 판단 불가로 둔다. */
export function advanceFrameClock(
  clock: FrameClock,
  framesDecoded: number | undefined,
  now: number,
): FrameClock {
  if (typeof framesDecoded !== "number") return { ...clock, supported: false };
  if (framesDecoded > clock.frames) {
    return { ...clock, lastFrameAt: now, frames: framesDecoded, supported: true };
  }
  return clock.supported ? clock : { ...clock, supported: true };
}

/** RTCStatsReport 에서 수신 영상의 framesDecoded 를 꺼낸다. */
export function readFramesDecoded(report: RTCStatsReport): number | undefined {
  let frames: number | undefined;
  report.forEach((s: { type?: string; kind?: string; mediaType?: string; framesDecoded?: number }) => {
    if (s.type !== "inbound-rtp") return;
    if ((s.kind ?? s.mediaType) !== "video") return;
    if (typeof s.framesDecoded === "number") frames = s.framesDecoded;
  });
  return frames;
}

function formatAge(ageMs: number): string {
  const sec = Math.floor(ageMs / 1000);
  return sec < 60 ? `${sec}초` : `${Math.floor(sec / 60)}분`;
}

/**
 * 멈춤 배지 문구. null 이면 멈추지 않았거나 판단할 수 없는 것 → 기존대로(LIVE 등) 둔다.
 *  - 프레임을 받은 적이 있음 → "마지막 화면 N초 전"
 *  - 트랙은 왔는데 프레임이 한 번도 없음 → "영상 없음 N초째"
 */
export function staleBadgeText(
  status: string,
  clock: FrameClock | null,
  now: number,
): string | null {
  if (status !== "playing" && status !== "recovering") return null;
  if (!clock || !clock.supported) return null;
  const ref = clock.lastFrameAt ?? clock.since;
  if (ref == null) return null;
  const age = now - ref;
  if (age < STALE_AFTER_MS) return null;
  return clock.lastFrameAt != null
    ? STALE_COPY.lastFrame(formatAge(age))
    : STALE_COPY.noFrame(formatAge(age));
}
