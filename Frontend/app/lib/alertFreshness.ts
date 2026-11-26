import type { AlertMockData } from "@/app/types";

/** "지금 가서 확인하라"는 알림을 창으로 띄우는 시간(분) — 지난 일이 페이지를 열 때 튀어나오지 않게 */
export const ALERT_POPUP_FRESH_MIN = 30;

/**
 * 알림이 발생한 지 minutes 분 안인가. 시각을 읽지 못하면 참(놓치는 쪽이 더 나쁘다).
 * 서버 시각은 "2026-10-23 14:59:27"(관제 서버 현지 시각, 시간대 없음) — 'T' 로 바꿔 어느 브라우저든 현지 시각으로 읽게 한다.
 */
export function isFreshAlert(
  alert: AlertMockData,
  now: number = Date.now(),
  minutes: number = ALERT_POPUP_FRESH_MIN
): boolean {
  const raw = alert.log?.CreatedAt ?? alert.date ?? "";
  const t = new Date(raw.replace(" ", "T")).getTime();
  return isNaN(t) || now - t <= minutes * 60_000;
}
