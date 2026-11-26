"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { X } from "lucide-react";
import { useToast } from "@/app/components/common/Toast";
import CustomSelect from "@/app/components/select/CustomSelect";
import { markBlocked, placeBlockReason, useMoveBlocks } from "@/app/lib/navReach";
import { VOICE_LIMITS } from "@/app/constants/voice";
import modalStyles from "@/app/components/modal/Modal.module.css";
import styles from "./voice.module.css";
import {
  voiceApi,
  type SlotConflict,
  type VoiceMap,
  type VoicePlace,
  type VoiceSlot,
} from "./voiceApi";

type Props = {
  robotId: number;
  onClose: () => void;
  onChanged: () => void;
};

type FormState = {
  id: number | null;
  value: string;
  synonyms: string;
  arrivalText: string;
  spokenName: string;
  /** 맵 id → 그 맵에서 연결할 장소 id. 맵마다 하나 */
  links: Record<number, number>;
  originalValue: string;
  keepOldName: boolean;
};

const EMPTY: FormState = {
  id: null,
  value: "",
  synonyms: "",
  arrivalText: "",
  spokenName: "",
  links: {},
  originalValue: "",
  keepOldName: true,
};

const linksOf = (s: VoiceSlot): Record<number, number> =>
  Object.fromEntries(s.links.map((l) => [l.mapId, l.locationId]));

export default function VoiceSlotModal({ robotId, onClose, onChanged }: Props) {
  const { showToast } = useToast();

  const [slots, setSlots] = useState<VoiceSlot[]>([]);
  const [places, setPlaces] = useState<VoicePlace[]>([]);
  const [maps, setMaps] = useState<VoiceMap[]>([]);
  const [currentMapId, setCurrentMapId] = useState<number | null>(null);
  const [view, setView] = useState<"list" | "form">("list");
  const [form, setForm] = useState<FormState>(EMPTY);
  const [conflict, setConflict] = useState<SlotConflict | null>(null);
  const [formError, setFormError] = useState<string | null>(null); // 중복 등 저장 거부 사유(인라인)
  const [deleteTarget, setDeleteTarget] = useState<VoiceSlot | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await voiceApi.listSlots(robotId);
      setSlots(res.items);
      setPlaces(res.places);
      setMaps(res.maps);
      setCurrentMapId(res.currentMapId);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "불러오지 못했습니다", "error");
    }
  }, [robotId, showToast]);

  useEffect(() => {
    load();
  }, [load]);

  // 뷰가 바뀌거나 다른 목적지를 열면 이전 저장 에러를 지운다
  useEffect(() => {
    setFormError(null);
  }, [view, form.id]);

  // 맵마다 그 맵의 장소만 고른다 — 다른 맵 좌표로 가면 엉뚱한 곳에 선다.
  // 노드망으로 갈 수 없는 장소는 고를 수 없다(노드망 경로가 꺼져 있으면 막지 않음).
  const moveBlocks = useMoveBlocks(maps.map((m) => m.id));
  const placeOptionsByMap = useMemo(() => {
    const out: Record<number, { id: number; label: string; disabled?: boolean; disabledReason?: string | null }[]> = {};
    for (const m of maps) out[m.id] = [{ id: 0, label: "연결 안 함" }];
    for (const p of places) {
      if (p.mapId && out[p.mapId]) {
        out[p.mapId].push(markBlocked({ id: p.id, label: p.name }, placeBlockReason(moveBlocks, p.mapId, p.id)));
      }
    }
    return out;
  }, [maps, places, moveBlocks]);

  const mapLabel = useCallback(
    (mapId: number) => {
      const m = maps.find((x) => x.id === mapId);
      if (!m) return "알 수 없는 맵";
      return `${m.floorName ? `${m.floorName} · ` : ""}${m.name}`;
    },
    [maps]
  );

  const openForm = (s: VoiceSlot) => {
    setForm({
      id: s.id,
      value: s.value,
      synonyms: s.synonyms,
      arrivalText: s.arrivalText ?? "",
      spokenName: s.spokenName ?? "",
      links: linksOf(s),
      originalValue: s.value,
      keepOldName: true,
    });
    setConflict(null);
    setView("form");
  };

  const submit = async (force = false) => {
    const value = form.value.trim();
    if (!value) {
      showToast("목적지를 입력하세요", "error");
      return;
    }
    const body = {
      value,
      synonyms: form.synonyms.trim() || null,
      // 빈칸이면 "" 로 보내 지운다(null 은 '안 바꿈')
      arrivalText: form.arrivalText.trim(),
      spokenName: form.spokenName.trim(),
      locationIds: Object.values(form.links).filter((id) => id > 0),
      // 이름을 바꾸면 사람들은 여전히 예전 이름으로 부르는데 로봇만 못 알아듣는다
      keepOldNameAsSynonym: form.keepOldName && form.id !== null && form.originalValue !== value,
    };
    try {
      const res =
        form.id === null
          ? await voiceApi.createSlot(robotId, body, force)
          : await voiceApi.updateSlot(form.id, body, force);

      if (res.status === "conflict" && res.conflict) {
        setConflict(res.conflict); // 유사 이름 — 확인 후 등록
        return;
      }
      showToast("저장했습니다", "success");
      setView("list");
      setForm(EMPTY);
      setConflict(null);
      setFormError(null);
      await load();
      onChanged();
    } catch (e) {
      // 중복(이미 있는 목적지)·0.90 이상 유사 등은 서버가 409로 막는다 — 토스트 + 인라인 둘 다
      const msg = e instanceof Error ? e.message : "저장에 실패했습니다";
      setFormError(msg);
      showToast(msg, "error");
    }
  };

  const remove = async () => {
    if (!deleteTarget) return;
    try {
      await voiceApi.deleteSlot(deleteTarget.id);
      showToast(`'${deleteTarget.value}' 목적지를 삭제했습니다`, "success");
      setDeleteTarget(null);
      // 삭제한 항목의 편집 폼에 머물지 않고 목록으로 돌아간다
      setView("list");
      setForm(EMPTY);
      setConflict(null);
      await load();
      onChanged();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "삭제에 실패했습니다", "error");
    }
  };

  const nameChanged = form.id !== null && form.originalValue !== form.value.trim();

  return (
    // 오버레이 클릭으로 닫지 않는다 — 텍스트 드래그 선택이 오버레이에서 끝나면 닫히기 때문
    <div className={modalStyles.modalOverlay}>
      <div className={modalStyles.detailModalContent}>
        <div className={modalStyles.detailHeader}>
          <div className={modalStyles.detailHeaderTop}>
            <h2>{view === "list" ? "목적지 사전" : form.id === null ? "목적지 추가" : "목적지 편집"}</h2>
            <button className={modalStyles.detailCloseBtn} onClick={onClose}>
              <X size={18} />
            </button>
          </div>
        </div>

        <div className={modalStyles.detailBody}>
          {view === "list" ? (
            <>
              <div className={styles.slotBar}>
                <span className={styles.totalCount}>
                  총 <span>{slots.length}</span> 개 · 연결됨{" "}
                  <span>{slots.filter((s) => s.links.length > 0).length}</span>
                </span>
                <div className={styles.grow} />
                <button
                  className={styles.workBtn}
                  onClick={() => {
                    setForm(EMPTY);
                    setConflict(null);
                    setView("form");
                  }}
                >
                  목적지 추가
                </button>
              </div>

              <div className={styles.slotScroll}>
                <table className={styles.table}>
                  <thead>
                    <tr>
                      <th className={styles.thLeft} style={{ width: 160 }}>목적지</th>
                      <th className={styles.thLeft}>동의어</th>
                      <th className={styles.thLeft} style={{ width: 230 }}>연결된 장소 (층별)</th>
                      <th style={{ width: 88 }}>편집</th>
                    </tr>
                  </thead>
                  <tbody>
                    {slots.length === 0 ? (
                      <tr>
                        <td colSpan={4}>
                          <div className={styles.emptyState}>등록된 목적지가 없습니다.</div>
                        </td>
                      </tr>
                    ) : (
                      slots.map((s) => (
                        <tr
                          key={s.id}
                          className={styles.rowClickable}
                          onClick={() => openForm(s)}
                        >
                          <td className={styles.tdLeft}>
                            <span className={styles.nameCell}>{s.value}</span>
                          </td>
                          <td
                            className={styles.tdLeft}
                            style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: 0 }}
                            title={s.synonyms || undefined}
                          >
                            {s.synonyms || "-"}
                          </td>
                          <td className={styles.tdLeft}>
                            {s.links.length === 0 ? (
                              <span className={`${styles.badge} ${styles.badgeOff}`}>미연결</span>
                            ) : (
                              <div className={styles.slotLinks}>
                                {s.links.map((l) => (
                                  <span
                                    key={l.mapId}
                                    className={`${styles.badge} ${styles.badgeOn}`}
                                    title={mapLabel(l.mapId)}
                                  >
                                    {maps.find((m) => m.id === l.mapId)?.floorName || "맵"} · {l.locationName}
                                  </span>
                                ))}
                              </div>
                            )}
                          </td>
                          <td className={styles.tdAction}>
                            <button
                              className={styles.detailButton}
                              onClick={(e) => {
                                e.stopPropagation();
                                openForm(s);
                              }}
                            >
                              편집
                            </button>
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
              <p className={styles.hint}>
                <b>연결된 장소</b>가 있어야 음성 명령이 <b>실제 이동</b>으로 이어집니다. 장소는 맵마다
                따로 연결하며, 로봇은 지금 쓰는 맵(안내 시나리오는 운영 맵)에 연결된 목적지만
                알아듣습니다.
              </p>
            </>
          ) : (
            <div className={styles.formGridFull}>
              <div className={styles.formRow}>
                <label>
                  목적지<span className={styles.req}>*</span>
                </label>
                <input
                  className={formError ? styles.inputError : undefined}
                  value={form.value}
                  maxLength={50}
                  onChange={(e) => {
                    setForm((f) => ({ ...f, value: e.target.value }));
                    setConflict(null);
                    setFormError(null);
                  }}
                  placeholder="예: 응급실"
                />
                {formError && <p className={styles.fieldError}>{formError}</p>}
              </div>

              {/* 이름을 바꾸면 예전 이름으로 부르는 사람을 못 알아듣는다 */}
              {nameChanged && (
                <div className={`${styles.inline} ${styles.inlineWarn}`}>
                  이름을 바꾸면 로봇은 예전 이름 <b>&quot;{form.originalValue}&quot;</b>을 못
                  알아듣습니다. 사람들은 여전히 그렇게 부를 텐데요.
                  <label className={styles.checkRow}>
                    <input
                      type="checkbox"
                      checked={form.keepOldName}
                      onChange={(e) =>
                        setForm((f) => ({ ...f, keepOldName: e.target.checked }))
                      }
                    />
                    <span>
                      <b>&quot;{form.originalValue}&quot;을 동의어로 남기기</b> (권장)
                    </span>
                  </label>
                </div>
              )}

              {conflict && (
                <div className={`${styles.inline} ${styles.inlineWarn}`}>
                  기존 {conflict.kind} <b>&quot;{conflict.against}&quot;</b>와{" "}
                  <b>{Math.round(conflict.score * 100)}% 유사</b>합니다. 음성 인식이 살짝 틀리면
                  엉뚱한 곳으로 이동할 수 있습니다.
                  <div className={styles.inlineActs}>
                    <button className={styles.workBtn} onClick={() => submit(true)}>
                      그래도 등록
                    </button>
                    <button
                      className={`${styles.workBtn} ${styles.ghost}`}
                      onClick={() => setConflict(null)}
                    >
                      이름 변경
                    </button>
                  </div>
                </div>
              )}

              <div className={styles.formRow}>
                <label>동의어 (쉼표로 구분)</label>
                <input
                  value={form.synonyms}
                  maxLength={VOICE_LIMITS.synonyms}
                  onChange={(e) => setForm((f) => ({ ...f, synonyms: e.target.value }))}
                  placeholder="예: 응급센터, 이머전시"
                />
                <p className={styles.hint}>다르게 받아써도 같은 목적지로 인식됩니다.</p>
              </div>

              <div className={styles.formRow}>
                <label>도착지 음성명 (선택)</label>
                <input
                  value={form.spokenName}
                  maxLength={VOICE_LIMITS.spokenName}
                  onChange={(e) => setForm((f) => ({ ...f, spokenName: e.target.value }))}
                  placeholder="예: 엘리베이터 입구"
                />
                <p className={styles.hint}>
                  안내 시나리오에서 이 목적지로 갈 때 장소 이름 대신 이 이름으로 말합니다 — 도착 멘트의 {"{도착지}"},
                  &quot;곧 ○○에 도착합니다&quot;, &quot;○○ 방향으로 이동하고 있습니다&quot;. 비우면 연결 장소 이름(예:
                  1F_엘리베이터입구)을 그대로 씁니다.
                </p>
              </div>

              <div className={styles.formRow}>
                <label>도착 문구 (선택)</label>
                <input
                  value={form.arrivalText}
                  maxLength={VOICE_LIMITS.arrivalText}
                  onChange={(e) => setForm((f) => ({ ...f, arrivalText: e.target.value }))}
                  placeholder="예: 엘리베이터를 타고 3층으로 이동해 주세요."
                />
                <p className={styles.hint}>
                  안내 시나리오 도착 멘트의 {"{도착안내}"} 자리에 들어갑니다. 목적지와 실제 도착 장소가 다를 때(3층
                  관공서 → 엘리베이터 입구) 다음 행동을 알려 주세요.
                </p>
              </div>

              <div className={styles.formRow}>
                <label>연결 장소 (맵별, 선택)</label>
                {maps.length === 0 ? (
                  <p className={styles.hint}>등록된 맵이 없습니다. 맵 관리에서 맵과 장소를 먼저 등록하세요.</p>
                ) : (
                  <div className={styles.slotMapList}>
                    {maps.map((m) => {
                      const opts = placeOptionsByMap[m.id] ?? [{ id: 0, label: "연결 안 함" }];
                      const picked = form.links[m.id] ?? 0;
                      return (
                        <div key={m.id} className={styles.slotMapRow}>
                          <span className={styles.slotMapName} title={mapLabel(m.id)}>
                            {mapLabel(m.id)}
                            {m.id === currentMapId && <em> (로봇 현재 맵)</em>}
                          </span>
                          <CustomSelect
                            options={opts}
                            value={opts.find((o) => o.id === picked) ?? opts[0]}
                            onChange={(o) =>
                              setForm((f) => ({ ...f, links: { ...f.links, [m.id]: Number(o.id) } }))
                            }
                            placeholder="장소 선택"
                            width={220}
                            overlay
                          />
                        </div>
                      );
                    })}
                  </div>
                )}
                <p className={styles.hint}>
                  맵 관리 › 장소 목록에 등록된 지점을 그대로 가져옵니다. 층마다 있는 목적지(엘리베이터
                  등)는 맵마다 그 층의 장소를 연결하세요. 연결하지 않은 맵에서는 이 목적지를 알아듣지
                  않습니다.
                </p>
              </div>
            </div>
          )}
        </div>

        {view === "form" && (
          <div className={`${modalStyles.detailFooter} ${styles.footSplit}`}>
            {form.id !== null ? (
              <button
                className={`${styles.btn} ${styles.btnDanger}`}
                onClick={() =>
                  setDeleteTarget(slots.find((s) => s.id === form.id) ?? null)
                }
              >
                삭제
              </button>
            ) : (
              <span />
            )}
            <div className={styles.footRight}>
              <button
                className={`${styles.btn} ${styles.btnSecondary}`}
                onClick={() => {
                  setView("list");
                  setConflict(null);
                }}
              >
                취소
              </button>
              <button
                className={`${styles.btn} ${styles.btnPrimary}`}
                onClick={() => submit(false)}
              >
                저장
              </button>
            </div>
          </div>
        )}
      </div>

      {/* 삭제하면 로봇이 그 말을 못 알아듣게 된다 — 무엇이 사라지는지 말해준다 */}
      {deleteTarget && (
        <div className={styles.confirmOverlay} onClick={(e) => e.stopPropagation()}>
          <div className={styles.confirmBox}>
            <div className={styles.confirmText}>
              목적지 <b>&quot;{deleteTarget.value}&quot;</b>을 삭제할까요?
            </div>
            <p className={styles.confirmSub}>
              로봇이 &quot;{deleteTarget.value}&quot;
              {deleteTarget.synonyms ? `와 동의어 "${deleteTarget.synonyms}"` : ""}를 더 이상
              알아듣지 못합니다.
              <br />
              안내 시나리오 자체는 그대로 남고, 다른 목적지는 정상 동작합니다.
            </p>
            <div className={styles.confirmBtns}>
              <button
                className={`${styles.btn} ${styles.btnSecondary}`}
                onClick={() => setDeleteTarget(null)}
              >
                취소
              </button>
              <button className={`${styles.btn} ${styles.btnDanger}`} onClick={remove}>
                삭제
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
