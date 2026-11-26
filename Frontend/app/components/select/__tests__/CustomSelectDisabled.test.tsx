import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import CustomSelect from "@/app/components/select/CustomSelect";

afterEach(() => cleanup());

const OPTIONS = [
  { id: 1, label: "간호실" },
  { id: 2, label: "수납", disabled: true, disabledReason: "'수납' — 한쪽 방향 연결 때문에 충전소에서 갈 수 없습니다." },
];

describe("CustomSelect — 고를 수 없는 항목", () => {
  it("흐리게·'이동 불가' 표시·사유 툴팁, 눌러도 선택되지 않는다", () => {
    const onChange = vi.fn();
    render(<CustomSelect options={OPTIONS} value={null} onChange={onChange} placeholder="장소 선택" />);
    fireEvent.click(screen.getByText("장소 선택"));
    const blocked = screen.getByText("수납").closest("[role=option]")!;
    expect(blocked.getAttribute("aria-disabled")).toBe("true");
    expect(blocked.getAttribute("title")).toContain("한쪽 방향");
    expect(screen.getByText("이동 불가")).toBeTruthy();
    fireEvent.click(blocked);
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("간호실"));
    expect(onChange).toHaveBeenCalledWith(OPTIONS[0]);
  });

  it("검색 상자에서 Enter 는 고를 수 있는 첫 항목을 고른다", () => {
    const onChange = vi.fn();
    render(<CustomSelect options={[OPTIONS[1], OPTIONS[0]]} value={null} onChange={onChange} placeholder="장소 선택" searchable />);
    const input = screen.getByPlaceholderText("장소 선택");
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith(OPTIONS[0]);
  });
});
