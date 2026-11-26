"use client";

import React from "react";
import type { ActiveRoute, NavNetwork, NetworkLevel } from "./types";
import { ROBOT_MARKER_FILL } from "./poiStyle";
import { chevronPath } from "@/app/(pages)/mapManagement/components/tabs/map/NodeLayer";

/**
 * 대시보드 2D 지도 위 노드망·이동 경로 (2026-10-19 노드망 경로).
 *
 * 노드망은 배경이다 — 기본 '흐리게'(얇은 선 + 작은 점). 이동 중인 로봇의 경로만 흰 선 + 로봇색 테두리로
 * 강조하고, 지나온 구간은 흐린 점선, 앞으로 지날 통과 지점은 번호(지난 것은 ✓)로 보인다.
 * (10-29: 경로 선·지날 지점 번호는 꺼 두었다 — SHOW_ROUTE_LINE·SHOW_ROUTE_NODE_BADGES)
 * 기존 구간 선(주황·청록)·장소 색(빨강·초록·보라)과 겹치지 않게 흰색·파랑만 쓴다.
 * 좌표는 로봇·장소 마커와 같은 worldToPixelScreen, 선 굵기는 확대 배율(scale)로 나눠 화면 크기를 유지한다.
 */

// 노드망은 로봇이 다니는 길 — 반투명한 넓은 띠(lane) + 가운데 선. 이동 경로(흰 선 + 파란 테두리)보다는 약하게.
// 띠 폭은 실제 폭(LANE_WIDTH_M)으로 그려 확대하면 같이 넓어진다. lane 은 축소했을 때의 최소 화면 px.
const LANE_WIDTH_M = 0.8;
const ARROW_GAP_M = 1.2;     // 한쪽 방향 꺾쇠 간격(m) — 맵 편집과 같다
const SHOW_ROUTE_LINE = false;     // 이동 구간 선(지나온 점선·남은 파란 띠) — 10-22 사용자 요청으로 끔
const SHOW_ROUTE_NODE_BADGES = false;  // 지날 노드 번호 배지 — 10-29 사용자 요청으로 끔(한 바퀴 전체 순번이라 헷갈림)
const FLOOR_ARROW_GAP_M = 0.45;    // 바닥 차로 화살표 간격(m) — 도로 차선처럼 촘촘히

/** a→b 선을 왼쪽(진행 방향 기준)으로 d 만큼 평행 이동 */
function shift(a: { x: number; y: number }, b: { x: number; y: number }, d: number) {
  const L = Math.hypot(b.x - a.x, b.y - a.y) || 1;
  const nx = -(b.y - a.y) / L * d; const ny = (b.x - a.x) / L * d;
  return [{ x: a.x + nx, y: a.y + ny }, { x: b.x + nx, y: b.y + ny }] as const;
}
const LEVEL = {
  off: null,
  faint: { r: 3.5, nodeOp: 0.6, w: 2, edgeOp: 0.45, lane: 12, laneOp: 0.16 },
  strong: { r: 4.5, nodeOp: 0.95, w: 3, edgeOp: 0.8, lane: 16, laneOp: 0.28 },
} as const;

interface Props {
  network: NavNetwork | null;
  level: NetworkLevel;
  route: ActiveRoute | null;
  toScreen: (wx: number, wy: number) => { x: number; y: number };
  scale: number;
}

export default function NodeNetworkOverlay({ network, level, route, toScreen, scale }: Props) {
  const lv = LEVEL[level];
  const k = 1 / scale;
  const byId = new Map((network?.nodes ?? []).map((n) => [n.id, n]));
  const o = toScreen(0, 0); const u = toScreen(1, 0);
  const pxPerM = Math.hypot(u.x - o.x, u.y - o.y);
  const laneW = lv ? Math.max(LANE_WIDTH_M * pxPerM, lv.lane * k) : 0;

  // 경로: 출발 → (지난 지점들) → 로봇 → (남은 지점들) → 목적지
  let passed: { x: number; y: number }[] = [];
  let remain: { x: number; y: number }[] = [];
  if (route && route.points.length > 0) {
    const cur = route.points.findIndex((p) => p.status === "current");
    const curIdx = cur >= 0 ? cur : route.points.length;
    const done = route.points.slice(0, curIdx);
    passed = [route.start, ...done, ...(route.robot ? [route.robot] : [])];
    remain = [...(route.robot ? [route.robot] : [route.start]), ...route.points.slice(curIdx)];
  }
  const pts = (list: { x: number; y: number }[]) =>
    list.map((p) => toScreen(p.x, p.y)).map((p) => `${p.x},${p.y}`).join(" ");
  const goal = route?.points[route.points.length - 1];

  return (
    <svg style={{ position: "absolute", inset: 0, width: "100%", height: "100%", overflow: "visible", pointerEvents: "none" }}
      aria-hidden="true">
      {lv && network?.edges.map((e, i) => {
        if (e.kind === "excluded") return null;
        const a = byId.get(e.a); const b = byId.get(e.b);
        if (!a || !b) return null;
        const pa = toScreen(a.x, a.y); const pb = toScreen(b.x, b.y);
        return (
          <g key={`ne${i}`}>
            <line x1={pa.x} y1={pa.y} x2={pb.x} y2={pb.y} stroke="#dbe6ff" strokeWidth={laneW}
              opacity={lv.laneOp} strokeLinecap="round" />
            {/* 바닥 차로 화살표 — 노드망과 같은 색·반투명, 도로 차선처럼 촘촘히.
                한쪽 방향: 가운데 한 줄 >>>>. 양방향: 띠를 두 차로로 나눠 오른쪽 차로 >>>>, 왼쪽 차로 <<<<. */}
            <path d={(() => {
                const gap = Math.max(FLOOR_ARROW_GAP_M * pxPerM, 18 * k);
                if (e.direction === "forward") return chevronPath(pa, pb, gap, laneW * 0.9);
                const off = laneW * 0.24;    // 우측 통행 — 화면 좌표(y 아래)에서 이 법선은 가는 방향의 오른쪽이다
                const [a1, b1] = shift(pa, pb, off);
                const [a2, b2] = shift(pa, pb, -off);
                return `${chevronPath(a1, b1, gap, laneW * 0.55)} ${chevronPath(b2, a2, gap, laneW * 0.55)}`;
              })()}
              fill="none" stroke="#dbe6ff" strokeWidth={2 * k} opacity={Math.min(1, lv.edgeOp + 0.15)}
              strokeLinecap="round" strokeLinejoin="round" />
            {e.direction !== "forward" && (
              // 두 차로 사이 가운데 점선 — 양방향 길임을 한눈에
              <line x1={pa.x} y1={pa.y} x2={pb.x} y2={pb.y} stroke="#dbe6ff" strokeWidth={1 * k}
                strokeDasharray={`${6 * k} ${6 * k}`} opacity={lv.edgeOp * 0.6} />
            )}
          </g>
        );
      })}
      {lv && network?.nodes.map((n) => {
        const p = toScreen(n.x, n.y);
        return <circle key={`nn${n.id}`} cx={p.x} cy={p.y} r={lv.r * k} fill="#dbe6ff" opacity={lv.nodeOp} />;
      })}

      {/* 이동 구간 선은 따로 그리지 않는다(10-22 사용자) — 노드망 바닥 꺾쇠로 길과 방향을 보이고,
          목적지 표시만 남긴다. 지날 지점 번호도 10-29 에 껐다 */}
      {SHOW_ROUTE_LINE && passed.length >= 2 && (
        <polyline points={pts(passed)} fill="none" stroke="#ffffff" strokeWidth={2.2 * k}
          strokeDasharray={`${4 * k} ${5 * k}`} opacity={0.45} strokeLinecap="round" strokeLinejoin="round" />
      )}
      {SHOW_ROUTE_LINE && remain.length >= 2 && (
        <polyline points={pts(remain)} fill="none" stroke={ROBOT_MARKER_FILL} strokeWidth={9 * k} opacity={0.5}
          strokeLinecap="round" strokeLinejoin="round" />
      )}
      {goal && (() => {
        const g = toScreen(goal.x, goal.y);
        return <circle cx={g.x} cy={g.y} r={15 * k} fill="rgba(255,255,255,0.12)" stroke="#fff" strokeWidth={1.8 * k}
          strokeDasharray={`${4 * k} ${3 * k}`} />;
      })()}
      {SHOW_ROUTE_NODE_BADGES && route?.points.map((p, i) => {
        if (!p.node) return null;
        const s = toScreen(p.x, p.y);
        const done = p.status === "completed";
        const cur = p.status === "current";
        // 지금 보낸 묶음 밖(아직 안 보낸 점)은 흐리게 — 묶음이 없으면(단일 NAV) 모두 또렷하게
        const later = !done && !cur && route.points.some((q) => q.inBundle) && !p.inBundle;
        return (
          <g key={`rb${i}`} transform={`translate(${s.x}, ${s.y + 16 * k}) scale(${k})`} opacity={later ? 0.45 : 1}>
            <circle r={cur ? 9.5 : 8} fill={done ? "#3B415F" : cur ? ROBOT_MARKER_FILL : "#fff"}
              stroke={cur ? "#fff" : ROBOT_MARKER_FILL} strokeWidth={2} />
            <text textAnchor="middle" dominantBaseline="central" fontSize={9} fontWeight={700}
              fill={done ? "#cfd6e6" : cur ? "#fff" : "#1557B0"}>{done ? "✓" : i + 1}</text>
          </g>
        );
      })}
    </svg>
  );
}
