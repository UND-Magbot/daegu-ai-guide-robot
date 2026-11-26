"use client";

import React from "react";
import type { NavCheck, NavEdge, NavNode } from "../../../hooks/useNavNodes";

/**
 * 맵 편집 SVG 안에 그리는 노드망 레이어 (2026-10-19 노드망 경로).
 * 좌표 변환·줌 보정은 같은 SVG 의 구간·장소 마커와 같다(1/zoom 로 화면 크기 고정).
 */

export const NODE_COLOR = "#00838F";       // 노드·자동/수동 간선 — 구간(초록)·장소(빨강·초록·보라)와 겹치지 않는 청록
export const DOOR_COLOR = "#C2185B";       // 문 간선
const EXCLUDED_COLOR = "#90A4AE";         // 막은 연결 — 연결 도구에서만, 흐린 회색 ✕
const PROBLEM_COLOR = "#E53935";          // 고쳐야 저장되는 연결(충전소 한쪽 방향)·고립 노드
const ATTACH_COLOR = "#546E7A";
const ROUTE_COLOR = "#FF6B35";             // 경로 미리보기 — 기존 '경로 만들기' 선과 같은 주황
// 노드망 띠 폭(m) — 로봇 몸체 폭에 여유를 더한 '로봇이 지나가는 길'. 확대하면 같이 넓어진다.
export const LANE_WIDTH_M = 0.8;
const LANE_MIN_PX = 16;                    // 축소했을 때 띠가 너무 가늘어지지 않게(화면 px)
const ARROW_GAP_M = 0.45;                  // 한쪽 방향 꺾쇠 간격(m) — 대시보드 바닥 화살표와 같게 촘촘히
const ARROW_GAP_MIN_PX = 14;               // 꺾쇠 간격이 화면에서 이보다 좁아지면(축소) 꺾쇠를 숨기고 띠만 그린다
const SELECTED_EDGE_COLOR = "#FFB300";

/** a–b 를 법선 방향으로 d 만큼 옮긴 선분 — SVG 좌표(y 아래)에서 d>0 은 a→b 진행 방향의 오른쪽 */
function shiftSeg(a: { x: number; y: number }, b: { x: number; y: number }, d: number) {
  const L = Math.hypot(b.x - a.x, b.y - a.y) || 1;
  const nx = -(b.y - a.y) / L * d; const ny = (b.x - a.x) / L * d;
  return [{ x: a.x + nx, y: a.y + ny }, { x: b.x + nx, y: b.y + ny }] as const;
}

/** a→b 띠 위에 같은 간격으로 놓는 꺾쇠(›) 화살표 path. w 는 띠 폭(SVG 단위) */
export function chevronPath(a: { x: number; y: number }, b: { x: number; y: number }, gap: number, w: number): string {
  const dx = b.x - a.x; const dy = b.y - a.y; const L = Math.hypot(dx, dy);
  if (L < 1e-6) return "";
  const ux = dx / L; const uy = dy / L;
  const n = Math.max(1, Math.floor(L / gap));
  const h = w * 0.32;                       // 꺾쇠 반폭
  const d = w * 0.22;                       // 꺾쇠 깊이
  let out = "";
  for (let i = 0; i < n; i++) {
    const t = (i + 0.5) * (L / n);
    const cx = a.x + ux * t; const cy = a.y + uy * t;
    out += `M${cx - ux * d - uy * h},${cy - uy * d + ux * h} L${cx + ux * d},${cy + uy * d} L${cx - ux * d + uy * h},${cy - uy * d - ux * h} `;
  }
  return out;
}

type MapMeta = { resolution: number; originX: number; originY: number };

interface Props {
  nodes: NavNode[];
  edges: NavEdge[];
  check: NavCheck | null;
  places: { id: number; name: string; x: number; y: number }[];
  meta: MapMeta;
  img: { w: number; h: number };
  zoom: number;
  active: boolean;                  // 노드 도구가 켜져 있음(편집 표시)
  showExcluded?: boolean;           // 막은 연결(빨간 점선) — 연결 도구를 쓸 때만 그린다
  selectedKey: string | null;
  linkFromKey: string | null;
  routeKeys: string[];              // 미리보기 노드 경로(보내는 것)
  routeSkipped: string[];           // 노드 경로 중 직선으로 건너뛰는 것
  routeStart: { x: number; y: number } | null;
  routeGoal: { x: number; y: number } | null;
  selectedEdge?: [string, string] | null;
  onEdgeClick?: (a: string, b: string) => void;   // 선택 도구에서만 넘긴다
  onNodeClick?: (key: string, e: React.MouseEvent) => void;
  onNodeMouseDown?: (key: string, e: React.MouseEvent) => void;
}

export default function NodeLayer({
  nodes, edges, check, places, meta, img, zoom, active, showExcluded = false, selectedKey, linkFromKey,
  routeKeys, routeSkipped, routeStart, routeGoal, selectedEdge = null, onEdgeClick, onNodeClick, onNodeMouseDown,
}: Props) {
  const toSvg = (x: number, y: number) => ({
    x: (x - meta.originX) / meta.resolution - img.w / 2,
    y: img.h - (y - meta.originY) / meta.resolution - img.h / 2,
  });
  const byKey = new Map(nodes.map((n) => [n.key, n]));
  const s = 1 / zoom;
  const isolated = new Set((check?.issues ?? []).filter((i) => i.kind === "isolated").map((i) => i.nodeKey));
  // 충전소 노드에 붙은 한쪽 연결 — 저장이 안 되니 빨간 선으로 알린다. 벽을 지나는 연결은 이을 때 막으므로 그리지 않는다.
  const wallEdges = new Set((check?.issues ?? []).filter((i) => i.kind === "charger_oneway" && i.edge)
    .map((i) => [String(i.edge![0]), String(i.edge![1])].sort().join("|")));

  const laneW = Math.max(LANE_WIDTH_M / meta.resolution, LANE_MIN_PX * s);
  // 축소하면 꺾쇠가 겹쳐 지저분하다 — 간격이 화면에서 너무 좁아지면 띠만 보인다(방향은 확대해서 본다)
  const arrowGap = ARROW_GAP_M / meta.resolution;
  const showArrows = arrowGap >= ARROW_GAP_MIN_PX * s;
  const isSel = (e: NavEdge) => !!selectedEdge
    && ((e.a === selectedEdge[0] && e.b === selectedEdge[1]) || (e.a === selectedEdge[1] && e.b === selectedEdge[0]));

  const routePts = [
    ...(routeStart ? [routeStart] : []),
    ...routeKeys.map((k) => byKey.get(k)).filter(Boolean).map((n) => ({ x: n!.x, y: n!.y })),
    ...(routeGoal ? [routeGoal] : []),
  ].map((p) => toSvg(p.x, p.y));

  return (
    <g>
      {/* 장소 → 붙은 노드 (자동) */}
      {active && check && places.map((p) => {
        const k = check.placeAttach[String(p.id)];
        const n = k ? byKey.get(k) : undefined;
        if (!n) return null;
        const a = toSvg(p.x, p.y);
        const b = toSvg(n.x, n.y);
        return (
          <line key={`att_${p.id}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={ATTACH_COLOR}
            strokeWidth={2 * s} opacity={0.85} strokeLinecap="round" pointerEvents="none" />
        );
      })}

      {/* 간선 */}
      {edges.map((e, i) => {
        const na = byKey.get(e.a);
        const nb = byKey.get(e.b);
        if (!na || !nb) return null;
        const a = toSvg(na.x, na.y);
        const b = toSvg(nb.x, nb.y);
        // 노드망은 로봇이 다니는 길 — 자동·수동·문 구분 없이 반투명한 띠(차로) + 가운데 실선 한 모양.
        // 한쪽 방향은 꺾쇠, 문은 가운데 문 아이콘. 막은 연결은 연결 도구에서만 흐린 회색(✕).
        if (e.kind === "excluded" && !(active && showExcluded)) return null;
        const style = e.kind === "excluded"
          ? { stroke: EXCLUDED_COLOR, width: 2, dash: "none", op: 0.7, lane: 0 }
          : wallEdges.has([e.a, e.b].sort().join("|"))
            ? { stroke: PROBLEM_COLOR, width: 3.5, dash: "none", op: active ? 0.95 : 0.6, lane: 14 }
            : { stroke: NODE_COLOR, width: 3.5, dash: "none", op: active ? 0.95 : 0.6, lane: 14 };
        const mx = (a.x + b.x) / 2;
        const my = (a.y + b.y) / 2;
        return (
          <g key={`e_${e.a}_${e.b}_${i}`} pointerEvents="none">
            {isSel(e) && (
              <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={SELECTED_EDGE_COLOR}
                strokeWidth={laneW * (Math.max(style.lane, 14) / 14) + 6 * s} opacity={0.55} strokeLinecap="round" />
            )}
            {style.lane > 0 && (
              <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={style.stroke}
                strokeWidth={laneW * (style.lane / 14)}
                opacity={style.op * 0.28} strokeLinecap="round" />
            )}
            {e.kind !== "excluded" ? (showArrows && (
              // 가운데 선 대신 같은 색 작은 꺾쇠 줄로 흐름 방향을 보인다.
              // 한쪽 방향: 가운데 한 줄 ›››. 양방향: 띠를 두 차로로 나눠 오른쪽 차로 ›››, 왼쪽 차로 ‹‹‹(우측 통행).
              <path d={(() => {
                  const gap = arrowGap;
                  if (e.direction === "forward") return chevronPath(a, b, gap, laneW * 0.5);
                  const off = laneW * 0.22;
                  const [a1, b1] = shiftSeg(a, b, off);
                  const [a2, b2] = shiftSeg(a, b, -off);
                  return `${chevronPath(a1, b1, gap, laneW * 0.36)} ${chevronPath(b2, a2, gap, laneW * 0.36)}`;
                })()}
                fill="none" stroke={style.stroke} strokeWidth={2 * s} strokeLinecap="round" strokeLinejoin="round"
                opacity={style.op} />
            )) : (
              <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={style.stroke} strokeWidth={style.width * s}
                strokeDasharray={style.dash} opacity={style.op} strokeLinecap="round" />
            )}
            {e.kind === "excluded" && (
              <path d={`M${mx - 5 * s} ${my - 5 * s}L${mx + 5 * s} ${my + 5 * s}M${mx + 5 * s} ${my - 5 * s}L${mx - 5 * s} ${my + 5 * s}`}
                stroke={EXCLUDED_COLOR} strokeWidth={2.2 * s} strokeLinecap="round" />
            )}
            {e.kind === "door" && (
              <g transform={`translate(${mx}, ${my}) scale(${s})`}>
                <rect x={-9} y={-9} width={18} height={18} rx={4} fill="#fff" stroke={DOOR_COLOR} strokeWidth={2} />
                <path d="M-4 6V-6h7v12M1 0h.01" stroke={DOOR_COLOR} strokeWidth={1.8} fill="none" strokeLinecap="round" />
              </g>
            )}
          </g>
        );
      })}

      {/* 간선 클릭 영역(선택 도구) — 띠 폭만큼 */}
      {active && onEdgeClick && edges.map((e, i) => {
        if (e.kind === "excluded") return null;   // 숨긴 막은 연결은 고르지 않는다
        const na = byKey.get(e.a); const nb = byKey.get(e.b);
        if (!na || !nb) return null;
        const a = toSvg(na.x, na.y); const b = toSvg(nb.x, nb.y);
        return (
          <line key={`eh_${e.a}_${e.b}_${i}`} data-edge={`${e.a}|${e.b}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
            stroke="transparent" strokeWidth={laneW} style={{ cursor: "pointer" }} pointerEvents="stroke"
            onClick={(ev) => { ev.stopPropagation(); onEdgeClick(e.a, e.b); }} />
        );
      })}

      {/* 경로 미리보기 */}
      {routePts.length >= 2 && (
        <polyline points={routePts.map((p) => `${p.x},${p.y}`).join(" ")} fill="none" stroke={ROUTE_COLOR}
          strokeWidth={3.5 * s} strokeLinecap="round" strokeLinejoin="round" opacity={0.9} pointerEvents="none" />
      )}

      {/* 노드 */}
      {nodes.map((n) => {
        const p = toSvg(n.x, n.y);
        const sel = n.key === selectedKey;
        const from = n.key === linkFromKey;
        const iso = isolated.has(n.key);
        const onRoute = routeKeys.indexOf(n.key);
        const skipped = routeSkipped.includes(n.key);
        const fill = iso ? PROBLEM_COLOR : onRoute >= 0 ? ROUTE_COLOR : NODE_COLOR;
        return (
          <g key={`n_${n.key}`} data-node-key={n.key} transform={`translate(${p.x}, ${p.y}) scale(${s})`}
            style={{ cursor: active ? "pointer" : "default" }}
            onMouseDown={active && onNodeMouseDown ? (e) => onNodeMouseDown(n.key, e) : undefined}
            onClick={active && onNodeClick ? (e) => { e.stopPropagation(); onNodeClick(n.key, e); } : undefined}>
            {active && <circle r={12} fill="transparent" />}
            {(sel || from) && (
              <circle r={11} fill="none" stroke={from ? "#FFB300" : "#263238"} strokeWidth={2.2} />
            )}
            {skipped && <circle r={10} fill="none" stroke={ROUTE_COLOR} strokeWidth={1.8} strokeDasharray="3 2" />}
            <rect x={-7} y={-7} width={14} height={14} transform="rotate(45)" fill={fill}
              stroke="#fff" strokeWidth={1.6} opacity={active ? 1 : 0.6} />
            {active && (
              <text x={9} y={-8} fontSize={9} fontWeight={700} fill="#0b3d40" stroke="#fff" strokeWidth={2.6}
                paintOrder="stroke" pointerEvents="none">{n.key.startsWith("new-") ? "새" : `N${n.key}`}</text>
            )}
            {onRoute >= 0 && (
              <g transform="translate(0, 15)">
                <circle r={7} fill={ROUTE_COLOR} stroke="#fff" strokeWidth={1.4} />
                <text textAnchor="middle" dominantBaseline="central" fill="#fff" fontSize={8} fontWeight={700}>
                  {onRoute + 1}
                </text>
              </g>
            )}
          </g>
        );
      })}

    </g>
  );
}
