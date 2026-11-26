// 흐름: 장소 리스트 클릭 → selectedPlaceId state 저장 → img 경로 자동 변경 → 확인 버튼 클릭 → 선택값 검증 → 로봇 이동 이벤트 실행
'use client';

import styles from './Modal.module.css';
import React, { useEffect, useRef, useState } from 'react';
import type { RobotRowData } from '@/app/types';
import { useCustomScrollbar } from "@/app/hooks/useCustomScrollbar";
import { useModalBehavior } from '@/app/hooks/useModalBehavior';
import { apiFetch } from "@/app/lib/api";
import { isMovablePlace } from "@/app/lib/placeFilter";
import { fetchMoveBlock, type MoveBlock } from "@/app/lib/navReach";
import { useToast } from "@/app/components/common/Toast";


type WorkModalProps = {
    isOpen: boolean;
    onClose: () => void;
    selectedRobotIds: number[];
    /** 로봇 현재 층. 이 층의 장소만 후보로 둔다(D-01 · Q8) */
    robotFloorId?: number | null;
    /** 로봇 현재 맵. 알면 이 맵의 장소만 후보로 둔다(한 층에 맵이 여럿이면 좌표계가 다르다) */
    robotMapId?: number | null;
    // selectedRobotId: number | null;
    // selectedRobot: RobotRowData | null;
    // robots: RobotRowData[];
}

type Place = {
  id: number;
  name: string;
  x?: number;
  y?: number;
};

type DBPlace = {
  id: number;
  LacationName: string;
  Floor: string;
  FloorId: number | null;
  Category: string | null;
  LocationX: number;
  LocationY: number;
  Yaw: number;
  RobotName: string;
};

export default function RobotDetailModal({
    isOpen,
    onClose,
    robotFloorId,
    robotMapId,
}:WorkModalProps ){


    const scrollRef = useRef<HTMLDivElement>(null);
    const trackRef = useRef<HTMLDivElement>(null);
    const thumbRef = useRef<HTMLDivElement>(null);

    const [selectedPlaceId, setSelectedPlaceId] = useState<number | null>(null);
    const [places, setPlaces] = useState<Place[]>([]);
    // 노드망 기준으로 갈 수 없는 장소(노드망 경로가 꺼져 있으면 null — 막지 않는다)
    const [moveBlock, setMoveBlock] = useState<MoveBlock | null>(null);
    const [loading, setLoading] = useState(false);

    const { showToast } = useToast();

    useModalBehavior({ isOpen, onClose });

    const handleCancel = () => {
        onClose();
    };

    const handleOk = async () => {
        if (selectedPlaceId === null) return;

        const selectedPlace = places.find(
            (p) => p.id === selectedPlaceId
        );

        if (!selectedPlace) return;

        try {
            const res = await apiFetch(`/nav/placemove/${selectedPlaceId}`, {
                method: "POST",
            });
            const data = await res.json();
            // 백엔드는 거부(위치 초기화 필요 등)도 200 + status:"error" 로 돌려준다 — 화면에 보여야 한다.
            if (!res.ok || data?.status === "error") {
                showToast(data?.msg ?? "장소 이동을 시작하지 못했습니다.", "error");
            } else {
                console.log("장소 이동 명령 전송:", data.msg ?? data.status);
            }
        } catch (err) {
            console.error("장소 이동 실패:", err);
            showToast("장소 이동 요청이 실패했습니다. 네트워크를 확인하세요.", "error");
        }

        onClose();
    };

    //선택된 장소 id 저장
    const handleSelectPlace = (placeId: number) => {
        setSelectedPlaceId(placeId);
    };

    // 모달 열릴 때 장소 목록 DB에서 가져오기
    useEffect(() => {
      if (isOpen) {
        setSelectedPlaceId(null);
        if (scrollRef.current) scrollRef.current.scrollTop = 0;

        setLoading(true);
        apiFetch(`/DB/places`)
          .then((res) => res.json())
          .then((data: DBPlace[]) => {
            // 위험구역(폴리곤 centroid)·충전소·다른 층을 후보에서 뺀다 — 공용 규칙 1곳에서 판정한다
            setPlaces(
              data
                .filter((p) => isMovablePlace(p, robotFloorId, robotMapId))
                .map((p) => ({
                  id: p.id,
                  name: p.LacationName,
                  x: p.LocationX,
                  y: p.LocationY,
                }))
            );
          })
          .catch((err) => {
            console.error("장소 목록 조회 실패:", err);
            setPlaces([]);
          })
          .finally(() => setLoading(false));
      }
    }, [isOpen, robotFloorId, robotMapId]);

    useEffect(() => {
      setMoveBlock(null);
      if (!isOpen || robotMapId == null) return;
      let cancelled = false;
      fetchMoveBlock(robotMapId)
        .then((b) => { if (!cancelled) setMoveBlock(b); })
        .catch(() => { /* 확인 실패는 막지 않는다 — 서버가 출발 전에 다시 확인한다 */ });
      return () => { cancelled = true; };
    }, [isOpen, robotMapId]);

    useCustomScrollbar({
        enabled: isOpen,
        scrollRef,
        trackRef,
        thumbRef,
        minThumbHeight: 50,
        deps: [places.length],
    });

    if (!isOpen) return null;

    return (
        <>
        <div className={styles.modalOverlay} onClick={onClose}>
            <div className={styles.placePathModalContent} onClick={(e) => e.stopPropagation()}>
                <button className={styles.placeCloseBtn} onClick={onClose}>✕</button>
                <div className={styles.placeModalHeader}>
                    <img src="/icon/robot_place_w.png" alt="" />
                    <h2>장소 이동</h2>
                </div>
                <div className={styles.placeTitle}>
                    아래 이동할 장소를 먼저 선택해 주세요.
                </div>

                <div className={styles.placePathBox}>
                    {loading ? (
                        <div className={styles.placeEmpty}>불러오는 중...</div>
                    ) : moveBlock?.map ? (
                        <div className={styles.placeEmpty}>{moveBlock.map}</div>
                    ) : places.length === 0 ? (
                        <div className={styles.placeEmpty}>
                            {robotFloorId == null
                                ? "로봇의 현재 층을 알 수 없어 이동 후보를 정할 수 없습니다."
                                : "이 층에 이동 가능한 장소가 없습니다."}
                        </div>
                    ) : (
                        <>
                            <div ref={scrollRef} className={styles.placeInner} role="listbox">
                                {places.map((place) => {
                                    const isSelected = selectedPlaceId === place.id;
                                    const blocked = moveBlock?.places[place.id];
                                    return (
                                        <div
                                            key={place.id}
                                            className={`${styles.placePathItem} ${isSelected ? styles.active : ""}`}
                                            role="option"
                                            aria-selected={isSelected}
                                            aria-disabled={!!blocked}
                                            title={blocked}
                                            style={blocked ? { opacity: 0.4, cursor: "not-allowed" } : undefined}
                                            onClick={() => { if (!blocked) handleSelectPlace(place.id); }}
                                        >
                                            <img
                                                src={isSelected ? "/icon/place_chk.png" : "/icon/place_none_chk.png"}
                                                alt=""
                                            />
                                            <div className={styles.placePathTitle}>{place.name}</div>
                                        </div>
                                    );
                                })}
                            </div>
                        </>
                    )}
                    <div ref={trackRef} className={styles.placeScrollTrack}>
                        <div ref={thumbRef} className={styles.placeScrollThumb} />
                    </div>
                </div>

                <div className={styles.workBtnBox}>
                    <button className={`${styles.workBtnCommon} ${styles.workBtnBgRed}`} onClick={handleCancel}>
                        <img src="/icon/close_btn.png" alt="" />
                        취소
                    </button>
                    <button
                        className={`${styles.workBtnCommon} ${styles.workBtnBgBlue} ${selectedPlaceId === null ? styles.workBtnDisabled : ""}`}
                        onClick={handleOk}
                    >
                        <img src="/icon/check.png" alt="" />
                        확인
                    </button>
                </div>
            </div>
        </div>
        </>
    );
    
}