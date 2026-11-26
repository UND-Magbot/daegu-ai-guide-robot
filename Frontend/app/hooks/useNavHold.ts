"use client";

import { useCallback, useEffect, useState } from "react";
import { navHoldApi, type NavHold } from "@/app/lib/navHold";

const POLL_MS = 3000;

/**
 * 이동 멈춤 상태 (`GET /nav/hold`). 백엔드 주행 상태는 로봇 하나(전역 1칸)라
 * robotName 이 지금 보는 로봇일 때만 돌려준다(useNavProgress 와 같은 가드).
 */
export function useNavHold(robotName: string | null | undefined, enabled: boolean) {
  const [data, setData] = useState<{ robotName: string | null; hold: NavHold | null } | null>(null);

  const reload = useCallback(async () => {
    try {
      setData(await navHoldApi.get());
    } catch {
      setData(null);
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      setData(null);
      return;
    }
    reload();
    const t = setInterval(reload, POLL_MS);
    return () => clearInterval(t);
  }, [enabled, reload]);

  const mine = !!data && !!robotName && data.robotName === robotName;
  return { hold: mine ? data!.hold : null, reload };
}
