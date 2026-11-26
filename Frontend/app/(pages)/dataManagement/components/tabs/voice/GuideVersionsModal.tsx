"use client";

import React, { useEffect, useState } from "react";
import { useToast } from "@/app/components/common/Toast";
import styles from "./voice.module.css";
import e from "./scenarioEditor.module.css";
import { guideScenarioApi, type ScenarioVersion } from "./guideScenarioApi";

type Props = {
  scenarioId: number;
  scenarioName: string;
  currentVersion: number;
  /** 저장하지 않은 변경이 있으면 되돌리기 전에 알린다 — 되돌리면 그 변경은 버려진다 */
  dirty: boolean;
  onClose: () => void;
  onRestored: () => void;
};

const when = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" });
};

/** 버전 기록 — 저장할 때마다 하나씩 쌓인다(최근 50개). 되돌리기도 새 버전으로 남는다. */
export default function GuideVersionsModal({ scenarioId, scenarioName, currentVersion, dirty, onClose, onRestored }: Props) {
  const { showToast } = useToast();
  const [items, setItems] = useState<ScenarioVersion[] | null>(null);
  const [confirm, setConfirm] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    guideScenarioApi
      .versions(scenarioId)
      .then((r) => setItems(r.items))
      .catch((err) => {
        showToast(err instanceof Error ? err.message : "버전 기록을 불러오지 못했습니다", "error");
        setItems([]);
      });
  }, [scenarioId, showToast]);

  const restore = async (version: number) => {
    setBusy(true);
    try {
      const r = await guideScenarioApi.restoreVersion(scenarioId, version, currentVersion);
      showToast(`버전 ${version}(으)로 되돌렸습니다 — 지금 버전 ${r.version}`, "success");
      onRestored();
    } catch (err) {
      showToast(err instanceof Error ? err.message : "되돌리지 못했습니다", "error");
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  return (
    <div className={styles.confirmOverlay}>
      <div role="dialog" aria-modal="true" aria-labelledby="versions-title" className={e.modalBox}>
        <h2 id="versions-title" className={e.modalTitle}>{scenarioName} — 버전 기록</h2>
        <div className={e.hintText}>저장할 때마다 버전이 쌓입니다. 되돌리면 그 버전의 단계로 바뀌고, 되돌린 것도 새 버전으로 남습니다.</div>
        {items === null ? (
          <div className={styles.emptyState}>불러오는 중...</div>
        ) : items.length === 0 ? (
          <div className={styles.emptyState}>아직 편집기로 저장한 기록이 없습니다.</div>
        ) : (
          <div className={e.versionList}>
            {items.map((v) => (
              <div key={v.version} className={e.versionRow}>
                <b>v{v.version}</b>
                <div>
                  <div>{v.note || "저장"}</div>
                  <div className={e.versionMeta}>
                    {when(v.createdAt)} · {v.createdBy ?? "알 수 없음"} · 단계 {v.stepCount}
                  </div>
                </div>
                {v.version === currentVersion ? (
                  <span className={e.versionMeta}>지금</span>
                ) : (
                  <button type="button" className={`${styles.btn} ${styles.btnSecondary}`} onClick={() => setConfirm(v.version)} disabled={busy}>
                    되돌리기
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
        {confirm !== null && (
          <div className={`${styles.inline} ${styles.inlineWarn}`}>
            버전 {confirm}(으)로 되돌릴까요? {dirty && <b>저장하지 않은 변경은 버려집니다. </b>}지금 운영 맵 기준으로 다시 검사합니다.
            <div className={styles.confirmBtns} style={{ marginTop: 8 }}>
              <button type="button" className={`${styles.btn} ${styles.btnSecondary}`} onClick={() => setConfirm(null)}>
                취소
              </button>
              <button type="button" className={`${styles.btn} ${styles.btnDanger}`} onClick={() => restore(confirm)} disabled={busy}>
                되돌리기
              </button>
            </div>
          </div>
        )}
        <div className={styles.confirmBtns}>
          <button type="button" className={`${styles.btn} ${styles.btnSecondary}`} onClick={onClose}>
            닫기
          </button>
        </div>
      </div>
    </div>
  );
}
