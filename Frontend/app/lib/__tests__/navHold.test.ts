import { describe, expect, it } from "vitest";
import { actionLabel, autoActionLabel, fmtMinSec, holdChipLabel, holdTitle, type NavHold } from "../navHold";
import { pickHoldAlert } from "@/app/components/common/NavHoldAlertWatcher";
import type { AlertMockData } from "@/app/types";

const hold = (over: Partial<NavHold> = {}): NavHold => ({
  kind: "work", wpNo: 3, wpName: "복도 끝", since: 0, pos: [8.4, 2.1], reason: "60초 제자리",
  heldSec: 72, autoActionInSec: 228, actions: ["retry", "skip", "abort"], ...over,
});

describe("navHold 문구", () => {
  it("종류마다 버튼 이름이 다르다", () => {
    expect(actionLabel("work", "abort")).toBe("작업 중단");
    expect(actionLabel("guide", "retry")).toBe("다시 출발");
    expect(actionLabel("standby", "abort")).toBe("근무 중단");
    expect(actionLabel("manual", "abort")).toBe("이동 취소");
  });

  it("자동 처리는 작업·직접 이동·대기 장소만 있다", () => {
    expect(autoActionLabel("work")).toBe("작업 중단 · 충전소 복귀");
    expect(autoActionLabel("charge")).toBeNull();
    expect(autoActionLabel("guide")).toBeNull();
  });

  it("칩·제목", () => {
    expect(holdChipLabel(hold())).toBe("멈춤 — 도착 못 함");
    expect(holdChipLabel(hold({ kind: "guide", heldSec: 72 }))).toBe("이동 막힘 1:12");
    expect(holdChipLabel(hold({ kind: "standby" }))).toBe("복귀하지 못함 — 대기 장소로 보내 주세요");
    expect(holdTitle("charge")).toBe("충전소 복귀 못 함");
    expect(fmtMinSec(305)).toBe("5:05");
    expect(fmtMinSec(-3)).toBe("0:00");
  });
});

describe("pickHoldAlert", () => {
  const alert = (id: number, action: string, date: string) =>
    ({ id, date, log: { Action: action } }) as unknown as AlertMockData;

  it("멈춤 알림 중 안 본 가장 최근 것", () => {
    const list = [
      alert(1, "nav_unreached_hold", "2026-10-22T10:00:00"),
      alert(2, "guide_nav_blocked", "2026-10-22T10:05:00"),
      alert(3, "guide_follow_lost", "2026-10-22T10:09:00"),
    ];
    expect(pickHoldAlert(list, new Set())?.id).toBe(2);
    expect(pickHoldAlert(list, new Set([2]))?.id).toBe(1);
    expect(pickHoldAlert(list, new Set([1, 2]))).toBeNull();
  });

  it("안내 중 장애물 30초 직원 호출도 확인창을 띄운다(관제에 알림이 갔다는 표시)", () => {
    const list = [
      alert(4, "guide_obstacle_stop", "2026-10-30T10:00:00"),
      alert(5, "guide_obstacle_staff", "2026-10-30T10:00:30"),
      alert(6, "guide_obstacle_resume", "2026-10-30T10:00:40"),
    ];
    expect(pickHoldAlert(list, new Set())?.id).toBe(5);
    expect(pickHoldAlert(list, new Set([5]))).toBeNull();
  });
});
