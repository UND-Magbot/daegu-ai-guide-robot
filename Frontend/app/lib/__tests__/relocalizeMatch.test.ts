import { describe, expect, it } from "vitest";
import { bodyToMap, matchLevel, scanToBody } from "@/app/lib/relocalizeMatch";

describe("relocalizeMatch", () => {
  it("점수 단계 — 75 이상 잘 맞음, 60 미만 낮음", () => {
    expect(matchLevel(80)).toBe("good");
    expect(matchLevel(75)).toBe("good");
    expect(matchLevel(70)).toBe("check");
    expect(matchLevel(60)).toBe("check");
    expect(matchLevel(59.9)).toBe("low");
  });

  it("로봇 기준 앞 1m 점은 동쪽(yaw 0)이면 x+1, 북쪽(yaw 90°)이면 y+1", () => {
    expect(bodyToMap([[1, 0]], { x: 2, y: 3, yaw: 0 })[0]).toEqual([3, 3]);
    const [x, y] = bodyToMap([[1, 0]], { x: 2, y: 3, yaw: Math.PI / 2 })[0];
    expect(x).toBeCloseTo(2);
    expect(y).toBeCloseTo(4);
  });

  it("지도 좌표 스캔을 로봇 기준으로 되돌리면 원래 점", () => {
    const odom = { x: -1.2, y: 4.5, yaw: 0.7 };
    const body = [[1.5, -0.3], [-2, 0.8]];
    const back = scanToBody(bodyToMap(body, odom), "map", odom)!;
    back.forEach((p, i) => {
      expect(p[0]).toBeCloseTo(body[i][0]);
      expect(p[1]).toBeCloseTo(body[i][1]);
    });
  });

  it("body 스캔은 그대로, 위치 모르는 map 스캔·빈 스캔은 null", () => {
    expect(scanToBody([[1, 2]], "body", null)).toEqual([[1, 2]]);
    expect(scanToBody([[1, 2]], "map", null)).toBeNull();
    expect(scanToBody([], "body", null)).toBeNull();
  });
});
