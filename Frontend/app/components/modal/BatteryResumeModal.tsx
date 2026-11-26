"use client";

import styles from "@/app/components/modal/InitPoseConfirmModal.module.css";

/** ask: 충전소로 갈 때 미리 묻기 / confirm: 충전 중 도크에서 나갔다 다시 충전해 80% 가 됐을 때 */
export type BatteryResumeStage = "ask" | "confirm";

type Props = {
  stage: BatteryResumeStage;
  robotName?: string;
  /** 알림 본문 — "배터리 부족으로 충전소로 갑니다 — 충전 후 경로 반복 'A → B'을(를) 이어 갈까요? …" */
  message?: string;
  detectedAt?: string;
  busy?: boolean;
  /** ask: '충전 후 재개'(기본·무응답과 같다) / confirm: '이어 가기'(바로 출발) */
  onResume: () => void;
  /** '오늘은 여기서 종료' — 근무는 끝내고, 경로 반복·예약 작업은 오늘 남은 일을 하지 않는다 */
  onEnd: () => void;
  /** '나중에' — 모달만 닫는다(알림은 알림센터에 남는다) */
  onClose: () => void;
  zIndex?: number;
};

const TEXT: Record<BatteryResumeStage, { title: string; lead: string; resume: string; end: string }> = {
  ask: {
    title: "배터리 부족 — 충전하러 갑니다",
    lead: "충전이 80%가 되면 하던 일을 이어 갑니다. 응답하지 않아도 이어 갑니다.",
    resume: "충전 후 재개",
    end: "오늘은 여기서 종료",
  },
  confirm: {
    title: "충전 완료 — 이어 갈까요?",
    lead: "충전 중에 로봇이 도크에서 나간 적이 있어 바로 출발하지 않고 기다립니다.",
    resume: "이어 가기",
    end: "그만하기",
  },
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
 * 배터리 부족 충전 후 이어 가기 확인창 — 안내 동행 이탈 확인창(GuideFollowLostModal)과 같은 모양.
 */
export default function BatteryResumeModal({
  stage,
  robotName,
  message,
  detectedAt,
  busy,
  onResume,
  onEnd,
  onClose,
  zIndex,
}: Props) {
  const text = TEXT[stage];
  return (
    <div
      className={styles.overlay}
      style={zIndex !== undefined ? { zIndex } : undefined}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="battery-resume-title"
    >
      <div className={styles.box} onClick={(e) => e.stopPropagation()}>
        <button className={styles.closeBtn} onClick={onClose} aria-label="닫기">
          <img src="/icon/close_btn.png" alt="" />
        </button>

        <div className={styles.iconWrap} aria-hidden>
          <svg width="34" height="34" viewBox="0 0 24 24" fill="none">
            <rect x="3" y="7" width="16" height="10" rx="2" stroke="currentColor" strokeWidth="1.8" />
            <path d="M21 10.5v3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            <path d="M6 10v4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </div>

        <h2 id="battery-resume-title" className={styles.title}>
          {text.title}
        </h2>
        <p className={styles.subtitle}>{text.lead}</p>

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
            {text.resume}
          </button>
          <div className={styles.actionsRow}>
            <button
              type="button"
              className={`${styles.btn} ${styles.btnGhost}`}
              onClick={onEnd}
              disabled={busy}
            >
              {text.end}
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
