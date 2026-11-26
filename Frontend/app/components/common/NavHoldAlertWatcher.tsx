"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAlertContext } from "@/app/context/AlertContext";
import { useToast } from "@/app/components/common/Toast";
import { markAlertRead } from "@/app/lib/alertData";
import type { AlertMockData } from "@/app/types";
import NavHoldModal from "@/app/components/modal/NavHoldModal";
import { HOLD_ALERT_ACTIONS, actionLabel, navHoldApi, type NavHold, type NavHoldAction } from "@/app/lib/navHold";

/**
 * 이동 멈춤(지점 도착 못 함·안내 이동 막힘) 알림을 글로벌 폴링에서 감지해 확인창을 띄운다.
 * 동행 이탈 확인창(GuideFollowAlertWatcher)과 같은 방식이다.
 *
 * - 열려 있는 동안 `GET /nav/hold` 를 본다 — 멈춤이 풀리거나(로봇이 다시 감) 처리되면 스스로 닫는다
 * - '나중에' 는 모달만 닫고 알림은 남긴다(로봇 상세 카드에서도 고를 수 있다)
 */

const SEEN_KEY = "nav_hold_alert_seen_ids_v1";
const HOLD_POLL_MS = 3000;

/** 아직 보지 않은 가장 최근 멈춤 알림 */
export function pickHoldAlert(alerts: AlertMockData[], seen: Set<number>): AlertMockData | null {
  return (
    alerts
      .filter((a) => (HOLD_ALERT_ACTIONS as readonly string[]).includes(a.log?.Action ?? "") && !seen.has(a.id))
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

type ModalState = { alertId: number; robotName?: string; hold: NavHold };

export default function NavHoldAlertWatcher() {
  const { unreadAlerts, refresh: refreshAlerts } = useAlertContext();
  const { showToast } = useToast();
  const seenRef = useRef<Set<number>>(loadSeen());
  const [modal, setModal] = useState<ModalState | null>(null);
  const [busy, setBusy] = useState(false);

  const dismiss = useCallback(
    (alertId: number, markRead: boolean) => {
      seenRef.current.add(alertId);
      saveSeen(seenRef.current);
      if (markRead) {
        markAlertRead(alertId)
          .then(() => refreshAlerts?.())
          .catch(() => {
            /* 읽음 처리 실패는 무시 — 알림센터에 남을 뿐이다 */
          });
      }
      setModal(null);
    },
    [refreshAlerts]
  );

  // 새 알림 → 지금 멈춤이 열려 있을 때만 띄운다(이미 풀렸으면 읽음 처리)
  useEffect(() => {
    if (modal) return;
    const fresh = pickHoldAlert(unreadAlerts, seenRef.current);
    if (!fresh) return;
    let cancelled = false;
    navHoldApi
      .get()
      .then((res) => {
        if (cancelled) return;
        if (!res.hold) {
          dismiss(fresh.id, true);
          return;
        }
        setModal({ alertId: fresh.id, robotName: res.robotName ?? fresh.robotName ?? undefined, hold: res.hold });
      })
      .catch(() => {
        /* 다음 알림 주기에 다시 본다 */
      });
    return () => {
      cancelled = true;
    };
  }, [unreadAlerts, modal, dismiss]);

  // 열려 있는 동안 멈춤 상태를 본다 — 풀리거나 처리되면 닫는다
  const alertId = modal?.alertId ?? null;
  useEffect(() => {
    if (alertId === null) return;
    let cancelled = false;
    const check = async () => {
      try {
        const res = await navHoldApi.get();
        if (cancelled) return;
        if (!res.hold) dismiss(alertId, true);
        else setModal((m) => (m ? { ...m, hold: res.hold as NavHold } : m));
      } catch {
        /* 폴링 실패는 넘긴다 */
      }
    };
    const t = setInterval(check, HOLD_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [alertId, dismiss]);

  const act = async (action: NavHoldAction) => {
    if (!modal) return;
    setBusy(true);
    try {
      await navHoldApi.act(action);
      showToast(`${actionLabel(modal.hold.kind, action)} — 요청했습니다`, "success");
      dismiss(modal.alertId, true);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "요청에 실패했습니다", "error");
    } finally {
      setBusy(false);
    }
  };

  if (!modal) return null;
  return (
    <NavHoldModal
      robotName={modal.robotName}
      hold={modal.hold}
      busy={busy}
      onAction={act}
      onClose={() => dismiss(modal.alertId, false)}
      zIndex={10000}
    />
  );
}
