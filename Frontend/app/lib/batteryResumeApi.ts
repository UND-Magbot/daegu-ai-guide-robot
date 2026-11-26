import { apiFetch } from "@/app/lib/api";

/** 충전 후 이어 갈 일 — 백엔드 robot_control/battery_resume.current */
export type BatteryResumePending = {
  token: number;
  kind: "standby" | "route" | "task";
  label: string;
  confirm_asked: boolean;
};

async function unwrap<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `요청에 실패했습니다 (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* 본문이 JSON이 아니면 기본 메시지를 쓴다 */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const batteryResumeApi = {
  pending: () =>
    apiFetch("/robot/battery-resume").then(unwrap<{ pending: BatteryResumePending | null }>),

  decide: (token: number, decision: "resume" | "end") =>
    apiFetch(`/robot/battery-resume/${token}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    }).then(unwrap<{ ok: boolean; msg: string }>),
};
