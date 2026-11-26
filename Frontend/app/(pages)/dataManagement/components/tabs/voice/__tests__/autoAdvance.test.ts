import { describe, expect, it } from "vitest";
import { autoAdvanceLabel, autoAdvancePayload, toAutoMode } from "../autoAdvance";

describe("운영자 단계 자동 진행 — 단계 값과 화면 선택", () => {
  it("단계 값에서 선택을 고른다 (비움=기본, 음수=끔, 0 이상=직접 입력)", () => {
    expect(toAutoMode(null)).toBe("default");
    expect(toAutoMode(undefined)).toBe("default");
    expect(toAutoMode(-1)).toBe("off");
    expect(toAutoMode(0)).toBe("sec");
    expect(toAutoMode(20)).toBe("sec");
  });

  it("저장 요청 — 기본은 비우기, 끔은 -1, 직접 입력은 그 초", () => {
    expect(autoAdvancePayload("default", 20)).toEqual({ clearAutoAdvance: true });
    expect(autoAdvancePayload("off", 20)).toEqual({ autoAdvanceSec: -1 });
    expect(autoAdvancePayload("sec", 20)).toEqual({ autoAdvanceSec: 20 });
    expect(autoAdvancePayload("sec", 0)).toEqual({ autoAdvanceSec: 0 });
  });

  it("직접 입력 범위 밖(음수·1800 초과·소수)이면 저장하지 않는다", () => {
    expect(autoAdvancePayload("sec", -5)).toBeNull();
    expect(autoAdvancePayload("sec", 1801)).toBeNull();
    expect(autoAdvancePayload("sec", 2.5)).toBeNull();
    expect(autoAdvancePayload("sec", Number.NaN)).toBeNull();
  });

  it("목록 표시 — 기본 규칙이면 표시 없음", () => {
    expect(autoAdvanceLabel(null)).toBeNull();
    expect(autoAdvanceLabel(-1)).toBe("자동 진행 끔");
    expect(autoAdvanceLabel(30)).toBe("30초 뒤 자동 진행");
  });
});
