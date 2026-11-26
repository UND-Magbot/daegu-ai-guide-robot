import { describe, expect, it, vi } from 'vitest';

import { seedWithRetry, type SeedAttempt } from '../relocalizeSeed';

const noSleep = () => Promise.resolve();

function seq(...outs: SeedAttempt['outcome'][]) {
  const fn = vi.fn<() => Promise<SeedAttempt>>();
  outs.forEach((o) => fn.mockResolvedValueOnce({ outcome: o }));
  return fn;
}

describe('seedWithRetry', () => {
  it('첫 번에 전달되면 한 번만 보낸다', async () => {
    const fn = seq('sent');
    const r = await seedWithRetry(fn, 3, 0, noSleep);
    expect(r).toMatchObject({ outcome: 'sent', tries: 1 });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('일시 실패 뒤 전달되면 거기서 멈춘다(11:58 같은 순간 끊김)', async () => {
    const fn = seq('failed', 'sent');
    const r = await seedWithRetry(fn, 3, 0, noSleep);
    expect(r).toMatchObject({ outcome: 'sent', tries: 2 });
  });

  it('끝까지 실패하면 실패를 돌려준다(화면이 빨간 안내·완료 막기)', async () => {
    const fn = seq('failed', 'failed', 'failed');
    const r = await seedWithRetry(fn, 3, 0, noSleep);
    expect(r).toMatchObject({ outcome: 'failed', tries: 3 });
  });

  it('층 이동이 아니라 건너뛰었거나 장소가 없으면 다시 보내지 않는다', async () => {
    expect(await seedWithRetry(seq('skipped'), 3, 0, noSleep)).toMatchObject({ outcome: 'skipped', tries: 1 });
    expect(await seedWithRetry(seq('none'), 3, 0, noSleep)).toMatchObject({ outcome: 'none', tries: 1 });
  });

  it('다시 보내기 전에 간격만큼 기다린다', async () => {
    const sleep = vi.fn(() => Promise.resolve());
    await seedWithRetry(seq('failed', 'failed', 'sent'), 3, 2000, sleep);
    expect(sleep).toHaveBeenCalledTimes(2);
    expect(sleep).toHaveBeenCalledWith(2000);
  });
});
