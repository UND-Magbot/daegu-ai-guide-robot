"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAlertContext } from "@/app/context/AlertContext";
import { useToast } from "@/app/components/common/Toast";
import { markAlertRead } from "@/app/lib/alertData";
import { isFreshAlert } from "@/app/lib/alertFreshness";
import type { AlertMockData } from "@/app/types";
import GuideTtsFailedModal from "@/app/components/modal/GuideTtsFailedModal";
import { guideScenarioApi } from "@/app/(pages)/dataManagement/components/tabs/voice/guideScenarioApi";

/**
 * 안내 음성 출력 실패(action=guide_tts_failed) 알림을 글로벌 폴링에서 감지해 알림창을 띄운다.
 * 상시 대기 시작 보류 알림창(StandbyMapHoldAlertWatcher)과 같은 방식이다.
 *
 * 예전에는 알림센터(종 아이콘)에만 쌓이고 창이 뜨지 않아, 제목이 "운영자 확인 필요"인데도 운영자가
 * 알아채지 못했다(11-02 세션 522~524). 전역 오류 창(GlobalErrorAlert)은 로봇 종류 알림만 띄운다.
 *
 * - '확인' → 읽음 처리, '안내 종료' → 세션 종료 + 읽음, '나중에' → 모달만 닫고 알림은 알림센터에 남긴다
 * - 관제는 세션마다 한 번만 이 알림을 만든다(voice_scenario.session_tts_failed). 같은 세션 반복은 guide_tts_failed_repeat
 * - 발생 후 30분이 지난 알림은 창으로 띄우지 않는다(alertFreshness)
 */

export const TTS_FAILED_ACTION = "guide_tts_failed";
const SEEN_KEY = "guide_tts_failed_seen_ids_v1";

export type TtsFailedInfo = { sessionId: number | null; step: string | null; text: string | null };

/** 알림 상세 "session=524 step=greet text='안녕하세요…' error=" 를 읽는다 */
export function parseTtsFailed(detail?: string | null): TtsFailedInfo {
  const d = detail ?? "";
  const session = /session=(\d+)/.exec(d);
  const step = /step=(\S+)/.exec(d);
  const text = /text=(['"])(.*?)\1(?:\s+error=|$)/.exec(d);
  return {
    sessionId: session ? Number(session[1]) : null,
    step: step && step[1] !== "None" ? step[1] : null,
    text: text ? text[2] : null,
  };
}

/** 본문 "로봇 안내 음성 출력 실패(질문)" 의 괄호 안 — 안내 멘트 | 질문 */
export function ttsFailedKind(message?: string | null): string | null {
  const m = /출력 실패\(([^)]+)\)/.exec(message ?? "");
  return m ? m[1] : null;
}

/** 아직 보지 않은, 방금 생긴 가장 최근 음성 실패 알림 */
export function pickTtsFailedAlert(
  alerts: AlertMockData[],
  seen: Set<number>,
  now: number = Date.now()
): AlertMockData | null {
  return (
    alerts
      .filter((a) => a.log?.Action === TTS_FAILED_ACTION && !seen.has(a.id) && isFreshAlert(a, now))
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

type ModalState = TtsFailedInfo & {
  alertId: number;
  robotName?: string;
  kind: string | null;
  message?: string;
  detectedAt?: string;
};

export default function GuideTtsFailedAlertWatcher() {
  const { unreadAlerts, refresh: refreshAlerts } = useAlertContext();
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

  useEffect(() => {
    if (modal) return;
    const fresh = pickTtsFailedAlert(unreadAlerts, seenRef.current);
    if (!fresh) return;
    const message = fresh.log?.Message ?? fresh.content;
    setModal({
      alertId: fresh.id,
      robotName: fresh.robotName ?? fresh.log?.RobotName ?? undefined,
      kind: ttsFailedKind(message),
      message,
      detectedAt: fresh.log?.CreatedAt ?? fresh.date,
      ...parseTtsFailed(fresh.log?.Detail),
    });
  }, [unreadAlerts, modal]);

  const endGuide = async () => {
    if (!modal || modal.sessionId === null) return;
    setBusy(true);
    try {
      await guideScenarioApi.closeSession(modal.sessionId, "음성 출력 실패 — 운영자 종료");
      showToast("안내를 종료했습니다", "success");
      dismiss(modal, true);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "요청에 실패했습니다", "error");
    } finally {
      setBusy(false);
    }
  };

  if (!modal) return null;
  return (
    <GuideTtsFailedModal
      robotName={modal.robotName}
      kind={modal.kind}
      text={modal.text}
      message={modal.message}
      detectedAt={modal.detectedAt}
      busy={busy}
      onConfirm={() => dismiss(modal, true)}
      onEnd={modal.sessionId !== null ? endGuide : undefined}
      onClose={() => dismiss(modal, false)}
      zIndex={10000}
    />
  );
}
