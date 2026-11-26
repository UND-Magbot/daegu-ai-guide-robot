"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { useToast } from "@/app/components/common/Toast";
import { useRobotStatusContext } from "@/app/context/RobotStatusContext";
import styles from "./voice.module.css";
import {
  guideScenarioApi,
  type EngineCapabilities,
  type GuideFloor,
  type GuideMap,
  type GuidePlace,
  type GuideRobotPos,
  type GuideScenario,
} from "./guideScenarioApi";
import GuideScenarioList from "./GuideScenarioList";
import GuideNewScenarioModal from "./GuideNewScenarioModal";
import GuideScenarioEditor from "./GuideScenarioEditor";

const nameOf = (list: { id: number; name: string }[], id: number | null | undefined, fallback: string) =>
  list.find((x) => x.id === id)?.name ?? fallback;

type Props = {
  robotId: number;
  robotName: string;
  /** 없으면 뒤로 가기 버튼을 숨긴다(음성 관리 상단 탭에 붙어 있을 때) */
  onClose?: () => void;
};

/**
 * 데이터관리 › 음성 관리 › 안내 시나리오.
 *
 * 단발 FAQ('시나리오' 목록)와 다르다 — 병원·관공서처럼 여러 단계를 이어 가는 안내다.
 * 목록에서 시나리오를 고르면 단계 편집기로 들어간다. 편집기는 초안을 들고 있다가 [저장]에서 한 번에 보내고,
 * 서버가 엔진 규칙대로 검사한다. 번들은 Jetson 이 안내를 시작할 때마다 새로 받아 가므로 '로봇 적용' 이 따로 없다.
 */
export default function VoiceGuidePanel({ robotId, robotName, onClose }: Props) {
  const { showToast } = useToast();
  const [scenarios, setScenarios] = useState<GuideScenario[]>([]);
  const [places, setPlaces] = useState<GuidePlace[]>([]);
  const [floors, setFloors] = useState<GuideFloor[]>([]);
  const [maps, setMaps] = useState<GuideMap[]>([]);
  const [robotPos, setRobotPos] = useState<GuideRobotPos | null>(null);
  const [missingDefaults, setMissingDefaults] = useState<string[]>([]);
  const [caps, setCaps] = useState<EngineCapabilities | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  // 맵 전환은 다른 화면에서도 일어나므로 처음 불러온 값보다 실시간 로봇 상태를 우선한다
  const { robots } = useRobotStatusContext();
  const liveRobot = robots.find((r) => r.id === robotId);
  const robotFloorId = liveRobot ? liveRobot.currentFloorId : robotPos?.currentFloorId ?? null;
  const robotMapId = liveRobot ? liveRobot.currentMapId : robotPos?.currentMapId ?? null;
  const robotKnown = !!liveRobot || !!robotPos;

  const load = useCallback(async () => {
    try {
      const res = await guideScenarioApi.list(robotId);
      setScenarios(res.scenarios);
      setPlaces(res.places);
      setFloors(res.floors);
      setMaps(res.maps);
      setRobotPos(res.robot);
      setMissingDefaults(res.missingDefaults);
      setLoadError(null);
      setCode((c) => (c && res.scenarios.some((s) => s.code === c) ? c : null));
    } catch (e) {
      // 사업장 없는 로봇 등 — 화면을 비우고 이유를 보여 준다
      const msg = e instanceof Error ? e.message : "시나리오를 불러오지 못했습니다";
      setLoadError(msg);
      showToast(msg, "error");
    } finally {
      setLoading(false);
    }
  }, [robotId, showToast]);

  useEffect(() => {
    load();
  }, [load]);

  // 엔진 지원 목록 — 못 받으면 편집기가 기본 목록을 쓴다(서버 검사가 최종 판단)
  useEffect(() => {
    guideScenarioApi.capabilities().then(setCaps).catch(() => setCaps(null));
  }, []);

  const scenario = useMemo(() => scenarios.find((s) => s.code === code) ?? null, [scenarios, code]);

  // 안내 시작 전에 미리 알린다. 실제 차단은 서버가 한다(다른 화면에서 맵을 바꿨을 수 있다).
  const mapWarning = useMemo(() => {
    if (!scenario) return null;
    if (!scenario.mapId) return "운영 맵을 먼저 지정하세요.";
    if (robotKnown && robotMapId !== scenario.mapId) {
      return `로봇은 ${nameOf(floors, robotFloorId, "층 미지정")} / ${nameOf(
        maps,
        robotMapId,
        "맵 미지정"
      )}에 있습니다. 시작하려면 로봇 맵을 ${nameOf(maps, scenario.mapId, "")}(으)로 전환하세요.`;
    }
    return null;
  }, [scenario, robotKnown, robotFloorId, robotMapId, floors, maps]);

  const loadDefaults = async (reset: boolean, codes?: string[]) => {
    try {
      const res = await guideScenarioApi.loadDefaults(robotId, reset, codes);
      const n = res.created.length + res.reset.length;
      showToast(n ? (reset ? "기본값으로 되돌렸습니다" : `기본 시나리오 ${n}종을 추가했습니다`) : "추가할 시나리오가 없습니다", "success");
      await load();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "불러오지 못했습니다", "error");
    }
  };

  const toggle = async (sc: GuideScenario) => {
    if (sc.isActive && sc.standbyTasks.length) {
      // 끄면 그 작업은 호출돼도 안내가 시작되지 않는다 — 편집기에서 확인하고 끄게 한다
      setCode(sc.code);
      showToast(`상시 대기 작업 ${sc.standbyTasks.length}건이 쓰는 시나리오입니다 — 상세에서 확인하고 끄세요`, "info");
      return;
    }
    if (!sc.isActive && !sc.ready) {
      setCode(sc.code);
      showToast("준비 점검을 통과해야 켤 수 있습니다 — 상세에서 고칠 곳을 확인하세요", "error");
      return;
    }
    try {
      await guideScenarioApi.updateScenario(sc.id, { isActive: !sc.isActive });
      showToast(sc.isActive ? `${sc.displayName}을(를) 껐습니다` : `${sc.displayName}을(를) 켰습니다 — 작업 등록의 응대 방식에 나옵니다`, "success");
      await load();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "바꾸지 못했습니다", "error");
    }
  };

  const create = async (displayName: string, mapId: number) => {
    setCreating(true);
    try {
      const r = await guideScenarioApi.createScenario(robotId, displayName, mapId);
      setNewOpen(false);
      await load();
      setCode(r.code);
      showToast("새 시나리오를 만들었습니다 — 단계를 추가하고 저장하세요", "success");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "만들지 못했습니다", "error");
    } finally {
      setCreating(false);
    }
  };

  if (loading) return <div className={styles.emptyState}>불러오는 중...</div>;

  if (scenario) {
    return (
      <GuideScenarioEditor
        key={scenario.code}
        robotId={robotId}
        robotName={robotName}
        scenario={scenario}
        floors={floors}
        maps={maps}
        places={places}
        robotMapId={robotMapId}
        mapWarning={mapWarning}
        userIntents={caps?.userIntents ?? null}
        operatorInputs={caps?.operatorInputs ?? null}
        onBack={() => setCode(null)}
        onReload={load}
        onDeleted={async () => {
          setCode(null);
          await load();
        }}
        onResetDefault={() => loadDefaults(true, [scenario.code])}
      />
    );
  }

  return (
    <div className={styles.detail} style={{ flex: 1 }}>
      <div className={styles.detailHead}>
        {onClose && (
          <button className={styles.backBtn} onClick={onClose} aria-label="음성 관리로">
            <ArrowLeft size={16} />
          </button>
        )}
        <h2>안내 시나리오</h2>
        <span className={styles.crumb}>음성 관리 / {robotName} / 다단계 안내</span>
        <div className={styles.grow} />
        {missingDefaults.length > 0 && !loadError && (
          <button className={`${styles.workBtn} ${styles.ghost}`} onClick={() => loadDefaults(false)}>
            기본 시나리오 불러오기
          </button>
        )}
        <button className={styles.primaryBtn} onClick={() => setNewOpen(true)} disabled={!!loadError}>
          + 새 시나리오
        </button>
      </div>

      {loadError ? (
        <div className={`${styles.inline} ${styles.inlineWarn}`}>{loadError}</div>
      ) : scenarios.length === 0 ? (
        <div className={styles.emptyState}>
          등록된 안내 시나리오가 없습니다. <b>+ 새 시나리오</b>로 만들거나 <b>기본 시나리오 불러오기</b>로 병원·관공서·주차장
          안내를 추가하세요.
        </div>
      ) : (
        <GuideScenarioList scenarios={scenarios} floors={floors} maps={maps} onOpen={setCode} onToggle={toggle} />
      )}

      {newOpen && (
        <GuideNewScenarioModal
          robotName={robotName}
          floors={floors}
          maps={maps}
          robotFloorId={robotFloorId}
          robotMapId={robotMapId}
          busy={creating}
          onCancel={() => setNewOpen(false)}
          onCreate={create}
        />
      )}
    </div>
  );
}
