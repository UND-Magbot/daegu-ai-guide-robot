"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import CanvasMap from "@/app/components/map/CanvasMap";
import type { MapConfig } from "@/app/components/map/types";
import { apiFetch } from "@/app/lib/api";
import { markRelocalizeOpen } from "@/app/lib/relocalizeOpen";
import { poseFromDrag } from "@/app/lib/poseDrag";
import { seedWithRetry, type SeedAttempt } from "@/app/lib/relocalizeSeed";
import {
  bodyToMap,
  MATCH_CHECK,
  MATCH_COLOR,
  MATCH_LABEL,
  matchLevel,
  scanToBody,
  type MatchResponse,
  type Pose2D,
} from "@/app/lib/relocalizeMatch";

/** 현재 위치 맞음 점수를 다시 재는 간격(ms). 서버 계산은 1회 수 ms(점수만) — 스캔 0.5s 주기보다 느슨하게. */
const MATCH_POLL_MS = 1500;
/** 층 도착 시드 전에 라이다 원본 스캔을 기다리는 최대 시간(ms) — 스캔 스트림 준비가 끝난 뒤부터 센다.
 *  11-05 실기: 스트림 준비(NOS 중계기 기동)에 약 8초 — 창 연 순간부터 8초만 기다려 두 번 모두 스캔 없이 넣었다. */
const LOBBY_LIDAR_WAIT_MS = 20000;
/** 스트림 준비가 끝나지 않아도 창을 연 뒤 이만큼 지나면 그만 기다린다(배수). */
const LOBBY_STREAM_MAX_FACTOR = 2;

type LobbyScanResult = {
  ok: boolean;
  reason?: string;
  fit?: { x: number; y: number; yaw: number; score: number };
};

type StreamState = { openedAt: number; readyAt: number | null; failed: boolean };
type LidarWait = { points: number[][] | null; why: "lidar" | "not_lidar" | "no_scan" };

/**
 * 라이다 원본 스캔(source=lidar)이 들어올 때까지 기다린다. 아무 스캔이나 받고 멈추지 않는다 — 새 중계기는
 * 측위 스캔(/LOC_BODY_POINTS)을 받은 뒤에야 원본을 구독해, 첫 한두 개는 원본이 아니다(11-05 NOS 로그).
 * 기한: 스트림 준비 뒤 waitMs, 준비가 안 끝나면 창 연 뒤 waitMs×배수. 스트림이 실패하면 바로 그만둔다.
 */
async function waitForLidar(
  scanRef: { current: number[][] | null },
  fullRef: { current: boolean },
  streamRef: { current: StreamState },
  waitMs: number
): Promise<LidarWait> {
  for (;;) {
    if (scanRef.current && fullRef.current) return { points: scanRef.current, why: "lidar" };
    const st = streamRef.current;
    const now = Date.now();
    const expired = st.failed
      || (st.readyAt != null && now - st.readyAt >= waitMs)
      || now - st.openedAt >= waitMs * LOBBY_STREAM_MAX_FACTOR;
    if (expired) return { points: null, why: scanRef.current ? "not_lidar" : "no_scan" };
    await new Promise((r) => setTimeout(r, 200));
  }
}

/**
 * 위치 재조정(스캔 정합) — rviz 2D Pose Estimate 를 관제 화면 안에서 수행.
 *
 * 흐름:
 *   1. 열리면 스캔 스트림 준비(POST /robot/relocalize/stream/start) + WS 연결
 *      → 로봇 정합 스캔(/LOC_BODY_POINTS)이 저장맵 위에 주황점으로 겹쳐 표시된다.
 *      (스캔이 벽과 어긋나면 = 로봇 측위가 틀어진 것 = 재조정 대상)
 *   2. 운영자가 로봇 실제 위치를 누른 채 바라보는 방향으로 끈다 → 놓으면 '미리보기'(아직 주입 안 함).
 *      같은 스캔을 그 위치로 옮긴 하늘색 점과 지도와 맞는 점수(%)가 보인다(11-05).
 *   3. (선택) '자동 맞춤' → 서버가 주변 ±1m·±30°에서 스캔이 가장 잘 맞는 위치·방향을 찾아 미리보기를 바꾼다.
 *   4. '이 위치로 적용' → POST /robot/initpose {x,y,yaw} 로 주입 → 로봇이 그 시드로 scan-match 수렴.
 *      예전에는 손을 떼는 순간 주입했다 — 다시 할수록 주입이 늘어 측위 폭주 기회가 커졌다(10-08 분석).
 */

type Props = {
  open: boolean;
  mapConfig: MapConfig | null;
  robotId?: number;
  robotName?: string;
  onClose: () => void;
  /** '적용'이 수렴 성공(위치 확정)으로 끝났을 때 호출(알림 읽음·위치 재조회 등). */
  onConfirmed?: () => void;
  /** 층 이동 도착 후 열 때 — 열리자마자 엘리베이터 입구 좌표를 첫 시드로 넣는다(층 이동 중일 때만 서버가 넣음). */
  seedElevatorLobby?: boolean;
  /** 층 도착 시드 전 원본 스캔 대기 상한(ms, 스트림 준비 뒤부터). 기본 LOBBY_LIDAR_WAIT_MS — 시험·조정용. */
  lobbyScanWaitMs?: number;
};

type PoseStage = "idle" | "aiming" | "set";
type ApplyResult = { state: "success" | "pending" | "error"; msg: string } | null;
type MapDefaultPose = { x: number; y: number; yaw: number } | null;

function withRobotId(path: string, robotId?: number): string {
  return robotId != null ? `${path}?robot_id=${robotId}` : path;
}

function wsBase(): string {
  if (typeof window === "undefined") return "ws://localhost:8010";
  const h = window.location.hostname;
  return h !== "localhost" ? `ws://${h}:8010` : "ws://localhost:8010";
}

export default function RelocalizeModal({
  open,
  mapConfig,
  robotId,
  robotName,
  onClose,
  onConfirmed,
  seedElevatorLobby = false,
  lobbyScanWaitMs = LOBBY_LIDAR_WAIT_MS,
}: Props) {
  // 열려 있는 동안 전역 '위치 확인 필요' 안내창이 위에 겹치지 않게 알린다
  useEffect(() => (open ? markRelocalizeOpen() : undefined), [open]);
  const wsRef = useRef<WebSocket | null>(null);
  // 스캔 스트림 상태 — 층 도착 시드가 원본 스캔을 기다릴 기한(창 연 시각·준비 끝난 시각·실패)
  const streamRef = useRef<StreamState>({ openedAt: Date.now(), readyAt: null, failed: false });
  // 원시 스캔 점군 + 프레임. body=로봇 기준(pose 로 변환 필요), map=이미 map 프레임.
  const [rawScan, setRawScan] = useState<number[][]>([]);
  const [scanFrame, setScanFrame] = useState<"body" | "map">("body");
  // 스캔 출처 — "lidar" = 라이다 원본(/LIDAR/POINTS, 360°·27m)을 중계기가 줄인 것. 그 밖(앞 3.4m 만 담긴
  // /LOC_BODY_POINTS 등)은 지도와 맞춰 볼 벽이 적어 점수·자동 맞춤·층 도착 방향이 틀린다(11-05 실측·시뮬)
  // → 원본일 때만 그 기능들을 쓴다. 미리보기 후 '적용'은 출처와 무관하게 쓴다.
  const [scanSource, setScanSource] = useState<string | null>(null);
  const scanFull = scanSource === "lidar";
  const scanFullRef = useRef(false);
  scanFullRef.current = scanFull;
  const [liveOdom, setLiveOdom] = useState<{ x: number; y: number; yaw: number } | null>(null);
  // 로봇이 보고하는 측위 상태(0=정상, 그 외=상실). null = 아직 모름(구버전 receiver 등).
  // 주황 스캔이 보인다고 위치가 맞은 건 아니다 — 스캔은 로봇이 '믿는' 위치로 그려진다.
  const [robotLocation, setRobotLocation] = useState<number | null>(null);
  const [positionStale, setPositionStale] = useState(true);
  const locationCandidateRef = useRef<number | null>(null);
  const locationCandidateSinceRef = useRef(0);
  const lastPositionReceivedAtRef = useRef(0);
  // 이 창이 열려 있으면 관제가 위치를 0.5s 마다 받는다(스캔 오버레이가 이 위치로 그려짐).
  // 3000ms 동안 Location=0 이 이어져야 통과 — 순간 0 한두 샘플로 수렴 전 '완료'가 켜지지 않게.
  const LOCATION_CONFIRM_MS = 3000;
  const [scanConnected, setScanConnected] = useState(false);
  const [streamMsg, setStreamMsg] = useState<string | null>(null);
  const [noData, setNoData] = useState(false);
  const gotDataRef = useRef(false);

  const [stage, setStage] = useState<PoseStage>("idle");
  const [posePoint, setPosePoint] = useState<{ x: number; y: number } | null>(null);
  const [poseYaw, setPoseYaw] = useState(0);
  const [applying, setApplying] = useState(false);
  const [completing, setCompleting] = useState(false);
  const [result, setResult] = useState<ApplyResult>(null);
  const dragStartRef = useRef<{ x: number; y: number } | null>(null);
  const poseToRef = useRef<{ x: number; y: number } | null>(null);
  const [poseTo, setPoseTo] = useState<{ x: number; y: number } | null>(null);
  // 현재 지도의 등록된 초기 위치(map_init_pose). undefined = 조회 전/실패, null = 미등록.
  const [mapDefault, setMapDefault] = useState<MapDefaultPose | undefined>(undefined);
  const [saveAsMapDefault, setSaveAsMapDefault] = useState(false);
  // 주입 전 미리보기(운영자가 고른 위치)와 맞음 점수. 현재 위치 점수는 로봇이 믿는 위치 기준.
  const [proposal, setProposal] = useState<Pose2D | null>(null);
  const [proposalScore, setProposalScore] = useState<number | null>(null);
  const [fitAmbiguous, setFitAmbiguous] = useState<(Pose2D & { score: number }) | null>(null);
  const [fitting, setFitting] = useState(false);
  const [currentScore, setCurrentScore] = useState<number | null>(null);

  // ── 현재 지도 초기 위치 조회 — 미등록이면 저장을 기본 선택해 둔다 ──
  useEffect(() => {
    if (!open) return;
    let stop = false;
    setMapDefault(undefined);
    apiFetch(withRobotId("/robot/initpose", robotId))
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d: MapDefaultPose) => {
        if (stop) return;
        setMapDefault(d);
        setSaveAsMapDefault(d === null);
      })
      .catch(() => {
        if (!stop) setSaveAsMapDefault(false);
      });
    return () => {
      stop = true;
    };
  }, [open, robotId]);

  // ── 스캔 스트림: 스트림 준비 + WS 연결(정합 스캔은 프레임마다 교체) ──
  useEffect(() => {
    if (!open) return;
    let closed = false;
    gotDataRef.current = false;
    setNoData(false);
    setStreamMsg(null);

    // 스캔 스트림 준비(로봇 relay_map 기동). 실패 사유를 화면에 표시해 진단을 돕는다.
    // 준비 끝난 시각은 층 도착 시드가 원본 스캔을 기다릴 기한의 기준이 된다(waitForLidar).
    streamRef.current = { openedAt: Date.now(), readyAt: null, failed: false };
    apiFetch("/robot/relocalize/stream/start", { method: "POST" })
      .then((r) => r.json())
      .then((d) => {
        if (d?.status !== "ok") {
          streamRef.current = { ...streamRef.current, failed: true };
          setStreamMsg(d?.msg ?? "스캔 스트림 준비에 실패했습니다(relay 기동 실패).");
        } else {
          streamRef.current = { ...streamRef.current, readyAt: Date.now() };
        }
      })
      .catch(() => {
        streamRef.current = { ...streamRef.current, failed: true };
        setStreamMsg("스캔 스트림 준비 요청이 실패했습니다(네트워크/SSH).");
      });

    // 일정 시간 내 스캔·pose 데이터가 하나도 안 오면 '토픽 미수신'으로 안내.
    const noDataTimer = setTimeout(() => {
      if (!gotDataRef.current) setNoData(true);
    }, 6000);

    const connect = () => {
      if (closed) return;
      const ws = new WebSocket(`${wsBase()}/ws/relocalize/view`);
      wsRef.current = ws;
      ws.onopen = () => setScanConnected(true);
      ws.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data);
          // 로봇 pose 는 POSITION 폴링(아래 effect). WS 는 스캔만 사용.
          //  - type=body(/LOC_BODY_POINTS): 로봇 프레임 → pose 로 map 변환해 오버레이.
          //  - type=aligned: 이미 map 프레임 → 그대로.
          if (d.type === "body") {
            gotDataRef.current = true;
            setNoData(false);
            setScanFrame("body");
            setScanSource(typeof d.source === "string" ? d.source : "loc_body");
            setRawScan(d.points || []);
          } else if (d.type === "aligned") {
            gotDataRef.current = true;
            setNoData(false);
            setScanSource("aligned");
            setScanFrame("map");
            setRawScan(d.points || []);
          }
        } catch {
          /* ignore malformed frame */
        }
      };
      ws.onclose = () => {
        setScanConnected(false);
        if (!closed) setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
    };
    connect();

    return () => {
      closed = true;
      clearTimeout(noDataTimer);
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      apiFetch("/robot/relocalize/stream/stop", { method: "POST" }).catch(() => {});
      setRawScan([]);
      setLiveOdom(null);
      setRobotLocation(null);
      setPositionStale(true);
      locationCandidateRef.current = null;
      locationCandidateSinceRef.current = 0;
      lastPositionReceivedAtRef.current = 0;
      setScanConnected(false);
      setStreamMsg(null);
      setNoData(false);
      setStage("idle");
      setPosePoint(null);
      setResult(null);
      setProposal(null);
      setProposalScore(null);
      setFitAmbiguous(null);
    };
  }, [open]);

  // ── 로봇 위치: POSITION(ASDU) 폴링 — 측위 모드에서도 동작(relay_map/SLAM 토픽과 무관) ──
  useEffect(() => {
    if (!open) return;
    let stop = false;
    setRobotLocation(null);
    setPositionStale(true);
    locationCandidateRef.current = null;
    locationCandidateSinceRef.current = 0;
    lastPositionReceivedAtRef.current = 0;
    const poll = async () => {
      try {
        const path = robotId != null ? `/robot/position?robot_id=${robotId}` : "/robot/position";
        const res = await apiFetch(path);
        if (!res.ok) return;
        const d = await res.json();
        if (!stop && typeof d?.x === "number" && d?.timestamp) {
          const sourceAgeMs = Math.max(0, Date.now() - Number(d.timestamp) * 1000);
          if (!Number.isFinite(sourceAgeMs) || sourceAgeMs > 3000) {
            setPositionStale(true);
            return;
          }
          setLiveOdom({ x: d.x, y: d.y, yaw: Number(d.yaw ?? 0) });
          lastPositionReceivedAtRef.current = Date.now();
          setPositionStale(false);
          if (typeof d.location === "number") {
            if (locationCandidateRef.current !== d.location) {
              locationCandidateRef.current = d.location;
              locationCandidateSinceRef.current = Date.now();
            }
            if (Date.now() - locationCandidateSinceRef.current >= LOCATION_CONFIRM_MS) {
              setRobotLocation(d.location);
            }
          }
        }
      } catch {
        /* 폴링 실패는 무시(다음 주기 재시도) */
      }
    };
    poll();
    const id = setInterval(poll, 500);
    const staleId = setInterval(() => {
      if (Date.now() - lastPositionReceivedAtRef.current > 3000) {
        setPositionStale(true);
      }
    }, 500);
    return () => {
      stop = true;
      clearInterval(id);
      clearInterval(staleId);
    };
  }, [open, robotId]);

  // ── 맞음 점수: 스캔(로봇 기준)을 서버에 보내 지도 벽과 맞는 정도를 잰다. 로봇에는 아무것도 안 보낸다 ──
  const bodyScan = useMemo(() => scanToBody(rawScan, scanFrame, liveOdom), [rawScan, scanFrame, liveOdom]);
  const bodyScanRef = useRef<number[][] | null>(null);
  bodyScanRef.current = bodyScan;
  const requestMatch = useCallback(
    async (pose: Pose2D, refine: boolean): Promise<MatchResponse | null> => {
      const points = bodyScanRef.current;
      if (!points) return null;
      try {
        const res = await apiFetch("/robot/relocalize/match", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ robot_id: robotId ?? null, points, x: pose.x, y: pose.y, yaw: pose.yaw, refine }),
          timeoutMs: 10000,
        });
        if (!res.ok) return null;
        return (await res.json()) as MatchResponse;
      } catch {
        return null;
      }
    },
    [robotId]
  );

  // 현재 위치(로봇이 믿는 위치) 점수 — '정상'인데 지도와 어긋난 경우를 숫자로 보인다(669 경고 없음, 11-05)
  const liveOdomRef = useRef<Pose2D | null>(null);
  liveOdomRef.current = liveOdom;
  useEffect(() => {
    if (!open) return;
    let stop = false;
    const tick = async () => {
      const pose = liveOdomRef.current;
      if (!pose || !bodyScanRef.current || !scanFullRef.current) {
        if (!stop) setCurrentScore(null);
        return;
      }
      const r = await requestMatch(pose, false);
      if (!stop) setCurrentScore(r?.status === "ok" && r.at ? r.at.score : null);
    };
    const id = setInterval(() => void tick(), MATCH_POLL_MS);
    void tick();
    return () => {
      stop = true;
      clearInterval(id);
      setCurrentScore(null);
    };
  }, [open, requestMatch]);

  const clearProposal = useCallback(() => {
    setProposal(null);
    setProposalScore(null);
    setFitAmbiguous(null);
  }, []);

  // ── rviz식 pose 드래그: 누른 지점=위치, 끌기=방향(초록 화살표가 드래그만큼 늘어남), 뗌=미리보기(주입 안 함) ──
  const handlePoseStart = useCallback((w: { x: number; y: number }) => {
    setResult(null);
    clearProposal();
    dragStartRef.current = { x: w.x, y: w.y };
    poseToRef.current = { x: w.x, y: w.y };
    setPosePoint({ x: w.x, y: w.y });
    setPoseTo({ x: w.x, y: w.y });
    setPoseYaw(0);
    setStage("aiming");
  }, [clearProposal]);

  const handlePoseDrag = useCallback((w: { x: number; y: number }) => {
    const s = dragStartRef.current;
    if (!s) return;
    const yaw = Math.atan2(w.y - s.y, w.x - s.x);
    poseToRef.current = { x: w.x, y: w.y };
    setPoseYaw(yaw);
    setPoseTo({ x: w.x, y: w.y });
  }, []);

  const resetPose = () => {
    dragStartRef.current = null;
    setPosePoint(null);
    setPoseTo(null);
    setStage("idle");
    setResult(null);
    clearProposal();
  };

  // 지정한 pose 를 로봇에 주입(시드). 수렴 여부(벗어남 등)는 스캔으로 눈으로 확인하므로
  // 실패성 문구를 띄우지 않는다(주입 전송 성공/실패만 표시). 확정은 '완료'가 담당.
  const applyPose = useCallback(
    async (pt: { x: number; y: number }, yaw: number) => {
      if (completing) return;
      setApplying(true);
      setResult(null);
      // 주입하면 측위가 다시 잡힐 때까지 '정상' 판정을 처음부터 다시 한다 — 주입 전 '정상'이
      // 최대 3초 남아 있어 폭주 중에 '완료'가 눌리는 것을 막는다.
      locationCandidateRef.current = null;
      locationCandidateSinceRef.current = 0;
      setRobotLocation(null);
      try {
        const res = await apiFetch("/robot/initpose", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ x: pt.x, y: pt.y, yaw, robot_id: robotId ?? null }),
          timeoutMs: 20000,
        });
        const data = await res.json().catch(() => null);
        const sent = res.ok && data?.transport_ok !== false && data?.status !== "error" && data?.status !== "disabled";
        const converged = sent && data?.converged === true;
        if (sent) setLobbySeedFailed(false);
        setResult({
          state: !sent ? "error" : converged ? "success" : "pending",
          msg: !sent
            ? (data?.msg ?? "주입 요청이 실패했습니다.")
            : converged
              ? "위치 주입 및 측위 수렴이 확인되었습니다. '완료'를 눌러 확정하세요."
              : `위치 주입은 전달됐지만 아직 미확정입니다${data?.error_code != null ? ` (ErrorCode=${data.error_code})` : ""}. Location=0 및 좌표 안정 후 '완료'하세요.`,
        });
      } catch {
        setResult({ state: "error", msg: "주입 요청이 실패했습니다. 네트워크를 확인하세요." });
      } finally {
        setApplying(false);
        // 재조정 반복할 수 있도록 pose 초기화(다시 드래그 유도).
        dragStartRef.current = null;
        setStage("idle");
        setPosePoint(null);
        setPoseTo(null);
        clearProposal();
      }
    },
    [completing, robotId, clearProposal]
  );

  // 미리보기 위치를 바꾼다(드래그 끝·자동 맞춤). 화살표는 그 위치·방향 고정 길이로 그린다.
  const showProposal = useCallback((p: Pose2D, score: number | null) => {
    setProposal(p);
    setProposalScore(score);
    setPosePoint({ x: p.x, y: p.y });
    setPoseYaw(p.yaw);
    setPoseTo(null);
    setStage("set");
  }, []);

  // '자동 맞춤' — 고른 위치(없으면 로봇이 믿는 위치) 주변에서 스캔이 가장 잘 맞는 곳을 찾아 미리보기로.
  const autoFit = useCallback(async () => {
    const base = proposal ?? liveOdom;
    if (!base) return;
    setFitting(true);
    setResult(null);
    const r = await requestMatch(base, true);
    setFitting(false);
    if (!r || r.status !== "ok" || !r.refined) {
      setResult({ state: "error", msg: r?.msg ?? "자동 맞춤을 하지 못했습니다(스캔 또는 지도 없음). 직접 끌어 맞추세요." });
      return;
    }
    const { x, y, yaw, score, ambiguous, alt } = r.refined;
    showProposal({ x, y, yaw }, score);
    setFitAmbiguous(ambiguous && alt ? alt : null);
  }, [proposal, liveOdom, requestMatch, showProposal]);

  const applyProposal = useCallback(() => {
    if (!proposal) return;
    void applyPose({ x: proposal.x, y: proposal.y }, proposal.yaw);
  }, [proposal, applyPose]);

  // 놓으면(드래그 종료) 그 위치·방향을 미리보기로 남긴다 — 주입은 '이 위치로 적용'(11-05, 예전엔 즉시 주입).
  // 짧게 끌면(클릭 포함) 방향을 알 수 없어 미리보기도 만들지 않는다.
  const handlePoseEnd = useCallback(() => {
    const s = dragStartRef.current;
    if (!s) return;
    const pose = poseFromDrag(s, poseToRef.current ?? s);
    if (!pose) {
      dragStartRef.current = null;
      poseToRef.current = null;
      setPosePoint(null);
      setPoseTo(null);
      setStage("idle");
      setResult({
        state: "error",
        msg: "방향이 정해지지 않았습니다. 로봇 위치를 누른 채 바라보는 쪽으로 조금 더 길게 끌어 주세요.",
      });
      return;
    }
    dragStartRef.current = null;
    showProposal(pose, null);
    if (!scanFullRef.current) return;   // 원본 스캔이 아니면 점수를 재지 않는다
    void requestMatch(pose, false).then((r) => {
      if (r?.status === "ok" && r.at) setProposalScore(r.at.score);
    });
  }, [requestMatch, showProposal]);

  // ── 층 이동 도착: 열리면 한 번, 엘리베이터 입구를 첫 시드로 넣는다 ──
  // 로봇은 방금 엘리베이터에서 내려 문 앞에 있다. 층 이동 중이 아니면 서버가 건너뛴다.
  const lobbySeededRef = useRef(false);
  // 시드가 끝내 로봇에 안 갔다 — 서버도 층 이동 중에는 주입 전달 전 '완료'를 거부한다(auto_init_pose).
  const [lobbySeedFailed, setLobbySeedFailed] = useState(false);
  // 층 도착 시드를 저장 방향으로 넣었을 때(스캔 없음·점수 낮음·모호) 운영자에게 확인을 부탁하는 문구
  const [lobbyHeadingNote, setLobbyHeadingNote] = useState<string | null>(null);
  // 층 도착 시드 전에 원본 스캔을 기다리는 중
  const [lobbyWaiting, setLobbyWaiting] = useState(false);
  const seedLobby = useCallback(async () => {
    setApplying(true);
    setResult(null);
    setLobbyHeadingNote(null);
    locationCandidateRef.current = null;
    locationCandidateSinceRef.current = 0;
    setRobotLocation(null);
    // 방향은 스캔으로 정한다(11-05) — 엘리베이터에서 어느 쪽을 보고 나왔는지 몰라도 되게. 첫 스캔을 잠깐 기다린다.
    // 원본 스캔일 때만 싣는다 — 아니면 저장 방향(앞 3.4m 스캔으로는 1층 30번 중 2번 틀린 방향, 11-05 시뮬)
    setLobbyWaiting(true);
    const wait = await waitForLidar(bodyScanRef, scanFullRef, streamRef, lobbyScanWaitMs);
    setLobbyWaiting(false);
    const points = wait.points;
    let lastScan: LobbyScanResult | null = null;
    const once = async (): Promise<SeedAttempt> => {
      try {
        const r = await apiFetch(withRobotId("/robot/initpose/elevator-lobby", robotId), {
          method: "POST",
          timeoutMs: 20000,
          ...(points
            ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify({ points }) }
            : {}),
        });
        const data = await r.json().catch(() => null);
        if (data?.status === "skipped") return { outcome: "skipped" };
        if (data?.status === "none" || !data?.seed) return { outcome: "none", msg: data?.msg };
        lastScan = data.seed.scan ?? null;
        if (data.transport_ok === false || data.status === "error") return { outcome: "failed", msg: data.msg };
        return { outcome: "sent" };
      } catch {
        return { outcome: "failed" };
      }
    };
    const res = await seedWithRetry(once);
    setLobbySeedFailed(res.outcome === "failed");
    if (res.outcome === "sent") {
      const sc = lastScan as LobbyScanResult | null;
      if (wait.why === "no_scan") {
        setLobbyHeadingNote("스캔이 오지 않아 저장된 방향으로 넣었습니다. 주황 스캔이 벽과 맞는지 확인하세요.");
      } else if (wait.why === "not_lidar") {
        setLobbyHeadingNote("라이다 원본 스캔이 오지 않아 저장된 방향으로 넣었습니다. 주황 스캔이 벽과 맞는지 확인하세요.");
      } else if (sc && !sc.ok) {
        setLobbyHeadingNote(`방향을 스캔으로 정하지 못해 저장된 방향으로 넣었습니다(${sc.reason}). 주황 스캔이 벽과 맞는지 확인하세요.`);
        // 스캔이 가장 잘 맞는다고 본 자리를 미리보기로만 보여 준다(주입은 운영자가 '적용')
        if (sc.fit) showProposal({ x: sc.fit.x, y: sc.fit.y, yaw: sc.fit.yaw }, sc.fit.score);
      }
    }
    // 넣은 것은 따로 알리지 않는다 — 실패했을 때만 직접 지정하도록 안내한다
    if (res.outcome === "none") {
      setResult({ state: "error", msg: res.msg ?? "엘리베이터 입구 시드를 넣지 못했습니다. 로봇 위치를 직접 지정하세요." });
    } else if (res.outcome === "failed") {
      setResult({
        state: "error",
        msg: `엘리베이터 입구 시드가 ${res.tries}번 모두 로봇에 전달되지 않았습니다. '다시 넣기'를 누르거나 로봇 위치를 직접 지정하세요.`,
      });
    }
    setApplying(false);
  }, [robotId, showProposal, lobbyScanWaitMs]);
  useEffect(() => {
    if (!open) {
      lobbySeededRef.current = false;
      setLobbySeedFailed(false);
      setLobbyHeadingNote(null);
      return;
    }
    if (!seedElevatorLobby || lobbySeededRef.current) return;
    lobbySeededRef.current = true;
    void seedLobby();
  }, [open, seedElevatorLobby, seedLobby]);

  // 확정된 현재 위치를 이 지도의 초기 위치로 저장. 실패 사유 문자열을 돌려준다(성공 시 null).
  const saveMapDefault = async (): Promise<string | null> => {
    try {
      const res = await apiFetch(withRobotId("/robot/initpose/map-default", robotId), {
        method: "PUT",
      });
      if (res.ok) return null;
      const data = await res.json().catch(() => null);
      return data?.detail ?? "초기 위치 저장에 실패했습니다.";
    } catch {
      return "초기 위치 저장 요청이 실패했습니다. 네트워크를 확인하세요.";
    }
  };

  // '완료' = 운영자가 스캔 정렬을 눈으로 확인하고 확정. 현재 로봇 위치로 미확정 해제 후 닫는다.
  // (자동 수렴검사와 무관 — 운영자의 시각 확인이 곧 확정.)
  const handleComplete = async () => {
    if (!canComplete) return;
    setCompleting(true);
    setResult(null);
    try {
      const res = await apiFetch(withRobotId("/robot/initpose/confirm-current", robotId), {
        method: "POST",
      });
      const data = await res.json().catch(() => null);
      if (res.ok && data?.confirmed) {
        onConfirmed?.();
        // 저장은 측위 정상일 때만 — 체크돼 있어도 상실 중이면 확정만 하고 저장은 건너뛴다.
        const saveError = saveAsMapDefault && canSaveMapDefault ? await saveMapDefault() : null;
        if (saveError) {
          // 위치 확정은 끝났다. 저장만 실패했으니 창을 열어 두고 사유를 보여준다.
          setResult({ state: "error", msg: `위치는 확정했지만 초기 위치 저장은 실패했습니다: ${saveError}` });
          setCompleting(false);
          return;
        }
        onClose();
      } else {
        setResult({
          state: "error",
          msg: data?.msg ?? "완료 처리에 실패했습니다. 로봇 현재 위치 수신을 확인하세요.",
        });
        setCompleting(false);
      }
    } catch {
      setResult({ state: "error", msg: "완료 요청이 실패했습니다. 네트워크를 확인하세요." });
      setCompleting(false);
    }
  };

  // 오버레이용 스캔(월드/map 프레임). body 스캔이면 로봇 pose 로 회전·평행이동해 map 에 얹는다
  // (rviz 가 /tf 로 하는 변환과 동일: mapX = rx + bx·cosθ − by·sinθ, mapY = ry + bx·sinθ + by·cosθ).
  // 측위가 틀어져 있으면 pose 도 틀어져 스캔이 벽과 어긋나 보이며, 그 어긋남이 곧 재조정 대상이다.
  const scanPoints = useMemo<number[][]>(() => {
    if (rawScan.length === 0) return [];
    if (scanFrame === "map") return rawScan;
    if (!liveOdom) return []; // body 스캔인데 pose 미수신 → 변환 불가
    const c = Math.cos(liveOdom.yaw);
    const s = Math.sin(liveOdom.yaw);
    const { x: rx, y: ry } = liveOdom;
    const out: number[][] = new Array(rawScan.length);
    for (let i = 0; i < rawScan.length; i++) {
      const bx = rawScan[i][0];
      const by = rawScan[i][1];
      out[i] = [rx + bx * c - by * s, ry + bx * s + by * c];
    }
    return out;
  }, [rawScan, scanFrame, liveOdom]);

  // 주입 전 미리보기 — 같은 스캔을 고른 위치로 옮겨 그린다(끄는 중에도 따라 움직임)
  const previewScanPoints = useMemo<number[][]>(() => {
    const p = stage === "aiming" && posePoint ? { x: posePoint.x, y: posePoint.y, yaw: poseYaw } : proposal;
    return p && bodyScan ? bodyToMap(bodyScan, p) : [];
  }, [bodyScan, stage, posePoint, poseYaw, proposal]);

  if (!open) return null;

  const locationView = positionStale
    ? { label: "통신 확인 불가", color: "#f59e0b" }
    : lobbySeedFailed
      ? { label: "확인 필요(층 이동 시드 미전달)", color: "#f59e0b" }
      : robotLocation === null
      ? { label: "확인 중", color: "#9aa4c4" }
      : robotLocation === 0
        ? { label: "정상", color: "#22c55e" }
        : { label: "상실", color: "#ef4444" };
  // 초기 위치는 로봇이 측위 정상을 보고할 때만 저장된다(백엔드도 같은 조건으로 거부).
  // 다음 전원 on·맵 적용의 시작 위치가 되므로, 저장은 이번 창에서 주입·수렴을 확인한 경우로 한정한다.
  const relocalizationVerified = result?.state === "success";
  const canSaveMapDefault = !positionStale && !lobbySeedFailed && robotLocation === 0 && relocalizationVerified;
  // 확정은 측위 '정상'(Location=0 이 3초 유지)이면 주입 없이도 허용한다. 이미 위치를 잡은 로봇에
  // 일부러 주입하면 측위가 폭주할 수 있다(2026-10-08 분석: 도크 밖 주입 성공률 37%).
  // 스캔이 벽과 맞는지는 운영자가 보고, 서버(confirm-current)도 Location=0·최신 위치를 다시 확인한다.
  const canComplete = !applying && !completing && !positionStale && !lobbySeedFailed && robotLocation === 0;

  const stageText = lobbyWaiting
    ? "층 도착 위치를 넣기 전에 로봇 스캔을 기다리는 중입니다(스캔으로 방향을 정합니다)..."
    : applying
    ? "위치 주입 중..."
    : fitting
      ? "자동 맞춤 중... (주변 ±1m·±30°에서 스캔이 가장 잘 맞는 곳을 찾습니다)"
      : stage === "aiming"
        ? "방향으로 끌고 있습니다 — 하늘색 점이 벽에 겹치는지 보세요. 놓아도 아직 주입하지 않습니다."
        : proposal
          ? "하늘색 미리보기가 벽과 맞으면 '이 위치로 적용'을 누르세요. 대충 찍었으면 '자동 맞춤'을 먼저 누르세요."
          : robotLocation === 0 && !result
            ? "측위가 정상입니다. 주황 스캔이 벽과 맞으면 드래그 없이 '완료'하세요. 어긋나면 로봇 실제 위치를 누른 채 바라보는 방향으로 끌어서 놓으세요."
            : "로봇 실제 위치를 누른 채 바라보는 방향으로 끌어서 놓으면 미리보기가 나옵니다. 맞으면 '이 위치로 적용', 주황 스캔이 벽과 맞으면 '완료'하세요.";
  const currentLevel = currentScore != null ? matchLevel(currentScore) : null;
  const proposalLevel = proposalScore != null ? matchLevel(proposalScore) : null;
  // 로봇은 '정상'이라는데 스캔이 지도와 덜 맞음 — 669 는 이런 때 경고를 남기지 않았다(10-30·11-05)
  const silentMismatch = robotLocation === 0 && currentScore != null && currentScore < MATCH_CHECK;

  return (
    <div style={overlayStyle} onClick={applying ? undefined : onClose}>
      <div style={boxStyle} onClick={(e) => e.stopPropagation()}>
        <div style={headerStyle}>
          <div>
            <div style={titleStyle}>위치 재조정 (스캔 정합)</div>
            <div style={subtitleStyle}>
              {robotName ? `${robotName} · ` : ""}
              맵의 벽과 로봇 스캔(주황점)을 맞춰 실제 위치를 지정합니다.
            </div>
          </div>
          <button style={closeBtnStyle} onClick={onClose} disabled={applying} aria-label="닫기">
            ✕
          </button>
        </div>

        <div style={mapWrapStyle}>
          {mapConfig ? (
            <CanvasMap
              config={mapConfig}
              robotPos={liveOdom}
              robotName={robotName}
              showRobot={!!liveOdom}
              robotMarkerSize={16}
              scanPoints={scanPoints}
              previewScanPoints={previewScanPoints}
              poseArrow={posePoint ? { x: posePoint.x, y: posePoint.y, yaw: poseYaw, toX: poseTo?.x, toY: poseTo?.y } : null}
              poseDrawMode
              onPoseDragStart={handlePoseStart}
              onPoseDrag={handlePoseDrag}
              onPoseDragEnd={handlePoseEnd}
              style={{ cursor: "crosshair" }}
            />
          ) : (
            <div style={loadingStyle}>
              지도를 불러오는 중입니다...
              <br />
              (표시되지 않으면 맵 관리 상단에서 맵이 선택돼 있는지 확인하세요)
            </div>
          )}

          {/* 스캔 상태 뱃지 */}
          <div style={badgeStyle}>
            <span
              style={{
                ...dotStyle,
                background: scanConnected && scanPoints.length > 0 ? "#22c55e" : "#f59e0b",
              }}
            />
            {scanConnected
              ? scanPoints.length > 0
                ? `스캔 수신 중 · ${scanPoints.length}점`
                : "스캔 수신 대기 중..."
              : "스캔 연결 중..."}
          </div>

          {/* 지도와 맞음 — 로봇이 믿는 위치에서 스캔이 지도 벽과 겹치는 비율(서버 계산) */}
          {currentLevel && currentScore != null && (
            <div style={{ ...badgeStyle, top: 44 }} data-testid="match-current">
              <span style={{ ...dotStyle, background: MATCH_COLOR[currentLevel] }} />
              지도와 맞음 {Math.round(currentScore)}% · {MATCH_LABEL[currentLevel]}
            </div>
          )}

          {/* 로봇 측위 상태 뱃지 — '완료' 전에 로봇이 실제로 위치를 잡았는지 보여준다 */}
          <div style={{ ...badgeStyle, left: "auto", right: 10 }}>
            <span style={{ ...dotStyle, background: locationView.color }} />
            로봇 측위: {locationView.label}
          </div>
        </div>

        {robotLocation !== null && robotLocation !== 0 && (
          <div style={warnStyle} role="status">
            <b>로봇이 아직 위치를 잡지 못했습니다(측위 상실).</b> 주황 스캔은 로봇이 믿는 위치로 그려지므로,
            보인다고 맞은 것은 아닙니다. 실제 위치를 드래그해 넣고 '정상'으로 바뀌면 '완료'하세요.
            상실 상태로 완료하면 잠시 뒤 다시 '위치 확인 필요'로 돌아갑니다.
          </div>
        )}

        {lobbySeedFailed && (
          <div style={warnStyle} role="alert">
            <b>층 이동 시드가 로봇에 전달되지 않았습니다.</b> 로봇이 측위 &apos;정상&apos;을 보내도 지금 좌표는
            이전 층 위치일 수 있습니다. 다시 넣거나 로봇 실제 위치를 끌어 주입한 뒤 &apos;완료&apos;하세요.{" "}
            <button type="button" onClick={() => void seedLobby()} disabled={applying}>
              다시 넣기
            </button>
          </div>
        )}

        {lobbyHeadingNote && (
          <div style={warnStyle} role="status" data-testid="lobby-heading-note">
            <b>층 도착 위치를 넣었습니다.</b> {lobbyHeadingNote}
          </div>
        )}

        {positionStale && (
          <div style={warnStyle} role="status">
            <b>로봇 위치 응답이 오래되었습니다.</b> 최신 위치가 확인될 때까지 완료와 초기위치 저장을 잠급니다.
          </div>
        )}

        {(streamMsg || noData) && (
          <div style={warnStyle} role="alert">
            <b>정합 스캔 오버레이를 사용할 수 없습니다(주황점 없음).</b>
            {streamMsg ? ` (${streamMsg})` : ""}
            <br />
            로봇 위치와 드래그 지정·주입은 정상 동작합니다. 스캔 정합 표시는 측위 모드에서 라이다 스캔
            토픽이 브리지될 때만 가능합니다(<code>/SLAM_*</code>은 매핑 전용이라 측위 중엔 나오지 않음).
          </div>
        )}

        {silentMismatch && (
          <div style={warnStyle} role="status">
            로봇은 측위 &apos;정상&apos;이라고 보고합니다. 스캔이 지도와 맞는 정도는 {Math.round(currentScore ?? 0)}%라,
            주황 스캔이 벽과 맞는지 한 번 더 확인하세요(사람·물건이 많아도 낮게 나올 수 있습니다). 밀려 있으면
            실제 위치를 끌어 미리보기로 맞춘 뒤 적용하세요.
          </div>
        )}

        <div style={guideStyle}>{stageText}</div>

        {proposal && (
          <div style={proposalStyle} data-testid="proposal-panel">
            <span>
              미리보기 ({proposal.x.toFixed(2)}, {proposal.y.toFixed(2)}, {Math.round((proposal.yaw * 180) / Math.PI)}°)
              {scanFull && (
                <>
                  {" "}· 지도와 맞음{" "}
                  {proposalLevel && proposalScore != null ? (
                    <b style={{ color: MATCH_COLOR[proposalLevel] }}>
                      {Math.round(proposalScore)}% {MATCH_LABEL[proposalLevel]}
                    </b>
                  ) : (
                    "재는 중..."
                  )}
                  {currentScore != null && ` (지금 위치 ${Math.round(currentScore)}%)`}
                </>
              )}
            </span>
            {scanFull && (
              <button style={ghostBtnStyle} onClick={() => void autoFit()} disabled={applying || fitting || !bodyScan}>
                {fitting ? "맞추는 중..." : "자동 맞춤"}
              </button>
            )}
            <button
              style={{ ...completeBtnStyle, background: "var(--color-primary, #2563eb)" }}
              onClick={applyProposal}
              disabled={applying || fitting || completing}
            >
              이 위치로 적용
            </button>
          </div>
        )}

        {fitAmbiguous && (
          <div style={warnStyle} role="alert">
            <b>비슷하게 맞는 곳이 또 있습니다</b> — ({fitAmbiguous.x.toFixed(2)}, {fitAmbiguous.y.toFixed(2)}) 도{" "}
            {Math.round(fitAmbiguous.score)}% 맞습니다. 긴 통로처럼 앞뒤 구분이 안 되는 곳이라 자동 맞춤을 믿기 어렵습니다.
            로봇 실제 위치를 눈으로 확인한 뒤 적용하거나, 문·기둥이 보이는 자리로 옮겨 다시 하세요.
          </div>
        )}

        {result && (
          <div
            style={{
              ...resultStyle,
          color: result.state === "success" ? "#bbf7d0" : result.state === "pending" ? "#fde68a" : "#ffd9d9",
          background: result.state === "success" ? "rgba(22,101,52,0.25)" : result.state === "pending" ? "rgba(146,64,14,0.22)" : "rgba(191,14,20,0.18)",
          borderColor: result.state === "success" ? "rgba(34,197,94,0.5)" : result.state === "pending" ? "rgba(245,158,11,0.55)" : "var(--color-error-border)",
            }}
            role="alert"
          >
            {result.msg}
          </div>
        )}

        <div style={actionsStyle}>
          <label style={saveDefaultStyle} title="다음 전원 on·지도 적용 때 이 위치를 시작 위치로 사용합니다.">
            <input
              type="checkbox"
              checked={saveAsMapDefault && canSaveMapDefault}
              onChange={(e) => setSaveAsMapDefault(e.target.checked)}
              disabled={applying || completing || !canSaveMapDefault}
            />
            확정 위치를 이 지도의 초기 위치로 저장
            <span style={mapDefaultHintStyle}>
              {!canSaveMapDefault
                ? "(측위 정상일 때만 가능)"
                : mapDefault === undefined
                  ? ""
                  : mapDefault === null
                    ? "(현재 미등록)"
                    : `(현재 ${mapDefault.x.toFixed(2)}, ${mapDefault.y.toFixed(2)})`}
            </span>
          </label>
          <button
            style={ghostBtnStyle}
            onClick={resetPose}
            disabled={applying || completing || !posePoint}
          >
            다시 찍기
          </button>
          <button
            style={{ ...completeBtnStyle, opacity: applying || completing ? 0.6 : 1 }}
            onClick={handleComplete}
            disabled={!canComplete}
          >
            {completing ? "완료 중..." : "완료 (위치 확정)"}
          </button>
          <button style={ghostBtnStyle} onClick={onClose} disabled={applying || completing}>
            닫기
          </button>
        </div>
      </div>
    </div>
  );
}

/* ── 인라인 스타일(관제 다크 토큰 사용) ── */
const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.6)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 10000,
  backdropFilter: "blur(3px)",
};
const boxStyle: React.CSSProperties = {
  width: "min(1080px, 96vw)",
  maxHeight: "94vh",
  overflowY: "auto",
  display: "flex",
  flexDirection: "column",
  background: "var(--surface-3, #1a1d2e)",
  border: "1px solid var(--border-subtle, #2c3350)",
  borderTop: "3px solid var(--color-warning, #ffc635)",
  borderRadius: 14,
  padding: 18,
  boxShadow: "0 20px 56px rgba(0,0,0,0.5)",
};
const headerStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "flex-start",
  marginBottom: 12,
};
const titleStyle: React.CSSProperties = {
  fontSize: 18,
  fontWeight: 700,
  color: "var(--text-primary, #fff)",
};
const subtitleStyle: React.CSSProperties = {
  fontSize: 13,
  color: "var(--text-tertiary, #9aa4c4)",
  marginTop: 4,
};
const closeBtnStyle: React.CSSProperties = {
  background: "transparent",
  border: "none",
  color: "var(--text-tertiary, #9aa4c4)",
  fontSize: 18,
  cursor: "pointer",
  padding: 4,
  lineHeight: 1,
};
const mapWrapStyle: React.CSSProperties = {
  // flex:1 은 auto-height 부모 안에서 0 으로 붕괴할 수 있어 확정 높이(vh)로 고정.
  // CanvasMap .wrapper 가 height:100% 라 부모의 확정 높이가 있어야 캔버스가 그려진다.
  position: "relative",
  flexShrink: 0,
  width: "100%",
  height: "62vh",
  minHeight: 420,
  background: "#1a1d2e",
  border: "1px solid var(--border-subtle, #2c3350)",
  borderRadius: 10,
  overflow: "hidden",
};
const loadingStyle: React.CSSProperties = {
  width: "100%",
  height: "100%",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  color: "#888",
};
const badgeStyle: React.CSSProperties = {
  position: "absolute",
  top: 10,
  left: 10,
  display: "flex",
  alignItems: "center",
  gap: 7,
  padding: "5px 10px",
  fontSize: 12,
  color: "#dfe4f5",
  background: "rgba(0,0,0,0.6)",
  borderRadius: 8,
  pointerEvents: "none",
};
const dotStyle: React.CSSProperties = {
  width: 8,
  height: 8,
  borderRadius: "50%",
  display: "inline-block",
};
const guideStyle: React.CSSProperties = {
  marginTop: 12,
  fontSize: 13.5,
  color: "var(--text-secondary, #c7cde0)",
  lineHeight: 1.5,
};
const warnStyle: React.CSSProperties = {
  marginTop: 12,
  padding: "10px 12px",
  fontSize: 12.5,
  lineHeight: 1.6,
  color: "#ffe4b8",
  background: "rgba(245, 158, 11, 0.14)",
  border: "1px solid rgba(245, 158, 11, 0.5)",
  borderRadius: 8,
};
const resultStyle: React.CSSProperties = {
  marginTop: 10,
  padding: "9px 12px",
  fontSize: 13,
  borderRadius: 8,
  border: "1px solid",
};
const proposalStyle: React.CSSProperties = {
  marginTop: 10,
  padding: "8px 12px",
  display: "flex",
  flexWrap: "wrap",
  alignItems: "center",
  gap: 10,
  fontSize: 13,
  color: "var(--text-secondary, #c7cde0)",
  background: "rgba(56, 189, 248, 0.10)",
  border: "1px solid rgba(56, 189, 248, 0.45)",
  borderRadius: 8,
};
const actionsStyle: React.CSSProperties = {
  marginTop: 14,
  display: "flex",
  flexWrap: "wrap",
  alignItems: "center",
  gap: 10,
  justifyContent: "flex-end",
};
const saveDefaultStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  marginRight: "auto",
  fontSize: 13,
  color: "var(--text-secondary, #c7cde0)",
  cursor: "pointer",
};
const mapDefaultHintStyle: React.CSSProperties = {
  color: "var(--text-tertiary, #9aa4c4)",
};
const completeBtnStyle: React.CSSProperties = {
  height: 42,
  padding: "0 24px",
  border: "none",
  borderRadius: 9,
  fontSize: 14,
  fontWeight: 700,
  color: "#fff",
  background: "var(--color-success, #16a34a)",
  cursor: "pointer",
};
const ghostBtnStyle: React.CSSProperties = {
  height: 42,
  padding: "0 18px",
  borderRadius: 9,
  fontSize: 14,
  fontWeight: 600,
  color: "var(--text-tertiary, #9aa4c4)",
  background: "transparent",
  border: "1px solid var(--border-default, #3a4166)",
  cursor: "pointer",
};
