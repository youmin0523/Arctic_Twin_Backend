"""
train_watchdog.py - train_all.py / train_departure.py 자동 감시 + 재시작
- 3분마다 로그 파일 mtime 체크
- 5분 이상 업데이트 없으면 자동 재시작
- watchdog_train.log에 이벤트 기록
- 실행: python train_watchdog.py
- 종료: Ctrl+C
"""

import time
import os
import subprocess
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # tools/ → 프로젝트 루트

# Python 실행은 uv 단일 환경(backend/.venv)으로 통일
VENV = BASE / "backend" / ".venv"
UVRUN = ["uv", "run", "--no-project", "--active"]
SERVICES = BASE / "backend" / "services"

PIPELINES = [
    {
        "name": "rl-pipeline",
        "log": SERVICES / "rl-pipeline/logs/train_all.log",
        "script": "train_all.py",
        "cwd": SERVICES / "rl-pipeline",
    },
    {
        "name": "report-service",
        "log": SERVICES / "report-service/logs/train_departure.log",
        "script": "train_departure.py",
        "cwd": SERVICES / "report-service",
    },
]

STALE_THRESHOLD = 300   # 5분 이상 업데이트 없으면 재시작
CHECK_INTERVAL  = 180   # 3분마다 체크
WATCHDOG_LOG    = BASE / "watchdog_train.log"


def log(msg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    print(line)
    with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_mtime(path):
    try:
        return os.path.getmtime(path)
    except FileNotFoundError:
        return 0


def restart(pipeline):
    name = pipeline["name"]
    log(f"[{name}] 로그 멈춤 감지 → 재시작")
    try:
        log_file = open(pipeline["log"], "a", encoding="utf-8")
        env = {**os.environ, "VIRTUAL_ENV": str(VENV)}  # uv run --active 가 backend/.venv 사용
        proc = subprocess.Popen(
            UVRUN + ["python", pipeline["script"]],
            cwd=str(pipeline["cwd"]),
            env=env,
            stdout=log_file,
            stderr=log_file,
        )
        log(f"[{name}] 재시작 완료 PID={proc.pid}")
    except Exception as e:
        log(f"[{name}] 재시작 실패: {e}")


if __name__ == "__main__":
    log("=" * 50)
    log("train_watchdog 시작")
    log(f"체크 주기: {CHECK_INTERVAL}초 | 재시작 임계값: {STALE_THRESHOLD}초")
    log("=" * 50)

    try:
        while True:
            for p in PIPELINES:
                elapsed = time.time() - get_mtime(p["log"])
                if elapsed > STALE_THRESHOLD:
                    restart(p)
                else:
                    log(f"[{p['name']}] 정상 (마지막 업데이트 {elapsed:.0f}초 전)")
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        log("train_watchdog 종료")
