import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import GuideStepPanel from "../GuideStepPanel";
import { blankStep, type DraftStep } from "../scenarioDraft";

const step = (code: string, patch: Partial<DraftStep> = {}): DraftStep => ({ ...blankStep(code), sayText: "멘트", ...patch });

function renderPanel(target: DraftStep, onChange = vi.fn()) {
  const steps = [step("greet", { nextStepCode: target.stepCode }), target, step("finish")];
  render(
    <GuideStepPanel
      steps={steps}
      index={1}
      mapId={47}
      placeOptions={[{ id: 7, label: "원무과_접수" }]}
      userIntents={["YES_NO", "PURPOSE", "DESTINATION", "FORM_COMPLETE"]}
      operatorInputs={["TICKET", "COUNTER", "RESUME"]}
      issues={[]}
      onChange={onChange}
      onInsert={vi.fn()}
      onDuplicate={vi.fn()}
      onDelete={vi.fn()}
    />
  );
  return onChange;
}

afterEach(cleanup);

describe("기다림 종류별 칸", () => {
  it("말만 — 인식 종류·입력 종류·이동 장소가 없다", () => {
    renderPanel(step("s_001"));
    expect(screen.queryByRole("radiogroup", { name: "인식 종류" })).toBeNull();
    expect(screen.queryByRole("radiogroup", { name: "입력 종류" })).toBeNull();
    expect(screen.queryByText("이동 장소")).toBeNull();
  });

  it("대답 — 인식 종류가 없으면 저장 불가 안내, 응답 대기·재질문·무응답 칸", () => {
    renderPanel(step("s_001", { waitFor: "user" }));
    expect(screen.getByRole("radiogroup", { name: "인식 종류" })).toBeTruthy();
    expect(screen.getByText("하나를 고르세요 — 없으면 저장할 수 없습니다")).toBeTruthy();
    expect(screen.getByText("응답 대기")).toBeTruthy();
    expect(screen.getByText("재질문")).toBeTruthy();
    expect(screen.getByText("끝내 무응답")).toBeTruthy();
  });

  it("대답·예/아니요 — '아니요'면 칸", () => {
    renderPanel(step("s_001", { waitFor: "user", expectedIntent: "YES_NO" }));
    expect(screen.getByText("'아니요'면")).toBeTruthy();
    expect(screen.queryByText("'없어요'면")).toBeNull();
  });

  it("대답·목적지 — '없어요'면 칸(더 갈 곳 묻기용), 작성 완료는 없음", () => {
    renderPanel(step("s_001", { waitFor: "user", expectedIntent: "DESTINATION" }));
    expect(screen.getByText("'없어요'면")).toBeTruthy();
    cleanup();
    renderPanel(step("s_001", { waitFor: "user", expectedIntent: "FORM_COMPLETE" }));
    expect(screen.queryByText("'없어요'면")).toBeNull();
    expect(screen.queryByText("'아니요'면")).toBeNull();
  });

  it("운영자 — 입력 종류와 자동 진행", () => {
    renderPanel(step("s_001", { waitFor: "operator", expectedIntent: "TICKET" }));
    expect(screen.getByRole("radiogroup", { name: "입력 종류" })).toBeTruthy();
    expect(screen.getByText("자동 진행")).toBeTruthy();
    expect(screen.queryByRole("radiogroup", { name: "인식 종류" })).toBeNull();
  });

  it("이동 — 정한 장소면 이동 장소, 고객 목적지면 안내 문구", () => {
    renderPanel(step("s_001", { waitFor: "arrival", targetLocationId: 7 }));
    expect(screen.getByText("이동 장소")).toBeTruthy();
    cleanup();
    renderPanel(step("s_001", { waitFor: "arrival", slotName: "목적지" }));
    expect(screen.queryByText("이동 장소")).toBeNull();
    expect(screen.getByText(/고객이 말한 목적지/)).toBeTruthy();
  });

  it("이동 — 부르는 이름: 비우면 '곧 도착합니다', 넣으면 그 이름으로 안내 문구가 바뀐다", () => {
    const onChange = renderPanel(step("s_001", { waitFor: "arrival", targetLocationId: 7 }));
    expect(screen.getByText(/비우면 도착 직전에 "곧 도착합니다."/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("부르는 이름"), { target: { value: "원무과" } });
    expect((onChange.mock.calls[0][0] as DraftStep).spokenPlaceName).toBe("원무과");
    cleanup();
    renderPanel(step("s_001", { waitFor: "arrival", slotName: "목적지", spokenPlaceName: "진료실 입구" }));
    expect(screen.getByText('도착 직전에 "곧 진료실 입구에 도착합니다."라고 말합니다.')).toBeTruthy();
  });

  it("이동 — 진행 멘트: 비우면 기본 문장, '-' 면 말하지 않음, 넣으면 그 값", () => {
    const onChange = renderPanel(step("s_001", { waitFor: "arrival", targetLocationId: 7, spokenPlaceName: "원무과" }));
    expect(screen.getByText(/비우면 "원무과 방향으로 이동하고 있습니다."라고 말합니다/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("진행 멘트"), { target: { value: "편의점 방향으로 이동하고 있습니다." } });
    expect((onChange.mock.calls[0][0] as DraftStep).progressText).toBe("편의점 방향으로 이동하고 있습니다.");
    cleanup();
    renderPanel(step("s_001", { waitFor: "arrival", targetLocationId: 7, progressText: "편의점 방향으로 이동하고 있습니다." }));
    expect(screen.getByText(/앞뒤 안내와 겹치지 않는 조금 긴 구간에서 한 번 말합니다/)).toBeTruthy();
    cleanup();
    renderPanel(step("s_001", { waitFor: "arrival", targetLocationId: 7, progressText: "-" }));
    expect(screen.getByText("이 단계는 이동 중에 따로 말하지 않습니다.")).toBeTruthy();
  });

  it("말만 단계에는 부르는 이름·진행 멘트 칸이 없다", () => {
    renderPanel(step("s_001"));
    expect(screen.queryByLabelText("부르는 이름")).toBeNull();
    expect(screen.queryByLabelText("진행 멘트")).toBeNull();
  });
});

describe("기다림 바꾸기", () => {
  it("이동 → 대답을 누르면 장소가 비워진 단계로 바뀐다", () => {
    const onChange = renderPanel(step("s_001", { waitFor: "arrival", targetLocationId: 7 }));
    fireEvent.click(screen.getByRole("radio", { name: "대답" }));
    const next = onChange.mock.calls[0][0] as DraftStep;
    expect(next.waitFor).toBe("user");
    expect(next.targetLocationId).toBeNull();
  });

  it("인식 종류를 예/아니요 → 작성 완료로 바꾸면 '아니요' 갈래가 비워진다", () => {
    const onChange = renderPanel(step("s_001", { waitFor: "user", expectedIntent: "YES_NO", onNoStepCode: "finish" }));
    fireEvent.click(screen.getByRole("radio", { name: "작성 완료" }));
    expect((onChange.mock.calls[0][0] as DraftStep).onNoStepCode).toBeNull();
  });
});
