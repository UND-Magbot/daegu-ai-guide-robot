@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

if /i "%~1"=="--clean" goto :clean

echo 컨테이너를 중지합니다 ^(DB 데이터는 보존^)...
docker compose down
goto :end

:clean
echo [경고] 컨테이너와 DB 볼륨을 모두 삭제합니다.
set /p ANS="정말 진행할까요? (y/N) "
if /i not "%ANS%"=="y" goto :end
docker compose down -v

:end
endlocal
