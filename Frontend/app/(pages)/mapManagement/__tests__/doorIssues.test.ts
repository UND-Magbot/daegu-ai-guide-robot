import { describe, expect, it, vi } from "vitest";

vi.mock("@/app/lib/api", () => ({ apiFetch: vi.fn() }));

import { doorIssues, type Draft, type DoorDto } from "../components/tabs/facility/DoorPanel";

const DRAFT: Draft = {
  floorId: 3, mapId: 47, near: 10, far: 11, controlType: "auto_sensor",
  detectType: "vision", openTimeoutSec: 20, passTimeoutSec: 30, controlConfig: {},
};

const saved = (over: Partial<DoorDto> = {}): DoorDto => ({
  id: 1, businessId: 1, name: "본관", floorId: 3, mapId: 47, nearLocationId: 10, farLocationId: 11,
  controlType: "auto_sensor", controlConfig: {}, detectType: "vision", openTimeoutSec: 20, passTimeoutSec: 30,
  complete: true, problem: null, ...over,
});

describe("자동문 등록 완료 판정(doorIssues)", () => {
  it("맵·장소·센서식이 모두 있고 서버 사유가 없으면 등록 완료", () => {
    expect(doorIssues(DRAFT, saved())).toEqual([]);
  });

  it("빈 칸은 무엇이 비었는지 알려 준다", () => {
    const issues = doorIssues({ ...DRAFT, mapId: "", near: "" }, saved());
    expect(issues).toHaveLength(1);
    expect(issues[0]).toContain("맵");
    expect(issues[0]).toContain("통과 POI");
  });

  it("자동 통과를 지원하지 않는 개폐 방식(접점/릴레이)은 미완료", () => {
    expect(doorIssues({ ...DRAFT, controlType: "gpio" }, saved({ controlType: "gpio" }))[0]).toContain("지원하지 않습니다");
  });

  it("상시 개방은 지원", () => {
    expect(doorIssues({ ...DRAFT, controlType: "manual" }, saved({ controlType: "manual" }))).toEqual([]);
  });

  it("저장본에 서버 사유(지워진 맵)가 있으면 번호가 다 채워져 있어도 미완료 — 10-27 맵 48 사례", () => {
    const s = saved({ mapId: 48, nearLocationId: 444, farLocationId: 446, complete: false,
      problem: "지정한 맵(#48)이 삭제되었습니다. 현재 맵으로 다시 지정해 주세요." });
    const d = { ...DRAFT, mapId: 48, near: 444, far: 446 };
    expect(doorIssues(d, s)).toEqual(["지정한 맵(#48)이 삭제되었습니다. 현재 맵으로 다시 지정해 주세요."]);
  });

  it("초안에서 맵·장소를 새로 고르면 옛 서버 사유는 보이지 않는다(저장하면 서버가 다시 판정)", () => {
    const s = saved({ mapId: 48, nearLocationId: 444, farLocationId: 446, complete: false, problem: "지정한 맵(#48)이 삭제되었습니다." });
    expect(doorIssues(DRAFT, s)).toEqual([]);
  });

  it("저장본이 없어도 동작", () => {
    expect(doorIssues(DRAFT, null)).toEqual([]);
  });
});
