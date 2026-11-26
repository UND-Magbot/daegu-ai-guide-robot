import { describe, expect, it } from 'vitest';

import { screenHeadingDeg } from '../yaw';

// 위=+Y(북) 화면: 화면 y 는 아래로 커진다
const northUp = (wx: number, wy: number) => ({ x: wx * 20, y: -wy * 20 });
// 지도를 시계 90° 돌려 보여 주는 화면(+X 가 아래)
const rotated = (wx: number, wy: number) => ({ x: wy * 20, y: wx * 20 });

describe('screenHeadingDeg', () => {
  it('북쪽 위 화면 — 동(0)은 오른쪽, 북(+90°)은 위, 남(−90°)은 아래', () => {
    expect(screenHeadingDeg(northUp, 1, 2, 0)).toBeCloseTo(0);
    expect(screenHeadingDeg(northUp, 1, 2, Math.PI / 2)).toBeCloseTo(-90);
    expect(screenHeadingDeg(northUp, 1, 2, -Math.PI / 2)).toBeCloseTo(90);
  });

  it('지도가 회전돼 보이면 화살표도 그 변환을 따른다', () => {
    expect(screenHeadingDeg(rotated, 0, 0, 0)).toBeCloseTo(90);
    expect(screenHeadingDeg(rotated, 0, 0, Math.PI / 2)).toBeCloseTo(0);
  });

  it('화면에서 두 점이 겹치면 null', () => {
    expect(screenHeadingDeg(() => ({ x: 5, y: 5 }), 0, 0, 1)).toBeNull();
  });
});
