"""원격 제어 창 충전 해제·충전소 이동 — 로봇 번호를 실은 경로(2026-10-30).

  sim\run.bat sim\scenarios\charge_scoped.py --cand sim\out\cand_scoped_charge
실측(10-30 10:34 관제 로그): 원격 제어 창 '충전 해제' → POST /robot/13/undock 404. 원격 창은 조작 명령을
/robot/{번호}/… 로 보내는데(RemoteSessionContext.scopeRobotPath) 충전 명령만 그 경로가 없었다.
변경: charge.py 에 /robot/{robot_id}/undock · /robot/{robot_id}/return-to-charge — 제어권(require_control) 확인,
관제가 주행시키는 로봇과 번호가 다르면 409. 번호 없는 옛 경로(대시보드·상세 화면)는 그대로.
무대: charge.router 의 경로 매칭 → 실제 require_control 검사 → 처리 함수(ASGI·HTTP 는 거치지 않음, 하네스 네트워크 차단).
로그인은 관리자로 대신하고, 제어권은 실제 control_lock 을 쓴다(인스턴스 간 DB 락만 통과로 가정).
충전 동작(_undock_internal·_return_to_charge_internal)은 호출 여부만 기록한다(로봇 동작 흉내 없음).
"""
from harness import scenario

DRIVING = 13      # 관제가 주행시키는 로봇(669)


def _client(sim):
    """charge 모듈을 준비한다 — 충전 동작은 기록만, 관제가 주행시키는 로봇은 DRIVING."""
    import app.robot_control.charge as ch
    import app.robot_control.control_deps as cd
    calls = []
    ch._undock_internal = lambda *a, **k: (calls.append("undock"), {"ok": True, "msg": "충전 해제", "dock_point": None})[1]
    ch._return_to_charge_internal = lambda *a, **k: (calls.append("return"), {
        "ok": True, "msg": "충전소 이동", "dock_point": None, "charge_station": None})[1]
    ch.get_robot_id = lambda: DRIVING
    cd.get_robot_id = lambda: DRIVING
    return ch, calls


def _lock(robot_id, owner="u1"):
    from app.robot_control import control_lock as cl
    # 인스턴스 간 DB 네임드 락(MariaDB GET_LOCK)은 시뮬 DB 에 없다 — 이 관제 한 대뿐인 것으로 본다(가정)
    cl.db_lock.acquire = lambda rid: True
    cl.db_lock.release = getattr(cl.db_lock, "release", None) and (lambda *a, **k: True)
    return cl.acquire(robot_id, "user", owner, owner_label=owner).token


def _post(ch, path, token=None):
    """경로를 라우터에서 찾아(FastAPI 경로 매칭) 제어권 검사 → 처리 함수를 차례로 부른다.

    시뮬 하네스가 실제 네트워크를 막아 Windows 비동기 루프(내부 소켓)를 못 쓴다 — ASGI 대신 같은 순서를 직접 밟는다.
    경로가 없으면 404, 의존성·처리 함수가 HTTPException 을 내면 그 코드.
    """
    from types import SimpleNamespace
    from fastapi import HTTPException
    from starlette.routing import Match
    from app.database.models import UserInfo
    from app.robot_control.control_deps import require_control
    route, params = None, {}
    for r in ch.router.routes:
        m, child = r.matches({"type": "http", "path": path, "method": "POST"})
        if m == Match.FULL:
            route, params = r, child.get("path_params", {})
            break
    if route is None:
        return 404, {"detail": "Not Found"}
    user = UserInfo(id=1, Permission=1, BusinessId=None)
    try:
        if "robot_id" in params:             # 번호 있는 경로 — 제어권 검사(require_control)를 실제로 돌린다
            checker = require_control()
            req = SimpleNamespace(path_params={"robot_id": str(params["robot_id"])},
                                  state=SimpleNamespace(user_permissions={"robot-list"}))
            checker(req, token, user)
            return 200, route.endpoint(int(params["robot_id"]), user)
        return 200, route.endpoint()
    except HTTPException as e:
        return e.status_code, {"detail": e.detail}


@scenario("C1 [재현] 원격 창 충전 해제 /robot/13/undock (제어권 있음) → 200, 충전 해제 실행",
          expect=lambda r: (r["code"] == 200 and r["calls"] == ["undock"]) or f"{r}")
def c1(sim):
    c, calls = _client(sim)
    code, body = _post(c, f"/robot/{DRIVING}/undock", _lock(DRIVING))
    return {"code": code, "calls": calls, "body": body}


@scenario("C2 [재현] 원격 창 충전소 이동 /robot/13/return-to-charge (제어권 있음) → 200, 실행",
          expect=lambda r: (r["code"] == 200 and r["calls"] == ["return"]) or f"{r}")
def c2(sim):
    c, calls = _client(sim)
    code, body = _post(c, f"/robot/{DRIVING}/return-to-charge", _lock(DRIVING))
    return {"code": code, "calls": calls, "body": body}


@scenario("C3 [경계] 다른 로봇 번호(10, 제어권 있음) → 409, 실행 안 함(관제가 주행시키는 로봇은 13)",
          expect=lambda r: (r["code"] == 409 and r["calls"] == []) or f"{r}")
def c3(sim):
    c, calls = _client(sim)
    code, body = _post(c, "/robot/10/undock", _lock(10))
    return {"code": code, "calls": calls, "body": body}


@scenario("C4 [경계] 제어권 없이 /robot/13/undock → 409, 실행 안 함",
          expect=lambda r: (r["code"] == 409 and r["calls"] == []) or f"{r}")
def c4(sim):
    c, calls = _client(sim)
    code, body = _post(c, f"/robot/{DRIVING}/undock")
    return {"code": code, "calls": calls, "body": body}


@scenario("C5 [경계] 다른 사람이 제어 중(토큰 다름) → 409, 실행 안 함",
          expect=lambda r: (r["code"] == 409 and r["calls"] == []) or f"{r}")
def c5(sim):
    c, calls = _client(sim)
    _lock(DRIVING, owner="other")
    code, body = _post(c, f"/robot/{DRIVING}/undock", "wrong-token")
    return {"code": code, "calls": calls, "body": body}


@scenario("C6 [회귀] 번호 없는 옛 경로 /robot/undock(대시보드·상세 화면) → 200, 그대로",
          expect=lambda r: (r["code"] == 200 and r["calls"] == ["undock"]) or f"{r}")
def c6(sim):
    c, calls = _client(sim)
    code, body = _post(c, "/robot/undock")
    return {"code": code, "calls": calls, "body": body}
