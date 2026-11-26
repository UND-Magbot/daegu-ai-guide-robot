"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import type { RobotRowData } from "@/app/types";
import { useToast } from "@/app/components/common/Toast";
import CustomSelect from "@/app/components/select/CustomSelect";
import { VOICE_LIMITS } from "@/app/constants/voice";
import videoListStyles from "../../DataManagementTabs.module.css";
import styles from "./voice.module.css";
import { voiceApi, type VoiceIntentRow } from "./voiceApi";
import VoiceScenarioList from "./VoiceScenarioList";
import VoiceScenarioDetail from "./VoiceScenarioDetail";
import VoiceSlotModal from "./VoiceSlotModal";
import VoiceAnnouncementModal from "./VoiceAnnouncementModal";
import VoiceConfirmWordModal from "./VoiceConfirmWordModal";
import VoiceConfigModal from "./VoiceConfigModal";
import VoiceSyncModal from "./VoiceSyncModal";
import VoiceImportModal from "./VoiceImportModal";
import VoiceGuidePanel from "./VoiceGuidePanel";

type Props = {
  robots: RobotRowData[];
  onLoaded?: () => void;
};

/**
 * 데이터관리 › 음성 관리 탭.
 *
 * '저장'과 '로봇 적용'은 다르다 — 저장은 관제 DB, 적용은 로봇 반영이다.
 * 번들이 통째로 로봇에 내려가므로 "이 시나리오만 적용"은 불가능하다.
 * 그래서 [로봇 적용]은 시나리오 상세가 아니라 툴바(로봇 단위)에 있다.
 */
export default function VoiceTab({ robots, onLoaded }: Props) {
  const { showToast } = useToast();

  const [robotId, setRobotId] = useState<number | null>(null);
  const [rows, setRows] = useState<VoiceIntentRow[]>([]);
  const [pendingCount, setPendingCount] = useState(0);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);

  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  // 전체 목록에서도 편집할 수 있어야 하므로 대상 로봇을 함께 들고 다닌다
  const [editing, setEditing] = useState<{ intentId: number | null; robotId: number } | null>(null);
  // 단발성(질문→답변 FAQ)과 안내 시나리오(병원·관공서 다단계)는 다른 화면이다 — 상단 탭으로 나눈다
  const [mode, setMode] = useState<"single" | "guide">("single");

  // 한 번에 하나의 모달만 — 상태를 하나로 두면 둘이 겹쳐 뜨는 게 구조적으로 불가능하다
  const [activeModal, setActiveModal] = useState<
    "slot" | "announce" | "confirmWord" | "dismissWord" | "config" | "import" | "sync" | null
  >(null);

  const robotOptions = useMemo(
    () => robots.map((r) => ({ id: r.id, label: r.no })),
    [robots]
  );
  const selectedRobot = useMemo(
    () => robotOptions.find((o) => o.id === robotId) ?? null,
    [robotOptions, robotId]
  );
  const robotName = selectedRobot?.label ?? String(robotId ?? "");

  // 첫 로봇을 기본 선택한다. '전체'는 두지 않는다 —
  // 여기서 로봇 선택은 조회 필터가 아니라 '작업 대상 선택'이다(목적지·설정·적용이 모두 로봇별).
  useEffect(() => {
    if (robotId === null && robots.length > 0) setRobotId(robots[0].id);
  }, [robots, robotId]);

  const load = useCallback(async () => {
    if (robotId === null) return;
    setLoading(true);
    try {
      const res = await voiceApi.listIntents(robotId, search.trim() || undefined);
      setRows(res.items);
      setPendingCount(res.pendingCount);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "목록을 불러오지 못했습니다", "error");
    } finally {
      setLoading(false);
      onLoaded?.();
    }
  }, [robotId, search, showToast, onLoaded]);

  // 검색은 디바운스한다 — 글자마다 서버를 때리지 않는다
  useEffect(() => {
    const t = setTimeout(load, 300);
    return () => clearTimeout(t);
  }, [load]);

  useEffect(() => {
    setSelectedIds([]);
    setEditing(null);
  }, [robotId]);

  const handleBulkDelete = async () => {
    if (selectedIds.length === 0) return;
    try {
      await voiceApi.bulkDelete(selectedIds);
      showToast(`${selectedIds.length}건을 삭제했습니다`, "success");
      setSelectedIds([]);
      load();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "삭제에 실패했습니다", "error");
    }
  };

  if (robots.length === 0 || robotId === null) {
    return (
      <div className={videoListStyles.DT}>
        <div className={styles.emptyState}>등록된 로봇이 없습니다.</div>
      </div>
    );
  }

  // 단발성 상세는 목록을 '대체'한다(모달이 아니다) — 질문 20 + 답변 20이면 모달로는 안 들어간다
  if (editing && mode === "single") {
    return (
      <div className={videoListStyles.DT}>
        <VoiceScenarioDetail
          robotId={editing.robotId}
          intentId={editing.intentId}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            load();
          }}
        />
      </div>
    );
  }

  return (
    <div className={videoListStyles.DT}>
      <div className={styles.topBar}>
        <h2>음성 관리</h2>

        <div className={styles.modeTabs} role="tablist" aria-label="시나리오 종류">
          <button
            role="tab"
            aria-selected={mode === "single"}
            className={`${styles.modeTab} ${mode === "single" ? styles.modeTabActive : ""}`}
            onClick={() => setMode("single")}
          >
            단발성 시나리오
          </button>
          <button
            role="tab"
            aria-selected={mode === "guide"}
            className={`${styles.modeTab} ${mode === "guide" ? styles.modeTabActive : ""}`}
            onClick={() => setMode("guide")}
          >
            안내 시나리오
          </button>
        </div>

        <div className={styles.filterBar}>
          <CustomSelect
            options={robotOptions}
            value={selectedRobot}
            onChange={(o) => setRobotId(Number(o.id))}
            placeholder="로봇 선택"
            width={190}
            className={styles.robotSelect}
          />
          {/* 두 방식 모두에 쓰인다 — 목적지 사전은 단발·안내 번들 모두에, 음성 설정은 두 번들의 인식·발화 값에 들어간다 */}
          <span className={styles.toolGroupLabel}>공통</span>
          <button
            className={styles.workBtn}
            onClick={() => setActiveModal("slot")}
            title="단발성·안내 시나리오 모두가 쓰는 목적지 이름과 장소 연결"
          >
            목적지 사전
          </button>
          <button
            className={styles.workBtn}
            onClick={() => setActiveModal("config")}
            title="단발성·안내 시나리오 모두에 적용되는 인식·발화 설정"
          >
            음성 설정
          </button>
        </div>
      </div>

      {mode === "guide" ? (
        <VoiceGuidePanel key={robotId} robotId={robotId} robotName={robotName} />
      ) : (
        <>
          <div className={styles.subBar}>
            {/* 경로 작업 경유지 도착 때만 쓰인다 — 안내 시나리오는 단계 편집기의 멘트·대답을 쓴다 */}
            <span className={styles.toolGroupLabel}>경로 경유지용</span>
            <button
              className={styles.workBtn}
              onClick={() => setActiveModal("announce")}
              title="경로 작업의 경유지 도착 때 말할 문구 — 안내 시나리오에는 쓰이지 않습니다"
            >
              안내 멘트
            </button>
            <button
              className={styles.workBtn}
              onClick={() => setActiveModal("confirmWord")}
              title="경유지 도착 확인을 통과로 볼 말 — 안내 시나리오에는 쓰이지 않습니다"
            >
              도착 인식어
            </button>
            <button
              className={styles.workBtn}
              onClick={() => setActiveModal("dismissWord")}
              title="경유지 안내를 마칠 말 — 안내 시나리오에는 쓰이지 않습니다"
            >
              종료 인식어
            </button>

            <div className={styles.grow} />

            <span className={styles.toolGroupLabel}>단발성 전용</span>
            <input
              className={styles.searchInput}
              placeholder="시나리오명 검색"
              maxLength={VOICE_LIMITS.search}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <button
              className={styles.workBtn}
              onClick={() => setActiveModal("import")}
              title="단발성 시나리오(질문→답변)를 파일로 한꺼번에 등록"
            >
              일괄 등록
            </button>
            <button
              className={styles.workBtn}
              onClick={() => setEditing({ intentId: null, robotId })}
              title="단발성 시나리오(질문→답변) 추가"
            >
              시나리오 추가
            </button>
            {/* 미적용 변경이 있으면 배지 — 저장했는데 로봇이 옛날 말을 하는 걸 막는다 */}
            <button
              className={styles.primaryBtn}
              onClick={() => setActiveModal("sync")}
              title="단발성 시나리오 묶음을 로봇에 내려보냄 — 안내 시나리오는 안내 시작 때마다 로봇이 새로 받아 가므로 적용이 필요 없습니다"
            >
              로봇 적용
              {pendingCount > 0 && <span className={styles.pendingDot} />}
            </button>
          </div>

          {/* 선택했을 때만 나타난다 — 못 누르는 버튼이 툴바 자리를 차지하지 않는다 */}
          {selectedIds.length > 0 && (
            <div className={styles.selBar}>
              <span>
                <b>{selectedIds.length}건</b> 선택됨
              </span>
              <button className={`${styles.workBtn} ${styles.danger}`} onClick={handleBulkDelete}>
                선택 삭제
              </button>
              <button className={`${styles.workBtn} ${styles.ghost}`} onClick={() => setSelectedIds([])}>
                선택 해제
              </button>
            </div>
          )}

          <VoiceScenarioList
            rows={rows}
            loading={loading}
            selectedIds={selectedIds}
            onToggleSelect={(id) =>
              setSelectedIds((prev) =>
                prev.includes(id) ? prev.filter((v) => v !== id) : [...prev, id]
              )
            }
            onToggleSelectAll={(checked) =>
              setSelectedIds(checked ? rows.map((r) => r.id) : [])
            }
            onEdit={(id, rid) => setEditing({ intentId: id, robotId: rid })}
          />
        </>
      )}

      {activeModal === "slot" && (
        <VoiceSlotModal robotId={robotId} onClose={() => setActiveModal(null)} onChanged={load} />
      )}
      {activeModal === "announce" && (
        <VoiceAnnouncementModal
          robotId={robotId}
          robotName={robotName}
          onClose={() => setActiveModal(null)}
          onChanged={load}
        />
      )}
      {activeModal === "confirmWord" && (
        <VoiceConfirmWordModal robotId={robotId} kind="arrival" onClose={() => setActiveModal(null)} />
      )}
      {activeModal === "dismissWord" && (
        <VoiceConfirmWordModal robotId={robotId} kind="dismiss" onClose={() => setActiveModal(null)} />
      )}
      {activeModal === "config" && (
        <VoiceConfigModal
          robotId={robotId}
          robotName={robotName}
          onClose={() => setActiveModal(null)}
          onModuleChanged={load}
        />
      )}
      {activeModal === "import" && (
        <VoiceImportModal
          robotId={robotId}
          robotName={robotName}
          onClose={() => setActiveModal(null)}
          onImported={load}
        />
      )}
      {activeModal === "sync" && (
        <VoiceSyncModal
          robotId={robotId}
          onClose={() => setActiveModal(null)}
          onApplied={load}
        />
      )}
    </div>
  );
}
