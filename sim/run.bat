@echo off
chcp 65001 >nul
rem 사전 시뮬 실행: sim\run.bat sim\scenarios\nav_basic.py [--cand sim\out\cand_xxx]
setlocal
set "SIM=%~dp0"
if not exist "%SIM%.venv\Scripts\python.exe" (
  echo [sim] 가상환경 생성 중...
  python -m venv "%SIM%.venv" || exit /b 1
  "%SIM%.venv\Scripts\python.exe" -m pip install -q -r "%SIM%requirements.txt" || exit /b 1
)
set PYTHONIOENCODING=utf-8
"%SIM%.venv\Scripts\python.exe" "%SIM%run.py" %*
