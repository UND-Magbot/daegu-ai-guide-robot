"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { useToast } from "@/app/components/common/Toast";
import CustomSelect from "@/app/components/select/CustomSelect";
import { markBlocked, placeBlockReason, useMoveBlocks } from "@/app/lib/navReach";
import styles from "./voice.module.css";
import e from "./scenarioEditor.module.css";
import {
  GuideApiError,
  guideScenarioApi,
  type CheckResult,
  type GuideFloor,
  type GuideMap,
  type GuidePlace,
  type GuideScenario,
  type GuideSpeed,
  type OperatorInput,
} from "./guideScenarioApi";
import { GUIDE_SPEED_LABELS, withStepLabels } from "./guideScenarioLabels";
import {
  appendIndex,
  canMoveDown,
  canMoveUp,
  clearTargets,
  deleteBlockers,
  deleteStep,
  duplicateStep,
  flowParts,
  insertStep,
  moveStep,
  nameOf,
  quickWarning,
  sameSteps,
  toDraft,
  toPayload,
  type DraftStep,
} from "./scenarioDraft";
import { scenarioState } from "./GuideScenarioList";
import GuideStepPanel, { WAIT_CHOICES } from "./GuideStepPanel";
import GuideVersionsModal from "./GuideVersionsModal";
import GuideFloorMoveBar from "./GuideFloorMoveBar";
import GuidePrerunTest from "./GuidePrerunTest";
import GuideSegmentTest from "./GuideSegmentTest";
import GuideSessionCard from "./GuideSessionCard";
import KeepGaitToggle from "./KeepGaitToggle";

const SPEED_OPTIONS = (Object.keys(GUIDE_SPEED_LABELS) as GuideSpeed[]).map((id) => ({ id, label: GUIDE_SPEED_LABELS[id] }));
const FOLLOW_INTERVAL_OPTIONS = [{ id: 0, label: "자동 (구간별 평균)" }, ...[15, 20, 30, 45, 60].map((s) => ({ id: s, label: `${s}초마다` }))];
const FOLLOW_WAIT_OPTIONS = [30, 60, 90, 120, 180].map((s) => ({ id: s, label: `${s}초` }));
const USER_INTENTS_FALLBACK = ["YES_NO", "PURPOSE", "DESTINATION", "FORM_COMPLETE"];
const OPERATOR_INPUTS_FALLBACK: OperatorInput[] = ["TICKET", "COUNTER", "RESUME"];
const WAIT_NAME = Object.fromEntries(WAIT_CHOICES.map((w) => [w.id, w.label]));

type Tab = "steps" | "settings" | "test";

/** 설정 초안 — 운영 맵은 단계 저장과 한 번에 보낸다(장소 검사가 맵 기준이라) */
type SettingsDraft = {
  displayName: string;
  floorId: number | null;
  mapId: number | null;
  startLocationId: number | null;
  guideSpeed: GuideSpeed;
  followCheckEnabled: boolean;
  followIntervalSec: number;
  followMaxWaitSec: number;
  turnCuesEnabled: boolean;
};

const toSettings = (sc: GuideScenario): SettingsDraft => ({
  displayName: sc.displayName,
  floorId: sc.floorId,
  mapId: sc.mapId,
  startLocationId: sc.startLocationId,
  guideSpeed: sc.guideSpeed,
  followCheckEnabled: sc.followCheckEnabled,
  followIntervalSec: sc.followIntervalSec ?? 0,
  followMaxWaitSec: sc.followMaxWaitSec,
  turnCuesEnabled: sc.turnCuesEnabled,
});

/** 저장할 설정만 — 운영 맵·층은 단계 저장이 함께 바꾼다 */
function settingsPatch(a: SettingsDraft, b: SettingsDraft): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (a.displayName.trim() !== b.displayName) body.displayName = a.displayName.trim();
  if (a.startLocationId !== b.startLocationId)
    Object.assign(body, a.startLocationId ? { startLocationId: a.startLocationId } : { clearStartLocation: true });
  if (a.guideSpeed !== b.guideSpeed) body.guideSpeed = a.guideSpeed;
  if (a.followCheckEnabled !== b.followCheckEnabled) body.followCheckEnabled = a.followCheckEnabled;
  if (a.followIntervalSec !== b.followIntervalSec) body.followIntervalSec = a.followIntervalSec;
  if (a.followMaxWaitSec !== b.followMaxWaitSec) body.followMaxWaitSec = a.followMaxWaitSec;
  if (a.turnCuesEnabled !== b.turnCuesEnabled) body.turnCuesEnabled = a.turnCuesEnabled;
  return body;
}

type Confirm =
  | { kind: "leave" }
  | { kind: "delete" }
  | { kind: "deleteStep"; index: number }
  | { kind: "off" }
  | { kind: "reset" };

type Props = {
  robotId: number;
  robotName: string;
  scenario: GuideScenario;
  floors: GuideFloor[];
  maps: GuideMap[];
  places: GuidePlace[];
  robotMapId: number | null;
  mapWarning: string | null;
  userIntents: string[] | null;
  operatorInputs: OperatorInput[] | null;
  onBack: () => void;
  onReload: () => Promise<void>;
  onDeleted: () => void;
  /** 기본 시나리오(병원·관공서·주차장)를 기본값으로 되돌린다 — 되돌린 것도 버전으로 남는다 */
  onResetDefault: () => Promise<void>;
};

/**
 * 안내 시나리오 상세 — 단계 편집기. 화면이 초안을 들고 있다가 머리줄 [저장]에서 한 번에 보낸다.
 * 탭을 바꿔도 초안은 남고(노란 점), 나갈 때 묻는다. '사용' 켜기·끄기만 바로 적용된다.
 * 저장하면 다음 안내부터 적용된다(엔진이 안내를 시작할 때 단계를 새로 받는다).
 */
export default function GuideScenarioEditor({
  robotId,
  robotName,
  scenario,
  floors,
  maps,
  places,
  robotMapId,
  mapWarning,
  userIntents,
  operatorInputs,
  onBack,
  onReload,
  onDeleted,
  onResetDefault,
}: Props) {
  const { showToast } = useToast();
  const [tab, setTab] = useState<Tab>("steps");
  const [steps, setSteps] = useState<DraftStep[]>(() => toDraft(scenario.steps));
  const [settings, setSettings] = useState<SettingsDraft>(() => toSettings(scenario));
  const [sel, setSel] = useState(0);
  const [issues, setIssues] = useState<CheckResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [sessionActive, setSessionActive] = useState(false);
  const saved = useMemo(() => ({ steps: toDraft(scenario.steps), settings: toSettings(scenario) }), [scenario]);
  const resetOnLoad = useRef(false);

  const dirtySteps = !sameSteps(steps, saved.steps);
  const dirtySet = JSON.stringify(settings) !== JSON.stringify(saved.settings);
  const dirty = dirtySteps || dirtySet;
  const mapChanged = settings.mapId !== scenario.mapId;

  // 저장·되돌리기 뒤 다시 불러온 내용으로 초안을 맞춘다(그 밖의 새로 고침은 편집 중인 초안을 건드리지 않는다)
  useEffect(() => {
    if (!resetOnLoad.current) return;
    resetOnLoad.current = false;
    setSteps(toDraft(scenario.steps));
    setSettings(toSettings(scenario));
    setSel((i) => Math.min(i, Math.max(0, scenario.steps.length - 1)));
  }, [scenario]);

  // 저장하지 않은 변경이 있으면 창을 닫거나 새로 고칠 때 브라우저가 묻게 한다
  useEffect(() => {
    if (!dirty) return;
    const stop = (ev: BeforeUnloadEvent) => {
      ev.preventDefault();
      ev.returnValue = "";
    };
    window.addEventListener("beforeunload", stop);
    return () => window.removeEventListener("beforeunload", stop);
  }, [dirty]);

  const moveBlocks = useMoveBlocks([settings.mapId]);
  const mapPlaces = useMemo(
    () =>
      places
        .filter((p) => settings.mapId && p.mapId === settings.mapId)
        .map((p) => markBlocked({ id: p.id, label: p.name }, placeBlockReason(moveBlocks, settings.mapId, p.id))),
    [places, settings.mapId, moveBlocks]
  );
  const placeIds = useMemo(() => new Set(mapPlaces.map((p) => Number(p.id))), [mapPlaces]);
  const savedMapPlaces = useMemo(
    () => places.filter((p) => scenario.mapId && p.mapId === scenario.mapId).map((p) => ({ id: p.id, label: p.name })),
    [places, scenario.mapId]
  );

  const issuesOf = (code: string) => [
    ...(issues?.errors ?? []).filter((x) => x.stepCode === code),
    ...(issues?.warnings ?? []).filter((x) => x.stepCode === code),
  ];

  const updateStep = (index: number, step: DraftStep) => setSteps((cur) => cur.map((s, i) => (i === index ? step : s)));

  const doInsert = (at: number) => {
    const r = insertStep(steps, at);
    setSteps(r.steps);
    setSel(r.index);
  };

  const doDuplicate = () => {
    const r = duplicateStep(steps, sel);
    setSteps(r.steps);
    setSel(r.index);
  };

  const askDeleteStep = () => {
    const blockers = deleteBlockers(steps, sel);
    if (blockers.length) {
      showToast(
        `${blockers.map((b) => `'${nameOf(b.from)}'의 ${b.field === "onNoStepCode" ? "아니요" : b.field === "voiceExitStepCode" ? "음성 출구" : "무응답"} 갈래`).join(", ")}가 이 단계로 이어져 있어 지울 수 없습니다. 먼저 바꾸세요.`,
        "error"
      );
      return;
    }
    setConfirm({ kind: "deleteStep", index: sel });
  };

  const pickMap = (mapId: number | null, floorId: number | null) => {
    if (mapId === settings.mapId) return;
    const r = clearTargets(steps);
    setSteps(r.steps);
    setSettings({ ...settings, floorId, mapId, startLocationId: null });
    if (r.cleared) showToast(`운영 맵을 바꿔 이동 장소 연결 ${r.cleared}곳을 비웠습니다 — 단계 탭에서 다시 연결하세요`, "info");
  };

  const runCheck = async () => {
    try {
      const r = await guideScenarioApi.validateSteps(scenario.id, toPayload(steps), mapChanged ? settings.mapId : null);
      setIssues(r);
      showToast(
        r.errors.length ? `고칠 곳 ${r.errors.length}건 · 경고 ${r.warnings.length}건` : `저장할 수 있습니다 · 경고 ${r.warnings.length}건`,
        r.errors.length ? "error" : "success"
      );
    } catch (err) {
      showToast(err instanceof Error ? err.message : "검사하지 못했습니다", "error");
    }
  };

  const save = async (): Promise<boolean> => {
    if (!dirty) {
      showToast("바뀐 것이 없습니다", "info");
      return true;
    }
    if (!settings.displayName.trim()) {
      setTab("settings");
      showToast("시나리오 이름을 입력하세요", "error");
      return false;
    }
    if (!settings.mapId) {
      setTab("settings");
      showToast("운영 맵을 고르세요", "error");
      return false;
    }
    setBusy(true);
    try {
      let warnings = issues?.warnings ?? [];
      if (dirtySteps || mapChanged) {
        const r = await guideScenarioApi.saveSteps(scenario.id, {
          steps: toPayload(steps),
          baseVersion: scenario.version,
          mapId: mapChanged ? settings.mapId : null,
        });
        warnings = r.warnings;
      }
      const patch = settingsPatch(settings, saved.settings);
      if (Object.keys(patch).length) await guideScenarioApi.updateScenario(scenario.id, patch);
      setIssues({ errors: [], warnings });
      resetOnLoad.current = true;
      await onReload();
      showToast(warnings.length ? `저장했습니다 · 경고 ${warnings.length}건 — 다음 안내부터 적용됩니다` : "저장했습니다 — 다음 안내부터 적용됩니다", "success");
      return true;
    } catch (err) {
      if (err instanceof GuideApiError && err.issues) {
        setIssues(err.issues);
        const first = err.issues.errors.find((x) => x.stepCode);
        const at = first ? steps.findIndex((s) => s.stepCode === first.stepCode) : -1;
        if (at >= 0) {
          setTab("steps");
          setSel(at);
        }
      }
      showToast(err instanceof Error ? err.message : "저장에 실패했습니다", "error");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const setActive = async (on: boolean) => {
    if (dirty) {
      showToast("저장한 뒤 켜고 끌 수 있습니다 — 켜기는 저장된 내용으로 점검합니다", "error");
      return;
    }
    try {
      await guideScenarioApi.updateScenario(scenario.id, { isActive: on });
      await onReload();
      showToast(on ? `${scenario.displayName}을(를) 켰습니다 — 작업 등록의 응대 방식에 나옵니다` : `${scenario.displayName}을(를) 껐습니다`, "success");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "바꾸지 못했습니다", "error");
    }
  };

  const toggleActive = () => {
    if (scenario.isActive && scenario.standbyTasks.length) setConfirm({ kind: "off" });
    else setActive(!scenario.isActive);
  };

  const doDelete = async () => {
    setConfirm(null);
    try {
      await guideScenarioApi.deleteScenario(scenario.id);
      showToast("삭제했습니다", "success");
      onDeleted();
    } catch (err) {
      showToast(err instanceof Error ? err.message : "삭제하지 못했습니다", "error");
    }
  };

  const back = () => (dirty ? setConfirm({ kind: "leave" }) : onBack());
  const state = scenarioState(scenario);
  const tone = { on: e.chipOn, off: e.chipOff, warn: e.chipWarn }[state.tone];
  const step = steps[Math.min(sel, steps.length - 1)];
  const floorOptions = floors.filter((f) => maps.some((m) => m.floorId === f.id)).map((f) => ({ id: f.id, label: f.name }));
  const mapOptions = maps
    .filter((m) => m.floorId === settings.floorId)
    .map((m) => ({ id: m.id, label: m.id === robotMapId ? `${m.name} (로봇 현재 맵)` : m.name }));
  const startOptions = [{ id: 0, label: "없음 (현재 위치)" }, ...mapPlaces];
  const savedErrors = scenario.checkErrors;
  const testBlocked = dirty
    ? "저장한 뒤 시험할 수 있습니다 — 시험은 저장된 버전으로 돕니다."
    : sessionActive
      ? "진행 중인 안내·사전 주행이 끝난 뒤 시작할 수 있습니다."
      : mapWarning;
  const tabs: { id: Tab; label: string; dot: boolean }[] = [
    { id: "steps", label: `단계 (${steps.length})`, dot: dirtySteps },
    { id: "settings", label: "설정", dot: dirtySet },
    { id: "test", label: "층 이동 · 시험", dot: false },
  ];

  return (
    <div className={`${styles.detail} ${e.editorRoot}`}>
      <div className={styles.detailHead}>
        <button className={styles.backBtn} onClick={back} aria-label="안내 시나리오 목록으로">
          <ArrowLeft size={16} />
        </button>
        <span className={styles.crumb}>음성 관리 / {robotName} / 안내 시나리오 /</span>
        <span className={e.headName}>{scenario.displayName}</span>
        <span className={e.headCode}>{scenario.code} · v{scenario.version}</span>
        <span className={`${e.chip} ${tone}`}>{state.label}</span>
        <div className={styles.grow} />
        {scenario.hasDefault && (
          <button
            className={`${styles.workBtn} ${styles.ghost}`}
            onClick={() => setConfirm({ kind: "reset" })}
            disabled={sessionActive}
            title={sessionActive ? "안내가 끝난 뒤 되돌릴 수 있습니다" : undefined}
          >
            기본값으로 되돌리기
          </button>
        )}
        <button className={`${styles.workBtn} ${styles.ghost}`} onClick={() => setVersionsOpen(true)}>
          버전 기록
        </button>
        <button
          className={scenario.isActive ? `${styles.workBtn} ${styles.ghost}` : styles.primaryBtn}
          onClick={toggleActive}
          disabled={!scenario.isActive && (!scenario.ready || dirty)}
          title={!scenario.isActive && !scenario.ready ? "준비 점검을 통과해야 켤 수 있습니다" : undefined}
        >
          {scenario.isActive ? "사용 끄기" : "사용 켜기"}
        </button>
        <button className={`${styles.btn} ${styles.btnDanger}`} onClick={() => setConfirm({ kind: "delete" })} disabled={sessionActive}>
          삭제
        </button>
      </div>

      <div className={e.scrollBody}>
      {!scenario.isActive && (
        <div className={e.checkBox}>
          <div>
            {scenario.steps.length <= 2 && scenario.version <= 1
              ? "새 시나리오입니다. 단계를 추가하고 저장한 뒤, 아래 점검을 모두 통과하면 '사용 켜기'를 누르세요."
              : "꺼진 시나리오입니다. 상시 대기 응대에는 쓰이지 않고, 운영자 안내 시작·구간 시험은 할 수 있습니다."}
          </div>
          <div className={e.checkList}>
            {[
              { label: "운영 맵 지정", ok: !!scenario.mapId },
              { label: savedErrors.length ? `저장된 단계에 고칠 곳 ${savedErrors.length}건` : "저장된 단계 점검 통과", ok: !savedErrors.length },
              { label: dirty ? "저장 안 한 변경 있음" : "저장됨", ok: !dirty },
            ].map((c) => (
              <span key={c.label} className={`${e.checkItem} ${c.ok ? "" : e.checkItemBad}`}>
                <span className={`${e.dot} ${c.ok ? "" : e.dotBad}`} aria-hidden="true" />
                {c.label}
              </span>
            ))}
          </div>
        </div>
      )}

      <GuideSessionCard
        robotId={robotId}
        scenario={scenario}
        mapWarning={mapWarning}
        startPlaces={savedMapPlaces}
        onActiveChange={setSessionActive}
      />
      {dirty && <div className={e.dirtyText} style={{ margin: "-4px 0 8px" }}>안내 시작·구간 시험은 저장된 버전으로 돕니다.</div>}

      <div className={e.tabBar} role="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            className={`${e.tabBtn} ${tab === t.id ? e.tabBtnActive : ""}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
            {t.dot && <span className={e.dirtyDot} aria-label="저장 안 한 변경" />}
          </button>
        ))}
        <div className={styles.grow} />
        {dirty && <span className={e.dirtyText}>저장 안 한 변경 있음</span>}
        <button className={`${styles.btn} ${styles.btnSecondary}`} onClick={runCheck} style={{ marginLeft: 8 }}>
          검사
        </button>
        <button className={`${styles.btn} ${styles.btnPrimary}`} onClick={save} disabled={busy || !dirty} style={{ marginLeft: 6 }}>
          {busy ? "저장 중…" : "저장"}
        </button>
      </div>

      {issues && (issues.errors.length > 0 || issues.warnings.length > 0) && (
        <ul className={e.issueList} aria-label="검사 결과">
          {[...issues.errors.map((x) => ({ ...x, bad: true })), ...issues.warnings.map((x) => ({ ...x, bad: false }))].map((x, i) => {
            const at = x.stepCode ? steps.findIndex((s) => s.stepCode === x.stepCode) : -1;
            return (
              <li key={`${x.stepCode}-${i}`} className={x.bad ? e.issueErr : e.issueWarn}>
                {x.bad ? "오류" : "경고"} ·{" "}
                {at >= 0 ? (
                  <button
                    type="button"
                    className={e.issueBtn}
                    onClick={() => {
                      setTab("steps");
                      setSel(at);
                    }}
                  >
                    {withStepLabels(x.message)}
                  </button>
                ) : (
                  withStepLabels(x.message)
                )}
              </li>
            );
          })}
        </ul>
      )}

      {tab === "steps" && step && (
        <div className={e.editor}>
          <div className={e.stepList}>
            <div className={e.stepListHead}>단계 목록 — 위에서 아래로 진행 · 맨 위가 시작 단계</div>
            {steps.map((s, i) => {
              const top = issuesOf(s.stepCode)[0];
              const server = top && (top.short ?? withStepLabels(top.message));
              const warn = server ?? quickWarning(steps, s, settings.mapId, placeIds);
              return (
                <div key={s.stepCode} className={`${e.stepRow} ${i === sel ? e.stepRowSel : ""} ${s.isActive ? "" : e.stepRowOff}`}>
                  <button type="button" className={e.stepPick} onClick={() => setSel(i)} aria-current={i === sel}>
                    <span className={e.stepNo}>{i + 1}</span>
                    <span className={e.stepBody}>
                      <span className={e.stepTitle}>
                        {nameOf(s)}
                        <span className={`${e.waitTag} ${e[`waitTag_${s.waitFor}`] ?? ""}`}>{WAIT_NAME[s.waitFor] ?? s.waitFor}</span>
                        {i === 0 && <span className={e.startTag}>시작</span>}
                        {!s.isActive && <span className={e.headCode}>꺼짐</span>}
                      </span>
                      <span className={e.stepFlow}>{flowParts(steps, s).join(" · ")}</span>
                      {warn && <span className={e.stepWarn}>⚠ {warn}</span>}
                    </span>
                  </button>
                  <button
                    type="button"
                    className={e.arrow}
                    aria-label={`${nameOf(s)} 위로`}
                    disabled={!canMoveUp(i)}
                    onClick={() => {
                      setSteps(moveStep(steps, i, -1));
                      setSel(i - 1);
                    }}
                  >
                    ▲
                  </button>
                  <button
                    type="button"
                    className={e.arrow}
                    aria-label={`${nameOf(s)} 아래로`}
                    disabled={!canMoveDown(steps, i)}
                    onClick={() => {
                      setSteps(moveStep(steps, i, 1));
                      setSel(i + 1);
                    }}
                  >
                    ▼
                  </button>
                </div>
              );
            })}
            <div className={e.addStep}>
              <button type="button" className={`${styles.btn} ${styles.btnSecondary}`} onClick={() => doInsert(appendIndex(steps))}>
                + 단계 추가
              </button>
            </div>
          </div>
          <GuideStepPanel
            steps={steps}
            index={Math.min(sel, steps.length - 1)}
            mapId={settings.mapId}
            placeOptions={mapPlaces}
            userIntents={userIntents ?? USER_INTENTS_FALLBACK}
            operatorInputs={operatorInputs ?? OPERATOR_INPUTS_FALLBACK}
            issues={issuesOf(step.stepCode)}
            onChange={(s) => updateStep(Math.min(sel, steps.length - 1), s)}
            onInsert={doInsert}
            onDuplicate={doDuplicate}
            onDelete={askDeleteStep}
          />
        </div>
      )}

      {tab === "settings" && (
        <div className={`${e.panel}`} style={{ maxWidth: 900 }}>
          <div className={e.fieldGrid} style={{ gridTemplateColumns: "150px minmax(0, 1fr)" }}>
            <label className={e.fieldLabel} htmlFor="sc-name">이름</label>
            <input
              id="sc-name"
              className={e.input}
              maxLength={50}
              value={settings.displayName}
              onChange={(ev) => setSettings({ ...settings, displayName: ev.target.value })}
            />
            <span className={e.fieldLabel}>운영 층 · 맵</span>
            <div className={e.pills}>
              <CustomSelect
                options={floorOptions}
                value={floorOptions.find((o) => o.id === settings.floorId) ?? null}
                onChange={(o) => {
                  const here = maps.filter((m) => m.floorId === Number(o.id));
                  const m = here.find((x) => x.id === robotMapId) ?? here[0];
                  pickMap(m?.id ?? null, Number(o.id));
                }}
                placeholder="층 선택"
                width={140}
                overlay
              />
              <CustomSelect
                options={mapOptions}
                value={mapOptions.find((o) => o.id === settings.mapId) ?? null}
                onChange={(o) => pickMap(Number(o.id), settings.floorId)}
                placeholder="맵 선택"
                width={280}
                overlay
              />
            </div>
            <span className={e.fieldLabel}>기본 시작 장소</span>
            <CustomSelect
              options={startOptions}
              value={startOptions.find((o) => o.id === (settings.startLocationId ?? 0)) ?? startOptions[0]}
              onChange={(o) => setSettings({ ...settings, startLocationId: Number(o.id) || null })}
              placeholder="장소 선택"
              width={240}
              disabled={!settings.mapId}
              overlay
            />
            <span className={e.fieldLabel}>안내 속도</span>
            <CustomSelect
              options={SPEED_OPTIONS}
              value={SPEED_OPTIONS.find((o) => o.id === settings.guideSpeed) ?? SPEED_OPTIONS[0]}
              onChange={(o) => setSettings({ ...settings, guideSpeed: o.id as GuideSpeed })}
              placeholder="속도 선택"
              width={200}
              overlay
            />
            <span className={e.fieldLabel}>이동 중 동행 확인</span>
            <div className={e.pills}>
              <label className={styles.checkRow}>
                <input
                  type="checkbox"
                  checked={settings.followCheckEnabled}
                  onChange={(ev) => setSettings({ ...settings, followCheckEnabled: ev.target.checked })}
                />
                <span>켜기</span>
              </label>
              <CustomSelect
                options={FOLLOW_INTERVAL_OPTIONS}
                value={FOLLOW_INTERVAL_OPTIONS.find((o) => o.id === settings.followIntervalSec) ?? FOLLOW_INTERVAL_OPTIONS[0]}
                onChange={(o) => setSettings({ ...settings, followIntervalSec: Number(o.id) })}
                placeholder="간격"
                width={180}
                disabled={!settings.followCheckEnabled}
                overlay
              />
              <span className={e.fieldLabel}>직원 호출까지</span>
              <CustomSelect
                options={FOLLOW_WAIT_OPTIONS}
                value={
                  FOLLOW_WAIT_OPTIONS.find((o) => o.id === settings.followMaxWaitSec) ?? {
                    id: settings.followMaxWaitSec,
                    label: `${settings.followMaxWaitSec}초`,
                  }
                }
                onChange={(o) => setSettings({ ...settings, followMaxWaitSec: Number(o.id) })}
                placeholder="대기 시간"
                width={110}
                disabled={!settings.followCheckEnabled}
                overlay
              />
            </div>
            <span className={e.fieldLabel}>경유지 방향 안내</span>
            <label className={styles.checkRow} title="꺾기 1~2m 앞에서 '잠시 후 왼쪽으로 돕니다', 마지막 구간에서 '곧 도착합니다'를 말합니다">
              <input
                type="checkbox"
                checked={settings.turnCuesEnabled}
                onChange={(ev) => setSettings({ ...settings, turnCuesEnabled: ev.target.checked })}
              />
              <span>켜기</span>
            </label>
            <span className={e.fieldLabel}>사용 중인 작업</span>
            <span className={e.hintText}>
              {scenario.standbyTasks.length
                ? `${scenario.standbyTasks.map((t) => t.name).join(", ")} — 이 시나리오로 응대합니다`
                : "없음"}
            </span>
          </div>
          {scenario.mapId && scenario.destinationsOffMap.length > 0 && (
            <div className={`${styles.inline} ${styles.inlineWarn}`}>
              목적지 사전에서 이 운영 맵에 장소가 연결되지 않은 목적지 <b>{scenario.destinationsOffMap.length}개</b>(
              {scenario.destinationsOffMap.join(", ")})는 이 시나리오에서 알아듣지 않습니다.
            </div>
          )}
          <div className={e.hintText}>설정도 단계와 함께 머리줄 [저장]으로 저장됩니다. 운영 맵을 바꾸면 이동 장소 연결이 비워집니다.</div>
        </div>
      )}

      {tab === "test" && (
        <>
          <GuideFloorMoveBar
            robotId={robotId}
            robotName={robotName}
            scenario={scenario}
            floors={floors}
            maps={maps}
            sessionActive={sessionActive}
            onChanged={onReload}
          />
          <KeepGaitToggle robotId={robotId} />
          <GuidePrerunTest robotId={robotId} scenario={scenario} startPlaces={savedMapPlaces} blockedReason={testBlocked} />
          <GuideSegmentTest robotId={robotId} scenario={scenario} blockedReason={testBlocked} />
          <p className={styles.hint}>고속 주행·구간 시험은 로봇이 실제로 그 구간을 진행합니다 — 이동 단계가 있으면 실제로 걸어갑니다. 주변을 확인하고 시작하세요.</p>
        </>
      )}
      </div>

      {versionsOpen && (
        <GuideVersionsModal
          scenarioId={scenario.id}
          scenarioName={scenario.displayName}
          currentVersion={scenario.version}
          dirty={dirty}
          onClose={() => setVersionsOpen(false)}
          onRestored={async () => {
            setVersionsOpen(false);
            setIssues(null);
            resetOnLoad.current = true;
            await onReload();
          }}
        />
      )}

      {confirm && (
        <div className={styles.confirmOverlay}>
          <div className={styles.confirmBox} role="alertdialog" aria-modal="true">
            {confirm.kind === "leave" && (
              <>
                <div className={styles.confirmText}>저장하지 않은 변경이 있습니다</div>
                <p className={styles.confirmSub}>단계·설정에서 바꾼 내용을 저장하지 않았습니다. 어떻게 할까요?</p>
                <div className={styles.confirmBtns}>
                  <button className={`${styles.btn} ${styles.btnSecondary}`} onClick={() => setConfirm(null)}>
                    취소
                  </button>
                  <button className={`${styles.btn} ${styles.btnDanger}`} onClick={onBack}>
                    버리고 나가기
                  </button>
                  <button
                    className={`${styles.btn} ${styles.btnPrimary}`}
                    onClick={async () => {
                      setConfirm(null);
                      if (await save()) onBack();
                    }}
                  >
                    저장하고 나가기
                  </button>
                </div>
              </>
            )}
            {confirm.kind === "delete" && (
              <>
                <div className={styles.confirmText}>{scenario.displayName}을(를) 삭제할까요?</div>
                <p className={styles.confirmSub}>
                  시나리오와 단계, 버전 기록이 모두 지워집니다. 되돌릴 수 없습니다.
                  {scenario.standbyTasks.length > 0 && " 상시 대기 작업이 쓰고 있으면 삭제되지 않습니다."}
                </p>
                <div className={styles.confirmBtns}>
                  <button className={`${styles.btn} ${styles.btnSecondary}`} onClick={() => setConfirm(null)}>
                    취소
                  </button>
                  <button className={`${styles.btn} ${styles.btnDanger}`} onClick={doDelete}>
                    삭제
                  </button>
                </div>
              </>
            )}
            {confirm.kind === "deleteStep" && (
              <>
                <div className={styles.confirmText}>&apos;{nameOf(steps[confirm.index])}&apos; 단계를 지울까요?</div>
                <p className={styles.confirmSub}>앞 단계가 다음 단계로 바로 이어집니다. [저장]을 눌러야 실제로 지워집니다.</p>
                <div className={styles.confirmBtns}>
                  <button className={`${styles.btn} ${styles.btnSecondary}`} onClick={() => setConfirm(null)}>
                    취소
                  </button>
                  <button
                    className={`${styles.btn} ${styles.btnDanger}`}
                    onClick={() => {
                      setSteps(deleteStep(steps, confirm.index));
                      setSel(Math.max(0, confirm.index - 1));
                      setConfirm(null);
                    }}
                  >
                    지우기
                  </button>
                </div>
              </>
            )}
            {confirm.kind === "reset" && (
              <>
                <div className={styles.confirmText}>{scenario.displayName}을(를) 기본값으로 되돌릴까요?</div>
                <p className={styles.confirmSub}>
                  단계·멘트·연결한 이동 장소가 모두 기본값으로 바뀝니다. 운영 층과 사용 여부는 유지되고, 지금 상태는 버전
                  기록에 남습니다.{dirty && " 저장하지 않은 변경은 버려집니다."}
                </p>
                <div className={styles.confirmBtns}>
                  <button className={`${styles.btn} ${styles.btnSecondary}`} onClick={() => setConfirm(null)}>
                    취소
                  </button>
                  <button
                    className={`${styles.btn} ${styles.btnDanger}`}
                    onClick={async () => {
                      setConfirm(null);
                      setIssues(null);
                      resetOnLoad.current = true;
                      await onResetDefault();
                    }}
                  >
                    되돌리기
                  </button>
                </div>
              </>
            )}
            {confirm.kind === "off" && (
              <>
                <div className={styles.confirmText}>{scenario.displayName}을(를) 끌까요?</div>
                <p className={styles.confirmSub}>
                  이 시나리오를 쓰는 상시 대기 작업 {scenario.standbyTasks.length}건(
                  {scenario.standbyTasks.map((t) => t.name).join(", ")})은 끄면 호출해도 안내가 시작되지 않습니다.
                </p>
                <div className={styles.confirmBtns}>
                  <button className={`${styles.btn} ${styles.btnSecondary}`} onClick={() => setConfirm(null)}>
                    취소
                  </button>
                  <button
                    className={`${styles.btn} ${styles.btnDanger}`}
                    onClick={() => {
                      setConfirm(null);
                      setActive(false);
                    }}
                  >
                    끄기
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
