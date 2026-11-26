"""방문자 한 명과의 안내 세션 상태.

구간마다 대화가 개입하므로, LLM 이 맥락을 유지하려면 이전 대화와 이미 다녀온
장소를 기억해야 한다. 세션이 끝나면 버리고 다음 방문자를 새로 받는다.
"""

import time
from dataclasses import dataclass, field

# LLM 컨텍스트가 무한정 커지지 않도록 최근 N 턴만 유지한다.
MAX_TURNS = 12


@dataclass
class Session:
    started_at: float = field(default_factory=time.time)
    last_input_at: float = field(default_factory=time.time)
    history: list[dict] = field(default_factory=list)
    visited: list[dict] = field(default_factory=list)   # [{"id":..., "name":...}]
    active: bool = True

    def add_turn(self, user_text: str, robot_say: str) -> None:
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": robot_say})
        if len(self.history) > MAX_TURNS * 2:
            self.history = self.history[-MAX_TURNS * 2:]
        self.last_input_at = time.time()

    def note_visit(self, place_id: int, name: str) -> None:
        self.visited.append({"id": place_id, "name": name})

    def idle_for(self) -> float:
        return time.time() - self.last_input_at

    def end(self) -> None:
        self.active = False

    def summary(self) -> str:
        names = ", ".join(v["name"] for v in self.visited) or "없음"
        mins = (time.time() - self.started_at) / 60
        return f"{mins:.1f}분, 방문 {len(self.visited)}곳: {names}"
