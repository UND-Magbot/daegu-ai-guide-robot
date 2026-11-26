"use client";

import React, { useMemo, useState } from "react";
import { X } from "lucide-react";
import modalStyles from "@/app/components/modal/Modal.module.css";
import styles from "./voice.module.css";
import g from "./guideScenario.module.css";
import type { PrerunPick, PrerunPlan } from "./guideScenarioApi";

type Props = {
  plan: PrerunPlan;
  /** 창 머리에 보일 출발 장소·속도 */
  startLabel: string;
  speedLabel: string;
  busy: boolean;
  onCancel: () => void;
  onStart: (pick: PrerunPick) => void;
};

/** 고른 구간을 서버와 같은 규칙으로 잇는다 — 연달아 같은 장소는 한 번 */
export function pickedRoute(plan: PrerunPlan, on: boolean[]): string[] {
  const legs = plan.legs.filter((_, i) => on[i]);
  return legs.filter((lg, i) => i === 0 || lg.id !== legs[i - 1].id).map((lg) => lg.name);
}

/**
 * 1회 주행 — 갈 곳 고르기. 시나리오 동선(plan.legs)을 체크 목록으로 보여 주고 고른 곳만 돈다.
 * 대답에 따라 가는 곳(branch)은 처음에 꺼 두고, 출발·복귀(start)는 늘 넣는다.
 */
export default function GuidePrerunPickModal({ plan, startLabel, speedLabel, busy, onCancel, onStart }: Props) {
  const initial = useMemo(() => plan.legs.map((lg) => lg.role === "start" || !lg.branch), [plan]);
  const [on, setOn] = useState<boolean[]>(initial);

  const route = pickedRoute(plan, on);
  const placesPicked = plan.legs.some((lg, i) => on[i] && lg.role !== "start");
  const lastStart = plan.legs.length - 1;

  const toggle = (i: number) => setOn((cur) => cur.map((v, j) => (j === i ? !v : v)));
  const start = () =>
    onStart({ legIndexes: on.flatMap((v, i) => (v ? [i] : [])), legCount: plan.legs.length });

  return (
    <div className={modalStyles.modalOverlay} onClick={onCancel}>
      <div className={styles.confirmWordModal} onClick={(e) => e.stopPropagation()} role="dialog" aria-label="갈 곳 고르기">
        <div className={styles.cwHead}>
          <span className={styles.cwTitle}>1회 주행 — 갈 곳 고르기</span>
          <button className={styles.cwClose} onClick={onCancel} aria-label="닫기">
            <X size={18} />
          </button>
        </div>
        <div className={styles.cwBody}>
          <div className={g.pickMeta}>
            출발 {startLabel} · {speedLabel}
          </div>
          <ul className={g.pickList}>
            {plan.legs.map((lg, i) => {
              const fixed = lg.role === "start";
              const tag = fixed
                ? i === 0
                  ? "출발"
                  : i === lastStart
                    ? "출발 장소로 복귀"
                    : "출발 장소"
                : lg.branch
                  ? "대답에 따라"
                  : null;
              return (
                <li key={`${i}-${lg.id}`} className={`${g.pickRow} ${on[i] ? "" : g.pickRowOff}`}>
                  <label className={g.pickLabel}>
                    <input type="checkbox" checked={on[i]} disabled={fixed} onChange={() => toggle(i)} />
                    <span className={g.pickNo}>{i + 1}.</span>
                    <span className={g.pickName}>{lg.name}</span>
                    {lg.step && <span className={g.pickStep}>{lg.step}</span>}
                    {tag && <span className={g.pickTag}>{tag}</span>}
                  </label>
                </li>
              );
            })}
          </ul>
          <div className={g.pickSummary}>
            고른 동선 {route.length}곳: <b>{route.join(" → ")}</b>
          </div>
        </div>
        <div className={styles.cwFoot}>
          <span className={styles.cwCount}>대답에 따라 가는 곳은 처음에 꺼 두었습니다</span>
          <div className={styles.grow} />
          <button className={styles.cwBtnQuiet} onClick={() => setOn(plan.legs.map(() => true))}>
            전체 선택
          </button>
          <button className={styles.cwBtnGhost} onClick={onCancel}>
            취소
          </button>
          <button className={styles.cwBtnPrimary} onClick={start} disabled={busy || !placesPicked}>
            주행 시작
          </button>
        </div>
      </div>
    </div>
  );
}
