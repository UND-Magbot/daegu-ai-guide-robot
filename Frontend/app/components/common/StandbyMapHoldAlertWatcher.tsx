"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAlertContext } from "@/app/context/AlertContext";
import { useToast } from "@/app/components/common/Toast";
import { apiFetch } from "@/app/lib/api";
import { markAlertRead } from "@/app/lib/alertData";
import { isFreshAlert } from "@/app/lib/alertFreshness";
import type { AlertMockData } from "@/app/types";
import StandbyMapHoldModal from "@/app/components/modal/StandbyMapHoldModal";

/**
 * 상시 대기 시작 보류(action=standby_map_mismatch) 알림을 글로벌 폴링에서 감지해 알림창을 띄운다.
 * 배터리 중단 알림창(GuideBatteryAbortAlertWatcher)과 같은 방식이다.
 *
 * - 대기 장소가 로봇이 있는 층과 달라 근무를 시작하지 못했다 — 사람이 로봇을 옮기면 서버가 스스로 시작한다
 * - '확인' → 읽음 처리, '나중에' → 모달만 닫고 알림은 알림센터에 남긴다
 * - 열려 있는 동안 근무가 시작되면(로봇을 옮김) 스스로 닫는다
 * - 발생 후 30분이 지난 알림은 창으로 띄우지 않는다(alertFreshness)
 */

export const MAP_HOLD_ACTION = "standby_map_mismatch";
const SEEN_KEY = "standby_map_hold_seen_ids_v1";
const LIVE_POLL_MS = 5000;

export type MapHoldInfo = {
  scheduleId: number | null;
  workName: string | null;
  place: string | null;
  placeFloor: string | null;
  robotFloor: string | null;
};

const EMPTY: MapHoldInfo = { scheduleId: null, workName: null, place: null, placeFloor: null, robotFloor: null };

/** 알림 상세 JSON(백엔드 scheduler/loop._note_map_hold)을 읽는다. 읽지 못하면 빈 값 */
export function parseMapHold(detail?: string | null): MapHoldInfo {
  try {
    const d = JSON.parse(detail ?? "");
    if (!d || typeof d !== "object") return EMPTY;
    const str = (v: unknown) => (typeof v === "string" && v ? v : null);
    return {
      scheduleId: typeof d.scheduleId === "number" ? d.scheduleId : null,
      workName: str(d.workName),
      place: str(d.place),
      placeFloor: str(d.placeFloor),
      robotFloor: str(d.robotFloor),
    };
  } catch {
    return EMPTY;
  }
}

/** 아직 보지 않은, 방금 생긴 가장 최근 보류 알림 */
export function pickMapHoldAlert(
  alerts: AlertMockData[],
  seen: Set<number>,
  now: number = Date.now()
): AlertMockData | null {
  return (
    alerts
      .filter((a) => a.log?.Action === MAP_HOLD_ACTION && !seen.has(a.id) && isFreshAlert(a, now))
      .sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime())[0] ?? null
  );
}

/** 그 근무가 시작됐는가 — /DB/standby-live 응답 */
export function shiftStarted(live: { active?: boolean; scheduleId?: number } | null, scheduleId: number): boolean {
  return !!live?.active && live.scheduleId === scheduleId;
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

type ModalState = MapHoldInfo & {
  alertId: number;
  robotName?: string;
  message?: string;
  detectedAt?: string;
};

export default function StandbyMapHoldAlertWatcher() {
  const { unreadAlerts, refresh: refreshAlerts } = useAlertContext();
  const { showToast } = useToast();
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
    const fresh = pickMapHoldAlert(unreadAlerts, seenRef.current);
    if (!fresh) return;
    setModal({
      alertId: fresh.id,
      robotName: fresh.robotName ?? fresh.log?.RobotName ?? undefined,
      message: fresh.log?.Message ?? fresh.content,
      detectedAt: fresh.log?.CreatedAt ?? fresh.date,
      ...parseMapHold(fresh.log?.Detail),
    });
  }, [unreadAlerts, modal]);

  // 열려 있는 동안 근무가 시작됐는지 본다 — 로봇을 옮겨 시작했으면 닫는다
  useEffect(() => {
    if (!modal || modal.scheduleId === null) return;
    let cancelled = false;
    const check = async () => {
      try {
        const res = await apiFetch("/DB/standby-live");
        if (!res.ok) return;
        if (!cancelled && shiftStarted(await res.json(), modal.scheduleId as number)) {
          showToast(`${modal.workName ?? "상시 대기"} 근무를 시작했습니다`, "success");
          dismiss(modal, true);
        }
      } catch {
        /* 폴링 실패는 넘긴다 — 다음 주기에 다시 본다 */
      }
    };
    check();
    const t = setInterval(check, LIVE_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [modal, dismiss, showToast]);

  if (!modal) return null;
  return (
    <StandbyMapHoldModal
      workName={modal.workName}
      robotName={modal.robotName}
      place={modal.place}
      placeFloor={modal.placeFloor}
      robotFloor={modal.robotFloor}
      message={modal.message}
      detectedAt={modal.detectedAt}
      onConfirm={() => dismiss(modal, true)}
      onClose={() => dismiss(modal, false)}
      zIndex={10000}
    />
  );
}
