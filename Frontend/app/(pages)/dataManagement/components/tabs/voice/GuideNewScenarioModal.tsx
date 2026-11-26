"use client";

import React, { useMemo, useState } from "react";
import CustomSelect from "@/app/components/select/CustomSelect";
import styles from "./voice.module.css";
import e from "./scenarioEditor.module.css";
import type { GuideFloor, GuideMap } from "./guideScenarioApi";

type Props = {
  robotName: string;
  floors: GuideFloor[];
  maps: GuideMap[];
  /** 로봇의 현재 층·맵 — 처음에 골라 둔다 */
  robotFloorId: number | null;
  robotMapId: number | null;
  busy: boolean;
  onCancel: () => void;
  onCreate: (displayName: string, mapId: number) => void;
};

/**
 * [+ 새 시나리오] — 빈 시나리오(인사 → 종료)만 만든다. 코드는 서버가 붙이고 '사용' 은 꺼진 채로 시작한다.
 * 운영 맵은 로봇 사업장의 맵에서만 고른다(목록은 서버가 사업장 기준으로 준다).
 */
export default function GuideNewScenarioModal({
  robotName,
  floors,
  maps,
  robotFloorId,
  robotMapId,
  busy,
  onCancel,
  onCreate,
}: Props) {
  const firstFloor = robotFloorId && maps.some((m) => m.floorId === robotFloorId) ? robotFloorId : maps[0]?.floorId ?? null;
  const [name, setName] = useState("");
  const [floorId, setFloorId] = useState<number | null>(firstFloor);
  const [mapId, setMapId] = useState<number | null>(
    robotMapId && maps.some((m) => m.id === robotMapId) ? robotMapId : maps.find((m) => m.floorId === firstFloor)?.id ?? null
  );

  const floorOptions = useMemo(
    () => floors.filter((f) => maps.some((m) => m.floorId === f.id)).map((f) => ({ id: f.id, label: f.name })),
    [floors, maps]
  );
  const mapOptions = useMemo(
    () =>
      maps
        .filter((m) => m.floorId === floorId)
        .map((m) => ({ id: m.id, label: m.id === robotMapId ? `${m.name} (로봇 현재 맵)` : m.name })),
    [maps, floorId, robotMapId]
  );

  const pickFloor = (id: number) => {
    setFloorId(id);
    const here = maps.filter((m) => m.floorId === id);
    setMapId(here.some((m) => m.id === robotMapId) ? robotMapId : here[0]?.id ?? null);
  };

  const canCreate = !!name.trim() && !!mapId && !busy;

  return (
    <div className={styles.confirmOverlay}>
      <div role="dialog" aria-modal="true" aria-labelledby="new-scenario-title" className={e.modalBox}>
        <h2 id="new-scenario-title" className={e.modalTitle}>새 안내 시나리오</h2>
        <div className={e.hintText}>인사 → 종료 두 단계로 시작합니다. 단계는 만든 뒤 편집 화면에서 추가합니다.</div>

        <div className={e.fieldGrid}>
          <label className={e.fieldLabel} htmlFor="new-scenario-name">이름</label>
          <input
            id="new-scenario-name"
            className={e.input}
            value={name}
            maxLength={50}
            placeholder="예: 약국 안내"
            autoFocus
            onChange={(ev) => setName(ev.target.value)}
          />
          <span className={e.fieldLabel}>운영 층</span>
          <CustomSelect
            options={floorOptions}
            value={floorOptions.find((o) => o.id === floorId) ?? null}
            onChange={(o) => pickFloor(Number(o.id))}
            placeholder="층 선택"
            width={160}
            overlay
          />
          <span className={e.fieldLabel}>운영 맵</span>
          <div>
            <CustomSelect
              options={mapOptions}
              value={mapOptions.find((o) => o.id === mapId) ?? null}
              onChange={(o) => setMapId(Number(o.id))}
              placeholder="맵 선택"
              width={280}
              overlay
            />
            <div className={e.hintText}>{robotName} 사업장의 맵 · 처음에는 로봇의 현재 층·맵이 골라져 있습니다</div>
          </div>
        </div>
        {maps.length === 0 && <div className={e.warnText}>이 사업장에 등록된 맵이 없습니다. 맵 관리에서 먼저 맵을 등록하세요.</div>}
        <div className={e.hintText}>코드는 저장할 때 자동으로 붙습니다. 처음에는 &apos;사용&apos;이 꺼진 상태로 만들어집니다.</div>

        <div className={styles.confirmBtns}>
          <button type="button" className={`${styles.btn} ${styles.btnSecondary}`} onClick={onCancel}>
            취소
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnPrimary}`}
            disabled={!canCreate}
            onClick={() => mapId && onCreate(name.trim(), mapId)}
          >
            {busy ? "만드는 중…" : "만들기"}
          </button>
        </div>
      </div>
    </div>
  );
}
