"use client";

import React from "react";
import { MapPin, BatteryCharging, Home, Navigation, Crosshair, Layers } from "lucide-react";
import type { POIItem, POICategory } from "./types";
import { isMovableCategory } from "@/app/lib/placeFilter";
import styles from "./POIDetailCard.module.css";

const CATEGORY_META: Record<POICategory, { label: string; color: string; icon: typeof MapPin }> = {
  work:     { label: "작업지",   color: "#ff6b6b", icon: MapPin },
  charge:   { label: "충전소",   color: "#4caf50", icon: BatteryCharging },
  standby:  { label: "대기소",   color: "#9c7cfa", icon: Home },
  waypoint: { label: "경유지",   color: "#64b4ff", icon: Navigation },
};

type Props = {
  poi: POIItem;
  screenX?: number;
  screenY?: number;
  /**
   * 카드 위치를 바깥 래퍼에서 지정하는 경우 사용.
   * - false/undefined: 기존 동작(screenX/Y에 absolute + 말풍선 transform)
   * - true: 카드 위치를 상위 컨테이너에 맡기고 내부 transform/absolute 해제
   */
  anchored?: boolean;
  onClose: () => void;
  onNavigate?: (poi: POIItem) => void;
  /** 노드망(2026-10-19): 이 장소로 갈 수 없는 이유 — 있으면 '장소 이동' 을 막고 이유를 보인다 */
  blockedReason?: string | null;
};

export default function POIDetailCard({ poi, screenX = 0, screenY = 0, anchored = false, onClose, onNavigate, blockedReason }: Props) {
  const cat = poi.category ?? "work";
  const meta = CATEGORY_META[cat];

  const cardStyle: React.CSSProperties = anchored
    ? { position: "relative", transform: "none" }
    : { left: screenX, top: screenY };

  return (
    <div className={styles.card} style={cardStyle}>
      <button className={styles.closeBtn} onClick={onClose}>✕</button>

      <div className={styles.header}>
        <span className={styles.name}>{poi.name}</span>
        <span className={styles.badge} style={{ background: meta.color }}>
          {meta.label}
        </span>
      </div>

      <div className={styles.infoRow}>
        <Crosshair size={12} />
        X: {poi.x.toFixed(2)}, Y: {poi.y.toFixed(2)}
      </div>

      <div className={styles.infoRow}>
        <Layers size={12} />
        {poi.floor}
      </div>

      {/* 위험구역(폴리곤 centroid)·충전소로는 보내지 않는다 — 규칙은 placeFilter 단일 출처(D-01) */}
      {onNavigate && isMovableCategory(poi.category) && (blockedReason ? (
        <>
          <button className={styles.navigateBtn} disabled aria-describedby={`poi-block-${poi.id}`}
            style={{ opacity: 0.45, cursor: "not-allowed" }}>
            이동 불가
          </button>
          <div id={`poi-block-${poi.id}`} style={{ marginTop: 6, fontSize: 11, lineHeight: 1.45, color: "#ffd0d0" }}>
            {blockedReason}
          </div>
          <a href="/mapManagement?tab=map" style={{ display: "inline-block", marginTop: 4, fontSize: 11, fontWeight: 600, color: "var(--color-accent)" }}>
            맵 편집에서 노드 추가 →
          </a>
        </>
      ) : (
        <button
          className={styles.navigateBtn}
          onClick={() => onNavigate(poi)}
        >
          장소 이동
        </button>
      ))}
    </div>
  );
}
