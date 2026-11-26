@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul
rem 대기는 ping 으로 한다 — timeout 은 입력이 리다이렉트되거나 Git Bash 에서 실행하면 실패한다.

echo [1/4] Docker 확인...
docker info >nul 2>&1
if not errorlevel 1 goto docker_ready
echo     Docker 데몬이 응답하지 않습니다. Docker Desktop 을 실행합니다...
start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
echo     기동 대기 중 (최대 180초)...
for /l %%i in (1,1,60) do (
    ping -n 4 127.0.0.1 >nul
    docker info >nul 2>&1
    if not errorlevel 1 goto docker_ready
)
echo     [실패] Docker Desktop 이 준비되지 않았습니다.
exit /b 1

:docker_ready
echo [2/4] .env 확인...
if not exist ".env" (
    echo     .env 가 없어 .env.example 로부터 생성합니다.
    copy /y ".env.example" ".env" >nul
    set "SECRET="
    for /f "delims=" %%s in ('python -c "import secrets;print(secrets.token_hex(32))" 2^>nul') do set "SECRET=%%s"
    if defined SECRET (
        powershell -NoProfile -Command "(Get-Content .env) -replace '^JWT_SECRET_KEY=$', 'JWT_SECRET_KEY=!SECRET!' | Set-Content .env"
        echo     JWT_SECRET_KEY 자동 생성 완료.
    ) else (
        echo     [주의] python 이 없습니다. .env 의 JWT_SECRET_KEY 를 직접 채우세요.
    )
)

echo [3/4] 이미지 빌드 + 컨테이너 기동... (최초 실행은 수 분 걸립니다)
docker compose up -d --build
if errorlevel 1 (
    echo     [실패] docker compose up 실패. logs.bat 으로 확인하세요.
    exit /b 1
)

echo [4/4] 백엔드 헬스체크 대기...
for /l %%i in (1,1,60) do (
    ping -n 4 127.0.0.1 >nul
    set "HS="
    for /f "delims=" %%h in ('docker inspect -f "{{.State.Health.Status}}" control-backend 2^>nul') do set "HS=%%h"
    if "!HS!"=="healthy" goto ready
)
echo     [주의] 백엔드가 아직 healthy 가 아닙니다. logs.bat backend 로 확인하세요.
goto end

:ready
echo.
echo ==========================================================
echo   기동 완료
echo.
echo   웹 UI   : http://localhost:3001
echo   백엔드  : http://localhost:8010/api   (API 문서 /docs)
echo   DB      : localhost:3307  (root / 1234)
echo.
echo   로그인  : superadmin / superadmin1234!
echo             admin      / admin1234!
echo             user       / user1234!
echo ==========================================================
start "" http://localhost:3001

:end
endlocal
