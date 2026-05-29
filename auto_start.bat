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
echo [1/4] 기존 프로세스 정리 중...
for %%p in (8000 8001 8002 8004 8005) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%%p " 2^>nul') do (
        taskkill /PID %%a /F >nul 2>&1
    )
)
timeout /t 3 /nobreak >nul

:: ── Node API 서버 (8000) ─────────────────────────────────────────
::  - /api/ice·/api/weather 등 직접 서빙 + /api/rl·/api/report 프록시
::  - fetcher 스케줄러(해빙·빙산·SAR·기상) 구동
::  - 부팅 시 RL(8001)·Report(8002) Python 서버를 자식 프로세스로 자동 기동 (별도 start 불필요)
echo [2/4] Node API 서버 (8000) 시작 중...
start "Node-API-8000" /D "%BASE%" /MIN cmd /c "node src\index.js >> %BASE%\logs\node_api.log 2>&1"

:: ── SAR Server (8005) ────────────────────────────────────────────
echo [3/4] SAR-Server (8005) 시작 중...
start "SAR-Server-8005" /D "%BASE%" /MIN cmd /c "%UVRUN% python sar_server.py >> %BASE%\logs\sar_server.log 2>&1"

:: ── ML Training Service (8004) ───────────────────────────────────
echo [4/4] ML-Training-Service (8004) 시작 중...
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
