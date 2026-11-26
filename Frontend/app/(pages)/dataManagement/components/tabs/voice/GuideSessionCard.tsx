"use client";

import React, { useCallback, useEffect, useState } from "react";
import { useToast } from "@/app/components/common/Toast";
import { useRobotStatusContext } from "@/app/context/RobotStatusContext";
import { isRobotLive } from "@/app/lib/robotLiveness";
import CustomSelect from "@/app/components/select/CustomSelect";
import styles from "./voice.module.css";
import g from "./guideScenario.module.css";
import GuidePrerunCard from "./GuidePrerunCard";
import {
  guideScenarioApi,
  type GuideLastEnded,
  type GuidePrerun,
  type GuideScenario,
  type GuideSession,
  type GuideStandbyWork,
  type PrerunPlan,
} from "./guideScenarioApi";
import { CALL_MOVE_STEP, HOLD_CLOSE_MIN, OPERATOR_ACTION, RESULT_LABELS, stepLabel } from "./guideScenarioLabels";
import { fixedInputFor } from "./guideFixedInput";
import { buildStartOptions, defaultStartPick, startButtonLabel } from "./guideStartPick";
import { LOOP_GAP_SEC, useGuideLoop, type LoopRun } from "./useGuideLoop";

// 운영자가 스스로 끝냈거나 정상 완료면 따로 알릴 필요가 없다
const QUIET_RESULTS = ["completed", "aborted_operator", "test_done"];

// 로봇 진행을 따라가야 하므로 짧게 폴링한다. 입력 대기 여부는 로봇이 바꾼다.
const POLL_MS = 3000;
const MAX_TICKET = 30;
const MAX_COUNTER = 20;
const FOLLOW_BADGE = { waiting: "방문자 기다리는 중", lost: "직원 호출됨" } as const;

// 안내 반복(시연 전 테스트) — 1회 · 횟수 지정 · 무한
type RepeatMode = "once" | "count" | "infinite";
const REPEAT_OPTIONS: { id: RepeatMode; label: string }[] = [
  { id: "once", label: "1회" },
  { id: "count", label: "횟수 지정" },
  { id: "infinite", label: "무한 반복" },
];
const REPEAT_MIN = 2;
const REPEAT_MAX = 99;

// 안내 전 사전 주행 — 시나리오 동선을 멘트 없이 고속으로 한 바퀴(voice_prerun)
type PrerunMode = "none" | "fast";
const PRERUN_OPTIONS: { id: PrerunMode; label: string }[] = [
  { id: "fast", label: "고속 1바퀴 후 안내 시작" },
  { id: "none", label: "없음" },
];
const PRERUN_ACTIVE = ["running", "loc_wait"];

/** 사전 주행이 끝난 뒤 열 안내 — 시작 창에서 고른 값을 기억해 둔다 */
type GuideOpen = { placeId: number | null; repeatMode: RepeatMode; repeatCount: number };

const loopLabel = (r: LoopRun) =>
  r.plan.total === null ? `무한 반복 ${r.round}회째` : `반복 ${r.round}/${r.plan.total}회`;

type Props = {
  robotId: number;
  scenario: GuideScenario | null;
  /** 로봇의 현재 층·맵이 시나리오와 다르면 그 안내 문구. 시작은 서버가 최종 판정한다 */
  mapWarning?: string | null;
  /** 호출 위치로 고를 수 있는 장소 — 시나리오 운영 맵의 장소 */
  startPlaces: { id: number; label: string }[];
  /** 세션이 열리거나 닫히면 부모가 편집 잠금 등을 갱신한다 */
  onActiveChange?: (active: boolean) => void;
};

/**
 * 안내 세션 — 사양 9.3 운영자 수동 입력.
 * 안내 시작 · 대기번호 입력 · 창구 번호 입력 · 진료 후 재개 · 작업 종료.
 */
export default function GuideSessionCard({ robotId, scenario, mapWarning, startPlaces, onActiveChange }: Props) {
  const { showToast } = useToast();
  // 로봇이 연결돼 있지 않으면 시작해도 서버가 거절한다 — 버튼을 미리 막아 헷갈리지 않게 한다.
  // 상태를 아직 못 받았으면(loaded=false) 막지 않는다(판단 근거가 없다).
  const { robots: liveRobots, loaded: statusLoaded, serverStatus } = useRobotStatusContext();
  const liveRobot = liveRobots.find((r) => r.id === robotId);
  const robotOffline = statusLoaded && !(liveRobot && isRobotLive(liveRobot.network, serverStatus));
  const [session, setSession] = useState<GuideSession | null>(null);
  const [lastEnded, setLastEnded] = useState<GuideLastEnded | null>(null);
  const [standbyWork, setStandbyWork] = useState<GuideStandbyWork | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  /** 안내 시작 창 — 열려 있으면 고른 호출 위치 id(0 = 제자리) */
  const [startPick, setStartPick] = useState<number | null>(null);
  const [repeatMode, setRepeatMode] = useState<RepeatMode>("once");
  const [repeatCount, setRepeatCount] = useState(5);
  const [prerunMode, setPrerunMode] = useState<PrerunMode>("none");
  const [plan, setPlan] = useState<PrerunPlan | null>(null);
  const [prerun, setPrerun] = useState<GuidePrerun | null>(null);
  const [afterPrerun, setAfterPrerun] = useState<GuideOpen | null>(null);
  /** 사전 주행을 마치고 연 안내 — 세션 카드에 표시한다 */
  const [prerunPill, setPrerunPill] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await guideScenarioApi.currentSession(robotId);
      setSession(res.session);
      setLastEnded(res.lastEnded);
      setStandbyWork(res.standbyWork ?? null);
    } catch {
      /* 폴링 실패는 조용히 넘긴다 — 다음 주기에 다시 본다 */
    } finally {
      setLoaded(true);
    }
    // 사전 주행은 따로 묻는다 — 실패해도 세션 표시는 그대로 둔다
    try {
      const res = await guideScenarioApi.prerunCurrent(robotId);
      setPrerun(res.prerun);
    } catch {
      /* 다음 주기에 다시 본다 */
    }
  }, [robotId]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  const notify = useCallback((msg: string, type: "success" | "error") => showToast(msg, type), [showToast]);
  const loop = useGuideLoop({ robotId, session, loaded, onOpened: setSession, notify });
  const looping = !!loop.run && !loop.run.ended;
  const prerunActive = !!prerun && PRERUN_ACTIVE.includes(prerun.status);

  useEffect(() => {
    // 회차 사이(카운트다운)·사전 주행 중에도 편집·구간 테스트를 막는다 — 로봇이 움직이거나 곧 안내가 열린다
    onActiveChange?.(session !== null || looping || prerunActive);
  }, [session, looping, prerunActive, onActiveChange]);

  // 안내가 끝나면 '사전 주행 완료' 표시도 거둔다
  useEffect(() => {
    if (loaded && session === null) setPrerunPill(null);
  }, [loaded, session]);

  // 시작 창에서 사전 주행을 고르면 동선을 미리 보여 준다(호출 위치가 바뀌면 다시)
  const planScenario = scenario?.code ?? null;
  useEffect(() => {
    if (startPick === null || prerunMode !== "fast" || !planScenario) {
      setPlan(null);
      return;
    }
    let alive = true;
    guideScenarioApi
      .prerunPlan(robotId, planScenario, startPick || null)
      .then((res) => alive && setPlan(res))
      .catch(() => alive && setPlan(null));
    return () => {
      alive = false;
    };
  }, [robotId, planScenario, startPick, prerunMode]);

  // 입력 단계가 바뀌면 그 단계의 고정값으로 다시 채운다 — 대기번호를 창구 칸에 그대로 넣는 실수를 막는다
  useEffect(() => {
    setValue(fixedInputFor(session?.operatorInput));
  }, [session?.currentStepCode, session?.operatorInput]);

  const run = async (fn: () => Promise<{ session: GuideSession | null }>, ok: string) => {
    setBusy(true);
    try {
      const res = await fn();
      setSession(res.session && ["running", "paused"].includes(res.session.result) ? res.session : null);
      showToast(ok, "success");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "요청에 실패했습니다", "error");
    } finally {
      setBusy(false);
    }
  };

  // 시나리오의 기본 시작 장소를 미리 고른다(운영 맵 장소 목록에 없으면 현재 위치)
  const defaultPick = defaultStartPick(scenario?.startLocationId, startPlaces);
  const startOptions = buildStartOptions(defaultPick, startPlaces);

  const openStart = () => setStartPick(defaultPick);

  const openGuide = (open: GuideOpen, then?: () => Promise<unknown>) => {
    if (!scenario) return;
    const { placeId } = open;
    const place = startPlaces.find((p) => p.id === placeId);
    const total =
      open.repeatMode === "count" ? Math.min(REPEAT_MAX, Math.max(REPEAT_MIN, open.repeatCount)) : null;
    const loopPlan =
      open.repeatMode === "once"
        ? null
        : { scenarioCode: scenario.code, scenarioName: scenario.displayName, startLocationId: placeId, total };
    return run(
      () =>
        guideScenarioApi.openSession(robotId, scenario.code, placeId).then(async (res) => {
          if (loopPlan && res.session) loop.start(loopPlan, res.session.id);
          if (then) await then().catch(() => {});
          return res;
        }),
      place ? `${place.label}(으)로 이동한 뒤 안내를 시작합니다` : `${scenario.displayName}을 시작했습니다`
    );
  };

  const start = async () => {
    if (!scenario || startPick === null) return;
    const open: GuideOpen = { placeId: startPick || null, repeatMode, repeatCount };
    setStartPick(null);
    if (prerunMode !== "fast") {
      openGuide(open);
      return;
    }
    setBusy(true);
    try {
      const res = await guideScenarioApi.prerunStart(robotId, scenario.code, open.placeId);
      setPrerun(res.prerun);
      setAfterPrerun(open);
      showToast("사전 주행을 시작했습니다. 끝나면 '안내 시작'을 눌러 주세요", "success");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "사전 주행을 시작하지 못했습니다", "error");
    } finally {
      setBusy(false);
    }
  };

  const prerunCall = async (fn: () => Promise<{ prerun: GuidePrerun | null }>, ok: string) => {
    setBusy(true);
    try {
      const res = await fn();
      setPrerun(res.prerun);
      showToast(ok, "success");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "요청에 실패했습니다", "error");
    } finally {
      setBusy(false);
    }
  };

  /** 사전 주행 뒤 안내 — 안내를 먼저 연 뒤 사전 주행 기록을 닫는다(열기 실패 때 기록을 남긴다) */
  const startGuideAfterPrerun = () => {
    if (!prerun) return;
    const open = afterPrerun ?? { placeId: prerun.startLocationId, repeatMode: "once" as const, repeatCount: 5 };
    const done = prerun.status === "done";
    const label = `사전 주행 완료 ${prerun.total}/${prerun.total}곳`;
    openGuide(open, async () => {
      await guideScenarioApi.prerunDismiss(robotId);
      setPrerun(null);
      setAfterPrerun(null);
      setPrerunPill(done ? label : null);
    });
  };

  const submitInput = () => {
    if (!session?.operatorInput) return;
    const v = value.trim();
    if (session.operatorInput === "TICKET") {
      if (!v) return showToast("대기번호를 입력하세요", "error");
      run(() => guideScenarioApi.operatorInput(session.id, { ticketNo: v }), `대기번호 ${v}번을 입력했습니다`);
    } else if (session.operatorInput === "COUNTER") {
      if (!v) return showToast("창구 번호를 입력하세요", "error");
      run(() => guideScenarioApi.operatorInput(session.id, { counterNo: v }), `${v}번 창구로 안내합니다`);
    } else {
      run(() => guideScenarioApi.operatorInput(session.id, {}), "안내를 재개했습니다");
    }
  };

  const followResume = () => {
    if (!session) return;
    run(() => guideScenarioApi.followResume(session.id), "로봇이 다시 출발합니다");
  };

  const hold = () => {
    if (!session) return;
    run(() => guideScenarioApi.holdSession(session.id), "다음 이동 전에 일시 정지합니다");
  };

  const releaseHold = () => {
    if (!session) return;
    const wasHolding = session.holdState === "holding";
    run(() => guideScenarioApi.releaseHold(session.id), wasHolding ? "안내를 재개합니다" : "일시 정지 예약을 취소했습니다");
  };

  const close = () => {
    if (!session) return;
    setConfirmClose(false);
    loop.requestStop();
    run(() => guideScenarioApi.closeSession(session.id, "운영자 종료"), "안내를 종료했습니다");
  };

  if (!loaded) return <div className={g.sessionCard}>세션 확인 중...</div>;

  if (!session && prerun) {
    return (
      <GuidePrerunCard
        prerun={prerun}
        busy={busy}
        onStop={() => prerunCall(() => guideScenarioApi.prerunStop(robotId), "사전 주행을 멈췄습니다")}
        onResume={(fromFailed) =>
          prerunCall(
            () => guideScenarioApi.prerunResume(robotId, fromFailed),
            fromFailed ? "멈춘 곳부터 다시 주행합니다" : "처음부터 다시 주행합니다"
          )
        }
        onStartGuide={startGuideAfterPrerun}
        onDismiss={() =>
          prerunCall(() => guideScenarioApi.prerunDismiss(robotId), "사전 주행 기록을 닫았습니다").then(() =>
            setAfterPrerun(null)
          )
        }
      />
    );
  }

  if (!session) {
    return (
      <div className={g.sessionCard}>
        <div className={g.sessionMain}>
          <span className={`${styles.badge} ${styles.badgeOff}`}>대기</span>
          <div className={g.sessionInfo}>
            <span className={g.sessionText}>진행 중인 안내가 없습니다.</span>
            {lastEnded && !QUIET_RESULTS.includes(lastEnded.result) && (
              <span className={g.mapWarn}>
                직전 안내 #{lastEnded.id} {RESULT_LABELS[lastEnded.result] ?? lastEnded.result} (
                {stepLabel(lastEnded.stepCode)}){lastEnded.reason ? ` — ${lastEnded.reason}` : ""}
              </span>
            )}
            {mapWarning && <span className={g.mapWarn}>{mapWarning}</span>}
            {robotOffline && (
              <span className={g.mapWarn}>로봇이 오프라인입니다 — 연결된 뒤 안내를 시작할 수 있습니다.</span>
            )}
            {standbyWork && (
              <span className={g.mapWarn}>
                상시 대기 &apos;{standbyWork.workName ?? `작업 #${standbyWork.scheduleId}`}&apos; 근무 중입니다 — 근무
                중 안내는 로봇 상세에서 시작합니다.
              </span>
            )}
            {loop.run && <LoopStatus run={loop.run} />}
          </div>
        </div>
        {looping && loop.run?.countdown !== null && (
          <button className={styles.workBtn} onClick={loop.startNow}>
            지금 시작
          </button>
        )}
        {looping && (
          <button className={`${styles.workBtn} ${styles.danger}`} onClick={loop.requestStop}>
            반복 중지
          </button>
        )}
        {loop.run?.ended && (
          <button className={`${styles.workBtn} ${styles.ghost}`} onClick={loop.dismiss}>
            반복 기록 닫기
          </button>
        )}
        {!standbyWork && (
          <button
            className={styles.primaryBtn}
            onClick={openStart}
            disabled={busy || looping || robotOffline || !scenario || !scenario.isActive}
            title={
              robotOffline
                ? "로봇이 오프라인입니다"
                : scenario && !scenario.isActive
                  ? "비활성 시나리오입니다"
                  : undefined
            }
          >
            {scenario ? `${scenario.displayName} 시작` : "안내 시작"}
          </button>
        )}

        {startPick !== null && scenario && (
          <div className={styles.confirmOverlay}>
            <div className={styles.confirmBox}>
              <div className={styles.confirmText}>{scenario.displayName}을 시작할까요?</div>
              <p className={styles.confirmSub}>
                고객이 호출한 위치를 고르면 로봇이 그곳으로 먼저 이동한 뒤 인사합니다.
              </p>
              <div className={g.startPick}>
                <label className={g.settingLabel}>호출 위치</label>
                <CustomSelect
                  options={startOptions}
                  value={startOptions.find((o) => o.id === startPick) ?? startOptions[0]}
                  onChange={(o) => setStartPick(Number(o.id))}
                  placeholder="장소 선택"
                  width={240}
                  overlay
                />
              </div>
              <div className={g.startPick}>
                <label className={g.settingLabel}>반복</label>
                <CustomSelect
                  options={REPEAT_OPTIONS}
                  value={REPEAT_OPTIONS.find((o) => o.id === repeatMode) ?? REPEAT_OPTIONS[0]}
                  onChange={(o) => setRepeatMode(o.id as RepeatMode)}
                  placeholder="반복"
                  width={140}
                  overlay
                />
                {repeatMode === "count" && (
                  <input
                    className={g.opInput}
                    type="number"
                    min={REPEAT_MIN}
                    max={REPEAT_MAX}
                    value={repeatCount}
                    onChange={(e) => setRepeatCount(Number(e.target.value))}
                    aria-label="반복 횟수"
                  />
                )}
              </div>
              <div className={g.startPick}>
                <label className={g.settingLabel}>사전 주행</label>
                <CustomSelect
                  options={PRERUN_OPTIONS}
                  value={PRERUN_OPTIONS.find((o) => o.id === prerunMode) ?? PRERUN_OPTIONS[1]}
                  onChange={(o) => setPrerunMode(o.id as PrerunMode)}
                  placeholder="사전 주행"
                  width={240}
                  overlay
                />
              </div>
              {prerunMode === "fast" && (
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
                    "동선을 불러오는 중..."
                  )}
                  <br />
                  멘트 없이 고속으로 돌고, 끝나면 &apos;안내 시작&apos;을 눌러야 시나리오 설정 속도로 안내합니다.
                </div>
              )}
              {repeatMode !== "once" && (
                <p className={g.startHint}>
                  정상 종료되면 {LOOP_GAP_SEC}초 뒤 같은 출발 장소로 다시 시작합니다. 비정상 종료, 고객 대답 없음,
                  배터리 최소 기준 이하, 위치 미확정이면 멈춥니다. 이 화면을 닫으면 반복도 멈춥니다.
                </p>
              )}
              {startPlaces.length === 0 && (
                <p className={g.startHint}>운영 맵에 등록된 장소가 없어 현재 위치에서만 시작할 수 있습니다.</p>
              )}
              <div className={styles.confirmBtns}>
                <button className={`${styles.btn} ${styles.btnSecondary}`} onClick={() => setStartPick(null)}>
                  취소
                </button>
                <button
                  className={`${styles.btn} ${styles.btnPrimary}`}
                  onClick={start}
                  disabled={busy || (prerunMode === "fast" && !!plan && plan.legs.length === 0)}
                >
                  {prerunMode === "fast" ? "사전 주행 시작" : startButtonLabel(startPick, startPlaces)}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    );
  }

  const waiting = session.operatorInput !== null;
  const action = session.operatorInput ? OPERATOR_ACTION[session.operatorInput] : null;
  // 로봇이 아직 그 단계에 닿지 않았어도 입력은 받는다 — 도착하면 바로 넘어간다
  const reached = session.result === "paused";
  const callMoving = session.currentStepCode === CALL_MOVE_STEP;
  // 동행 확인으로 멈춘 상태 — 방문자가 따라오지 않아 로봇이 그 자리에서 기다린다
  const followHold = session.followState === "waiting" || session.followState === "lost";
  // 운영자 일시 정지 — 예약(다음 이동 전에 멈출 예정)과 실제 멈춤을 나눠 보여 준다
  const holding = session.holdState === "holding";
  const holdRequested = session.holdState === "requested";
  // 상시 대기 근무의 안내 — 이 화면(일회성 시연용)에서는 시작·종료·일시 정지를 하지 않는다. 입력은 받는다
  const workOwned = !!session.scheduleId;
  const attention = waiting || followHold || holding;
  const badge = holding
    ? "일시 정지 중"
    : followHold
    ? FOLLOW_BADGE[session.followState as "waiting" | "lost"]
    : session.followState === "resume"
      ? "재출발 중"
      : waiting
        ? reached ? "입력 대기" : "입력 단계로 이동 중"
        : callMoving ? "호출 위치로 이동 중" : "안내 중";

  return (
    <div className={`${g.sessionCard} ${attention ? g.sessionWaiting : ""}`}>
      <div className={g.sessionMain}>
        <span className={`${styles.badge} ${attention ? g.badgeWait : styles.badgeOn}`}>{badge}</span>
        <div className={g.sessionInfo}>
          <div className={g.sessionTitle}>
            #{session.id} {session.scenarioName ?? session.scenarioCode} ·{" "}
            <b>{stepLabel(session.currentStepCode)}</b>
            {loop.run && !loop.run.ended && (
              <span className={g.testBadge} title="정상 종료되면 같은 출발 장소로 다음 회차를 시작합니다">
                {loopLabel(loop.run)}
                {loop.run.stopRequested && " · 이번 회차 후 중지"}
              </span>
            )}
            {prerunPill && (
              <span className={g.testBadge} title="이 안내를 시작하기 전에 시나리오 동선을 고속으로 한 바퀴 돌았습니다">
                {prerunPill}
              </span>
            )}
            {session.isTest && (
              <span className={g.testBadge} title="종료 단계를 마치면 자동으로 끝납니다. 응대 통계에 들어가지 않습니다">
                구간 테스트 ~ {stepLabel(session.testEndStepCode)}
              </span>
            )}
          </div>
          <div className={g.sessionMeta}>
            {session.startLocationName && <span>호출 위치 {session.startLocationName}</span>}
            <span title="고객이 말해 확정한 곳 — 멘트의 {목적지}">고객 목적지 {session.destinationName ?? "-"}</span>
            <span title="로봇이 지금 향하는(또는 마지막으로 향한) 장소">이동 목표 {session.navTargetName ?? "-"}</span>
            <span>대기번호 {session.ticketNo ?? "-"}</span>
            <span>창구 {session.counterNo ?? "-"}</span>
            {session.heard && <span title={session.heard}>최근 발화 &quot;{session.heard}&quot;</span>}
          </div>
          {followHold && (
            <span className={g.mapWarn}>
              {session.followState === "lost"
                ? "방문자가 따라오지 않아 직원 호출을 요청했습니다. 현장을 확인한 뒤 재개하거나 종료하세요."
                : "방문자가 따라오지 못해 로봇이 멈춰 기다립니다. 방문자가 '네'라고 하면 다시 출발합니다."}
            </span>
          )}
          {holding && (
            <span className={g.mapWarn}>
              다음 이동 전에 멈춰 있습니다. 재개하면 다음 안내 멘트부터 이어 갑니다. {HOLD_CLOSE_MIN}분 동안
              재개가 없으면 안내를 종료합니다.
            </span>
          )}
          {holdRequested && (
            <span className={g.mapWarn}>
              일시 정지 예정 — 지금 단계를 마친 뒤 다음 이동 단계 앞에서 멈춥니다.
            </span>
          )}
          {workOwned && (
            <span className={g.mapWarn}>
              상시 대기 &apos;{session.scheduleName ?? `작업 #${session.scheduleId}`}&apos; 근무의 안내입니다 — 일시
              정지·종료는 로봇 상세에서 합니다.
            </span>
          )}
        </div>
      </div>

      <div className={g.sessionActions}>
        {followHold && (
          <button className={styles.primaryBtn} onClick={followResume} disabled={busy}>
            안내 재개
          </button>
        )}
        {holding && !workOwned && (
          <button className={styles.primaryBtn} onClick={releaseHold} disabled={busy}>
            안내 재개
          </button>
        )}
        {holdRequested && !workOwned && (
          <button className={styles.workBtn} onClick={releaseHold} disabled={busy}>
            일시 정지 취소
          </button>
        )}
        {!session.holdState && !workOwned && (
          <button className={styles.workBtn} onClick={hold} disabled={busy}>
            일시 정지
          </button>
        )}
        {looping && !loop.run?.stopRequested && (
          <button className={styles.workBtn} onClick={loop.requestStop} title="지금 회차를 마치고 반복을 멈춥니다">
            반복 중지
          </button>
        )}
        {action && session.operatorInput !== "RESUME" && (
          <input
            className={g.opInput}
            value={value}
            maxLength={session.operatorInput === "TICKET" ? MAX_TICKET : MAX_COUNTER}
            placeholder={session.operatorInput === "TICKET" ? "대기번호" : "창구 번호"}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submitInput()}
            autoFocus
          />
        )}
        {action && (
          <button className={styles.primaryBtn} onClick={submitInput} disabled={busy}>
            {action.button}
          </button>
        )}
        {!workOwned && (
          <button
            className={`${styles.workBtn} ${styles.danger}`}
            onClick={() => setConfirmClose(true)}
            disabled={busy}
          >
            안내 종료
          </button>
        )}
      </div>

      {confirmClose && (
        <div className={styles.confirmOverlay}>
          <div className={styles.confirmBox}>
            <div className={styles.confirmText}>안내를 종료할까요?</div>
            <p className={styles.confirmSub}>
              로봇이 이동 중이면 그 자리에 멈춥니다. 고객에게는 더 이상 안내하지 않습니다.
            </p>
            <div className={styles.confirmBtns}>
              <button className={`${styles.btn} ${styles.btnSecondary}`} onClick={() => setConfirmClose(false)}>
                취소
              </button>
              <button className={`${styles.btn} ${styles.btnDanger}`} onClick={close}>
                종료
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** 반복 상태 한 줄 + 회차별 결과 — 회차 사이·종료 뒤 대기 카드에 보인다 */
function LoopStatus({ run }: { run: LoopRun }) {
  const done = run.rounds.length;
  const okCount = run.rounds.filter((r) => r.ok).length;
  return (
    <>
      {!run.ended && run.countdown !== null && (
        <span className={g.sessionText}>
          {loopLabel(run)} 완료 — <b>{run.countdown}초</b> 뒤 {run.round + 1}회차를 시작합니다.
        </span>
      )}
      {!run.ended && run.countdown === null && run.sessionId !== null && (
        <span className={g.sessionText}>{loopLabel(run)} 결과 확인 중…</span>
      )}
      {run.ended && (
        <span className={run.ended.kind === "done" || run.ended.kind === "stopped" ? g.sessionText : g.mapWarn}>
          {run.plan.scenarioName} 반복 종료 — {run.ended.reason} (정상 {okCount}/{done}회)
        </span>
      )}
      {done > 0 && (
        <span className={g.sessionMeta}>
          {run.rounds.map((r) => (
            <span key={r.sessionId} title={`세션 #${r.sessionId}`}>
              {r.round}회 {RESULT_LABELS[r.result] ?? r.result}
              {r.ok ? "" : " (중지)"}
            </span>
          ))}
        </span>
      )}
    </>
  );
}
