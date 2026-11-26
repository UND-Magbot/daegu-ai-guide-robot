// 층 이동 뒤 엘리베이터 입구 시드 — 로봇에 전달될 때까지 몇 번 다시 보낸다.
// 10-30 11:58 1F: 시드가 일시 통신 끊김(TCP·UDP 시간 초과)으로 한 번 실패했는데 다시 보내지 않았고,
// 로봇은 측위 '정상'을 보내 '완료'가 통과해 옛 층 좌표로 확정됐다. 1분 뒤 주입은 정상 전달됐다.

export type SeedOutcome = "sent" | "failed" | "skipped" | "none";

export interface SeedAttempt {
  outcome: SeedOutcome;
  msg?: string;
}

export const SEED_TRIES = 3;
export const SEED_RETRY_MS = 2000;

/** attempt 를 전달될 때까지(최대 tries 번) 부른다. 건너뜀·장소 없음은 다시 보내지 않는다. */
export async function seedWithRetry(
  attempt: () => Promise<SeedAttempt>,
  tries: number = SEED_TRIES,
  retryMs: number = SEED_RETRY_MS,
  sleep: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
): Promise<SeedAttempt & { tries: number }> {
  let last: SeedAttempt = { outcome: "failed" };
  for (let i = 1; i <= tries; i += 1) {
    last = await attempt();
    if (last.outcome !== "failed") return { ...last, tries: i };
    if (i < tries) await sleep(retryMs);
  }
  return { ...last, tries };
}
