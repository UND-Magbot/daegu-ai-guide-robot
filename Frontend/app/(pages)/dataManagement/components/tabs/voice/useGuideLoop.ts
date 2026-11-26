"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { guideScenarioApi, type GuideSession, type LoopCheck, type LoopStopKind } from "./guideScenarioApi";
import { RESULT_LABELS } from "./guideScenarioLabels";

/** 회차 사이 대기(초) — 운영자가 로봇·주변을 보고 중지할 틈 */
export const LOOP_GAP_SEC = 10;
/** 회차가 아직 진행 중이라는 답을 받으면 이만큼 뒤 다시 묻는다 */
const RECHECK_MS = 2000;

export type LoopPlan = {
  scenarioCode: string;
  scenarioName: string;
  startLocationId: number | null;
  /** null = 무한 */
  total: number | null;
};

export type LoopRound = { round: number; sessionId: number; result: string; ok: boolean };

export type LoopEndKind = LoopStopKind | "done" | "stopped" | "open_failed";

export type LoopRun = {
  plan: LoopPlan;
  /** 진행 중이거나 방금 끝난 회차 번호(1부터) */
  round: number;
  /** 진행 중 회차의 세션. null 이면 회차 사이(카운트다운) 또는 종료 */
  sessionId: number | null;
  countdown: number | null;
  /** 지금 회차까지만 하고 멈춘다 */
  stopRequested: boolean;
  rounds: LoopRound[];
  ended: { kind: LoopEndKind; reason: string } | null;
};

type Args = {
  robotId: number;
  /** 폴링한 진행 중 세션 */
  session: GuideSession | null;
  /** 첫 폴링이 끝났는가 — 그 전의 null 은 '끝남'이 아니다 */
  loaded: boolean;
  onOpened: (s: GuideSession) => void;
  notify: (msg: string, type: "success" | "error") => void;
};

function stopReason(chk: LoopCheck): string {
  if (chk.kind === "failed") return `직전 회차가 '${RESULT_LABELS[chk.result] ?? chk.result}'(으)로 끝났습니다.`;
  return chk.reason ?? "반복을 멈췄습니다.";
}

/** 회차 결과로 다음 상태를 정한다 — 멈춤 판정은 서버(loop-check), 횟수·중지 요청은 화면 */
export function afterRound(r: LoopRun, sessionId: number, chk: LoopCheck): LoopRun {
  const rounds = [...r.rounds, { round: r.round, sessionId, result: chk.result, ok: chk.ok }];
  const end = (kind: LoopEndKind, reason: string): LoopRun => ({ ...r, sessionId: null, rounds, ended: { kind, reason } });
  if (!chk.ok) return end(chk.kind === "running" || !chk.kind ? "failed" : chk.kind, stopReason(chk));
  if (r.stopRequested) return end("stopped", `반복을 중지했습니다(${r.round}회 완료).`);
  if (r.plan.total !== null && r.round >= r.plan.total) return end("done", `${r.plan.total}회 반복을 마쳤습니다.`);
  return { ...r, sessionId: null, rounds, countdown: LOOP_GAP_SEC };
}

/**
 * 안내 반복(시연 전 테스트) — 회차가 끝나면 서버에 다음 회차를 열어도 되는지 묻고, 대기 뒤 같은
 * 시나리오·출발 장소로 다시 연다. 화면을 닫거나 새로고침하면 반복도 끝난다(무인 주행이 이어지지 않게).
 */
export function useGuideLoop({ robotId, session, loaded, onOpened, notify }: Args) {
  const [run, setRun] = useState<LoopRun | null>(null);
  const checked = useRef<number | null>(null);
  const opening = useRef(false);
  const notified = useRef<LoopRun["ended"]>(null);
  const [recheck, setRecheck] = useState(0);

  const start = useCallback((plan: LoopPlan, firstSessionId: number) => {
    checked.current = null;
    notified.current = null;
    setRun({ plan, round: 1, sessionId: firstSessionId, countdown: null, stopRequested: false, rounds: [], ended: null });
  }, []);

  const requestStop = useCallback(() => {
    setRun((r) => {
      if (!r || r.ended) return r;
      if (r.sessionId === null) return { ...r, countdown: null, ended: { kind: "stopped", reason: `반복을 중지했습니다(${r.round}회 완료).` } };
      return { ...r, stopRequested: true };
    });
  }, []);

  const startNow = useCallback(() => setRun((r) => (r && r.countdown !== null ? { ...r, countdown: 0 } : r)), []);
  const dismiss = useCallback(() => setRun(null), []);

  // 회차 끝 — 폴링한 세션이 없어졌거나 다른 세션이면 끝났다
  const pendingId = run && !run.ended ? run.sessionId : null;
  const round = run?.round ?? 1;
  const ended = loaded && pendingId !== null && session?.id !== pendingId;
  useEffect(() => {
    if (!ended || pendingId === null || checked.current === pendingId) return;
    checked.current = pendingId;
    guideScenarioApi
      .loopCheck(robotId, pendingId, round)
      .then((chk) => {
        if (chk.kind === "running") {
          setTimeout(() => {
            checked.current = null;
            setRecheck((n) => n + 1);
          }, RECHECK_MS);
          return;
        }
        setRun((r) => (r && r.sessionId === pendingId ? afterRound(r, pendingId, chk) : r));
      })
      .catch((e) =>
        setRun((r) =>
          r && r.sessionId === pendingId
            ? { ...r, sessionId: null, ended: { kind: "failed", reason: e instanceof Error ? e.message : "판정 실패" } }
            : r
        )
      );
  }, [ended, pendingId, round, robotId, recheck]);

  // 카운트다운 → 다음 회차
  const countdown = run && !run.ended ? run.countdown : null;
  useEffect(() => {
    if (countdown === null || !run) return;
    if (countdown > 0) {
      const t = setTimeout(() => setRun((r) => (r && r.countdown !== null ? { ...r, countdown: r.countdown - 1 } : r)), 1000);
      return () => clearTimeout(t);
    }
    if (opening.current) return;
    opening.current = true;
    const { plan } = run;
    guideScenarioApi
      .openSession(robotId, plan.scenarioCode, plan.startLocationId)
      .then((res) => {
        const s = res.session;
        if (!s) throw new Error("세션을 열지 못했습니다");
        setRun((r) => (r ? { ...r, round: r.round + 1, sessionId: s.id, countdown: null } : r));
        onOpened(s);
      })
      .catch((e) =>
        setRun((r) =>
          r
            ? { ...r, countdown: null, ended: { kind: "open_failed", reason: e instanceof Error ? e.message : "다음 회차를 열지 못했습니다" } }
            : r
        )
      )
      .finally(() => {
        opening.current = false;
      });
  }, [countdown, run, robotId, onOpened]);

  // 멈추면 한 번 알린다(배터리 부족은 서버가 관제 알림도 남긴다)
  useEffect(() => {
    const e = run?.ended ?? null;
    if (!e || notified.current === e) return;
    notified.current = e;
    notify(`안내 반복 종료 — ${e.reason}`, e.kind === "done" || e.kind === "stopped" ? "success" : "error");
  }, [run?.ended, notify]);

  return { run, start, requestStop, startNow, dismiss };
}
