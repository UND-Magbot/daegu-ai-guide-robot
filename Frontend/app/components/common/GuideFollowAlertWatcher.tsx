"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAlertContext } from "@/app/context/AlertContext";
import { useRobotStatusContext } from "@/app/context/RobotStatusContext";
import { useToast } from "@/app/components/common/Toast";
import { markAlertRead } from "@/app/lib/alertData";
import type { AlertMockData } from "@/app/types";
import GuideFollowLostModal from "@/app/components/modal/GuideFollowLostModal";
import { guideScenarioApi } from "@/app/(pages)/dataManagement/components/tabs/voice/guideScenarioApi";

/**
 * 안내 동행 이탈(action=guide_follow_lost) 알림을 글로벌 폴링에서 감지해 확인창을 띄운다.
 * 위치 재조정 확인창(InitPoseAlertWatcher)과 같은 방식이다.
 *
 * - '안내 재개' → follow-resume. 로봇(Jetson)이 세션을 폴링하다 보고 같은 목적지로 다시 출발한다
 * - '안내 종료' → 세션 종료
 * - '나중에'   → 모달만 닫고 알림은 알림센터에 남긴다
 * - 방문자가 "네" 해서 로봇이 먼저 출발했거나 세션이 끝났으면 모달을 스스로 닫는다
 */

export const FOLLOW_LOST_ACTION = "guide_follow_lost";
const SEEN_KEY = "guide_follow_alert_seen_ids_v1";
const SESSION_POLL_MS = 3000;

/** 알림 상세 "session=12 step=clinic_move target=7" 에서 세션 id 를 꺼낸다 */
export function parseSessionId(detail?: string | null): number | null {
  const m = /session=(\d+)/.exec(detail ?? "");
  return m ? Number(m[1]) : null;
}

/** 아직 보지 않은 가장 최근 동행 이탈 알림 */
export function pickFollowAlert(alerts: AlertMockData[], seen: Set<number>): AlertMockData | null {
  return (
    alerts
      .filter((a) => a.log?.Action === FOLLOW_LOST_ACTION && !seen.has(a.id))
      .sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime())[0] ?? null
  );
}

/** 세션이 여전히 운영자를 기다리는가 — 아니면 모달을 닫는다 */
export function stillWaiting(
  session: { id: number; followState: string | null } | null,
  sessionId: number
): boolean {
  return !!session && session.id === sessionId && (session.followState === "waiting" || session.followState === "lost");
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

type ModalState = {
  alertId: number;
  sessionId: number;
  robotId: number | null;
  robotName?: string;
  message?: string;
  detectedAt?: string;
};

export default function GuideFollowAlertWatcher() {
  const { unreadAlerts, refresh: refreshAlerts } = useAlertContext();
  const { robots } = useRobotStatusContext();
  const { showToast } = useToast();
  const seenRef = useRef<Set<number>>(loadSeen());
  const [modal, setModal] = useState<ModalState | null>(null);
  const [busy, setBusy] = useState(false);

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
    const fresh = pickFollowAlert(unreadAlerts, seenRef.current);
    if (!fresh) return;
    const sessionId = parseSessionId(fresh.log?.Detail);
    if (sessionId === null) return;
    const robotName = fresh.robotName ?? fresh.log?.RobotName ?? undefined;
    const robot = robots.find((r) => r.no === robotName);
    setModal({
      alertId: fresh.id,
      sessionId,
      robotId: robot?.id ?? null,
      robotName,
      message: fresh.log?.Message ?? fresh.content,
      detectedAt: fresh.log?.CreatedAt ?? fresh.date,
    });
  }, [unreadAlerts, robots, modal]);

  // 열려 있는 동안 세션을 본다 — 로봇이 먼저 출발했거나 세션이 끝났으면 닫는다
  useEffect(() => {
    if (!modal || modal.robotId === null) return;
    let cancelled = false;
    const check = async () => {
      try {
        const res = await guideScenarioApi.currentSession(modal.robotId as number);
        if (!cancelled && !stillWaiting(res.session, modal.sessionId)) dismiss(modal, true);
      } catch {
        /* 폴링 실패는 넘긴다 — 다음 주기에 다시 본다 */
      }
    };
    check();
    const t = setInterval(check, SESSION_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [modal, dismiss]);

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    if (!modal) return;
    setBusy(true);
    try {
      await fn();
      showToast(ok, "success");
      dismiss(modal, true);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "요청에 실패했습니다", "error");
    } finally {
      setBusy(false);
    }
  };

  if (!modal) return null;
  return (
    <GuideFollowLostModal
      robotName={modal.robotName}
      message={modal.message}
      detectedAt={modal.detectedAt}
      busy={busy}
      onResume={() => act(() => guideScenarioApi.followResume(modal.sessionId), "로봇이 다시 출발합니다")}
      onEnd={() =>
        act(() => guideScenarioApi.closeSession(modal.sessionId, "동행 이탈 — 운영자 종료"), "안내를 종료했습니다")
      }
      onClose={() => dismiss(modal, false)}
      zIndex={10000}
    />
  );
}
