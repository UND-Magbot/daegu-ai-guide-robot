"use client";

import styles from "@/app/components/modal/InitPoseConfirmModal.module.css";

type Props = {
  workName?: string | null;
  robotName?: string;
  /** 대기 장소 이름 */
  place?: string | null;
  /** 대기 장소가 있는 층 */
  placeFloor?: string | null;
  /** 로봇이 지금 있는 층 */
  robotFloor?: string | null;
  /** 상세를 읽지 못한 예전 알림이면 본문을 그대로 보여 준다 */
  message?: string;
  detectedAt?: string;
  /** '확인' — 알림을 읽음 처리하고 닫는다 */
  onConfirm: () => void;
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
 * 상시 대기 시작 보류(standby_map_mismatch) 알림창 — 배터리 중단 알림창(GuideBatteryAbortModal)과 같은 모양.
 * 대기 장소가 로봇이 있는 층과 달라 근무를 시작하지 못했다. 사람이 로봇을 옮기면 스스로 시작하니 고를 것이 없다.
 */
export default function StandbyMapHoldModal({
  workName,
  robotName,
  place,
  placeFloor,
  robotFloor,
  message,
  detectedAt,
  onConfirm,
  onClose,
  zIndex,
}: Props) {
  const parsed = !!place;
  return (
    <div
      className={styles.overlay}
      style={zIndex !== undefined ? { zIndex } : undefined}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="standby-map-hold-title"
    >
      <div className={styles.box} onClick={(e) => e.stopPropagation()}>
        <button className={styles.closeBtn} onClick={onClose} aria-label="닫기">
          <img src="/icon/close_btn.png" alt="" />
        </button>

        <div className={styles.iconWrap} aria-hidden>
          <svg width="34" height="34" viewBox="0 0 24 24" fill="none">
            <path d="M4 20h16M6 20V9h5v11M13 20V4h5v16" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
            <path d="M8.5 12v2M15.5 8v2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
        </div>

        <h2 id="standby-map-hold-title" className={styles.title}>
          상시 대기 시작 보류
        </h2>
        <p className={styles.subtitle}>
          대기 장소가 로봇이 있는 층과 달라 근무를 시작하지 못했습니다.
          <br />
          로봇을 대기 장소가 있는 층으로 옮기면 <b>자동으로 시작</b>합니다.
        </p>

        <dl className={styles.meta}>
          {workName && (
            <div className={styles.metaRow}>
              <dt>작업</dt>
              <dd>{workName}</dd>
            </div>
          )}
          {robotName && (
            <div className={styles.metaRow}>
              <dt>로봇</dt>
              <dd>{robotName}</dd>
            </div>
          )}
          {parsed ? (
            <>
              <div className={styles.metaRow}>
                <dt>대기 장소</dt>
                <dd>
                  {place}
                  {placeFloor && ` (${placeFloor})`}
                </dd>
              </div>
              <div className={styles.metaRow}>
                <dt>로봇 위치</dt>
                <dd>{robotFloor || "알 수 없음"}</dd>
              </div>
            </>
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
            autoFocus
          >
            확인
          </button>
          <button type="button" className={`${styles.btn} ${styles.btnGhost}`} onClick={onClose}>
            나중에
          </button>
        </div>
      </div>
    </div>
  );
}
