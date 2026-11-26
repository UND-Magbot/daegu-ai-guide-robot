import { describe, expect, it } from "vitest";

import { robotConnectionNote } from "../robotConnection";

describe("robotConnectionNote", () => {
  it("연결됨 — 안내 문구 없음", () => {
    expect(robotConnectionNote("Online", "ok")).toEqual({ tone: "online", label: "연결됨", note: null });
  });

  it("연결 불안정 — 등록은 되고 실행 시각에 기다린다고 안내", () => {
    const r = robotConnectionNote("Error", "ok");
    expect(r?.label).toBe("연결 불안정");
    expect(r?.note).toContain("등록은 됩니다");
  });

  it("연결 안 됨 — 지금 바로 실행되는 작업은 등록 불가", () => {
    for (const n of ["Offline", "-"] as const) {
      const r = robotConnectionNote(n, "ok");
      expect(r?.tone).toBe("offline");
      expect(r?.note).toContain("지금 바로 실행되는 작업은 등록할 수 없습니다");
    }
  });

  it("서버 두절이면 로봇 상태를 단정하지 않는다", () => {
    expect(robotConnectionNote("Online", "unreachable")?.label).toBe("확인 불가");
    expect(robotConnectionNote(null, "unreachable")?.label).toBe("확인 불가");
  });

  it("실시간 상태에 없는 로봇은 표시하지 않는다", () => {
    expect(robotConnectionNote(null, "ok")).toBeNull();
  });
});
