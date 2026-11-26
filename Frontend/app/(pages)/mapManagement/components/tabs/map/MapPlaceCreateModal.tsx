"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useModalBehavior } from "@/app/hooks/useModalBehavior";
import styles from "./MapPlaceCreateModal.module.css";
import DropdownSelect from "@/app/components/button/DropdownSelect";

import { API_BASE } from "@/app/config";
import { apiFetch } from "@/app/lib/api";
import { degFromYaw, yawFromDeg } from "@/app/lib/yaw";
import DirectionPicker from "@/app/components/common/DirectionPicker";
import { MapPin } from "lucide-react";

type DBRobot = { id: number; RobotName: string };

const DUPLICATE_NAME_MSG = "이미 사용 중인 장소명입니다. 층이 달라도 이름은 중복될 수 없습니다.";

// 충전소는 고를 수 없다 — 저장하면 맵 초기 위치가 그 좌표로 바뀌고 도킹 포인트도 따로 잡아야 해서
// 상단 [충전소 생성](로봇 실제 충전 위치 + 도킹 포인트 단계)으로만 만든다.
const CATEGORY_OPTIONS = [
  { value: "waypoint", label: "경유지" },
  { value: "work", label: "작업지" },
  { value: "standby", label: "대기소" },
];
const CHARGE_OPTION = { value: "charge", label: "충전소" };

export type PlaceEditData = {
  key: string;
  name: string;
  robotName: string;
  floor: string;
  x: number;
  y: number;
  yaw: number;
  desc: string;
  mapId: number | null;
  category?: string;
};

type Props = {
  isOpen: boolean;
  mode: "create" | "edit";
  /** 맵 클릭으로 얻은 월드 좌표 (create 모드) */
  worldX: number;
  worldY: number;
  /** 맵 이미지 상의 픽셀 좌표 */
  pixelX: number;
  pixelY: number;
  /** 연결된 robot_map_info.id */
  mapId?: number;
  /** 자동 선택할 로봇 이름 (연결된 로봇) */
  defaultRobotName?: string;
  /** 자동 선택할 층 이름 */
  defaultFloor?: string;
  /** 수정 모드 시 기존 데이터 */
  editData?: PlaceEditData | null;
  /** 로봇 현 위치에서 생성 시 좌표/방향 고정 */
  lockCoords?: boolean;
  /** 로봇 yaw (라디안) — lockCoords=true 시 방향 자동 세팅 */
  defaultYaw?: number;
  /** 카테고리 기본값 (충전소 생성 등) */
  defaultCategory?: string;
  /** 층 목록 (floor_info) */
  floors?: { id: number; FloorName: string }[];
  /** 이미 쓰이고 있는 장소명(사업장 전체 + 미저장분). 서버가 409 로 막기 전에 미리 걸러낸다. */
  usedNames?: string[];
  onClose: () => void;
  onConfirm: (place: PendingPlace, oldName?: string) => void;
  /** 노드망(2026-10-19): 찍은 자리가 노드에 붙는지 — undefined 면 구역을 숨긴다, null 이면 확인 중 */
  nodeAttach?: NodeAttach | null;
  nodeChoice?: NodeChoice;
  onNodeChoice?: (c: NodeChoice) => void;
  /** 확인 뒤(장소 반영 다음)에 부른다 — 노드 자동 추가·노드 도구 전환은 페이지가 한다 */
  onAfterConfirm?: (place: PendingPlace) => void;
};

export type PendingPlace = {
  tempId: string;
  RobotName: string;
  LacationName: string;
  FloorId: number | null;
  LocationX: number;
  LocationY: number;
  Yaw: number;
  MapId: number | null;
  Category: string;
  Imformation: string | null;
};

export type NodeChoice = "auto" | "manual" | "none";
export interface NodeAttach { nodeKey: string | null; distM?: number; nearestKey?: string | null; nearestM?: number | null; reason?: string }

export default function MapPlaceCreateModal({
  isOpen,
  mode = "create",
  worldX,
  worldY,
  pixelX,
  pixelY,
  mapId,
  defaultRobotName = "",
  defaultFloor = "",
  editData = null,
  lockCoords = false,
  defaultYaw,
  defaultCategory,
  floors = [],
  usedNames = [],
  onClose,
  onConfirm,
  nodeAttach,
  nodeChoice = "auto",
  onNodeChoice,
  onAfterConfirm,
}: Props) {
  const isEdit = mode === "edit";

  const [dbRobots, setDbRobots] = useState<DBRobot[]>([]);
  const [robotFetchError, setRobotFetchError] = useState(false);

  // form state
  const [robotNo, setRobotNo] = useState("");
  const [floor, setFloor] = useState("");
  const [name, setName] = useState("");
  const [direction, setDirection] = useState("");
  const [category, setCategory] = useState("waypoint");
  const [desc, setDesc] = useState("");
  const isSubmitting = false; // 즉시 반환이므로 항상 false
  const [fieldErrors, setFieldErrors] = useState<{
    robotNo?: string;
    floor?: string;
    name?: string;
    direction?: string;
  }>({});

  const DEFAULT_FLOORS = ["B1", "1F", "2F", "3F", "4F"];
  const floorOptions = floors.length > 0
    ? floors.map((a) => a.FloorName)
    : DEFAULT_FLOORS;

  // DB 로봇 목록 fetch
  const fetchRobots = useCallback(() => {
    setRobotFetchError(false);
    apiFetch("/DB/robots")
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => setDbRobots(data))
      .catch((err) => {
        console.error("로봇 목록 DB 조회 실패", err);
        setRobotFetchError(true);
      });
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    fetchRobots();
  }, [isOpen, fetchRobots]);

  // 모달 열릴 때 초기화
  useEffect(() => {
    if (!isOpen) return;
    if (isEdit && editData) {
      setRobotNo(editData.robotName);
      setFloor(editData.floor);
      setName(editData.name);
      // ROS yaw(라디안) → 방향 도(로봇 yaw 와 동일: 0=동·반시계)
      setDirection(String(degFromYaw(editData.yaw)));
      setCategory(editData.category || "waypoint");
      setDesc(editData.desc);
    } else {
      setRobotNo(defaultRobotName);
      setFloor(defaultFloor);
      setName("");
      if (lockCoords && defaultYaw !== undefined) {
        setDirection(String(degFromYaw(defaultYaw)));
      } else {
        setDirection("");
      }
      setCategory(defaultCategory ?? "waypoint");
      setDesc("");
    }
    setFieldErrors({});
  }, [isOpen, defaultRobotName, defaultFloor, defaultCategory, isEdit, editData]);

  // ESC / 오버레이 클릭 닫기
  useModalBehavior({ isOpen, onClose, disabled: isSubmitting });

  // 장소명 중복 검사 — 서버는 경로가 장소를 '이름'으로 찾는 탓에 사업장 내 중복을 409 로 막는다.
  // 층·맵이 달라도 막히므로 저장 전에 여기서 알린다.
  const takenNames = useMemo(() => {
    const own = isEdit && editData ? editData.name.trim() : null;
    return new Set(usedNames.map((n) => n.trim()).filter((n) => n && n !== own));
  }, [usedNames, isEdit, editData]);
  const isDuplicateName = !!name.trim() && takenNames.has(name.trim());
  // 충전소(생성 버튼으로 연 경우·기존 충전소 수정)는 타입을 바꾸지 못한다 — 접근점 "<이름>-N" 과 초기 위치가 엮여 있다.
  const isChargeLocked = category === "charge";

  // 필수 필드 검증
  // 노드망: 충전소는 노드에 반드시 붙어야 한다 — '노드 없이 저장' 은 막는다
  const nodeUnlinked = !isEdit && nodeAttach != null && nodeAttach.nodeKey == null;
  const nodeBlocked = nodeUnlinked && category === "charge" && nodeChoice === "none";
  const canSave = !!(robotNo && floor && name.trim() && (direction || direction === "0")) && !isDuplicateName && !nodeBlocked;

  // Enter 키 저장
  useEffect(() => {
    if (!isOpen) return;
    const onEnter = (e: KeyboardEvent) => {
      if (e.key !== "Enter") return;
      const tag = (e.target as HTMLElement)?.tagName?.toLowerCase();
      if (tag === "textarea") return;
      if (isSubmitting || !canSave) return;
      e.preventDefault();
      submit();
    };
    document.addEventListener("keydown", onEnter);
    return () => document.removeEventListener("keydown", onEnter);
  }, [isOpen, isSubmitting, canSave, robotNo, floor, name, direction]);


  const submit = async () => {
    const errors: typeof fieldErrors = {};
    if (!robotNo) errors.robotNo = "로봇명을 선택해 주세요.";
    if (!floor) errors.floor = "층을 선택해 주세요.";
    if (!name.trim()) errors.name = "장소명을 입력해 주세요.";
    else if (isDuplicateName) errors.name = DUPLICATE_NAME_MSG;
    if (!direction && direction !== "0") errors.direction = "방향을 입력해 주세요.";
    else {
      const dirNum = Number(direction);
      if (!Number.isFinite(dirNum) || dirNum < 0 || dirNum > 360)
        errors.direction = "방향은 0~360 범위여야 합니다.";
    }

    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;

    const matchedFloor = floors.find((a) => a.FloorName === floor);
    // 방향 칸은 정수 도라, 손대지 않았는데 다시 변환하면 원래 yaw 가 최대 0.5° 틀어진다.
    // 표시값이 처음 그대로면 원래 yaw(수정 대상 또는 로봇 현재 방향)를 그대로 쓴다.
    const baseYaw = isEdit && editData ? editData.yaw : lockCoords ? defaultYaw : undefined;
    const yaw = baseYaw !== undefined && direction === String(degFromYaw(baseYaw))
      ? baseYaw
      : yawFromDeg(Number(direction));
    const place: PendingPlace = {
      tempId: isEdit && editData ? editData.key : `pending_${Date.now()}`,
      RobotName: robotNo,
      LacationName: name.trim(),
      FloorId: matchedFloor?.id ?? null,
      LocationX: isEdit && editData ? editData.x : Number(worldX.toFixed(3)),
      LocationY: isEdit && editData ? editData.y : Number(worldY.toFixed(3)),
      Yaw: yaw,
      MapId: mapId ?? editData?.mapId ?? null,
      Category: category,
      Imformation: desc || null,
    };
    const oldName = isEdit && editData && editData.name !== name.trim() ? editData.name : undefined;
    onConfirm(place, oldName);
    onAfterConfirm?.(place);
    onClose();
  };

  if (!isOpen) return null;

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
        {/* ─── 헤더 ─── */}
        <div className={styles.header}>
          <div className={styles.headerLeft}>
            <MapPin size={20} />
            <h2>{isEdit ? "장소 수정" : lockCoords ? "현 위치에서 장소 등록" : "장소 등록"}</h2>
          </div>
          <button className={styles.closeBtn} onClick={onClose} aria-label="close">
            &#10005;
          </button>
        </div>

        {/* ─── 본문 ─── */}
        <div className={styles.body}>
          {/* 기본 정보 */}
          <div className={styles.sectionGroup}>
            <div className={styles.sectionTitle}>
              기본 정보<span className={styles.sectionTitleLine} />
            </div>

            {/* 로봇명 */}
            <div className={styles.row}>
              <div className={`${styles.label} ${styles.required}`}>로봇명</div>
              <div className={styles.selectWrap}>
                <DropdownSelect<DBRobot>
                  placeholder="로봇명을 선택하세요"
                  value={dbRobots.find((r) => r.RobotName === robotNo) ?? null}
                  options={dbRobots}
                  getLabel={(r) => r.RobotName}
                  getKey={(r) => r.id}
                  onChange={(r) => {
                    setRobotNo(r.RobotName);
                    setFieldErrors((prev) => ({ ...prev, robotNo: undefined }));
                  }}
                  emptyMessage={
                    robotFetchError
                      ? "불러오기 실패"
                      : "등록된 로봇이 없습니다"
                  }
                  className={styles.modalSelect}
                />
                {fieldErrors.robotNo && <div className={styles.fieldError}>{fieldErrors.robotNo}</div>}
              </div>
            </div>

            {/* 층 */}
            <div className={styles.row}>
              <div className={`${styles.label} ${styles.required}`}>층</div>
              <div className={styles.selectWrap}>
                <DropdownSelect<string>
                  placeholder="층을 선택하세요"
                  value={floor || null}
                  options={floorOptions}
                  getLabel={(f) => f}
                  getKey={(f) => f}
                  onChange={(f) => {
                    setFloor(f);
                    setFieldErrors((prev) => ({ ...prev, floor: undefined }));
                  }}
                  className={styles.modalSelect}
                />
                {fieldErrors.floor && <div className={styles.fieldError}>{fieldErrors.floor}</div>}
              </div>
            </div>

            {/* 장소명 */}
            <div className={styles.row}>
              <div className={`${styles.label} ${styles.required}`}>장소명</div>
              <div className={styles.inputWrap}>
                <input
                  className={`${styles.input} ${styles.edit} ${fieldErrors.name || isDuplicateName ? styles.inputError : ""}`}
                  value={name}
                  onChange={(e) => {
                    setName(e.target.value);
                    setFieldErrors((prev) => ({ ...prev, name: undefined }));
                  }}
                  placeholder="50자 이내로 작성하세요"
                  maxLength={50}
                  autoFocus
                />
                <div className={`${styles.charCounter} ${name.length >= 40 ? styles.charCounterWarn : ""}`}>
                  {name.length}/50
                </div>
                {(fieldErrors.name || isDuplicateName) && (
                  <div className={styles.fieldError}>{fieldErrors.name ?? DUPLICATE_NAME_MSG}</div>
                )}
              </div>
            </div>

            {/* 장소 타입 */}
            <div className={styles.row}>
              <div className={styles.label}>장소 타입</div>
              <div className={styles.selectWrap}>
                <DropdownSelect<typeof CATEGORY_OPTIONS[number]>
                  placeholder="장소 타입을 선택하세요"
                  value={isChargeLocked ? CHARGE_OPTION : CATEGORY_OPTIONS.find((o) => o.value === category) ?? null}
                  options={CATEGORY_OPTIONS}
                  getLabel={(o) => o.label}
                  getKey={(o) => o.value}
                  onChange={(o) => setCategory(o.value)}
                  disabled={isChargeLocked}
                  className={styles.modalSelect}
                />
                {!isChargeLocked && (
                  <div className={styles.fieldHint}>충전소는 상단 [충전소 생성]으로 로봇이 충전 중일 때 등록합니다.</div>
                )}
              </div>
            </div>
          </div>

          {/* 위치 정보 */}
          <div className={styles.sectionGroup}>
            <div className={styles.sectionTitle}>
              위치 정보<span className={styles.sectionTitleLine} />
            </div>

            {/* 좌표 표시 (월드 + 픽셀) */}
            <div className={`${styles.row} ${styles.rowAlignTop}`}>
              <div className={styles.label}>좌표</div>
              <div className={styles.coordInfo}>
                {/* 월드 좌표 */}
                <div className={styles.coordRow}>
                  <span className={styles.coordTag}>World</span>
                  <div className={styles.coordValues}>
                    <div className={styles.xyInlineItem}>
                      <div className={styles.xyLabel}>X</div>
                      <input
                        className={`${styles.xyInput} ${styles.xyInputFilled}`}
                        value={worldX.toFixed(3)}
                        readOnly
                        tabIndex={-1}
                      />
                    </div>
                    <div className={styles.xyInlineItem}>
                      <div className={styles.xyLabel}>Y</div>
                      <input
                        className={`${styles.xyInput} ${styles.xyInputFilled}`}
                        value={worldY.toFixed(3)}
                        readOnly
                        tabIndex={-1}
                      />
                    </div>
                  </div>
                </div>
                {/* 픽셀 좌표 */}
                <div className={styles.coordRow}>
                  <span className={styles.coordTag}>Pixel</span>
                  <div className={styles.coordValues}>
                    <div className={styles.xyInlineItem}>
                      <div className={styles.xyLabel}>X</div>
                      <input
                        className={`${styles.xyInput} ${styles.xyInputEmpty}`}
                        value={Math.round(pixelX)}
                        readOnly
                        tabIndex={-1}
                      />
                    </div>
                    <div className={styles.xyInlineItem}>
                      <div className={styles.xyLabel}>Y</div>
                      <input
                        className={`${styles.xyInput} ${styles.xyInputEmpty}`}
                        value={Math.round(pixelY)}
                        readOnly
                        tabIndex={-1}
                      />
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {/* 방향 — DirectionPicker 공통(다이얼 포인팅 + 현재 로봇 방향, 숫자 부차) */}
            <DirectionPicker
              value={direction}
              onChange={(d) => {
                setDirection(d);
                setFieldErrors((prev) => ({ ...prev, direction: undefined }));
              }}
              liveRobotYawRad={defaultYaw ?? null}
              disabled={lockCoords}
              error={fieldErrors.direction}
            />
          </div>

          {/* 노드망 연결 (2026-10-19) */}
          {!isEdit && nodeAttach !== undefined && (
            <div className={styles.sectionGroup}>
              <div className={styles.sectionTitle}>
                노드망 연결<span className={styles.sectionTitleLine} />
              </div>
              {nodeAttach === null ? (
                <div className={styles.fieldHint}>노드망 연결을 확인하는 중…</div>
              ) : nodeAttach.nodeKey ? (
                <div style={{ fontSize: "var(--font-size-sm)", color: "var(--color-success)" }}>
                  N{nodeAttach.nodeKey}에 붙습니다({nodeAttach.distM?.toFixed(1)} m). 이 장소로 이동할 수 있습니다.
                </div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  <div role="alert" style={{ display: "flex", gap: 8, padding: "8px 10px", borderRadius: 8, background: "rgba(169,110,58,0.3)", border: "1px solid #A96E3A" }}>
                    <span aria-hidden="true" style={{ color: "#FFC635", fontWeight: 700 }}>!</span>
                    <span style={{ fontSize: "var(--font-size-sm)", color: "#ffe2a8" }}>
                      이 위치는 노드망과 연결되지 않습니다 — {nodeAttach.reason}
                      {nodeAttach.nearestKey ? ` (가장 가까운 N${nodeAttach.nearestKey}: ${nodeAttach.nearestM?.toFixed(1)} m)` : ""}.
                    </span>
                  </div>
                  {([
                    ["auto", "이 자리에 노드 자동 추가 (권장)", "장소 자리에 노드를 놓습니다. 3 m 안의 보이는 노드와 자동으로 이어집니다."],
                    ["manual", "직접 노드 찍기", "장소를 반영하고 노드 추가 도구로 바꿉니다. 장소 2 m 안, 벽에 막히지 않는 곳에 놓으세요."],
                    ["none", "노드 없이 저장", category === "charge"
                      ? "충전소는 노드망에 반드시 연결해야 해서 고를 수 없습니다."
                      : "이 장소로는 이동 명령이 거부되고 장소 목록에 '미연결'로 남습니다."],
                  ] as [NodeChoice, string, string][]).map(([v, title, desc]) => {
                    const disabled = v === "none" && category === "charge";
                    const on = nodeChoice === v;
                    return (
                      <label key={v} style={{
                        display: "flex", gap: 10, alignItems: "flex-start", padding: "8px 10px", borderRadius: 8,
                        border: `1px solid ${on ? "var(--color-info-border)" : "var(--border-subtle)"}`,
                        background: on ? "rgba(25,97,137,0.35)" : "transparent",
                        opacity: disabled ? 0.5 : 1, cursor: disabled ? "not-allowed" : "pointer",
                      }}>
                        <input type="radio" name="nodeChoice" checked={on} disabled={disabled}
                          onChange={() => onNodeChoice?.(v)} style={{ marginTop: 3 }} />
                        <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                          <span style={{ fontSize: "var(--font-size-sm)", fontWeight: 600, color: "var(--text-primary)" }}>{title}</span>
                          <span style={{ fontSize: "var(--font-size-xs)", color: "var(--text-tertiary)" }}>{desc}</span>
                        </span>
                      </label>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {/* 장소설명 */}
          <div className={styles.sectionGroup}>
            <div className={`${styles.row} ${styles.rowTextArea}`} style={{ marginBottom: 0 }}>
              <div className={styles.label}>장소설명</div>
              <div className={styles.textareaWrap}>
                <textarea
                  className={`${styles.textarea} ${styles.edit}`}
                  value={desc}
                  onChange={(e) => setDesc(e.target.value)}
                  placeholder="100자 이내로 작성하세요"
                  maxLength={100}
                />
                <div className={`${styles.charCounter} ${desc.length >= 90 ? styles.charCounterWarn : ""}`}>
                  {desc.length}/100
                </div>
              </div>
            </div>
          </div>
        </div>

        {isEdit && (
          <div className={styles.footerHint}>
            확인 후 상단 [저장]을 눌러야 서버에 반영됩니다. 여기서 방향을 바꾸면 장소를 끌어 옮겨도 그 방향이 유지됩니다.
          </div>
        )}

        {/* ─── 하단 버튼 ─── */}
        <div className={styles.footer}>
          <button
            className={`${styles.footerBtn} ${styles.btnRed}`}
            onClick={onClose}
          >
            <img src="/icon/close_btn.png" alt="" />
            취소
          </button>
          <button
            className={`${styles.footerBtn} ${styles.btnBlue}`}
            onClick={submit}
            disabled={!canSave}
            title={!canSave ? "모든 필수 항목을 입력해 주세요." : undefined}
          >
            <img src="/icon/check.png" alt="" />
            확인
          </button>
        </div>
      </div>
    </div>
  );
}
