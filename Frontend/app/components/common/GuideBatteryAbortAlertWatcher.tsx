"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAlertContext } from "@/app/context/AlertContext";
import { markAlertRead } from "@/app/lib/alertData";
import { ALERT_POPUP_FRESH_MIN, isFreshAlert } from "@/app/lib/alertFreshness";
import type { AlertMockData } from "@/app/types";
import GuideBatteryAbortModal from "@/app/components/modal/GuideBatteryAbortModal";

/**
 * 배터리 부족 안내 중단(action=guide_battery_aborted) 알림을 글로벌 폴링에서 감지해 알림창을 띄운다.
 * 안내 동행 이탈 확인창(GuideFollowAlertWatcher)과 같은 방식이다.
 *
 * - 로봇은 이미 안내를 닫고 충전소로 가는 중이다 — 고를 것 없이 멈춘 곳·못 간 목적지를 알린다
 * - '확인' → 읽음 처리, '나중에' → 모달만 닫고 알림은 알림센터에 남긴다
 * - 발생 후 FRESH_MIN 분이 지난 알림은 창으로 띄우지 않는다 — "지금 가서 방문객을 확인하라"는 알림이라
 *   나중에 페이지를 열었을 때 지난 일이 튀어나오면 지금 일로 오해한다. 알림센터에는 남는다
 * - 근무 중이었다면 충전 후 이어 가기 확인창(BatteryResumeAlertWatcher)이 이 창 아래에 따로 뜬다
 */

export const BATTERY_ABORT_ACTION = "guide_battery_aborted";
const SEEN_KEY = "guide_battery_abort_seen_ids_v1";
/** 이 시간 안에 생긴 알림만 창으로 띄운다(분) */
export const FRESH_MIN = ALERT_POPUP_FRESH_MIN;

export type BatteryAbortInfo = {
  battery: number | null;
  stoppedAt: string | null;
  /** 가장 가까운 장소 "내과에서 1.2m" — 맵을 모르거나 예전 알림이면 없다 */
  nearPlace: string | null;
  destination: string | null;
};

/**
 * 알림 본문·상세에서 배터리·멈춘 곳·못 간 목적지를 꺼낸다.
 * 상세는 백엔드 scheduler/loop.on_scenario_closed 형식(가까운 곳은 10-23 부터):
 * "session=12 멈춘 곳=(1.2, 3.4) 가까운 곳=내과에서 1.2m step=clinic_move 못 간 목적지=내과"
 */
export function parseBatteryAbort(message?: string | null, detail?: string | null): BatteryAbortInfo {
  const d = (detail ?? "").trim();
  const bat = /배터리\s*(\d+)%/.exec(message ?? "");
  const where = /멈춘 곳=(.+?)\s+(?:가까운 곳=|step=)/.exec(d);
  const near = /가까운 곳=(.+?)\s+step=/.exec(d);
  const dest = /못 간 목적지=(.+)$/.exec(d);
  const value = (m: RegExpExecArray | null, empty: string) => (m && m[1].trim() !== empty ? m[1].trim() : null);
  return {
    battery: bat ? Number(bat[1]) : null,
    stoppedAt: value(where, "알 수 없음"),
    nearPlace: value(near, "-"),
    destination: value(dest, "-"),
  };
}

/** 발생한 지 FRESH_MIN 분 안인가 — alertFreshness 참고 */
export function isFresh(alert: AlertMockData, now: number = Date.now()): boolean {
  return isFreshAlert(alert, now, FRESH_MIN);
}

/** 아직 보지 않은, 방금 생긴 가장 최근 배터리 중단 알림 */
export function pickBatteryAbortAlert(
  alerts: AlertMockData[],
  seen: Set<number>,
  now: number = Date.now()
): AlertMockData | null {
  return (
    alerts
      .filter((a) => a.log?.Action === BATTERY_ABORT_ACTION && !seen.has(a.id) && isFresh(a, now))
      .sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime())[0] ?? null
  );
}

function loadSeen(): Set<number> {
  if (typeof window === "undefined") return new Set();
  try {
    const arr = JSON.parse(sessionStorage.getItem(SEEN_KEY) ?? "[]");
    return new Set(Array.isArray(arr) ? arr.filter((v) => typeof v === "number") : []);
  } catch {
    return new Set();
  }
}

function saveSeen(seen: Set<number>) {
  try {
    sessionStorage.setItem(SEEN_KEY, JSON.stringify(Array.from(seen)));
  } catch {
    /* 저장 실패는 무시 — 새로고침하면 다시 뜰 뿐이다 */
  }
}

type ModalState = BatteryAbortInfo & {
  alertId: number;
  robotName?: string;
  detectedAt?: string;
};

export default function GuideBatteryAbortAlertWatcher() {
  const { unreadAlerts, refresh: refreshAlerts } = useAlertContext();
  const seenRef = useRef<Set<number>>(loadSeen());
  const [modal, setModal] = useState<ModalState | null>(null);

  const dismiss = useCallback(
    (state: ModalState, markRead: boolean) => {
      seenRef.current.add(state.alertId);
      saveSeen(seenRef.current);
      if (markRead) {
        markAlertRead(state.alertId)
          .then(() => refreshAlerts?.())
          .catch(() => {
            /* 읽음 처리 실패는 무시 — 알림센터에 남을 뿐이다 */
          });
      }
      setModal(null);
    },
    [refreshAlerts]
  );

  // 새 알림 감지
  useEffect(() => {
    if (modal) return;
    const fresh = pickBatteryAbortAlert(unreadAlerts, seenRef.current);
    if (!fresh) return;
    setModal({
      alertId: fresh.id,
      robotName: fresh.robotName ?? fresh.log?.RobotName ?? undefined,
      detectedAt: fresh.log?.CreatedAt ?? fresh.date,
      ...parseBatteryAbort(fresh.log?.Message ?? fresh.content, fresh.log?.Detail),
    });
  }, [unreadAlerts, modal]);

  if (!modal) return null;
  return (
    <GuideBatteryAbortModal
      robotName={modal.robotName}
      battery={modal.battery}
      stoppedAt={modal.stoppedAt}
      nearPlace={modal.nearPlace}
      destination={modal.destination}
      detectedAt={modal.detectedAt}
      onConfirm={() => dismiss(modal, true)}
      onClose={() => dismiss(modal, false)}
      zIndex={10000}
    />
  );
}
