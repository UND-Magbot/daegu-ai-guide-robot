"""가상 시계 — time.sleep 을 가로채 400초 주행을 1초 안에 재생한다.

sleep(dt) 는 그 사이 예약된 이벤트를 시각 순서대로 실행하고, 등록된 ticker(가짜 로봇)를
그만큼 진행시킨다. 정지 조건이 맞으면 SimDone 을 던져 관제 루프(nav_thread 등)를 끊는다.
"""
from __future__ import annotations

import heapq
import itertools
import time as _real_time
import types
from typing import Callable


class SimDone(BaseException):
    """시뮬 종료 신호. 관제 코드의 `except Exception` 에 잡히지 않도록 BaseException 이다."""


class Clock:
    def __init__(self, start: float = 1_000_000.0, limit_sec: float = 600.0):
        self.t = start
        self.t0 = start
        self.limit_sec = limit_sec
        self._events: list[tuple[float, int, Callable[[], None]]] = []
        self._seq = itertools.count()
        self._tickers: list[Callable[[float], None]] = []
        self._stop_preds: list[tuple[Callable[[], bool], float]] = []
        self._stop_since: float | None = None

    # ── 시각 ──
    def now(self) -> float:
        return self.t

    def elapsed(self) -> float:
        return self.t - self.t0

    # ── 이벤트 ──
    def at(self, t: float, fn: Callable[[], None]) -> None:
        heapq.heappush(self._events, (t, next(self._seq), fn))

    def after(self, dt: float, fn: Callable[[], None]) -> None:
        self.at(self.t + max(0.0, dt), fn)

    def add_ticker(self, fn: Callable[[float], None]) -> None:
        self._tickers.append(fn)

    def _tick(self, dt: float) -> None:
        if dt <= 0:
            return
        for fn in self._tickers:
            fn(dt)
        self.t += dt

    # ── 정지 조건 ──
    def stop_when(self, pred: Callable[[], bool], grace: float = 3.0) -> None:
        """pred 가 참이 된 뒤 grace 초 더 돌리고 끊는다(도착 뒤 후처리까지 보려고)."""
        self._stop_preds.append((pred, grace))

    def clear_stops(self) -> None:
        self._stop_preds.clear()
        self._stop_since = None

    def _check_stop(self) -> None:
        if self.elapsed() > self.limit_sec:
            raise SimDone("limit")
        for pred, grace in self._stop_preds:
            if pred():
                if self._stop_since is None:
                    self._stop_since = self.t
                if self.t - self._stop_since >= grace:
                    raise SimDone("stop_when")
                return

    # ── sleep ──
    def sleep(self, dt: float) -> None:
        target = self.t + max(0.0, float(dt))
        while self._events and self._events[0][0] <= target:
            at, _, fn = heapq.heappop(self._events)
            self._tick(at - self.t)
            fn()
        self._tick(target - self.t)
        self._check_stop()

    def flush(self, max_sec: float = 60.0) -> None:
        """남은 타이머를 max_sec 안쪽까지 실행한다(시나리오 끝 정리용)."""
        end = self.t + max_sec
        while self._events and self._events[0][0] <= end:
            at, _, fn = heapq.heappop(self._events)
            self._tick(at - self.t)
            fn()

    # ── 관제 코드에 끼워 넣을 가짜 모듈 ──
    def time_module(self) -> types.ModuleType:
        fake = types.ModuleType("time")
        fake.__dict__.update(_real_time.__dict__)
        fake.time = self.now
        fake.monotonic = self.now
        fake.perf_counter = self.now
        fake.sleep = self.sleep
        return fake

    def threading_module(self, real_threading: types.ModuleType) -> types.ModuleType:
        clock = self

        class Timer:
            def __init__(self, interval, function, args=None, kwargs=None):
                self.interval, self.function = interval, function
                self.args, self.kwargs = args or (), kwargs or {}
                self.daemon = False
                self._cancelled = False

            def start(self):
                clock.after(self.interval, self._fire)

            def _fire(self):
                if not self._cancelled:
                    self.function(*self.args, **self.kwargs)

            def cancel(self):
                self._cancelled = True

        class Thread:
            """start() 하면 다음 sleep 에서 같은 스레드로 실행한다(동시 실행은 재현하지 않는다)."""

            def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, daemon=None):
                self.target, self.args, self.kwargs = target, args, kwargs or {}
                self.name, self.daemon = name, daemon
                self._done = False

            def start(self):
                clock.after(0, self.run)

            def run(self):
                try:
                    if self.target:
                        self.target(*self.args, **self.kwargs)
                finally:
                    self._done = True

            def is_alive(self):
                return not self._done

            def join(self, timeout=None):
                return None

        fake = types.ModuleType("threading")
        fake.__dict__.update(real_threading.__dict__)
        fake.Timer = Timer
        fake.Thread = Thread
        return fake
