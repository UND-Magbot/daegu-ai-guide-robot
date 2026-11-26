/**
 * 이동 멈춤(막힘·도착 못 함) — 백엔드 `navigation/nav_hold.py` 와 1:1.
 *
 * 주행 감시가 '60초 제자리 + 다시 보내기 3번 실패' 를 잡으면 건너뛰지 않고 멈춘다.
 * 운영자는 로봇 상세 카드나 확인창에서 고르고, 응답이 없으면 종류별로 자동 처리된다.
 */
import { apiFetch } from "@/app/lib/api";

export type NavHoldKind = "guide" | "work" | "manual" | "standby" | "charge";
export type NavHoldAction = "retry" | "skip" | "abort";
/** 장애물 정지일 때만 — NOS 감지 노드가 본 지금 앞(관제 obstacle_hold.front_state) */
export type NavHoldFront = "clear" | "blocked" | "unknown";

export type NavHold = {
  kind: NavHoldKind;
  wpNo: number;
  wpName: string;
  since: number;
  pos: [number, number] | null;
  reason: string;
  heldSec: number;
  /** 응답 없을 때 자동 처리까지 남은 초 — 자동 처리가 없는 종류는 null */
  autoActionInSec: number | null;
  actions: NavHoldAction[];
  front?: NavHoldFront;
};

export type NavHoldRes = { robotName: string | null; hold: NavHold | null };

/** 알림 action — 확인창을 띄우는 것(장애물 2분 직원 호출 포함, 10-30) */
export const HOLD_ALERT_ACTIONS = ["nav_unreached_hold", "guide_nav_blocked", "guide_obstacle_staff"] as const;

const FRONT_LABELS: Record<NavHoldFront, string> = { clear: "비어 있음", blocked: "막혀 있음", unknown: "확인 중" };

/** 장애물 정지의 '지금 앞' 문구 — 장애물 정지가 아니면 null */
export function frontLabel(hold: NavHold): string | null {
  return hold.front ? FRONT_LABELS[hold.front] : null;
}

/** '다시 출발' 을 막는 사유 — 장애물 정지인데 앞이 비었다고 확인되지 않았을 때(서버도 같은 이유로 거절한다) */
export function retryBlockedReason(hold: NavHold): string | null {
  return hold.front && hold.front !== "clear" ? "앞이 아직 막혀 있어 출발할 수 없습니다" : null;
}

const ACTION_LABELS: Record<NavHoldKind, Partial<Record<NavHoldAction, string>>> = {
  guide: { retry: "다시 출발", abort: "안내 종료" },
  work: { retry: "다시 시도", skip: "건너뛰고 계속", abort: "작업 중단" },
  manual: { retry: "다시 시도", skip: "건너뛰고 계속", abort: "이동 취소" },
  standby: { retry: "다시 시도", abort: "근무 중단" },
  charge: { retry: "다시 시도" },
};

const AUTO_LABELS: Partial<Record<NavHoldKind, string>> = {
  work: "작업 중단 · 충전소 복귀",
  manual: "이동 취소",
  standby: "자리 비움(복귀하지 못함)",
};

export function actionLabel(kind: NavHoldKind, action: NavHoldAction): string {
  return ACTION_LABELS[kind][action] ?? action;
}

export function autoActionLabel(kind: NavHoldKind): string | null {
  return AUTO_LABELS[kind] ?? null;
}

export function fmtMinSec(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** 카드 상태 칩 문구 */
export function holdChipLabel(hold: NavHold): string {
  if (hold.kind === "guide") return `이동 막힘 ${fmtMinSec(hold.heldSec)}`;
  if (hold.kind === "standby") return "복귀하지 못함 — 대기 장소로 보내 주세요";
  if (hold.kind === "charge") return "멈춤 — 충전소 도착 못 함";
  return "멈춤 — 도착 못 함";
}

/** 확인창 제목 */
export function holdTitle(kind: NavHoldKind): string {
  if (kind === "guide") return "안내 이동 막힘";
  if (kind === "standby") return "대기 장소 복귀 못 함";
  if (kind === "charge") return "충전소 복귀 못 함";
  return "이동 지점 도착 못 함";
}

export const navHoldApi = {
  get: async (): Promise<NavHoldRes> => {
    const res = await apiFetch(`/nav/hold`);
    if (!res.ok) throw new Error(`멈춤 상태 조회 실패 (${res.status})`);
    return res.json();
  },
  act: async (action: NavHoldAction): Promise<void> => {
    const res = await apiFetch(`/nav/hold-action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw new Error(body?.detail ?? `처리 실패 (${res.status})`);
    }
  },
};
