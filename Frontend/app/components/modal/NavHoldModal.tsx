"use client";

import styles from "@/app/components/modal/InitPoseConfirmModal.module.css";
import {
  actionLabel, autoActionLabel, fmtMinSec, frontLabel, holdTitle, retryBlockedReason,
  type NavHold, type NavHoldAction,
} from "@/app/lib/navHold";

type Props = {
  robotName?: string;
  hold: NavHold;
  busy?: boolean;
  onAction: (action: NavHoldAction) => void;
  /** '나중에' — 모달만 닫는다(알림은 알림센터에 남고, 로봇 상세에서 고를 수 있다) */
  onClose: () => void;
  zIndex?: number;
};

/** 이동 멈춤 확인창 — 위치 재조정·동행 이탈 확인창과 같은 모양. 버튼은 이동 종류마다 다르다 */
export default function NavHoldModal({ robotName, hold, busy, onAction, onClose, zIndex }: Props) {
  const auto = autoActionLabel(hold.kind);
  const [primary, ...rest] = hold.actions;
  const where = hold.pos ? `(${hold.pos[0].toFixed(1)}, ${hold.pos[1].toFixed(1)})` : "알 수 없음";
  const front = frontLabel(hold);
  const blocked = retryBlockedReason(hold);
  const disabledFor = (a: NavHoldAction) => busy || (a === "retry" && blocked !== null);
  const lead =
    hold.kind === "guide"
      ? `방문객을 '${hold.wpName}'(으)로 안내하던 중 움직이지 못하고 있습니다.`
      : `로봇이 '${hold.wpName}'(으)로 가다 움직이지 못해 멈췄습니다.`;

  return (
    <div
      className={styles.overlay}
      style={zIndex !== undefined ? { zIndex } : undefined}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="nav-hold-title"
    >
      <div className={styles.box} onClick={(e) => e.stopPropagation()}>
        <button className={styles.closeBtn} onClick={onClose} aria-label="닫기">
          <img src="/icon/close_btn.png" alt="" />
        </button>

        <div className={styles.iconWrap} aria-hidden>
          <svg width="32" height="32" viewBox="0 0 24 24" fill="none">
            <path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z" stroke="currentColor" strokeWidth="1.8" />
            <path d="M12 7v4M12 13.5v.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        </div>

        <h2 id="nav-hold-title" className={styles.title}>
          {holdTitle(hold.kind)}
        </h2>
        <p className={styles.subtitle}>
          {lead}
          <br />
          {front
            ? "앞에 장애물이 있어 직원을 불렀습니다. 로봇은 그 자리에서 기다립니다 — 앞이 비면 '다시 출발', 끝내려면 '안내 종료'."
            : "현장을 확인해 주세요. 막힘이 풀리면 로봇이 이어서 갑니다."}
        </p>

        <dl className={styles.meta}>
          {robotName && (
            <div className={styles.metaRow}>
              <dt>로봇</dt>
              <dd>{robotName}</dd>
            </div>
          )}
          <div className={styles.metaRow}>
            <dt>멈춘 곳</dt>
            <dd>{where}</dd>
          </div>
          {front && (
            <div className={styles.metaRow}>
              <dt>지금 앞</dt>
              <dd>{front}</dd>
            </div>
          )}
          <div className={styles.metaRow}>
            <dt>멈춘 지</dt>
            <dd>{fmtMinSec(hold.heldSec)}</dd>
          </div>
          {auto && hold.autoActionInSec !== null && (
            <div className={styles.metaRow}>
              <dt>자동 처리</dt>
              <dd>
                {fmtMinSec(hold.autoActionInSec)} 뒤 {auto}
              </dd>
            </div>
          )}
        </dl>

        <div className={styles.actions}>
          {primary && (
            <button
              type="button"
              className={`${styles.btn} ${styles.btnPrimary}`}
              onClick={() => onAction(primary)}
              disabled={disabledFor(primary)}
              title={primary === "retry" && blocked ? blocked : undefined}
              autoFocus
            >
              {actionLabel(hold.kind, primary)}
            </button>
          )}
          {blocked && hold.actions.includes("retry") && <p className={styles.subtitle}>{blocked}</p>}
          <div className={styles.actionsRow}>
            {rest.map((a) => (
              <button
                key={a}
                type="button"
                className={`${styles.btn} ${a === "abort" ? styles.btnGhost : styles.btnSecondary}`}
                onClick={() => onAction(a)}
                disabled={disabledFor(a)}
              >
                {actionLabel(hold.kind, a)}
              </button>
            ))}
            <button type="button" className={`${styles.btn} ${styles.btnGhost}`} onClick={onClose} disabled={busy}>
              나중에
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
