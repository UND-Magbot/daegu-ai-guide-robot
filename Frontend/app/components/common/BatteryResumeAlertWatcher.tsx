"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAlertContext } from "@/app/context/AlertContext";
import { useToast } from "@/app/components/common/Toast";
import { markAlertRead } from "@/app/lib/alertData";
import { batteryResumeApi, type BatteryResumePending } from "@/app/lib/batteryResumeApi";
import type { AlertMockData } from "@/app/types";
import BatteryResumeModal, { type BatteryResumeStage } from "@/app/components/modal/BatteryResumeModal";

/**
 * 배터리 부족 충전 후 이어 가기 알림을 글로벌 폴링에서 감지해 확인창을 띄운다.
 * 안내 동행 이탈 확인창(GuideFollowAlertWatcher)과 같은 방식이다.
 *
 * - battery_resume_ask(충전소로 갈 때): '충전 후 재개'(무응답과 같다) / '오늘은 여기서 종료' / '나중에'
 * - battery_resume_confirm(충전 중 도크에서 나갔다 다시 충전해 80%): '이어 가기'(바로 출발) / '그만하기' / '나중에'
 *   — 응답 전에는 로봇이 출발하지 않는다
 * - 요청이 이미 처리됐거나(다른 관제·자동 재개) 새 요청으로 바뀌면 모달을 스스로 닫는다
 * - '나중에'로 닫은 뒤에는 로봇 카드의 '⚠ 확인 필요' 배지로 다시 연다(openBatteryResumeModal)
 */

export const ASK_ACTION = "battery_resume_ask";
export const CONFIRM_ACTION = "battery_resume_confirm";
const SEEN_KEY = "battery_resume_alert_seen_ids_v1";
const PENDING_POLL_MS = 3000;
const OPEN_EVENT = "battery-resume:open";

/** 로봇 카드 배지에서 확인창을 연다 — 알림 없이 지금 기다리는 요청으로 띄운다 */
export function openBatteryResumeModal(robotName?: string) {
  window.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail: { robotName } }));
}

/** 알림 없이 열 때 보여 줄 내용 */
export function pendingMessage(p: BatteryResumePending): string {
  const what = { standby: "근무", route: "경로 반복", task: "예약 작업" }[p.kind];
  return `${what} '${p.label}'`;
}

/** 알림 상세 "resume=12 kind=route" 에서 요청 번호를 꺼낸다 */
export function parseResumeToken(detail?: string | null): number | null {
  const m = /resume=(\d+)/.exec(detail ?? "");
  return m ? Number(m[1]) : null;
}

/** 아직 보지 않은 가장 최근 이어 가기 알림 */
export function pickResumeAlert(alerts: AlertMockData[], seen: Set<number>): AlertMockData | null {
  return (
    alerts
      .filter((a) => (a.log?.Action === ASK_ACTION || a.log?.Action === CONFIRM_ACTION) && !seen.has(a.id))
      .sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime())[0] ?? null
  );
}

/**
 * 이 확인창이 아직 유효한가. 서버가 다시 묻는 단계(confirm)로 넘어가면 처음 확인창(ask)은 닫는다 —
 * 그때 '충전 후 재개'를 누르면 문구와 달리 바로 출발하게 된다. confirm 알림이 따로 뜬다.
 */
export function stillPending(pending: BatteryResumePending | null, token: number, stage: BatteryResumeStage): boolean {
  if (!pending || pending.token !== token) return false;
  return stage === "confirm" ? pending.confirm_asked : !pending.confirm_asked;
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
  /** 알림으로 열었으면 그 알림 id, 카드 배지로 열었으면 null */
  alertId: number | null;
  token: number;
  stage: BatteryResumeStage;
  robotName?: string;
  message?: string;
  detectedAt?: string;
};

export default function BatteryResumeAlertWatcher() {
  const { unreadAlerts, refresh: refreshAlerts } = useAlertContext();
  const { showToast } = useToast();
  const seenRef = useRef<Set<number>>(loadSeen());
  const [modal, setModal] = useState<ModalState | null>(null);
  const [busy, setBusy] = useState(false);

  const dismiss = useCallback(
    (state: ModalState, markRead: boolean) => {
      if (state.alertId === null) {
        setModal(null);
        return;
      }
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
    const fresh = pickResumeAlert(unreadAlerts, seenRef.current);
    if (!fresh) return;
    const token = parseResumeToken(fresh.log?.Detail);
    if (token === null) return;
    setModal({
      alertId: fresh.id,
      token,
      stage: fresh.log?.Action === CONFIRM_ACTION ? "confirm" : "ask",
      robotName: fresh.robotName ?? fresh.log?.RobotName ?? undefined,
      message: fresh.log?.Message ?? fresh.content,
      detectedAt: fresh.log?.CreatedAt ?? fresh.date,
    });
  }, [unreadAlerts, modal]);

  // 로봇 카드 배지에서 열기
  useEffect(() => {
    const onOpen = async (e: Event) => {
      const robotName = (e as CustomEvent<{ robotName?: string }>).detail?.robotName;
      try {
        const { pending } = await batteryResumeApi.pending();
        if (!pending) {
          showToast("확인을 기다리는 요청이 없습니다", "info");
          return;
        }
        setModal({
          alertId: null,
          token: pending.token,
          stage: pending.confirm_asked ? "confirm" : "ask",
          robotName,
          message: pendingMessage(pending),
        });
      } catch (err) {
        showToast(err instanceof Error ? err.message : "요청을 불러오지 못했습니다", "error");
      }
    };
    window.addEventListener(OPEN_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_EVENT, onOpen);
  }, [showToast]);

  // 열려 있는 동안 요청이 살아 있는지 본다 — 이미 처리됐으면 닫는다
  useEffect(() => {
    if (!modal) return;
    let cancelled = false;
    const check = async () => {
      try {
        const res = await batteryResumeApi.pending();
        if (!cancelled && !stillPending(res.pending, modal.token, modal.stage)) dismiss(modal, true);
      } catch {
        /* 폴링 실패는 넘긴다 — 다음 주기에 다시 본다 */
      }
    };
    check();
    const t = setInterval(check, PENDING_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [modal, dismiss]);

  const decide = async (decision: "resume" | "end") => {
    if (!modal) return;
    setBusy(true);
    try {
      const res = await batteryResumeApi.decide(modal.token, decision);
      showToast(res.msg, "success");
      dismiss(modal, true);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "요청에 실패했습니다", "error");
    } finally {
      setBusy(false);
    }
  };

  if (!modal) return null;
  return (
    <BatteryResumeModal
      stage={modal.stage}
      robotName={modal.robotName}
      message={modal.message}
      detectedAt={modal.detectedAt}
      busy={busy}
      onResume={() => decide("resume")}
      onEnd={() => decide("end")}
      onClose={() => dismiss(modal, false)}
      zIndex={10000}
    />
  );
}
