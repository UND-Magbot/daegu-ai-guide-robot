"use client";

import React, { useEffect, useMemo, useState } from "react";
import { useToast } from "@/app/components/common/Toast";
import CustomSelect from "@/app/components/select/CustomSelect";
import { markBlocked, placeBlockReason, useMoveBlocks } from "@/app/lib/navReach";
import styles from "./voice.module.css";
import g from "./guideScenario.module.css";
import { FIXED_COUNTER_NO, FIXED_TICKET_NO } from "./guideFixedInput";
import { guideScenarioApi, type GuideScenario, type SegmentInput } from "./guideScenarioApi";
import { SEGMENT_INPUT_LABELS, stepLabel } from "./guideScenarioLabels";
import { voiceApi, type VoiceSlot } from "./voiceApi";

const MAX_TICKET = 30;
const MAX_COUNTER = 20;

type Props = {
  robotId: number;
  scenario: GuideScenario;
  /** 진행 중 안내가 있거나 로봇 맵이 시나리오와 다르면 시작할 수 없다 */
  blockedReason: string | null;
};

type Check = { reachable: boolean; requiredInputs: SegmentInput[] } | null;

/**
 * 구간 테스트 — 시나리오 처음부터 돌리지 않고 원하는 단계만 확인한다.
 * 시작 단계부터 Jetson 이 평소처럼 돌고, 종료 단계를 마치면 관제가 세션을 닫는다(응대 통계 제외).
 * 진행 상황은 위의 안내 세션 카드에 그대로 보인다.
 */
export default function GuideSegmentTest({ robotId, scenario, blockedReason }: Props) {
  const { showToast } = useToast();
  const [start, setStart] = useState<string | null>(null);
  const [end, setEnd] = useState<string | null>(null);
  const [check, setCheck] = useState<Check>(null);
  const [slots, setSlots] = useState<VoiceSlot[]>([]);
  const [destSlotId, setDestSlotId] = useState(0);
  const [ticket, setTicket] = useState(FIXED_TICKET_NO);
  const [counter, setCounter] = useState(FIXED_COUNTER_NO);
  const [busy, setBusy] = useState(false);

  const steps = useMemo(() => scenario.steps.filter((s) => s.isActive), [scenario.steps]);
  const stepOptions = useMemo(
    () => steps.map((s, i) => ({ id: s.stepCode, label: `${i + 1}. ${s.stepLabel?.trim() || stepLabel(s.stepCode)}` })),
    [steps]
  );

  // 시나리오를 바꾸면 고른 단계가 그 시나리오에 없을 수 있다
  useEffect(() => {
    setStart(null);
    setEnd(null);
    setCheck(null);
    setDestSlotId(0);
  }, [scenario.id]);

  // 고객 목적지는 이 시나리오 운영 맵에 장소가 연결된 것만 — 엔진도 그것만 알아듣는다
  useEffect(() => {
    voiceApi
      .listSlots(robotId)
      .then((res) => setSlots(res.items))
      .catch(() => setSlots([]));
  }, [robotId]);
  // 연결된 장소로 노드망이 갈 수 없으면 고를 수 없다(노드망 경로가 꺼져 있으면 막지 않음)
  const moveBlocks = useMoveBlocks([scenario.mapId]);
  const destOptions = useMemo(
    () => [
      { id: 0, label: "목적지 선택" },
      ...slots
        .filter((s) => s.links.some((l) => l.mapId === scenario.mapId))
        .map((s) => {
          const link = s.links.find((l) => l.mapId === scenario.mapId);
          return markBlocked(
            { id: s.id, label: `${s.value} → ${link?.locationName ?? ""}` },
            link ? placeBlockReason(moveBlocks, scenario.mapId, link.locationId) : null,
          );
        }),
    ],
    [slots, scenario.mapId, moveBlocks]
  );

  useEffect(() => {
    if (!start || !end) {
      setCheck(null);
      return;
    }
    let alive = true;
    guideScenarioApi
      .checkSegment(scenario.id, start, end)
      .then((res) => alive && setCheck(res))
      .catch(() => alive && setCheck(null));
    return () => {
      alive = false;
    };
  }, [scenario.id, start, end]);

  const needs = check?.requiredInputs ?? [];
  const missing = needs.filter(
    (n) =>
      (n === "destination" && !destSlotId) ||
      (n === "ticket" && !ticket.trim()) ||
      (n === "counter" && !counter.trim())
  );
  const canStart = !!start && !!end && !!check?.reachable && missing.length === 0 && !blockedReason && !busy;

  const run = async () => {
    if (!canStart || !start || !end) return;
    setBusy(true);
    try {
      await guideScenarioApi.openTestSession(robotId, {
        scenarioCode: scenario.code,
        startStepCode: start,
        endStepCode: end,
        destinationSlotId: needs.includes("destination") ? destSlotId : null,
        ticketNo: needs.includes("ticket") ? ticket.trim() : null,
        counterNo: needs.includes("counter") ? counter.trim() : null,
      });
      showToast(`구간 테스트를 시작했습니다 (${stepLabel(start)} ~ ${stepLabel(end)})`, "success");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "구간 테스트를 시작하지 못했습니다", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`${g.sessionCard} ${g.segmentCard}`}>
      <div className={g.segmentRow}>
        <span className={g.sessionTitle}>구간 테스트</span>
        <label className={g.settingLabel}>시작 단계</label>
        <CustomSelect
          options={stepOptions}
          value={stepOptions.find((o) => o.id === start) ?? null}
          onChange={(o) => setStart(String(o.id))}
          placeholder="시작 단계"
          width={200}
          overlay
        />
        <label className={g.settingLabel}>종료 단계</label>
        <CustomSelect
          options={stepOptions}
          value={stepOptions.find((o) => o.id === end) ?? null}
          onChange={(o) => setEnd(String(o.id))}
          placeholder="종료 단계"
          width={200}
          overlay
        />
        <div className={styles.grow} />
        <button className={styles.primaryBtn} onClick={run} disabled={!canStart}>
          테스트 시작
        </button>
      </div>

      {needs.length > 0 && (
        <div className={g.segmentRow}>
          <span className={styles.hint}>이 구간 앞에서 정해지는 값을 미리 넣어 주세요.</span>
          {needs.includes("destination") && (
            <>
              <label className={g.settingLabel}>{SEGMENT_INPUT_LABELS.destination}</label>
              <CustomSelect
                options={destOptions}
                value={destOptions.find((o) => o.id === destSlotId) ?? destOptions[0]}
                onChange={(o) => setDestSlotId(Number(o.id))}
                placeholder="목적지 선택"
                width={260}
                overlay
              />
            </>
          )}
          {needs.includes("ticket") && (
            <input
              className={g.opInput}
              value={ticket}
              maxLength={MAX_TICKET}
              placeholder={SEGMENT_INPUT_LABELS.ticket}
              onChange={(e) => setTicket(e.target.value)}
            />
          )}
          {needs.includes("counter") && (
            <input
              className={g.opInput}
              value={counter}
              maxLength={MAX_COUNTER}
              placeholder={SEGMENT_INPUT_LABELS.counter}
              onChange={(e) => setCounter(e.target.value)}
            />
          )}
        </div>
      )}

      {start && end && check && !check.reachable && (
        <span className={g.mapWarn}>
          {stepLabel(start)}에서 {stepLabel(end)}(으)로 이어지지 않습니다. 종료 단계를 시작 단계 뒤에서 고르세요.
        </span>
      )}
      {blockedReason && <span className={g.mapWarn}>{blockedReason}</span>}
      <span className={styles.hint}>
        이동 단계부터 시작하면 로봇이 지금 자리에서 출발합니다. 앞 단계의 도착 장소에 로봇을 먼저 세워 두세요.
        종료 단계를 마치면 안내가 자동으로 끝나고, 테스트 기록은 응대 통계에 들어가지 않습니다.
      </span>
    </div>
  );
}
