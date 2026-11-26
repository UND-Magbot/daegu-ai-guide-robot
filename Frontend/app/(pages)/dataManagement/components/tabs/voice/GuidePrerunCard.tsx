"use client";

import React from "react";
import styles from "./voice.module.css";
import g from "./guideScenario.module.css";
import type { GuidePrerun } from "./guideScenarioApi";
import { GUIDE_SPEED_SHORT } from "./guideScenarioLabels";

type Props = {
  prerun: GuidePrerun;
  busy: boolean;
  onStop: () => void;
  /** fromFailed — 멈춘 구간부터 · false — 처음부터 */
  onResume: (fromFailed: boolean) => void;
  onStartGuide: () => void;
  onDismiss: () => void;
};

const BADGE: Record<GuidePrerun["status"], string> = {
  running: "사전 주행 중",
  loc_wait: "측위 회복 대기",
  failed: "사전 주행 멈춤",
  stopped: "사전 주행 중지",
  done: "사전 주행 완료",
};

const STOP_CLASS = {
  done: g.prerunStop_done,
  current: g.prerunStop_current,
  wait: g.prerunStop_wait,
  error: g.prerunStop_error,
  pending: "",
} as const;

function elapsed(sec: number): string {
  const m = Math.floor(sec / 60);
  return m > 0 ? `${m}분 ${sec % 60}초` : `${sec}초`;
}

/**
 * 안내 사전 주행 진행 카드 — 안내 세션 카드 자리에 대신 보인다.
 * 끝나도 안내를 저절로 시작하지 않는다. 운영자가 '안내 시작'을 눌러야 한다.
 */
export default function GuidePrerunCard({ prerun, busy, onStop, onResume, onStartGuide, onDismiss }: Props) {
  const { status, legs, total, doneLegs } = prerun;
  // 장소 단위로 센다 — 출발 장소(호출 위치)에 이미 서 있으면 그곳은 처음부터 지난 곳이다
  const segs = Math.max(total, 1);
  const segDone = Math.min(doneLegs, segs);
  const cur = legs[prerun.leg];
  const halted = status === "failed" || status === "stopped";
  const badgeClass =
    status === "done" ? styles.badgeOn : status === "loc_wait" ? g.badgeWait : halted ? g.badgeFail : g.badgeRun;
  const cardClass = status === "loc_wait" || status === "done" ? g.sessionWaiting : halted ? g.prerunFailed : "";

  return (
    <div className={`${g.sessionCard} ${g.prerunCard} ${cardClass}`}>
      <div className={g.sessionMain}>
        <span className={`${styles.badge} ${badgeClass}`}>
          {status === "loc_wait" && prerun.locWaitLeft !== null
            ? `측위 회복 대기 ${prerun.locWaitLeft}초`
            : BADGE[status]}
        </span>
        <div className={g.sessionInfo}>
          <div className={g.sessionTitle}>
            {prerun.scenarioName} · 경로 점검
            {status === "done" && ` — ${segs}/${segs}곳 정상`}
            <span className={g.testBadge} title="멘트 없이 돕니다. 응대 통계에 들어가지 않습니다">
              {GUIDE_SPEED_SHORT[prerun.speed ?? "fast"]} · 멘트 없음
            </span>
          </div>
          <div className={g.sessionMeta}>
            {status === "running" && cur && (
              <>
                <span>
                  {Math.min(segDone + 1, segs)}/{segs}번째 장소로 이동 중
                </span>
                <span>이동 목표 {cur.name}</span>
              </>
            )}
            {status !== "running" && status !== "done" && cur && (
              <span>
                멈춘 곳: {cur.name}(으)로 가던 중 · 완료 {segDone}/{segs}곳
              </span>
            )}
            <span>{status === "done" ? "소요" : "경과"} {elapsed(prerun.elapsedSec)}</span>
            <span>측위 상실 {prerun.locLost}회</span>
          </div>
          {status === "loc_wait" && (
            <span className={g.mapWarn}>
              위치를 잃어 멈춰 기다립니다. 위치가 돌아오면 이 장소부터 이어서 주행합니다. 돌아오지 않으면 위쪽
              &apos;로봇 위치 미확정&apos; 줄의 위치 재조정으로 위치를 맞추세요.
            </span>
          )}
          {halted && prerun.reason && <span className={g.prerunReason}>{prerun.reason}</span>}
          {status === "failed" && (
            <span className={styles.hint}>막힌 곳을 치우거나 위치를 다시 맞춘 뒤 이어서 주행하세요.</span>
          )}
          {status === "done" && (
            <span className={styles.hint}>
              &apos;안내 시작&apos;을 누르면 시나리오 설정 속도로 안내를 시작합니다.
            </span>
          )}
        </div>
      </div>

      <div className={g.sessionActions}>
        {(status === "running" || status === "loc_wait") && (
          <button className={`${styles.workBtn} ${styles.danger}`} onClick={onStop} disabled={busy}>
            사전 주행 중지
          </button>
        )}
        {halted && (
          <>
            <button className={styles.primaryBtn} onClick={() => onResume(true)} disabled={busy}>
              실패 지점부터 다시
            </button>
            <button className={styles.workBtn} onClick={() => onResume(false)} disabled={busy}>
              처음부터 다시
            </button>
            <button className={styles.workBtn} onClick={onStartGuide} disabled={busy}>
              사전 주행 건너뛰고 안내 시작
            </button>
            <button className={`${styles.workBtn} ${styles.ghost}`} onClick={onDismiss} disabled={busy}>
              취소
            </button>
          </>
        )}
        {status === "done" && (
          <>
            <button className={styles.primaryBtn} onClick={onStartGuide} disabled={busy}>
              안내 시작
            </button>
            <button className={styles.workBtn} onClick={() => onResume(false)} disabled={busy}>
              한 바퀴 더
            </button>
            <button className={`${styles.workBtn} ${styles.ghost}`} onClick={onDismiss} disabled={busy}>
              닫기
            </button>
          </>
        )}
      </div>

      {status !== "done" && (
        <div className={g.prerunBar} aria-hidden="true">
          <i style={{ width: `${Math.round((segDone / segs) * 100)}%` }} />
        </div>
      )}
      <div className={g.prerunRoute} aria-label="사전 주행 경로">
        {legs.map((lg, i) => (
          <React.Fragment key={`${i}-${lg.id}`}>
            {i > 0 && <span className={g.prerunArrow}>→</span>}
            <span className={`${g.prerunStop} ${STOP_CLASS[lg.status]}`}>
              {lg.status === "done" && "✓ "}
              {lg.name}
              {lg.role === "destination" && <small>목적지</small>}
            </span>
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}
