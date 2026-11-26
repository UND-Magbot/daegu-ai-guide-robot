import { describe, it, expect } from "vitest";
import { foldStandbyGuides, standbyGuideSummary, type StandbyGuideRow } from "@/app/lib/standbyGuide";

// 11-05 실제 데이터 모양: 상시 대기 185 + 큐리야 호출 세션 574(ScheduleId 185) + 화면에서 바로 연 573·575
const standby: StandbyGuideRow = { id: 185, ScheduleMode: "standby", Repeat: "Y", TaskStatus: "진행중", WorkName: "상시대기 테스트" };
const g573: StandbyGuideRow = { id: -573, Kind: "guide", ScheduleId: null, TaskStatus: "취소", WorkName: "병원 안내" };
const g574: StandbyGuideRow = { id: -574, Kind: "guide", ScheduleId: 185, TaskStatus: "진행중", WorkName: "병원 안내 · 정형외과" };
const g575: StandbyGuideRow = { id: -575, Kind: "guide", ScheduleId: null, TaskStatus: "진행중", WorkName: "병원 안내 · 정형외과" };

describe("foldStandbyGuides", () => {
  it("상시 대기 근무에서 연 세션은 근무 카드에 접혀 따로 안 보인다", () => {
    expect(foldStandbyGuides([standby, g573, g574]).map((r) => r.id)).toEqual([185, -573]);
  });

  it("근무와 무관하게 화면에서 연 세션은 그대로 남는다", () => {
    expect(foldStandbyGuides([standby, g575]).map((r) => r.id)).toEqual([185, -575]);
  });

  it("근무 카드가 목록에 없으면(오늘 회차 취소 등) 세션을 지우지 않는다", () => {
    expect(foldStandbyGuides([g574]).map((r) => r.id)).toEqual([-574]);
  });

  it("상시 대기가 아닌 일정 id 와 같아도 접지 않는다", () => {
    const once: StandbyGuideRow = { id: 185, ScheduleMode: "once", TaskStatus: "진행중", WorkName: "순찰" };
    expect(foldStandbyGuides([once, g574]).map((r) => r.id)).toEqual([185, -574]);
  });
});

describe("standbyGuideSummary", () => {
  it("응대 중이면 그 안내를 보여 준다", () => {
    expect(standbyGuideSummary([g573, g574, g575], 185)).toBe("응대 중 · 병원 안내 · 정형외과");
  });

  it("끝났으면 오늘 응대 건수", () => {
    const done = { ...g574, TaskStatus: "오류" };
    const g2 = { ...g574, id: -580, TaskStatus: "완료" };
    expect(standbyGuideSummary([done, g2], 185)).toBe("오늘 응대 2건");
  });

  it("그 근무의 응대가 없으면 줄을 안 붙인다", () => {
    expect(standbyGuideSummary([g573, g575], 185)).toBeNull();
  });
});
