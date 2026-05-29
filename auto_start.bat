@echo off
chcp 65001 >nul
title Digital Twin Auto Starter

echo ============================================================
echo  Digital Twin 자동 시작 스크립트
echo  %DATE% %TIME%
echo ============================================================

set "BASE=%~dp0"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"

:: ── Python 실행은 uv 단일 환경(.venv)으로 통일 (BASE=backend/) ──────────
set "VIRTUAL_ENV=%BASE%\.venv"
set "UVRUN=uv run --no-project --active"
set "MODELS=%BASE%\services"

:: 잠깐 대기 (부팅 직후 네트워크 안정화)
timeout /t 15 /nobreak >nul

:: ── 포트 중복 프로세스 정리 ──────────────────────────────────────
echo [1/5] 기존 프로세스 정리 중...
for %%p in (8001 8002 8003 8004) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%%p " 2^>nul') do (
        taskkill /PID %%a /F >nul 2>&1
    )
)
timeout /t 3 /nobreak >nul

:: ── RL-Pipeline (8001) ───────────────────────────────────────────
echo [2/5] RL-Pipeline (8001) 시작 중...
start "RL-Pipeline-8001" /D "%MODELS%\rl-pipeline" /MIN cmd /c "%UVRUN% uvicorn server:app --host 0.0.0.0 --port 8001 >> %BASE%\logs\rl_server.log 2>&1"

:: ── Report-Service (8002) ────────────────────────────────────────
echo [3/5] Report-Service (8002) 시작 중...
start "Report-Service-8002" /D "%MODELS%\report-service" /MIN cmd /c "%UVRUN% uvicorn server:app --host 0.0.0.0 --port 8002 >> %BASE%\logs\report_server.log 2>&1"

:: ── SAR Server (8003) ────────────────────────────────────────────
echo [4/5] SAR-Server (8003) 시작 중...
start "SAR-Server-8003" /D "%BASE%" /MIN cmd /c "%UVRUN% python sar_server.py >> %BASE%\logs\sar_server.log 2>&1"

:: ── ML Training Service (8004) ───────────────────────────────────
echo [5/5] ML-Training-Service (8004) 시작 중...
start "ML-Training-8004" /D "%MODELS%\ml-pipeline" /MIN cmd /c "%UVRUN% uvicorn train_server:app --host 0.0.0.0 --port 8004 >> %BASE%\logs\ml_server.log 2>&1"

:: ── 서버 기동 대기 ───────────────────────────────────────────────
echo.
echo 서버 기동 대기 중 (30초)...
timeout /t 30 /nobreak >nul

:: ── 학습 트리거 ──────────────────────────────────────────────────
echo 학습 트리거 시작...
%UVRUN% python "%BASE%\tools\start_training.py" >> "%BASE%\logs\training_trigger.log" 2>&1

echo.
echo ============================================================
echo  완료: %TIME%
echo  로그 위치: %BASE%\logs\
echo ============================================================
