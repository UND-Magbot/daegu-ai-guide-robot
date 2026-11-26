import { describe, expect, it } from "vitest";
import { buildStartOptions, defaultStartPick, IN_PLACE_START, startButtonLabel } from "../guideStartPick";

const PLACES = [
  { id: 401, label: "로비" },
  { id: 402, label: "로봇 대기 장소" },
  { id: 403, label: "원무과" },
];

describe("안내 시작 창 — 기본 시작 장소", () => {
  it("기본 시작 장소가 운영 맵 목록에 있으면 그 장소를 미리 고른다", () => {
    expect(defaultStartPick(402, PLACES)).toBe(402);
  });

  it("기본값이 없거나 목록에 없으면(맵 변경·삭제) 현재 위치", () => {
    expect(defaultStartPick(null, PLACES)).toBe(0);
    expect(defaultStartPick(999, PLACES)).toBe(0);
    expect(defaultStartPick(402, [])).toBe(0);
  });

  it("목록은 기본 장소(기본) → 현재 위치 → 나머지, 기본 장소는 한 번만", () => {
    expect(buildStartOptions(402, PLACES)).toEqual([
      { id: 402, label: "로봇 대기 장소 (기본)" },
      IN_PLACE_START,
      { id: 401, label: "로비" },
      { id: 403, label: "원무과" },
    ]);
  });

  it("기본 장소가 없으면 현재 위치가 맨 위", () => {
    expect(buildStartOptions(0, PLACES)).toEqual([IN_PLACE_START, ...PLACES]);
  });

  it("버튼은 실제로 갈 곳을 보여 준다", () => {
    expect(startButtonLabel(402, PLACES)).toBe("로봇 대기 장소(으)로 이동 후 시작");
    expect(startButtonLabel(0, PLACES)).toBe("현재 위치에서 시작");
  });

  it("노드망으로 갈 수 없게 된 기본 장소는 미리 고르지 않는다(현재 위치로)", () => {
    const places = [{ id: 401, label: "로비" }, { id: 402, label: "로봇 대기 장소", disabled: true, disabledReason: "미연결" }];
    expect(defaultStartPick(402, places)).toBe(0);
    expect(defaultStartPick(401, places)).toBe(401);
  });
});
