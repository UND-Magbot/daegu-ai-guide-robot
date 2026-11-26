"use client";

import React, { useEffect, useState } from "react";
import { useToast } from "@/app/components/common/Toast";
import CustomSelect from "@/app/components/select/CustomSelect";
import styles from "./voice.module.css";
import g from "./guideScenario.module.css";
import { guideScenarioApi, type GuideScenario, type GuideSpeed, type PrerunPick, type PrerunPlan } from "./guideScenarioApi";
import GuidePrerunPickModal from "./GuidePrerunPickModal";
import { GUIDE_SPEED_LABELS, GUIDE_SPEED_SHORT } from "./guideScenarioLabels";
import { buildStartOptions, defaultStartPick } from "./guideStartPick";

type Props = {
  robotId: number;
  scenario: GuideScenario;
  /** 출발 장소로 고를 수 있는 장소 — 시나리오 운영 맵의 장소 */
  startPlaces: { id: number; label: string }[];
  /** 진행 중 안내·사전 주행이 있거나 로봇 맵이 시나리오와 다르면 시작할 수 없다 */
  blockedReason: string | null;
};

const SPEED_OPTIONS = (Object.keys(GUIDE_SPEED_LABELS) as GuideSpeed[]).map((id) => ({ id, label: GUIDE_SPEED_LABELS[id] }));
const DEFAULT_SPEED: GuideSpeed = "fast";

/**
 * 1회 주행 테스트 — 안내 없이 시나리오 동선만 멘트 없이 고른 속도로 한 바퀴(voice_prerun).
 * 안내 시작 창의 '고속 1바퀴 후 안내 시작'과 같은 기능에 속도만 고를 수 있고, 진행 상황은 위의 안내 세션 카드 자리에 보인다.
 */
export default function GuidePrerunTest({ robotId, scenario, startPlaces, blockedReason }: Props) {
  const { showToast } = useToast();
  const defaultPick = defaultStartPick(scenario.startLocationId, startPlaces);
  const startOptions = buildStartOptions(defaultPick, startPlaces);
  const [pick, setPick] = useState(defaultPick);
  const [speed, setSpeed] = useState<GuideSpeed>(DEFAULT_SPEED);
  const [plan, setPlan] = useState<PrerunPlan | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState(false);

  // 시나리오를 바꾸면 그 시나리오의 기본 출발 장소로 되돌린다
  useEffect(() => {
    setPick(defaultPick);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scenario.id]);

  // 출발 장소가 바뀌면 동선을 다시 그린다
  useEffect(() => {
    let alive = true;
    setPlan(null);
    setPlanError(null);
    guideScenarioApi
      .prerunPlan(robotId, scenario.code, pick || null)
      .then((res) => alive && setPlan(res))
      .catch((e) => alive && setPlanError(e instanceof Error ? e.message : "동선을 불러오지 못했습니다"));
    return () => {
      alive = false;
    };
  }, [robotId, scenario.code, pick]);

  const noLegs = !!plan && plan.legs.length === 0;
  const canStart = !blockedReason && !busy && !noLegs;

  // 누르면 바로 출발하지 않고 갈 곳 고르기 창을 띄운다 — 대답에 따라 가는 갈래까지 다 도는 대신 고른 곳만
  const run = async (picked: PrerunPick) => {
    if (!canStart) return;
    setBusy(true);
    try {
      await guideScenarioApi.prerunStart(robotId, scenario.code, pick || null, speed, picked);
      setPicking(false);
      showToast(`${GUIDE_SPEED_SHORT[speed]} 1회 주행을 시작했습니다. 진행 상황은 위 카드에 보입니다`, "success");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "1회 주행을 시작하지 못했습니다", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`${g.sessionCard} ${g.segmentCard}`}>
      <div className={g.segmentRow}>
        <span className={g.sessionTitle}>1회 주행 테스트</span>
        <label className={g.settingLabel}>출발 장소</label>
        <CustomSelect
          options={startOptions}
          value={startOptions.find((o) => o.id === pick) ?? startOptions[0]}
          onChange={(o) => setPick(Number(o.id))}
          placeholder="장소 선택"
          width={240}
          overlay
        />
        <label className={g.settingLabel}>속도</label>
        <CustomSelect
          options={SPEED_OPTIONS}
          value={SPEED_OPTIONS.find((o) => o.id === speed) ?? SPEED_OPTIONS[0]}
          onChange={(o) => setSpeed(o.id as GuideSpeed)}
          placeholder="속도 선택"
          width={200}
          overlay
        />
        <div className={styles.grow} />
        <button className={styles.primaryBtn} onClick={() => setPicking(true)} disabled={!canStart || !plan}>
          1회 주행 시작
        </button>
      </div>

      <div className={g.prerunPreview}>
        {plan ? (
          <>
            시나리오 동선 {plan.legs.length}곳 (목적지 장소 {plan.destinations}곳, 목적지마다 실제 동선)
            <br />
            <b>{plan.legs.map((lg) => lg.name).join(" → ") || "갈 장소가 없습니다"}</b>
            {plan.warnings.map((w) => (
              <span key={w} className={g.mapWarn}>
                <br />
                {w}
              </span>
            ))}
          </>
        ) : (
          planError ?? "동선을 불러오는 중..."
        )}
      </div>

      {blockedReason && <span className={g.mapWarn}>{blockedReason}</span>}
      {picking && plan && (
        <GuidePrerunPickModal
          plan={plan}
          startLabel={(startOptions.find((o) => o.id === pick) ?? startOptions[0])?.label ?? "현재 위치"}
          speedLabel={GUIDE_SPEED_LABELS[speed]}
          busy={busy}
          onCancel={() => setPicking(false)}
          onStart={run}
        />
      )}
      <span className={styles.hint}>
        멘트 없이 고른 속도로 한 바퀴 돌고 끝납니다. 안내는 열리지 않고 응대 통계에도 들어가지 않습니다. 진행 상황과
        중지 버튼은 위 카드에 보입니다.
      </span>
    </div>
  );
}
