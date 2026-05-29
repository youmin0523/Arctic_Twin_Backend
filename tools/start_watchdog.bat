@echo off
:: Watchdog 데몬 런처 — Python 실행을 uv 단일 환경(backend\.venv)으로 통일.
:: register_scheduler.bat 의 Task Scheduler 항목이 이 파일을 호출한다.
set "BASE=%~dp0.."
set "VIRTUAL_ENV=%BASE%\backend\.venv"
:: 창 없이 백그라운드 실행 (pythonw)
uv run --no-project --active pythonw "%~dp0watchdog.py"
