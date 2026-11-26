"use client";

import styles from "@/app/components/modal/InitPoseConfirmModal.module.css";

type Props = {
  robotName?: string;
  /** 알림 당시 배터리(%) */
  battery?: number | null;
  /** 멈춘 곳 — 지도 좌표 "(1.2, 3.4)" 또는 "알 수 없음" */
  stoppedAt?: string | null;
  /** 멈춘 곳에서 가장 가까운 장소 "내과에서 1.2m" */
  nearPlace?: string | null;
  /** 가던 목적지. 목적지를 정하기 전에 멈췄으면 없다 */
  destination?: string | null;
  detectedAt?: string;
  /** '확인' — 알림을 읽음 처리하고 닫는다 */
  onConfirm: () => void;
  /** '나중에' — 모달만 닫는다(알림은 알림센터에 남는다) */
  onClose: () => void;
  zIndex?: number;
};

function formatTime(value?: string): string {
  if (!value) return "";
  const d = new Date(value);
  if (isNaN(d.getTime())) return value;
  return d.toLocaleString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

/**
 * 배터리 부족 안내 중단(guide_battery_aborted) 알림창 — 동행 이탈 확인창(GuideFollowLostModal)과 같은 모양.
 * 로봇은 이미 사과 멘트를 하고 충전소로 가는 중이라 고를 것이 없다. 방문객이 남은 곳을 직원에게 알리는 창이다.
 */
export default function GuideBatteryAbortModal({
  robotName,
  battery,
  stoppedAt,
  nearPlace,
  destination,
  detectedAt,
  onConfirm,
  onClose,
  zIndex,
}: Props) {
  return (
    <div
      className={styles.overlay}
      style={zIndex !== undefined ? { zIndex } : undefined}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="guide-battery-abort-title"
    >
      <div className={styles.box} onClick={(e) => e.stopPropagation()}>
        <button className={styles.closeBtn} onClick={onClose} aria-label="닫기">
          <img src="/icon/close_btn.png" alt="" />
        </button>

        <div className={styles.iconWrap} aria-hidden>
          <svg width="34" height="34" viewBox="0 0 24 24" fill="none">
            <rect x="3" y="7" width="16" height="10" rx="2" stroke="currentColor" strokeWidth="1.8" />
            <path d="M21 10v4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            <path d="M6 10v4" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" />
          </svg>
        </div>

        <h2 id="guide-battery-abort-title" className={styles.title}>
          배터리 부족 — 안내 중단
        </h2>
        <p className={styles.subtitle}>
          배터리가 최소 기준 아래로 떨어져 로봇이 안내를 멈추고 충전소로 가고 있습니다.
          <br />
          방문객이 <b>멈춘 곳</b>에 남아 있을 수 있으니 직원이 확인해 주세요.
        </p>

        <dl className={styles.meta}>
          {robotName && (
            <div className={styles.metaRow}>
              <dt>로봇</dt>
              <dd>{robotName}</dd>
            </div>
          )}
          {battery != null && (
            <div className={styles.metaRow}>
              <dt>배터리</dt>
              <dd>{battery}%</dd>
            </div>
          )}
          <div className={styles.metaRow}>
            <dt>멈춘 곳</dt>
            <dd>
              {nearPlace && <b>{nearPlace}</b>}
              {nearPlace && stoppedAt && <br />}
              {stoppedAt ? `지도 좌표 ${stoppedAt}` : !nearPlace && "알 수 없음"}
            </dd>
          </div>
          <div className={styles.metaRow}>
            <dt>못 간 목적지</dt>
            <dd>{destination || "목적지를 정하기 전"}</dd>
          </div>
          {detectedAt && (
            <div className={styles.metaRow}>
              <dt>발생 시각</dt>
              <dd>{formatTime(detectedAt)}</dd>
            </div>
          )}
        </dl>

        <div className={styles.actions}>
          <button type="button" className={`${styles.btn} ${styles.btnPrimary}`} onClick={onConfirm} autoFocus>
            확인
          </button>
          <div className={styles.actionsRow}>
            <button type="button" className={`${styles.btn} ${styles.btnGhost}`} onClick={onClose}>
              나중에
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
