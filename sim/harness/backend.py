"""관제 Backend 를 가상 시계·가짜 로봇에 붙여 띄운다.

안전장치
- DB 는 임시 SQLite 로 고정한다. 운영 MySQL 접속 정보(.env)는 읽지 않는다.
- 실제 소켓 연결·송신을 막는다. 로봇·receiver·DB 서버 어디에도 닿지 않는다.

연결 방식
- `from X import f` 로 가져간 함수도 바꾸도록, 로드된 app.* 모듈 전체에서
  원본 객체와 같은(identity) 속성을 찾아 가짜로 바꾼다.
- 함수 안에서 `import threading` 하는 곳은 실제 threading 을 쓴다(그 경로는 기본 가짜로 덮어 둔다).
"""
from __future__ import annotations

import contextlib
import io
import os
import socket as _socket
import sys
import tempfile
import threading as _real_threading
import time as _real_time
import types
from typing import Any, Callable
from unittest.mock import MagicMock

from .clock import Clock, SimDone
from .robot import FakeRobot, FakeSocket, RobotParams

HEAVY_MODULES = ("cv2", "numpy", "ultralytics", "paramiko",
                 "jose", "jose.jwt", "jose.exceptions")
ROBOT_ID = 10

_booted = False


class SimNetworkBlocked(RuntimeError):
    pass


def _block_network() -> None:
    def blocked(self, *a, **k):
        raise SimNetworkBlocked(f"sim: 실제 네트워크 사용 차단 {a[:1]}")

    for name in ("connect", "connect_ex", "sendto", "send", "sendall"):
        setattr(_socket.socket, name, blocked)


def _prepare_env(backend_dir: str) -> None:
    backend_dir = os.path.abspath(backend_dir)
    if not os.path.isdir(os.path.join(backend_dir, "app")):
        raise SystemExit(f"SIM_BACKEND 에 app/ 이 없습니다: {backend_dir}")
    sys.path.insert(0, backend_dir)
    db_path = os.path.join(tempfile.mkdtemp(prefix="sim_db_"), "sim.sqlite")
    for k in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME", "APP_ENV"):
        os.environ.pop(k, None)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["JWT_SECRET_KEY"] = "sim-only-not-a-secret"   # import 시 필수 검사 통과용
    for m in HEAVY_MODULES:
        sys.modules.setdefault(m, MagicMock())
    try:
        import dotenv
        dotenv.load_dotenv = lambda *a, **k: False   # 운영 .env 를 끌어오지 않는다
    except ImportError:
        pass
    _block_network()


def _app_modules() -> list[types.ModuleType]:
    return [m for n, m in list(sys.modules.items())
            if m is not None and (n == "app" or n.startswith("app."))]


def replace_everywhere(orig: Any, new: Any) -> int:
    """로드된 app.* 모듈에서 orig 를 가리키는 속성을 모두 new 로 바꾼다."""
    n = 0
    for mod in _app_modules():
        for k, v in list(vars(mod).items()):
            if v is orig:
                setattr(mod, k, new)
                n += 1
    return n


class Sim:
    """시나리오 한 건의 무대. 시나리오 함수는 sim 하나를 받는다."""

    # 시나리오에서 자주 쓰는 Backend 모듈
    IMPORTS = {
        "sender": "app.robot_io.sender",
        "rt": "app.robot_io.runtime",
        "pl": "app.robot_io.polling",
        "sm": "app.navigation.send_move",
        "ft": "app.navigation.floor_transition",
        "lp": "app.scheduler.loop",
        "ss": "app.scheduler.standby_session",
        "ch": "app.robot_control.charge",
        "uc": "app.user_cache",
    }

    def __init__(self, params: RobotParams | None = None, limit_sec: float = 600.0):
        self.clock = Clock(limit_sec=limit_sec)
        self.robot = FakeRobot(self.clock, params)
        self.events: list[tuple[float, str, str]] = []   # log_event 기록
        self.notes: list[str] = []
        self.stdout = io.StringIO()
        self._wire()

    # ── 연결 ──
    def _wire(self) -> None:
        import importlib
        for attr, name in self.IMPORTS.items():
            setattr(self, attr, importlib.import_module(name))
        # 측위 기록(loc_trace)이 운영 backend_logs 에 시뮬 줄을 쓰지 않게 sim/out 으로 돌린다(10-30 섞인 일 있음)
        try:
            lt = importlib.import_module("app.robot_io.loc_trace")
            lt.TRACE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out", "_loc_trace")
        except ImportError:
            pass
        try:
            from app.database.database import Base, engine
            import app.database.models  # noqa: F401  테이블 등록
            _sqlite_compat()
            failed = []
            for table in Base.metadata.sorted_tables:   # 한 테이블 실패가 나머지를 막지 않게
                try:
                    table.create(engine, checkfirst=True)
                except Exception:
                    failed.append(table.name)
            if failed:
                self.notes.append(f"SQLite 테이블 생성 실패: {', '.join(failed)}")
        except Exception as e:   # DB 를 안 쓰는 경로면 무관
            self.notes.append(f"SQLite 테이블 생성 실패: {e.__class__.__name__}")

        robot, clock = self.robot, self.clock
        fake_time = clock.time_module()
        fake_threading = clock.threading_module(_real_threading)
        for mod in _app_modules():
            if getattr(mod, "time", None) is _real_time:
                mod.time = fake_time
            if getattr(mod, "threading", None) is _real_threading:
                mod.threading = fake_threading
            if getattr(mod, "socket", None) is _socket and mod is self.pl:
                mod.socket = FakeSocket(robot)

        s = self.sender
        replace_everywhere(s.send_to_robot, lambda action, robot_id=None: robot.cmd(action))
        replace_everywhere(
            s.send_nav_to_robot,
            lambda idx, x, y, yaw, speed=None, map_id=None, continuing=False:
                robot.nav(x, y, yaw, continuing))
        if hasattr(s, "send_nav_multi_to_robot"):    # 다지점 송신이 있는 Backend 만
            replace_everywhere(
                s.send_nav_multi_to_robot,
                lambda points, map_id=None, continuing=False: robot.nav_multi(
                    [(p["x"], p["y"], p.get("yaw", 0.0)) for p in points], continuing))
        replace_everywhere(s.send_posture_to_robot, lambda *a, **k: robot._log("POSTURE"))
        replace_everywhere(s.request_receiver_tcp,
                           lambda msg, **k: robot._log(f"TCP:{msg.get('action')}") or None)

        uc = self.uc
        replace_everywhere(uc.get_robot_id, lambda: ROBOT_ID)
        replace_everywhere(uc.get_robot_name, lambda: "SIM")
        replace_everywhere(uc.get_robot_business_id, lambda: 1)

        from app.logs.service import log_event
        replace_everywhere(log_event, self._log_event)

        rt = self.rt
        rt.get_robot_id_by_ip = lambda ip: ROBOT_ID
        rt.get_position = lambda rid: {"x": robot.x, "y": robot.y, "yaw": robot.yaw,
                                       "timestamp": clock.now()}
        rt.get_gait = lambda rid: robot.gait
        rt.update_nav = lambda *a, **k: None
        rt.is_charging = lambda rid: robot.charging
        rt.is_docked = lambda rid: robot.charging

        # 이번 무대 밖의 기능은 꺼 둔다. 필요한 시나리오에서 다시 바꾼다.
        self.ft.is_transitioning = lambda rid: False
        self.ft.cancel_transition = lambda rid: False
        self.lp.get_active_schedule_id = lambda: None
        self.ss.is_active = lambda: False
        self.sm._update_is_working = lambda *a, **k: None
        self.ch.start_charge_with_retry = lambda *a, **k: setattr(robot, "charging", True)

    def rewire_late_modules(self) -> int:
        """_wire 뒤에 처음 import 된 app.* 모듈에도 가짜 time·threading·log_event 를 건다.

        시나리오가 라우트 모듈(voice_scenario 등)을 나중에 불러오면 그 모듈은 실제 스레드·시계를 써서
        가상 시계 밖에서 돈다. 새 모듈을 부른 직후 한 번 부른다. 반환: 바꾼 모듈 수.
        """
        from app.logs import service as log_service
        fake_time = self.sm.time if getattr(self.sm, "time", None) is not _real_time else None
        fake_threading = self.lp.threading
        n = 0
        for mod in _app_modules():
            hit = False
            if fake_time is not None and getattr(mod, "time", None) is _real_time:
                mod.time = fake_time
                hit = True
            if getattr(mod, "threading", None) is _real_threading:
                mod.threading = fake_threading
                hit = True
            if getattr(mod, "log_event", None) is log_service.log_event and mod is not log_service:
                mod.log_event = self._log_event
                hit = True
            n += hit
        return n

    def _log_event(self, *a, **k) -> None:
        kind = a[1] if len(a) > 1 else k.get("event_type", "")
        msg = a[2] if len(a) > 2 else k.get("message", "")
        self.events.append((round(self.clock.elapsed(), 2), str(kind), str(msg)))

    # ── 실행 도우미 ──
    def run(self, fn: Callable[[], Any], until: Callable[[], bool] | None = None,
            grace: float = 3.0) -> Any:
        """fn 을 가상 시계 위에서 돌린다. until 이 참이 되고 grace 초 뒤, 또는 한도에서 끊는다."""
        self.clock.clear_stops()
        if until is not None:
            self.clock.stop_when(until, grace)
        try:
            with contextlib.redirect_stdout(self.stdout):
                return fn()
        except SimDone:
            return None
        finally:
            self.clock.clear_stops()

    def wait(self, sec: float) -> None:
        with contextlib.redirect_stdout(self.stdout):
            self.clock.sleep(sec)

    def at(self, sec_from_now: float, fn: Callable[[], Any]) -> None:
        def call():
            with contextlib.redirect_stdout(self.stdout):
                fn()
        self.clock.after(sec_from_now, call)

    def move(self, points: list[tuple], keep_gait: bool = False, charge: bool = False,
             stop_at: float | None = None) -> None:
        """웨이포인트 이동 한 번을 nav_thread 로 끝까지 돌린다.

        points: [(x, y)] 또는 [(x, y, pass_through)]. stop_at: 출발 후 몇 초에 사용자 정지.
        """
        import math
        sm, robot = self.sm, self.robot
        pts = [(p[0], p[1], p[2] if len(p) > 2 else False) for p in points]
        wps = []
        for i, (x, y, passing) in enumerate(pts):
            nxt = pts[i + 1] if i + 1 < len(pts) else None
            yaw = math.atan2(nxt[1] - y, nxt[0] - x) if nxt else 0.0
            w = {"x": x, "y": y, "yaw": yaw, "name": f"P{i}", "map_id": None}
            if passing:
                w["pass_through"] = True
            wps.append(w)
        if keep_gait:
            wps[-1]["keep_gait"] = True

        final = (pts[-1][0], pts[-1][1])
        n_arr = len(robot.arrivals)
        stopped: list[float] = []

        sm.waypoints_list = wps
        sm.current_wp_index = 0
        sm.is_navigating = True
        sm.charge_on_arrival = charge
        sm._signal_nav_reset(full=True)
        if stop_at is not None:
            def _stop():
                stopped.append(self.clock.now())
                sm.stop_navigation_internal("사용자 정지")
            self.at(stop_at, _stop)

        def done() -> bool:
            if stopped:
                return True
            return any((a[1], a[2]) == final for a in robot.arrivals[n_arr:]) and not sm.is_navigating

        def go():
            sm.navigation_send_next()
            self.pl.nav_thread()

        self.run(go, until=done)

    def flush(self, max_sec: float = 60.0) -> None:
        with contextlib.redirect_stdout(self.stdout):
            try:
                self.clock.flush(max_sec)
            except SimDone:
                pass

    # ── 결과 ──
    def log(self) -> str:
        return self.stdout.getvalue()

    def summary(self) -> dict:
        log = self.log()
        return {
            **self.robot.summary(),
            "sim_sec": round(self.clock.elapsed(), 1),
            "errors": log.count("Traceback") + log.count("[ERR]"),
            "events": [f"{t}s {k}: {m}" for t, k, m in self.events][-20:],
            "notes": self.notes,
        }


_compat_done = False


def _sqlite_compat() -> None:
    """MySQL 전용 컬럼 타입을 SQLite 에서 만들 수 있게 한다(LONGBLOB → BLOB)."""
    global _compat_done
    if _compat_done:
        return
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.dialects.mysql import LONGBLOB

    @compiles(LONGBLOB, "sqlite")
    def _longblob(element, compiler, **kw):   # noqa: ANN001
        return "BLOB"

    _compat_done = True


def boot(backend_dir: str | None = None) -> None:
    """Backend import 전에 한 번 부른다. 프로세스당 한 번."""
    global _booted
    if _booted:
        return
    _prepare_env(backend_dir or os.environ.get("SIM_BACKEND") or _default_backend())
    _booted = True


def _default_backend() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "Backend")
