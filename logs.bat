@echo off
cd /d "%~dp0"
chcp 65001 >nul
if "%~1"=="" (
    docker compose logs -f --tail 100
) else (
    docker compose logs -f --tail 100 %*
)
