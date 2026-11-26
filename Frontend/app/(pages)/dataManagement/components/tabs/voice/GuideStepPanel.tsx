"use client";

import React from "react";
import CustomSelect, { type SelectOption } from "@/app/components/select/CustomSelect";
import styles from "./voice.module.css";
import e from "./scenarioEditor.module.css";
import type { OperatorInput, StepIssue, WaitFor } from "./guideScenarioApi";
import { INTENT_LABELS, OPERATOR_ACTION, SAY_TOKENS, withStepLabels } from "./guideScenarioLabels";
import { AUTO_MAX_SEC, AUTO_MODE_OPTIONS, AUTO_OFF, toAutoMode, type AutoMode } from "./autoAdvance";
import { SLOT_DEST, hasNoBranch, nameOf, setWait, type DraftStep } from "./scenarioDraft";

const MAX_SAY = 400;
const MAX_LABEL = 40;
/** 서버 voice_scenario_editor.MAX_SPOKEN_NAME */
const MAX_SPOKEN_NAME = 30;
/** 서버 voice_scenario_editor.MAX_PROGRESS_TEXT */
const MAX_PROGRESS_TEXT = 100;
/** 서버 voice_scenario_editor.MAX_VOICE_EXIT_KEYWORDS */
const MAX_VOICE_EXIT = 200;

export const WAIT_CHOICES: { id: WaitFor; label: string }[] = [
  { id: "none", label: "말만" },
  { id: "user", label: "대답" },
  { id: "operator", label: "운영자" },
  { id: "arrival", label: "이동" },
];

/** 인식 종류마다 엔진이 알아듣는 말 — robot_voice/scenario.py */
const INTENT_HEARS: Record<string, string> = {
  YES_NO: "예·아니요·취소 (\"준비됐어요\"·\"가요\" 포함)",
  PURPOSE: "목적지 사전의 진료과·민원 종류 (층 이동 목적지 제외)",
  DESTINATION: "목적지 사전 + 운영 맵의 모든 장소",
  FORM_COMPLETE: "\"작성 완료\"",
};

const INPUT_SHOWS: Record<OperatorInput, string> = {
  TICKET: "관제에 [대기번호 입력] 버튼이 뜹니다 → 멘트의 {대기번호}",
  COUNTER: "순번이 되면 [창구 번호 입력] 버튼이 뜹니다 → 멘트의 {창구}",
  RESUME: "[안내 재개] 버튼을 누르면 다음 단계로 갑니다",
};

const TIMEOUT_OPTIONS = [5, 10, 15, 20, 30, 60, 120, 300].map((s) => ({ id: s, label: s >= 60 ? `${s / 60}분` : `${s}초` }));
const RETRY_OPTIONS = [0, 1, 2, 3, 4, 5].map((n) => ({ id: n, label: `${n}회` }));

type Props = {
  steps: DraftStep[];
  index: number;
  mapId: number | null;
  placeOptions: SelectOption[];
  userIntents: string[];
  operatorInputs: OperatorInput[];
  issues: StepIssue[];
  onChange: (step: DraftStep) => void;
  onInsert: (at: number) => void;
  onDuplicate: () => void;
  onDelete: () => void;
};

export default function GuideStepPanel({
  steps,
  index,
  mapId,
  placeOptions,
  userIntents,
  operatorInputs,
  issues,
  onChange,
  onInsert,
  onDuplicate,
  onDelete,
}: Props) {
  const st = steps[index];
  const set = (patch: Partial<DraftStep>) => onChange({ ...st, ...patch });
  const others = steps.filter((x) => x.stepCode !== st.stepCode);
  const branchOptions = (empty: string): SelectOption[] => [
    { id: "", label: empty },
    ...others.map((x) => ({ id: x.stepCode, label: nameOf(x) + (x.isActive ? "" : " (꺼짐)") })),
  ];
  const pick = (opts: SelectOption[], code: string | null) => opts.find((o) => o.id === (code ?? "")) ?? opts[0];
  const nextOpts = branchOptions("(끝) 안내 종료");
  const timeoutOpts = branchOptions("안내 종료");
  const noOpts = branchOptions(st.expectedIntent === "YES_NO" ? "다음 단계와 같음" : "고르지 않음 (다시 묻기)");
  const exitOpts = branchOptions("쓰지 않음");
  const hasVoiceExit = !!(st.voiceExitStepCode && st.voiceExitKeywords?.trim());
  const fieldIssue = (field: string) => issues.find((x) => x.field === field)?.message ?? null;
  // 칸에 딸리지 않은 단계 경고(예: 실제 안내에서 실행되지 않음)는 제목 아래 전체 문장으로 보인다
  const stepIssues = issues.filter((x) => !x.field);
  const autoMode = toAutoMode(st.autoAdvanceSec);
  const isFirst = index === 0;

  const setAuto = (mode: AutoMode, sec?: number) =>
    set({ autoAdvanceSec: mode === "default" ? null : mode === "off" ? AUTO_OFF : Math.max(0, Math.min(AUTO_MAX_SEC, sec ?? 20)) });

  return (
    <div className={e.panel} aria-label="단계 편집">
      <div className={e.panelTitle}>
        단계 편집 — <b style={{ color: "var(--text-primary)" }}>{index + 1}. {nameOf(st)}</b>{" "}
        <span className={e.headCode}>{st.stepCode}</span>
        {isFirst && <span className={e.startTag} style={{ marginLeft: 6 }}>시작 단계</span>}
      </div>
      {stepIssues.map((x, i) => (
        <div key={i} className={e.warnText}>⚠ {withStepLabels(x.message)}</div>
      ))}

      <div className={e.fieldGrid}>
        <label className={e.fieldLabel} htmlFor="step-label">표시 이름</label>
        <input
          id="step-label"
          className={e.input}
          value={st.stepLabel ?? ""}
          maxLength={MAX_LABEL}
          placeholder={nameOf({ stepCode: st.stepCode, stepLabel: null })}
          onChange={(ev) => set({ stepLabel: ev.target.value })}
        />

        <span className={e.fieldLabel}>기다림</span>
        <div className={e.pills} role="radiogroup" aria-label="기다림">
          {WAIT_CHOICES.map((w) => (
            <button
              key={w.id}
              type="button"
              role="radio"
              aria-checked={st.waitFor === w.id}
              className={`${e.pill} ${st.waitFor === w.id ? e.pillOn : ""}`}
              onClick={() => st.waitFor !== w.id && onChange(setWait(st, w.id))}
            >
              {w.label}
            </button>
          ))}
        </div>

        {st.waitFor === "arrival" && (
          <>
            <span className={e.fieldLabel}>이동 대상</span>
            <div className={e.pills} role="radiogroup" aria-label="이동 대상">
              <button
                type="button"
                role="radio"
                aria-checked={!st.slotName}
                className={`${e.pill} ${!st.slotName ? e.pillOn : ""}`}
                onClick={() => set({ slotName: null })}
              >
                정한 장소
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={!!st.slotName}
                className={`${e.pill} ${st.slotName ? e.pillOn : ""}`}
                onClick={() => set({ slotName: SLOT_DEST, targetLocationId: null })}
                title="고객이 말한 목적지의 연결 장소(목적지 사전)로 갑니다"
              >
                고객 목적지
              </button>
            </div>
            {!st.slotName && (
              <>
                <span className={e.fieldLabel}>이동 장소</span>
                <div>
                  {mapId ? (
                    <CustomSelect
                      options={[{ id: 0, label: "장소 선택" }, ...placeOptions]}
                      value={placeOptions.find((o) => o.id === st.targetLocationId) ?? { id: 0, label: "장소 선택" }}
                      onChange={(o) => set({ targetLocationId: Number(o.id) || null })}
                      placeholder="장소 선택"
                      width={240}
                      overlay
                    />
                  ) : (
                    <span className={e.warnText}>운영 맵을 먼저 지정하세요(설정 탭).</span>
                  )}
                  {fieldIssue("targetLocationId") && <div className={e.warnText}>{fieldIssue("targetLocationId")}</div>}
                </div>
              </>
            )}
            {st.slotName && (
              <>
                <span />
                <span className={e.hintText}>앞 단계에서 고객이 말한 목적지(진료과·민원 종류)의 연결 장소로 갑니다.</span>
              </>
            )}
            <label className={e.fieldLabel} htmlFor="step-spoken">부르는 이름</label>
            <div>
              <input
                id="step-spoken"
                className={e.input}
                value={st.spokenPlaceName ?? ""}
                maxLength={MAX_SPOKEN_NAME}
                placeholder="예: 원무과"
                onChange={(ev) => set({ spokenPlaceName: ev.target.value || null })}
              />
              <div className={e.hintText}>
                {st.spokenPlaceName?.trim()
                  ? `도착 직전에 "곧 ${st.spokenPlaceName.trim()}에 도착합니다."라고 말합니다.`
                  : '비우면 도착 직전에 "곧 도착합니다."라고 말합니다.'}
              </div>
              {fieldIssue("spokenPlaceName") && <div className={e.warnText}>{fieldIssue("spokenPlaceName")}</div>}
            </div>
            <label className={e.fieldLabel} htmlFor="step-progress">진행 멘트</label>
            <div>
              <input
                id="step-progress"
                className={e.input}
                value={st.progressText ?? ""}
                maxLength={MAX_PROGRESS_TEXT}
                placeholder="예: 편의점 방향으로 이동하고 있습니다."
                onChange={(ev) => set({ progressText: ev.target.value || null })}
              />
              <div className={e.hintText}>
                {st.progressText?.trim() === "-"
                  ? "이 단계는 이동 중에 따로 말하지 않습니다."
                  : st.progressText?.trim()
                    ? "걷기 시작하고 조금 뒤, 앞뒤 안내와 겹치지 않는 조금 긴 구간에서 한 번 말합니다. 5m 미만이거나 그런 구간이 없으면 말하지 않습니다."
                    : `비우면 "${st.spokenPlaceName?.trim() || "장소 이름"} 방향으로 이동하고 있습니다."라고 말합니다. "-"를 넣으면 말하지 않습니다.`}
              </div>
              {fieldIssue("progressText") && <div className={e.warnText}>{fieldIssue("progressText")}</div>}
            </div>
          </>
        )}

        <label className={e.fieldLabel} htmlFor="step-say">멘트</label>
        <div>
          <textarea
            id="step-say"
            className={`${e.textarea} ${!st.sayText.trim() && st.waitFor !== "arrival" ? e.fieldBad : ""}`}
            rows={3}
            maxLength={MAX_SAY}
            value={st.sayText}
            placeholder={st.waitFor === "arrival" ? "비우면 말 없이 이동합니다" : "로봇이 말할 문장"}
            onChange={(ev) => set({ sayText: ev.target.value })}
            style={{ width: "100%", boxSizing: "border-box" }}
          />
          <div className={e.pills} style={{ marginTop: 4 }}>
            {SAY_TOKENS.map((t) => (
              <button key={t} type="button" className={styles.insertTok} onClick={() => set({ sayText: `${st.sayText}${t}` })}>
                {t}
              </button>
            ))}
            <span className={e.hintText}>
              {st.sayText.length}/{MAX_SAY}
            </span>
          </div>
          {fieldIssue("sayText") && <div className={e.warnText}>{fieldIssue("sayText")}</div>}
        </div>

        <span className={e.fieldLabel}>다음</span>
        <CustomSelect
          options={nextOpts}
          value={pick(nextOpts, st.nextStepCode)}
          onChange={(o) => set({ nextStepCode: String(o.id) || null })}
          placeholder="다음 단계"
          width={240}
          overlay
        />
      </div>

      {st.waitFor === "user" && (
        <div className={`${e.fieldGrid} ${e.fieldGridSub}`}>
          <span className={e.fieldLabel}>인식 종류</span>
          <div>
            <div className={e.pills} role="radiogroup" aria-label="인식 종류">
              {userIntents.map((k) => (
                <button
                  key={k}
                  type="button"
                  role="radio"
                  aria-checked={st.expectedIntent === k}
                  className={`${e.pill} ${st.expectedIntent === k ? e.pillOn : ""}`}
                  onClick={() => set({ expectedIntent: k, onNoStepCode: hasNoBranch(k) ? st.onNoStepCode : null })}
                >
                  {INTENT_LABELS[k] ?? k}
                </button>
              ))}
            </div>
            {!st.expectedIntent && <div className={e.warnText}>하나를 고르세요 — 없으면 저장할 수 없습니다</div>}
          </div>
          <span className={e.fieldLabel}>알아듣는 말</span>
          <span className={e.hintText}>
            {st.expectedIntent ? `${INTENT_HEARS[st.expectedIntent] ?? st.expectedIntent} — 그 밖의 말은 다시 묻습니다` : "—"}
          </span>
          <span className={e.fieldLabel}>응답 대기</span>
          <div className={e.pills}>
            <CustomSelect
              options={TIMEOUT_OPTIONS}
              value={TIMEOUT_OPTIONS.find((o) => o.id === st.responseTimeoutSec) ?? { id: st.responseTimeoutSec, label: `${st.responseTimeoutSec}초` }}
              onChange={(o) => set({ responseTimeoutSec: Number(o.id) })}
              placeholder="응답 대기"
              width={110}
              overlay
            />
            <span className={e.fieldLabel}>재질문</span>
            <CustomSelect
              options={RETRY_OPTIONS}
              value={RETRY_OPTIONS.find((o) => o.id === st.retryCount) ?? RETRY_OPTIONS[2]}
              onChange={(o) => set({ retryCount: Number(o.id) })}
              placeholder="재질문"
              width={90}
              overlay
            />
          </div>
          {hasNoBranch(st.expectedIntent) && (
            <>
              <span className={e.fieldLabel}>{st.expectedIntent === "YES_NO" ? "'아니요'면" : "'없어요'면"}</span>
              <div>
                <CustomSelect
                  options={noOpts}
                  value={pick(noOpts, st.onNoStepCode)}
                  onChange={(o) => set({ onNoStepCode: String(o.id) || null })}
                  placeholder="갈 단계"
                  width={240}
                  overlay
                />
                <div className={e.hintText}>
                  {st.expectedIntent === "YES_NO"
                    ? "고르지 않으면 '아니요'라고 해도 다음 단계로 갑니다."
                    : "더 갈 곳을 묻는 단계에서 \"없어요\"·\"출구요\"라고 하면 갈 곳 — 첫 목적지 질문에는 비워 둡니다."}
                </div>
              </div>
            </>
          )}
          <span className={e.fieldLabel}>끝내 무응답</span>
          <CustomSelect
            options={timeoutOpts}
            value={pick(timeoutOpts, st.onTimeoutStepCode)}
            onChange={(o) => set({ onTimeoutStepCode: String(o.id) || null })}
            placeholder="갈 단계"
            width={240}
            overlay
          />
        </div>
      )}

      {st.waitFor === "operator" && (
        <div className={`${e.fieldGrid} ${e.fieldGridSub}`}>
          <span className={e.fieldLabel}>입력 종류</span>
          <div>
            <div className={e.pills} role="radiogroup" aria-label="입력 종류">
              {operatorInputs.map((k) => (
                <button
                  key={k}
                  type="button"
                  role="radio"
                  aria-checked={st.expectedIntent === k}
                  className={`${e.pill} ${st.expectedIntent === k ? e.pillOn : ""}`}
                  onClick={() => set({ expectedIntent: k })}
                >
                  {OPERATOR_ACTION[k]?.title ?? k}
                </button>
              ))}
            </div>
            {!st.expectedIntent && <div className={e.warnText}>하나를 고르세요 — 없으면 저장할 수 없습니다</div>}
          </div>
          <span className={e.fieldLabel}>관제 화면</span>
          <span className={e.hintText}>
            {st.expectedIntent ? `${INPUT_SHOWS[st.expectedIntent as OperatorInput] ?? ""} · 기다리는 동안 45초마다 "잠시만 기다려 주세요"` : "—"}
          </span>
          <span className={e.fieldLabel}>음성 출구</span>
          <div>
            <input
              type="text"
              aria-label="음성 출구 말"
              className={e.input}
              style={{ width: "100%" }}
              maxLength={MAX_VOICE_EXIT}
              placeholder="예: 대기 자리, 대기장소, 돌아갈"
              value={st.voiceExitKeywords ?? ""}
              onChange={(ev) => set({ voiceExitKeywords: ev.target.value || null })}
            />
            <div style={{ marginTop: 6 }}>
              <CustomSelect
                options={exitOpts}
                value={pick(exitOpts, st.voiceExitStepCode)}
                onChange={(o) => set({ voiceExitStepCode: String(o.id) || null })}
                placeholder="넘어갈 단계"
                width={240}
                overlay
              />
            </div>
            <div className={e.hintText}>
              기다리는 동안 방문자가 이 말(쉼표로 구분) 중 하나를 하면 운영자 입력 없이 고른 단계로 갑니다.
              {hasVoiceExit && " 음성 출구가 있으면 '기본 규칙' 자동 진행은 하지 않습니다(방문자가 말할 틈을 둡니다)."}
            </div>
            {fieldIssue("voiceExitKeywords") && <div className={e.warnText}>{fieldIssue("voiceExitKeywords")}</div>}
            {fieldIssue("voiceExitStepCode") && <div className={e.warnText}>{fieldIssue("voiceExitStepCode")}</div>}
          </div>
          <span className={e.fieldLabel}>자동 진행</span>
          <div className={e.pills}>
            <CustomSelect
              options={AUTO_MODE_OPTIONS}
              value={AUTO_MODE_OPTIONS.find((o) => o.id === autoMode) ?? AUTO_MODE_OPTIONS[0]}
              onChange={(o) => setAuto(o.id as AutoMode)}
              placeholder="자동 진행"
              width={170}
              overlay
            />
            {autoMode === "sec" && (
              <input
                type="number"
                aria-label="자동 진행(초)"
                className={e.input}
                style={{ width: 90 }}
                min={0}
                max={AUTO_MAX_SEC}
                value={st.autoAdvanceSec ?? 0}
                onChange={(ev) => setAuto("sec", Number(ev.target.value))}
              />
            )}
          </div>
        </div>
      )}

      <div className={e.fieldGrid}>
        <span className={e.fieldLabel}>사용</span>
        <label className={styles.checkRow}>
          <input type="checkbox" checked={st.isActive} onChange={(ev) => set({ isActive: ev.target.checked })} disabled={isFirst} />
          <span>{isFirst ? "시작 단계는 끌 수 없습니다" : "끄면 이 단계를 건너뜁니다(이어진 연결은 검사에서 알려 줍니다)"}</span>
        </label>
      </div>

      <div className={e.panelActions}>
        <button type="button" className={`${styles.btn} ${styles.btnSecondary}`} onClick={() => onInsert(index)} disabled={isFirst}>
          위에 추가
        </button>
        <button type="button" className={`${styles.btn} ${styles.btnSecondary}`} onClick={() => onInsert(index + 1)}>
          아래에 추가
        </button>
        <button type="button" className={`${styles.btn} ${styles.btnSecondary}`} onClick={onDuplicate} disabled={isFirst}>
          복제
        </button>
        <button type="button" className={`${styles.btn} ${styles.btnDanger}`} onClick={onDelete} disabled={isFirst}>
          삭제
        </button>
      </div>
    </div>
  );
}
