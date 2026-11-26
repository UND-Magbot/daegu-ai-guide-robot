"use client";

import styles from "@/app/components/modal/InitPoseConfirmModal.module.css";

type Props = {
  robotName?: string;
  /** "안내 멘트" | "질문" — 질문이 안 들리면 방문자가 대답할 수 없어 안내가 멈춘다 */
  kind?: string | null;
  /** 말하지 못한 문장 */
  text?: string | null;
  /** 상세를 읽지 못한 예전 알림이면 본문을 그대로 보여 준다 */
  message?: string;
  detectedAt?: string;
  busy?: boolean;
  /** '확인' — 알림을 읽음 처리하고 닫는다(안내는 계속된다) */
  onConfirm: () => void;
  /** '안내 종료' — 세션을 끝낸다. 세션 번호를 모르면 버튼을 숨긴다 */
  onEnd?: () => void;
  /** '나중에' — 모달만 닫는다(알림은 알림센터에 남는다) */
  onClose: () => void;
  zIndex?: number;
};

function formatTime(value?: string): string {
  if (!value) return "";
  const d = new Date(value.replace(" ", "T"));
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
 * 안내 음성 출력 실패(guide_tts_failed) 알림창 — 상시 대기 시작 보류 알림창(StandbyMapHoldModal)과 같은 모양.
 * 로봇 스피커로 말하지 못했다. 방문자는 안내를 듣지 못하고 있으니 운영자가 현장을 확인해야 한다.
 */
export default function GuideTtsFailedModal({
  robotName,
  kind,
  text,
  message,
  detectedAt,
  busy = false,
  onConfirm,
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
      aria-labelledby="guide-tts-failed-title"
    >
      <div className={styles.box} onClick={(e) => e.stopPropagation()}>
        <button className={styles.closeBtn} onClick={onClose} aria-label="닫기">
          <img src="/icon/close_btn.png" alt="" />
        </button>

        <div className={styles.iconWrap} aria-hidden>
          <svg width="34" height="34" viewBox="0 0 24 24" fill="none">
            <path d="M4 9v6h4l5 4V5L8 9H4Z" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
            <path d="M17 9l4 6M21 9l-4 6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
        </div>

        <h2 id="guide-tts-failed-title" className={styles.title}>
          안내 음성 출력 실패
        </h2>
        <p className={styles.subtitle}>
          로봇이 {kind === "질문" ? "질문을" : "안내 멘트를"} 말하지 못했습니다.
          <br />
          방문자가 안내를 듣지 못하고 있을 수 있습니다 — <b>로봇 스피커와 현장을 확인</b>해 주세요.
        </p>

        <dl className={styles.meta}>
          {robotName && (
            <div className={styles.metaRow}>
              <dt>로봇</dt>
              <dd>{robotName}</dd>
            </div>
          )}
          {text ? (
            <div className={styles.metaRow}>
              <dt>못 한 말</dt>
              <dd>{text}</dd>
            </div>
          ) : (
            message && (
              <div className={styles.metaRow}>
                <dt>내용</dt>
                <dd>{message}</dd>
              </div>
            )
          )}
          {detectedAt && (
            <div className={styles.metaRow}>
              <dt>발생 시각</dt>
              <dd>{formatTime(detectedAt)}</dd>
            </div>
          )}
        </dl>

        <div className={styles.actionsRow}>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnPrimary}`}
            style={{ flex: 1 }}
            onClick={onConfirm}
            disabled={busy}
            autoFocus
          >
            확인
          </button>
          {onEnd && (
            <button type="button" className={`${styles.btn} ${styles.btnGhost}`} onClick={onEnd} disabled={busy}>
              안내 종료
            </button>
          )}
          <button type="button" className={`${styles.btn} ${styles.btnGhost}`} onClick={onClose} disabled={busy}>
            나중에
          </button>
        </div>
      </div>
    </div>
  );
}
