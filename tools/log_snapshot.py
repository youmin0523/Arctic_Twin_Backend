"""
log_snapshot.py — Claude 없이 독립 실행되는 로그 스냅샷 스케줄러
3분마다 두 파이프라인 로그를 읽어 log_snapshot.txt 에 저장

실행: python log_snapshot.py
종료: Ctrl+C  또는 작업 관리자에서 종료
"""

import time
from datetime import datetime
from pathlib import Path

BASE    = Path(__file__).resolve().parent.parent  # tools/ → 프로젝트 루트
RL_LOG  = BASE / "backend/services/rl-pipeline" / "logs" / "train_all.log"
DEP_LOG = BASE / "backend/services/report-service" / "logs" / "train_departure.log"
OUT     = BASE / "log_snapshot.txt"
INTERVAL = 180  # 3분


def tail(path: Path, n: int = 8) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"[읽기 오류: {e}]"


def snapshot():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    block = (
        f"\n{'='*60}\n"
        f"[{now}]\n"
        f"=== rl-pipeline ===\n"
        f"{tail(RL_LOG)}\n"
        f"=== report-service ===\n"
        f"{tail(DEP_LOG)}\n"
    )
    with OUT.open("a", encoding="utf-8") as f:
        f.write(block)
    print(block)


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(f"스냅샷 저장 시작 -> {OUT}")
    print(f"갱신 주기: {INTERVAL}초 (3분) | 종료: Ctrl+C\n")
    while True:
        snapshot()
        time.sleep(INTERVAL)
