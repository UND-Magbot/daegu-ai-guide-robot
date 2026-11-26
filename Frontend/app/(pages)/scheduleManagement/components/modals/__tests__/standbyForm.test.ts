import { describe, expect, it } from "vitest";
import {
  MOTION_RESPONSE,
  SINGLE_RESPONSE,
  keepPlace,
  keepResponse,
  placeBlockedReason,
  placesOnRobotMap,
  responseHint,
  responseOptions,
  responsePayload,
  scenarioMapWarning,
} from "../standbyForm";

const places = [
  { id: 1, name: "로비 대기", mapId: 47 },
  { id: 2, name: "약제실", mapId: 47 },
  { id: 3, name: "로봇대기장소", mapId: 43 },
  { id: 4, name: "맵 없는 장소", mapId: null },
];
const scenarios = [
  { code: "hospital", name: "병원 안내", mapId: 47 },
  { code: "public_office", name: "관공서 안내", mapId: 43 },
];

describe("대기 장소 — 로봇이 지금 있는 맵의 장소만", () => {
  it("로봇 맵(47)의 장소만 이름순으로", () => {
    expect(placesOnRobotMap(places, 47).map((p) => p.name)).toEqual(["로비 대기", "약제실"]);
  });
  it("로봇을 고르기 전·맵을 모르면 후보가 없다", () => {
    expect(placesOnRobotMap(places, null)).toEqual([]);
    expect(placesOnRobotMap(places, undefined)).toEqual([]);
  });
  it("막힌 이유 — 로봇 미선택 / 맵 모름 / 그 맵에 장소 없음", () => {
    expect(placeBlockedReason(false, 47, 2)).toBe("로봇을 먼저 선택하세요");
    expect(placeBlockedReason(true, null, 0)).toContain("현재 맵을 알 수 없습니다");
    expect(placeBlockedReason(true, 99, 0)).toContain("장소가 없습니다");
    expect(placeBlockedReason(true, 47, 2)).toBeNull();
  });
  it("로봇을 바꾸면 새 로봇 맵에 없는 대기 장소는 비운다", () => {
    expect(keepPlace(1, placesOnRobotMap(places, 47))).toBe(1);
    expect(keepPlace(1, placesOnRobotMap(places, 43))).toBeNull();
    expect(keepPlace(null, placesOnRobotMap(places, 47))).toBeNull();
  });
});

describe("응대 방식", () => {
  it("단발 응대가 맨 앞, 단순 동작이 그 다음, 켜진 시나리오가 뒤에", () => {
    expect(responseOptions(scenarios)).toEqual([
      { id: SINGLE_RESPONSE, label: "단발 응대 (목적지 안내)" },
      { id: MOTION_RESPONSE, label: "단순 동작 (앉아·일어서)" },
      { id: "hospital", label: "안내 시나리오 · 병원 안내" },
      { id: "public_office", label: "안내 시나리오 · 관공서 안내" },
    ]);
    expect(responseOptions([])).toHaveLength(2);
  });
  it("마이크가 없는 로봇은 단순 동작을 고를 수 없다", () => {
    expect(responseOptions(scenarios, false).map((o) => o.id)).toEqual([SINGLE_RESPONSE, "hospital", "public_office"]);
  });
  it("저장 값 — 단순 동작은 시나리오 코드를 비우고 mode=motion, 나머지는 mode 없음", () => {
    expect(responsePayload(MOTION_RESPONSE)).toEqual({ StandbyScenarioCode: null, StandbyResponseMode: "motion" });
    expect(responsePayload("hospital")).toEqual({ StandbyScenarioCode: "hospital", StandbyResponseMode: null });
    expect(responsePayload(SINGLE_RESPONSE)).toEqual({ StandbyScenarioCode: null, StandbyResponseMode: null });
  });
  it("단순 동작 설명 — 마이크 방식 안내 문구는 붙이지 않는다", () => {
    expect(responseHint(MOTION_RESPONSE, scenarios, "mic")).toContain("\"큐리야, 앉아\" 또는 \"큐리야, 일어서\"");
    expect(responseHint(MOTION_RESPONSE, scenarios, "mic")).not.toContain("주기적으로 안내");
    expect(scenarioMapWarning(MOTION_RESPONSE, scenarios, 47)).toBeNull();
  });
  it("로봇을 바꿔 마이크가 없어지면 단순 동작은 단발 응대로 돌린다", () => {
    expect(keepResponse(MOTION_RESPONSE, scenarios)).toBe(MOTION_RESPONSE);
    expect(keepResponse(MOTION_RESPONSE, scenarios, false)).toBe(SINGLE_RESPONSE);
  });
  it("설명 문구", () => {
    expect(responseHint(SINGLE_RESPONSE, scenarios)).toContain("목적지 한 곳");
    expect(responseHint("hospital", scenarios)).toContain("병원 안내 시나리오를 첫 단계부터");
  });
  it("호출 방식을 고르면 시작 방법을 적는다", () => {
    expect(responseHint("hospital", scenarios, "mic")).toBe(
      "대기 장소에서 \"큐리야\"라고 부르면 병원 안내 시나리오를 첫 단계부터 진행하고, 끝나면 대기 장소로 돌아옵니다."
        + " 로봇이 대기 장소에서 \"큐리야\"라고 부르는 방법을 주기적으로 안내합니다.",
    );
    expect(responseHint("hospital", scenarios, "button")).not.toContain("주기적으로 안내");
    expect(responseHint(SINGLE_RESPONSE, scenarios, "mic")).toContain("\"큐리야\"만 부르면 어디로 갈지 묻습니다");
    expect(responseHint("hospital", scenarios, "button")).toContain("[안내 시작]을 누르면");
    expect(responseHint(SINGLE_RESPONSE, scenarios, null)).toContain("호출되면");
  });
  it("시나리오 맵이 로봇 맵과 다르면 경고, 같거나 단발이면 없음", () => {
    expect(scenarioMapWarning("public_office", scenarios, 47)).toContain("'관공서 안내' 시나리오의 맵이 다릅니다");
    expect(scenarioMapWarning("hospital", scenarios, 47)).toBeNull();
    expect(scenarioMapWarning(SINGLE_RESPONSE, scenarios, 47)).toBeNull();
    expect(scenarioMapWarning("public_office", scenarios, null)).toBeNull();
  });
  it("로봇을 바꿔 그 시나리오가 없으면 단발 응대로 돌린다", () => {
    expect(keepResponse("hospital", scenarios)).toBe("hospital");
    expect(keepResponse("hospital", [])).toBe(SINGLE_RESPONSE);
    expect(keepResponse(SINGLE_RESPONSE, [])).toBe(SINGLE_RESPONSE);
  });
});
