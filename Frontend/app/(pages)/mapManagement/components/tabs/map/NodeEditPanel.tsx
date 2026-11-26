"use client";

import React, { useEffect, useState } from "react";
import { apiFetch } from "@/app/lib/api";
import type { EdgeDir, NavCheck, NavEdge, NavMigration, NavNode, RoutePreview } from "../../../hooks/useNavNodes";

/**
 * 노드 편집 플로팅 패널 — map 탭 오른쪽(경로 생성 패널과 같은 자리·모양).
 * 도구 선택 + 점검(이동 가능 여부) + 선택 노드 정보 + 경로 미리보기 + 저장.
 */

export type NodeTool = "select" | "add" | "link" | "door" | "delete";

const TOOLS: { id: NodeTool; label: string; hint: string }[] = [
  { id: "select", label: "선택", hint: "노드를 클릭하면 연결 상태를, 연결(띠)을 클릭하면 방향을 정합니다. 노드는 끌어서 옮길 수 있습니다." },
  { id: "add", label: "노드 추가", hint: "맵 빈 곳을 클릭해 노드를 놓습니다 — 길목·모퉁이·문 앞에만 두세요." },
  { id: "link", label: "연결", hint: "노드 두 개를 차례로 클릭: 이어져 있지 않으면 잇고, 이어져 있으면 끊습니다. 끊은 연결은 이 도구에서만 회색 ✕로 보입니다." },
  { id: "door", label: "문 간선", hint: "자동문 양쪽 노드를 차례로 클릭하세요(벽 검사 안 함)." },
  { id: "delete", label: "삭제", hint: "지울 노드를 클릭하세요 — 이어진 선도 함께 지워집니다." },
];

// 서버 /door 응답(map/door.py _door_dict) — 이름 칸은 소문자 name
interface Door { id: number; name: string; mapId: number | null }
interface PlaceOpt { id: number; name: string; x: number; y: number }

interface Props {
  isOpen: boolean;
  /** 편집 중인 맵과 그 맵의 사업장 — 자동문 목록을 이 기준으로 불러온다 */
  mapId: number | "";
  businessId: number | null;
  tool: NodeTool;
  setTool: (t: NodeTool) => void;
  nodes: NavNode[];
  edges: NavEdge[];
  check: NavCheck | null;
  error: string | null;
  dirty: boolean;
  saving: boolean;
  selectedKey: string | null;
  linkFromKey: string | null;
  doorPair: [string, string] | null;
  places: PlaceOpt[];
  robotPos: { x: number; y: number } | null;
  selectedEdge: [string, string] | null;
  chargerNodes: string[];
  migration: NavMigration | null;
  onSelectEdge: (edge: [string, string] | null) => void;
  onSetDirection: (a: string, b: string, dir: EdgeDir) => void;
  onBlockEdge: (a: string, b: string) => void;
  onConfirmDoor: (doorId: number, direction: "both" | "forward") => void;
  onCancelDoor: () => void;
  onPreview: (fromX: number, fromY: number, toPlaceId: number) => Promise<RoutePreview>;
  onPreviewShown: (p: { keys: string[]; skipped: string[]; start: { x: number; y: number }; goal: { x: number; y: number } } | null) => void;
  onReset: () => void;
  onSave: () => void;
  onClose: () => void;
}

const sectionTitle: React.CSSProperties = {
  fontSize: "var(--font-size-xs)", color: "var(--text-tertiary)", marginBottom: 6, fontWeight: 600,
  display: "flex", justifyContent: "space-between", alignItems: "center",
};
const selectStyle: React.CSSProperties = {
  width: "100%", height: 32, borderRadius: 6, border: "1px solid var(--border-input)",
  background: "var(--surface-input)", color: "var(--text-primary)", padding: "0 8px", fontSize: "var(--font-size-sm)",
};

// 연결 종류(자동·수동)는 화면에 드러내지 않는다 — 문만 알린다
const EDGE_KIND_LABEL: Record<NavEdge["kind"], string> = {
  auto: "", manual: "", door: "자동문 통과", excluded: "",
};

export function nodeLabel(key: string): string {
  return key.startsWith("new-") ? `새 노드 ${key.slice(4)}` : `N${key}`;
}

export default function NodeEditPanel(p: Props) {
  const [doors, setDoors] = useState<Door[]>([]);
  const [doorId, setDoorId] = useState<number | "">("");
  const [doorDir, setDoorDir] = useState<"both" | "forward">("both");
  const [from, setFrom] = useState<string>("robot");
  const [to, setTo] = useState<number | "">("");
  const [result, setResult] = useState<RoutePreview | null>(null);

  useEffect(() => {
    if (!p.isOpen || p.businessId == null) { setDoors([]); return; }
    // 다른 맵의 자동문은 서버가 문 간선으로 받지 않으므로(nav_nodes._validate_door_edge) 이 맵 것만 보인다
    apiFetch(`/door?business_id=${p.businessId}`).then((r) => r.json())
      .then((d: Door[]) => setDoors(Array.isArray(d) ? d.filter((x) => x.mapId == null || x.mapId === p.mapId) : []))
      .catch(() => setDoors([]));
  }, [p.isOpen, p.businessId, p.mapId]);

  if (!p.isOpen) return null;

  const hint = p.tool === "link" && p.linkFromKey
    ? `${nodeLabel(p.linkFromKey)} 선택됨 — 이을(또는 연결을 뺄) 두 번째 노드를 클릭하세요.`
    : p.tool === "door" && p.linkFromKey
      ? `${nodeLabel(p.linkFromKey)} 선택됨 — 문 건너편 노드를 클릭하세요.`
      : TOOLS.find((t) => t.id === p.tool)!.hint;

  const sel = p.selectedKey ? p.nodes.find((n) => n.key === p.selectedKey) : null;
  const neighbors = sel ? p.edges.filter((e) => e.kind !== "excluded" && (e.a === sel.key || e.b === sel.key)) : [];
  const attachedPlaces = sel && p.check
    ? p.places.filter((pl) => p.check!.placeAttach[String(pl.id)] === sel.key).map((pl) => pl.name)
    : [];
  const blocks = (p.check?.issues ?? []).filter((i) => i.level === "block");
  const warns = (p.check?.issues ?? []).filter((i) => i.level === "warn");
  const unlinkedNames = p.places.filter((pl) => p.check?.unlinkedPlaceIds.includes(pl.id)).map((pl) => pl.name);

  // 선택한 간선 — 저장 순서(a→b)가 한쪽 방향의 기준
  const sameEdge = (e: NavEdge, a: string, b: string) => (e.a === a && e.b === b) || (e.a === b && e.b === a);
  const [ea, eb] = p.selectedEdge ?? ["", ""];
  const selEdge = p.selectedEdge ? p.edges.find((e) => e.kind !== "excluded" && sameEdge(e, ea, eb)) : undefined;
  const curDir: EdgeDir | null = !selEdge ? null
    : selEdge.direction !== "forward" ? "both" : selEdge.a === ea ? "ab" : "ba";
  const chargerLocked = !!selEdge && (p.chargerNodes.includes(selEdge.a) || p.chargerNodes.includes(selEdge.b));
  const edgeKindLabel = selEdge ? EDGE_KIND_LABEL[selEdge.kind] : "";
  const oneWayNow = p.edges.filter((e) => e.kind !== "excluded" && e.direction === "forward");
  const dirBtn = (on: boolean, disabled: boolean): React.CSSProperties => ({
    height: 30, borderRadius: 6, cursor: disabled ? "not-allowed" : "pointer", fontSize: "var(--font-size-sm)",
    border: `1px solid ${on ? "var(--color-info-border)" : "var(--border-input)"}`,
    background: on ? "var(--color-info-bg)" : "var(--surface-5)", color: "var(--text-primary)",
    fontWeight: on ? 600 : 400, opacity: disabled ? 0.4 : 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
  });

  const runPreview = async () => {
    if (to === "") return;
    const start = from === "robot" ? p.robotPos : p.places.find((pl) => String(pl.id) === from) ?? null;
    if (!start) { setResult({ ok: false, msg: "로봇 위치를 알 수 없습니다. 로봇을 연결하거나 출발 장소를 고르세요." }); return; }
    const goal = p.places.find((pl) => pl.id === to)!;
    const r = await p.onPreview(start.x, start.y, to);
    setResult(r);
    if (r.ok) {
      const sent = (r.sent ?? []).map(String);
      p.onPreviewShown({ keys: sent, skipped: (r.nodePath ?? []).map(String).filter((k) => !sent.includes(k)),
        start: { x: start.x, y: start.y }, goal: { x: goal.x, y: goal.y } });
    } else {
      p.onPreviewShown(null);
    }
  };

  return (
    <div style={{
      position: "absolute", top: 0, right: 0, bottom: 0, zIndex: 15, width: 300,
      background: "var(--surface-3)", borderLeft: "1px solid rgba(255,255,255,0.08)",
      boxShadow: "-4px 0 24px rgba(0,0,0,0.3)", display: "flex", flexDirection: "column",
    }}>
      <div style={{
        padding: "14px 16px 10px", borderBottom: "1px solid rgba(255,255,255,0.06)",
        display: "flex", alignItems: "center", justifyContent: "space-between", flexShrink: 0,
      }}>
        <span style={{ fontSize: "var(--font-size-lg)", fontWeight: 600, color: "var(--text-primary)" }}>노드 편집</span>
        <button onClick={p.onClose} aria-label="닫기"
          style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 18, lineHeight: 1 }}>×</button>
      </div>

      <div style={{ padding: "12px 16px", display: "flex", flexDirection: "column", gap: 14, overflowY: "auto", flex: 1, minHeight: 0 }}>
        {/* 도구 */}
        <div>
          <div style={sectionTitle}><span>도구</span></div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 4 }}>
            {TOOLS.map((t) => {
              const on = p.tool === t.id;
              return (
                <button key={t.id} onClick={() => p.setTool(t.id)} aria-pressed={on}
                  style={{
                    height: 30, borderRadius: 6, cursor: "pointer", fontSize: "var(--font-size-sm)",
                    border: `1px solid ${on ? "var(--color-info-border)" : "var(--border-input)"}`,
                    background: on ? "var(--color-info-bg)" : "var(--surface-5)",
                    color: "var(--text-primary)", fontWeight: on ? 600 : 400,
                  }}>{t.label}</button>
              );
            })}
          </div>
          <div style={{ marginTop: 8, fontSize: "var(--font-size-sm)", color: "var(--text-secondary)", lineHeight: 1.45 }}>{hint}</div>
        </div>

        {/* 문 간선 확정 */}
        {p.doorPair && (
          <div style={{ background: "var(--surface-4)", border: "1px solid #C2185B", borderRadius: 8, padding: 10, display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ fontSize: "var(--font-size-sm)", fontWeight: 600, color: "var(--text-primary)" }}>
              문 간선 {nodeLabel(p.doorPair[0])} ↔ {nodeLabel(p.doorPair[1])}
            </div>
            {doors.length === 0 ? (
              <div style={{ fontSize: "var(--font-size-sm)", color: "var(--color-error-soft)" }}>
                이 맵에 등록된 자동문이 없습니다. 시설 장치 탭에서 먼저 자동문과 문 앞 장소를 등록하세요.
              </div>
            ) : (
              <>
                <select value={doorId} onChange={(e) => setDoorId(e.target.value ? Number(e.target.value) : "")} style={selectStyle} aria-label="연결할 자동문">
                  <option value="">연결할 자동문 선택</option>
                  {doors.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                </select>
                <select value={doorDir} onChange={(e) => setDoorDir(e.target.value as "both" | "forward")} style={selectStyle} aria-label="통과 방향">
                  <option value="both">양방향 통과</option>
                  <option value="forward">한쪽만 ({nodeLabel(p.doorPair[0])} → {nodeLabel(p.doorPair[1])})</option>
                </select>
              </>
            )}
            <div style={{ fontSize: "var(--font-size-xs)", color: "var(--text-tertiary)" }}>벽 검사를 하지 않고, 경로 단순화에서 문 양쪽 노드를 생략하지 않습니다.</div>
            <div style={{ display: "flex", gap: 6 }}>
              <button onClick={p.onCancelDoor} style={{ flex: 1, height: 30, borderRadius: 6, border: "1px solid var(--border-input)", background: "var(--surface-5)", color: "var(--text-primary)", cursor: "pointer" }}>취소</button>
              <button onClick={() => doorId !== "" && p.onConfirmDoor(doorId, doorDir)} disabled={doorId === ""}
                style={{ flex: 1, height: 30, borderRadius: 6, border: "1px solid var(--color-info-border)", background: "var(--color-info-bg)", color: "var(--text-primary)", cursor: "pointer", opacity: doorId === "" ? 0.5 : 1 }}>만들기</button>
            </div>
          </div>
        )}

        {/* 선택 간선 — 방향 */}
        {selEdge && (
          <div style={{ background: "var(--surface-4)", border: "1px solid #FFB300", borderRadius: 8, padding: 10, display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span style={{ fontSize: "var(--font-size-sm)", fontWeight: 600, color: "var(--text-primary)" }}>
                연결 {nodeLabel(ea)} – {nodeLabel(eb)}
              </span>
              <span style={{ fontSize: "var(--font-size-xs)", color: "var(--text-tertiary)" }}>{edgeKindLabel}</span>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 4 }}>
              <button style={{ ...dirBtn(curDir === "both", false), gridColumn: "1 / -1" }} aria-pressed={curDir === "both"}
                onClick={() => p.onSetDirection(ea, eb, "both")}>양방향 ↔</button>
              <button style={dirBtn(curDir === "ab", chargerLocked)} aria-pressed={curDir === "ab"} disabled={chargerLocked}
                onClick={() => p.onSetDirection(ea, eb, "ab")}>{nodeLabel(ea)} → {nodeLabel(eb)}</button>
              <button style={dirBtn(curDir === "ba", chargerLocked)} aria-pressed={curDir === "ba"} disabled={chargerLocked}
                onClick={() => p.onSetDirection(ea, eb, "ba")}>{nodeLabel(eb)} → {nodeLabel(ea)}</button>
            </div>
            {chargerLocked && (
              <div style={{ fontSize: "var(--font-size-xs)", color: "#ffe2a8" }}>
                충전소 노드에 붙은 연결은 양방향만 됩니다 — 출도킹·복귀 통로입니다.
              </div>
            )}
            <div style={{ fontSize: "var(--font-size-xs)", color: "var(--text-tertiary)", lineHeight: 1.45 }}>
              한쪽 방향은 문·출입구 분리·경사로처럼 꼭 필요한 곳에만 쓰세요. 돌아올 수 없는 장소가 생기면 저장되지 않습니다.
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <button onClick={() => p.onSelectEdge(null)}
                style={{ flex: 1, height: 30, borderRadius: 6, border: "1px solid var(--border-input)", background: "var(--surface-5)", color: "var(--text-primary)", cursor: "pointer" }}>닫기</button>
              <button onClick={() => { p.onBlockEdge(ea, eb); p.onSelectEdge(null); }}
                style={{ flex: 1, height: 30, borderRadius: 6, border: "1px solid rgba(229,57,53,0.6)", background: "rgba(148,22,33,0.35)", color: "#ffd0d0", cursor: "pointer" }}>
                연결 삭제
              </button>
            </div>
          </div>
        )}

        {/* 점검 */}
        <div>
          <div style={sectionTitle}>
            <span>이 맵에서 이동</span>
            <span style={{
              padding: "1px 8px", borderRadius: 10, fontSize: 11, fontWeight: 700,
              background: p.check?.ready ? "var(--color-success-bg)" : "var(--color-error-bg)", color: "#fff",
            }}>{p.check?.ready ? "가능" : "불가"}</span>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {blocks.map((i, k) => (
              <div key={`b${k}`} style={{ fontSize: "var(--font-size-sm)", color: "#ffd0d0", background: "rgba(148,22,33,0.35)", borderRadius: 6, padding: "6px 8px" }}>{i.text}</div>
            ))}
            {warns.map((i, k) => (
              <div key={`w${k}`} style={{ fontSize: "var(--font-size-sm)", color: "#ffe2a8", background: "rgba(169,110,58,0.28)", borderRadius: 6, padding: "6px 8px" }}>{i.text}</div>
            ))}
            {unlinkedNames.length > 0 && (
              <div style={{ fontSize: "var(--font-size-sm)", color: "#ffe2a8", background: "rgba(169,110,58,0.28)", borderRadius: 6, padding: "6px 8px" }}>
                노드에 안 붙은 장소 {unlinkedNames.length}곳: {unlinkedNames.join(", ")} — 이 장소로는 이동할 수 없습니다.
              </div>
            )}
            {p.check && blocks.length + warns.length + unlinkedNames.length === 0 && (
              <div style={{ fontSize: "var(--font-size-sm)", color: "var(--color-success)" }}>모든 장소가 노드망에 연결되어 있습니다.</div>
            )}
            <div style={{ fontSize: "var(--font-size-xs)", color: "var(--text-tertiary)" }}>
              노드 {p.nodes.length}개 · 연결 {p.edges.filter((e) => e.kind !== "excluded").length}개
            </div>
            <div aria-label="범례" style={{ display: "flex", flexDirection: "column", gap: 3, marginTop: 4, fontSize: "var(--font-size-xs)", color: "var(--text-tertiary)" }}>
              <span><b style={{ color: "#4DD0E1" }}>━</b> 로봇이 다니는 길 — 확대하면 방향이 보입니다: <b style={{ color: "#4DD0E1" }}>›››/‹‹‹</b> 양방향, <b style={{ color: "#4DD0E1" }}>›››</b> 한쪽 방향</span>
              <span><b style={{ color: "#90A4AE" }}>─</b> 장소가 붙은 노드 — 선이 없는 장소로는 이동할 수 없습니다</span>
              <span><b style={{ color: "#E57373" }}>◆</b> 빨강은 고쳐야 할 곳(연결 없는 노드·충전소 한쪽 방향)</span>
            </div>
          </div>
        </div>

        {/* 한쪽 방향 목록 + 구간 이관 확인 */}
        {(oneWayNow.length > 0 || (p.migration && p.migration.oneWay.length > 0)) && (
          <div>
            <div style={sectionTitle}><span>방향</span></div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: "var(--font-size-sm)", color: "var(--text-secondary)" }}>
              <div>
                {oneWayNow.length === 0 ? "한쪽 방향 연결 없음 — 모두 양방향입니다." : <>한쪽 방향 연결 {oneWayNow.length}개:</>}
                {oneWayNow.map((e, k) => (
                  <button key={k} onClick={() => p.onSelectEdge([e.a, e.b])}
                    style={{ background: "none", border: "none", padding: "0 4px", color: "var(--color-info)", cursor: "pointer", fontSize: "inherit" }}>
                    {nodeLabel(e.a)}→{nodeLabel(e.b)}
                  </button>
                ))}
              </div>
              {p.migration && p.migration.oneWay.length > 0 && (
                <div style={{ background: "var(--surface-4)", borderRadius: 6, padding: "8px 10px", lineHeight: 1.5 }}>
                  기존 구간 {p.migration.routes}개를 옮겼습니다. 원래 한쪽 방향이던 {p.migration.oneWay.length}개는
                  노드망 안에서만 움직이므로 <b>양방향</b>으로 옮겼습니다. 꼭 한쪽이어야 하는 곳만 눌러서 다시 정하세요.
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }}>
                    {p.migration.oneWay.map((m, k) => {
                      const live = p.edges.find((e) => e.kind !== "excluded" && sameEdge(e, m.a, m.b));
                      const nowOne = live?.direction === "forward";
                      return (
                        <button key={k} onClick={() => live && p.onSelectEdge([m.a, m.b])} disabled={!live}
                          title={live ? "눌러서 방향 정하기" : "이 연결은 지금 노드망에 없습니다"}
                          style={{
                            padding: "2px 8px", borderRadius: 10, fontSize: 11, cursor: live ? "pointer" : "default",
                            border: `1px solid ${nowOne ? "var(--color-info-border)" : "var(--border-input)"}`,
                            background: nowOne ? "var(--color-info-bg)" : "var(--surface-5)", color: "var(--text-primary)",
                            opacity: live ? 1 : 0.5,
                          }}>
                          {m.from} → {m.to}{nowOne ? " · 한쪽" : ""}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* 선택 노드 */}
        {sel && (
          <div style={{ background: "var(--surface-4)", border: "1px solid var(--border-input)", borderRadius: 8, padding: 10, fontSize: "var(--font-size-sm)", color: "var(--text-secondary)", display: "flex", flexDirection: "column", gap: 4 }}>
            <div style={{ display: "flex", justifyContent: "space-between", color: "var(--text-primary)", fontWeight: 600 }}>
              <span>{nodeLabel(sel.key)}</span><span style={{ fontWeight: 400, color: "var(--text-tertiary)" }}>x {sel.x.toFixed(2)} · y {sel.y.toFixed(2)}</span>
            </div>
            <div>연결 {neighbors.length}개 · {neighbors.map((e) => nodeLabel(e.a === sel.key ? e.b : e.a)).join(", ") || "없음"}</div>
            <div>붙은 장소: {attachedPlaces.join(", ") || "없음"}</div>
          </div>
        )}

        {/* 경로 미리보기 */}
        <div>
          <div style={sectionTitle}><span>경로 미리보기 (저장된 노드망 기준)</span></div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <select value={from} onChange={(e) => setFrom(e.target.value)} style={selectStyle} aria-label="출발">
              <option value="robot">로봇 현재 위치</option>
              {p.places.map((pl) => <option key={pl.id} value={String(pl.id)}>{pl.name}</option>)}
            </select>
            <select value={to} onChange={(e) => setTo(e.target.value ? Number(e.target.value) : "")} style={selectStyle} aria-label="도착">
              <option value="">도착 장소 선택</option>
              {p.places.map((pl) => <option key={pl.id} value={pl.id}>{pl.name}</option>)}
            </select>
            <button onClick={runPreview} disabled={to === ""}
              style={{ height: 32, borderRadius: 8, border: "1px solid var(--border-input)", background: "var(--surface-5)", color: "var(--text-primary)", cursor: "pointer", opacity: to === "" ? 0.5 : 1 }}>
              경로 보기
            </button>
            {result && (
              result.ok ? (
                <div style={{ fontSize: "var(--font-size-sm)", color: "var(--text-secondary)", background: "var(--surface-4)", borderRadius: 6, padding: "8px 10px", lineHeight: 1.5 }}>
                  노드 {result.nodePath?.length ?? 0}개 경유 → 로봇에 보낼 통과 지점 {result.sent?.length ?? 0}개
                  {result.entry ? " (노드망 밖 → 진입)" : ""}<br />
                  거리 {result.lengthM?.toFixed(1)} m{result.ratio ? ` · 직선의 ${result.ratio.toFixed(1)}배` : ""}
                  {result.ratio && result.ratio > 2 ? " — 출발 전 확인 대상" : ""}
                </div>
              ) : (
                <div style={{ fontSize: "var(--font-size-sm)", color: "#ffd0d0", background: "rgba(148,22,33,0.35)", borderRadius: 6, padding: "8px 10px" }}>{result.msg}</div>
              )
            )}
          </div>
        </div>
        {p.error && <div role="alert" style={{ fontSize: "var(--font-size-sm)", color: "var(--color-error-soft)" }}>{p.error}</div>}
      </div>

      <div style={{ padding: "12px 16px", borderTop: "1px solid rgba(255,255,255,0.06)", display: "flex", gap: 8, flexShrink: 0 }}>
        <button onClick={p.onReset} disabled={!p.dirty}
          style={{ flex: 1, height: 34, borderRadius: 8, border: "1px solid var(--border-input)", background: "var(--surface-5)", color: "var(--text-primary)", fontSize: "var(--font-size-md)", cursor: "pointer", opacity: p.dirty ? 1 : 0.5 }}>되돌리기</button>
        <button onClick={p.onSave} disabled={!p.dirty || p.saving}
          style={{ flex: 1, height: 34, borderRadius: 8, border: "1px solid var(--color-info-border)", background: "var(--color-info-bg)", color: "var(--text-primary)", fontSize: "var(--font-size-md)", cursor: "pointer", opacity: !p.dirty || p.saving ? 0.5 : 1 }}>
          {p.saving ? "저장 중…" : "노드 저장"}
        </button>
      </div>
    </div>
  );
}
