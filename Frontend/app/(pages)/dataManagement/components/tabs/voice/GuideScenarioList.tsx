"use client";

import React from "react";
import styles from "./voice.module.css";
import e from "./scenarioEditor.module.css";
import type { GuideFloor, GuideMap, GuideScenario } from "./guideScenarioApi";

type Props = {
  scenarios: GuideScenario[];
  floors: GuideFloor[];
  maps: GuideMap[];
  onOpen: (code: string) => void;
  onToggle: (sc: GuideScenario) => void;
};

const nameIn = (list: { id: number; name: string }[], id: number | null) => list.find((x) => x.id === id)?.name ?? null;

/** 상태 — 켜짐 / 꺼짐 · 준비됨 / 꺼짐 · 고칠 곳 N */
export function scenarioState(sc: GuideScenario): { label: string; tone: "on" | "off" | "warn" } {
  if (sc.isActive) return sc.checkErrors.length ? { label: `사용 · 고칠 곳 ${sc.checkErrors.length}`, tone: "warn" } : { label: "사용", tone: "on" };
  if (!sc.mapId) return { label: "꺼짐 · 운영 맵 없음", tone: "warn" };
  return sc.ready ? { label: "꺼짐 · 준비됨", tone: "off" } : { label: `꺼짐 · 고칠 곳 ${sc.checkErrors.length}`, tone: "warn" };
}

export default function GuideScenarioList({ scenarios, floors, maps, onOpen, onToggle }: Props) {
  const tone = { on: e.chipOn, off: e.chipOff, warn: e.chipWarn };
  return (
    <>
      <div className={styles.listArea} style={{ overflowY: "auto" }}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th className={styles.thLeft}>시나리오</th>
              <th className={styles.thLeft} style={{ width: 220 }}>층 · 운영 맵</th>
              <th style={{ width: 70 }}>단계</th>
              <th className={styles.thLeft} style={{ width: 200 }}>상시 대기 작업</th>
              <th style={{ width: 150 }}>상태</th>
              <th style={{ width: 70 }}>사용</th>
            </tr>
          </thead>
          <tbody>
            {scenarios.map((sc) => {
              const st = scenarioState(sc);
              return (
                <tr key={sc.code} className={styles.rowClickable} onClick={() => onOpen(sc.code)}>
                  <td className={styles.tdLeft}>
                    <button
                      type="button"
                      className={styles.linkBtn}
                      onClick={(ev) => {
                        ev.stopPropagation();
                        onOpen(sc.code);
                      }}
                    >
                      {sc.displayName}
                    </button>
                    <div className={e.headCode}>{sc.code}</div>
                  </td>
                  <td className={styles.tdLeft}>
                    {sc.mapId
                      ? `${nameIn(floors, sc.floorId) ?? "층 미지정"} · ${nameIn(maps, sc.mapId) ?? `맵 ${sc.mapId}`}`
                      : "운영 맵 없음"}
                  </td>
                  <td>{sc.steps.length}</td>
                  <td className={styles.tdLeft}>
                    {sc.standbyTasks.length
                      ? `${sc.standbyTasks[0].name}${sc.standbyTasks.length > 1 ? ` 외 ${sc.standbyTasks.length - 1}` : ""}`
                      : "—"}
                  </td>
                  <td>
                    <span className={`${e.chip} ${tone[st.tone]}`}>{st.label}</span>
                  </td>
                  <td>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={sc.isActive}
                      aria-label={`${sc.displayName} 사용`}
                      className={e.switch}
                      onClick={(ev) => {
                        ev.stopPropagation();
                        onToggle(sc);
                      }}
                    >
                      <span className={e.switchKnob} />
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className={e.listHint}>
        이름을 누르면 단계를 편집합니다. 작업 등록의 응대 방식에는 &apos;사용&apos;이 켜진 시나리오만 나옵니다. 켜려면
        준비 점검(운영 맵 · 끊긴 흐름 · 장소 연결 등)을 통과해야 합니다.
      </p>
    </>
  );
}
