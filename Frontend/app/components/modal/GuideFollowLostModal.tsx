"use client";

import styles from "@/app/components/modal/InitPoseConfirmModal.module.css";

type Props = {
  robotName?: string;
  /** 알림 본문 — "R296 안내 중 방문자가 따라오지 않습니다 (안과 가는 길)" */
  message?: string;
  detectedAt?: string;
  busy?: boolean;
  /** '안내 재개' — 로봇이 같은 목적지로 다시 출발한다 */
  onResume: () => void;
  /** '안내 종료' — 세션을 닫는다. 로봇은 그 자리에 선다 */
  onEnd: () => void;
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
 * 안내 동행 이탈(guide_follow_lost) 확인창 — 위치 재조정 확인창(InitPoseConfirmModal)과 같은 모양.
 * 로봇이 "잘 따라오고 계신가요?" 에 답이 없어 멈춰 기다리다 운영자를 호출했을 때 뜬다.
 */
export default function GuideFollowLostModal({
  robotName,
  message,
  detectedAt,
  busy,
  onResume,
  onEnd,
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
      aria-labelledby="guide-follow-lost-title"
    >
      <div className={styles.box} onClick={(e) => e.stopPropagation()}>
        <button className={styles.closeBtn} onClick={onClose} aria-label="닫기">
          <img src="/icon/close_btn.png" alt="" />
        </button>

        <div className={styles.iconWrap} aria-hidden>
          <svg width="34" height="34" viewBox="0 0 24 24" fill="none">
            <circle cx="9" cy="6" r="2.4" stroke="currentColor" strokeWidth="1.8" />
            <path d="M5 21v-5l2-5h4l2 5v5" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
            <path d="M17 8v5M17 16v.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </div>

        <h2 id="guide-follow-lost-title" className={styles.title}>
          안내 동행 이탈
        </h2>
        <p className={styles.subtitle}>
          방문자가 따라오지 않아 로봇이 멈춰 기다리고 있습니다.
          <br />
          현장을 확인한 뒤 <b>안내 재개</b> 또는 <b>안내 종료</b>를 선택하세요.
        </p>

        <dl className={styles.meta}>
          {robotName && (
            <div className={styles.metaRow}>
              <dt>로봇</dt>
              <dd>{robotName}</dd>
            </div>
          )}
          {message && (
            <div className={styles.metaRow}>
              <dt>내용</dt>
              <dd>{message}</dd>
            </div>
          )}
          {detectedAt && (
            <div className={styles.metaRow}>
              <dt>발생 시각</dt>
              <dd>{formatTime(detectedAt)}</dd>
            </div>
          )}
        </dl>

        <div className={styles.actions}>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnPrimary}`}
            onClick={onResume}
            disabled={busy}
            autoFocus
          >
            안내 재개
          </button>
          <div className={styles.actionsRow}>
            <button
              type="button"
              className={`${styles.btn} ${styles.btnGhost}`}
              onClick={onEnd}
              disabled={busy}
            >
              안내 종료
            </button>
            <button
              type="button"
              className={`${styles.btn} ${styles.btnGhost}`}
              onClick={onClose}
              disabled={busy}
            >
              나중에
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
