"use client";

import React, { useCallback, useEffect, useState } from "react";
import { useToast } from "@/app/components/common/Toast";
import styles from "./voice.module.css";
import g from "./guideScenario.module.css";
import { guideScenarioApi, type KeepGaitState } from "./guideScenarioApi";

// 출발 확인 실패로 이번 안내만 꺼졌는지 보여 주려면 세션 카드처럼 폴링해야 한다
const POLL_MS = 5000;

/**
 * 안내 도착 후 내비 보행 유지 — 시연 중에도 재배포 없이 켜고 끈다.
 * 이 관제가 주행시키지 않는 로봇이면(409) 숨긴다.
 */
export default function KeepGaitToggle({ robotId }: { robotId: number }) {
  const { showToast } = useToast();
  const [state, setState] = useState<KeepGaitState | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setState(await guideScenarioApi.keepGait(robotId));
    } catch {
      setState(null);
    }
  }, [robotId]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  if (!state) return null;

  const toggle = async (enabled: boolean) => {
    setBusy(true);
    try {
      setState(await guideScenarioApi.setKeepGait(robotId, enabled));
      showToast(enabled ? "도착 후 보행 유지를 켰습니다" : "도착 후 보행 유지를 껐습니다", "success");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "요청에 실패했습니다", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={g.keepGaitRow}>
      <label
        className={styles.checkRow}
        title="안내 도착·경유지마다 기본 보행으로 바꾸지 않고 내비 보행을 유지한 채 다음 이동을 보냅니다(다리 재정렬 소음 감소)"
      >
        <input type="checkbox" checked={state.enabled} disabled={busy} onChange={(e) => toggle(e.target.checked)} />
        <span>도착 후 보행 유지</span>
      </label>
      {state.enabled && state.sessionOff && (
        <span className={g.mapWarn}>이번 안내는 출발 확인이 안 돼 기존 방식으로 진행합니다. 다음 안내에서 다시 시도합니다.</span>
      )}
      {state.enabled !== state.envDefault && (
        <span className={g.keepGaitHint}>백엔드 재시작 시 {state.envDefault ? "켜짐" : "꺼짐"}으로 돌아갑니다</span>
      )}
    </div>
  );
}
