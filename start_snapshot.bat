@echo off
cd /d "%~dp0"
set "VIRTUAL_ENV=%~dp0.venv"
start "Log Snapshot" /min uv run --no-project --active python "%~dp0tools\log_snapshot.py"
echo 스냅샷 스케줄러 백그라운드 시작됨
echo 로그 저장 위치: %~dp0log_snapshot.txt
