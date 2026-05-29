"""
continuous_trainer.py
=====================
SAR YOLOv8 + Fuel XGBoost + What-if Analysis 지속 반복 개선 루프.

★ 리소스 안전 우선 ★
  - CPU 사용률이 임계값(기본 60%) 이상이면 RL 학습 중으로 판단 → 대기
  - RL 서버(8001/8002) running_models > 0 이면 절대 무거운 작업 안 함
  - 가용 RAM 이 2GB 미만이면 작업 연기
  - 각 작업 사이 CPU 확인 후 대기

완료 → 메트릭 분석 → 파라미터 자동 조정 → 재학습 → 완료 → ...
수렴 후 재도전: Fuel R² 목표 +0.005, SAR mAP50 목표 +0.05, What-if 시나리오 +1

실행:
  python continuous_trainer.py
  (Ctrl+C로 현재 작업 완료 후 중단)

로그:
  Digital_twin/logs/continuous_trainer.log
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib import request as ur

BASE = Path(__file__).resolve().parent.parent  # tools/ → 프로젝트 루트
LOG_FILE  = BASE / "logs" / "continuous_trainer.log"
STATE_FILE = BASE / "logs" / "continuous_trainer_state.json"
LOG_FILE.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("continuous_trainer")

_stop = False
def _handle_signal(sig, frame):
    global _stop
    log.info("중단 요청 — 현재 작업 완료 후 종료합니다...")
    _stop = True
signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ── 리소스 임계값 ──────────────────────────────────────────
CPU_SAFE_THRESHOLD  = 95.0   # CPU % 이하일 때만 무거운 작업 시작
CPU_STOP_THRESHOLD  = 98.0   # 작업 중 이 이상이면 다음 작업 연기
RAM_MIN_FREE_GB     = 2.0    # 최소 여유 RAM (GB)
RL_WAIT_POLL_SEC    = 60     # RL 대기 중 확인 주기 (초)
RESOURCE_POLL_SEC   = 30     # 리소스 대기 중 확인 주기 (초)

ML_DIR     = BASE / "backend/services/ml-pipeline"
REPORT_DIR = BASE / "backend/services/report-service"
TRAINERS   = BASE / "backend" / "pipeline" / "trainers"

for p in [str(ML_DIR), str(REPORT_DIR), str(TRAINERS)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ══════════════════════════════════════════════════════════
# 리소스 체크
# ══════════════════════════════════════════════════════════
def get_resources() -> dict:
    try:
        import psutil
        cpu  = psutil.cpu_percent(interval=1)
        mem  = psutil.virtual_memory()
        free = mem.available / 1024**3
        return {"cpu": cpu, "ram_free_gb": free, "ram_pct": mem.percent}
    except ImportError:
        return {"cpu": 0.0, "ram_free_gb": 99.0, "ram_pct": 0.0}

def get_rl_running() -> tuple[int, int]:
    """(8001 running_models, 8002 running_models) 반환. 오류 시 (1, 1) 반환 (안전 측)."""
    r1, r2 = 1, 1
    for port, path, idx in [
        (8001, "/api/rl/multi/status",        0),
        (8002, "/api/report/rl/multi/status", 1),
    ]:
        try:
            with ur.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
                d = json.loads(r.read())
                val = int(d.get("running_models", 1))
                if idx == 0: r1 = val
                else:        r2 = val
        except Exception:
            pass  # 응답 없으면 안전하게 1 유지
    return r1, r2

def wait_for_resources(task_name: str) -> bool:
    """리소스 여유가 생길 때까지 대기. _stop 이면 False 반환."""
    while not _stop:
        res = get_resources()
        rl1, rl2 = get_rl_running()
        rl_running = rl1 + rl2

        cpu_ok  = res["cpu"] < CPU_SAFE_THRESHOLD
        ram_ok  = res["ram_free_gb"] > RAM_MIN_FREE_GB
        rl_ok   = rl_running == 0

        if cpu_ok and ram_ok:
            log.info("[%s] 리소스 OK — CPU=%.1f%%, RAM여유=%.1fGB, RL실행중=%d",
                     task_name, res["cpu"], res["ram_free_gb"], rl_running)
            return True

        reason = []
        if not cpu_ok:
            reason.append(f"CPU {res['cpu']:.1f}%>{CPU_SAFE_THRESHOLD}%")
        if not ram_ok:
            reason.append(f"RAM여유 {res['ram_free_gb']:.1f}GB<{RAM_MIN_FREE_GB}GB")
        if not rl_ok:
            reason.append(f"RL실행중 {rl_running}개")

        log.info("[%s] 대기 중 (%s) — %d초 후 재확인...",
                 task_name, ", ".join(reason), RESOURCE_POLL_SEC)
        time.sleep(RESOURCE_POLL_SEC)

    return False  # _stop

def check_cpu_during(task_name: str) -> bool:
    """작업 중 CPU 과부하 여부 확인 (경고만, 중단하지 않음)."""
    res = get_resources()
    if res["cpu"] > CPU_STOP_THRESHOLD:
        log.warning("[%s] CPU 과부하 %.1f%% — RL 영향 가능. 다음 작업은 대기합니다.",
                    task_name, res["cpu"])
        return False
    return True


# ══════════════════════════════════════════════════════════
# 상태 관리
# ══════════════════════════════════════════════════════════
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "cycle": 0,
        "fuel":   {"cycles": 0, "best_r2": 0.0,    "target_r2": 0.95},
        "sar":    {"cycles": 0, "best_map50": 0.0,  "target_map50": 0.70},
        "whatif": {"cycles": 0, "best_scenarios": 0,"target_scenarios": 4},
    }

def save_state(state: dict):
    STATE_FILE.parent.mkdir(exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ══════════════════════════════════════════════════════════
# Fuel XGBoost 사이클
# ══════════════════════════════════════════════════════════
def run_fuel_cycle(state: dict, cycle_num: int) -> dict:
    fuel_state = state["fuel"]
    target_r2  = fuel_state.get("target_r2", 0.95)

    log.info("━" * 60)
    log.info("[FUEL] 사이클 %d — 목표 R² ≥ %.3f", cycle_num, target_r2)

    if not wait_for_resources("FUEL"):
        return {"success": False, "error": "중단 요청"}

    try:
        for mod_name in list(sys.modules.keys()):
            if "fuel_iterative" in mod_name or "model_trainer" in mod_name:
                del sys.modules[mod_name]

        import modules.fuel_iterative_trainer as fit_mod
        fit_mod.TARGET_R2 = target_r2

        from modules.fuel_iterative_trainer import FuelIterativeTrainer

        def cb(stage, progress, **kw):
            log.info("  [FUEL] %s (%d%%)", stage, progress)

        trainer = FuelIterativeTrainer(base_dir=ML_DIR, status_callback=cb)
        result  = trainer.run(max_iterations=5)

        best_r2   = result.get("best_metrics", {}).get("R2", 0.0) or 0.0
        converged = result.get("converged", False)

        fuel_state["cycles"]  = fuel_state.get("cycles", 0) + 1
        fuel_state["best_r2"] = max(fuel_state.get("best_r2", 0.0), best_r2)

        if converged:
            new_t = min(target_r2 + 0.005, 0.99)
            fuel_state["target_r2"] = new_t
            log.info("[FUEL] ✓ 수렴 R²=%.4f → 다음 목표 R²=%.3f", best_r2, new_t)
        else:
            log.info("[FUEL] 미수렴 최고 R²=%.4f (목표 %.3f)", best_r2, target_r2)

        return {"success": True, "best_r2": best_r2, "converged": converged}

    except Exception as e:
        log.error("[FUEL] 실패: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════
# SAR YOLOv8 사이클
# ══════════════════════════════════════════════════════════
def run_sar_cycle(state: dict, cycle_num: int) -> dict:
    sar_state  = state["sar"]
    target_map = sar_state.get("target_map50", 0.70)

    log.info("━" * 60)
    log.info("[SAR] 사이클 %d — 목표 mAP50 ≥ %.2f", cycle_num, target_map)

    if not wait_for_resources("SAR"):
        return {"success": False, "error": "중단 요청"}

    try:
        for mod_name in list(sys.modules.keys()):
            if "iceberg_iterative" in mod_name:
                del sys.modules[mod_name]

        import iceberg_iterative_trainer as iit_mod
        iit_mod.TARGET_MAP50     = target_map
        iit_mod.TARGET_PRECISION = max(0.60, target_map - 0.05)
        iit_mod.TARGET_RECALL    = max(0.60, target_map - 0.05)

        from iceberg_iterative_trainer import IcebergIterativeTrainer

        def cb(stage, progress, **kw):
            log.info("  [SAR] %s (%d%%)", stage, progress)

        trainer  = IcebergIterativeTrainer(status_callback=cb)
        result   = trainer.run(max_iterations=3)

        best_map  = float(result.get("best_metrics", {}).get("mAP50") or 0.0)
        converged = result.get("converged", False)

        sar_state["cycles"]     = sar_state.get("cycles", 0) + 1
        sar_state["best_map50"] = max(sar_state.get("best_map50", 0.0), best_map)

        if converged:
            new_t = min(target_map + 0.05, 0.95)
            sar_state["target_map50"] = new_t
            log.info("[SAR] ✓ 수렴 mAP50=%.4f → 다음 목표 %.2f", best_map, new_t)
        else:
            log.info("[SAR] 미수렴 최고 mAP50=%.4f (목표 %.2f)", best_map, target_map)

        return {"success": True, "best_map50": best_map, "converged": converged}

    except Exception as e:
        log.error("[SAR] 실패: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════
# What-if Analysis 사이클
# ══════════════════════════════════════════════════════════
def run_whatif_cycle(state: dict, cycle_num: int) -> dict:
    wi_state  = state["whatif"]
    target_n  = wi_state.get("target_scenarios", 4)

    log.info("━" * 60)
    log.info("[WHAT-IF] 사이클 %d — 목표 시나리오 ≥ %d개", cycle_num, target_n)

    # What-if는 Claude API 호출 → CPU 부담 낮음, RAM만 확인
    try:
        import psutil
        mem = psutil.virtual_memory()
        if mem.available / 1024**3 < RAM_MIN_FREE_GB:
            log.warning("[WHAT-IF] RAM 부족 %.1fGB — 대기", mem.available/1024**3)
            if not wait_for_resources("WHAT-IF"):
                return {"success": False, "error": "중단 요청"}
    except ImportError:
        pass

    try:
        for mod_name in list(sys.modules.keys()):
            if "whatif_iterative" in mod_name or "whatif_generator" in mod_name:
                del sys.modules[mod_name]

        import modules.whatif_iterative_runner as wir_mod
        wir_mod.TARGET_SCENARIOS = target_n

        from dotenv import load_dotenv
        load_dotenv(BASE / "backend" / ".env")

        from modules.data_loader import DataLoader
        from modules.route_scorer import RouteScorer
        from modules.whatif_iterative_runner import WhatIfIterativeRunner

        data_loader  = DataLoader()
        route_scorer = RouteScorer(data_loader)

        def cb(stage, progress, **kw):
            log.info("  [WHAT-IF] %s (%d%%)", stage, progress)

        runner = WhatIfIterativeRunner(
            route_scorer=route_scorer,
            data_loader=data_loader,
            status_callback=cb,
        )
        result   = runner.run(max_iterations=3)

        best_n    = result.get("best_quality", {}).get("scenarios_count", 0) or 0
        converged = result.get("converged", False)

        wi_state["cycles"]         = wi_state.get("cycles", 0) + 1
        wi_state["best_scenarios"] = max(wi_state.get("best_scenarios", 0), best_n)

        if converged:
            new_t = min(target_n + 1, 6)
            wi_state["target_scenarios"] = new_t
            log.info("[WHAT-IF] ✓ 완성 시나리오=%d개 → 다음 목표 %d개", best_n, new_t)
        else:
            log.info("[WHAT-IF] 미달 최고=%d개 (목표 %d개)", best_n, target_n)

        return {"success": True, "best_scenarios": best_n, "converged": converged}

    except Exception as e:
        log.error("[WHAT-IF] 실패: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════
# RL 상태 모니터 (건드리지 않음)
# ══════════════════════════════════════════════════════════
def log_rl_status():
    for port, path, label in [
        (8001, "/api/rl/multi/status",        "RL-8001"),
        (8002, "/api/report/rl/multi/status", "RP-8002"),
    ]:
        try:
            with ur.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
                d = json.loads(r.read())
                log.info("[%s] 실행중=%s, 수렴=%s/%s",
                         label,
                         d.get("running_models", "?"),
                         d.get("converged_models", "?"),
                         d.get("total_models", "?"))
        except Exception as e:
            log.info("[%s] 응답없음: %s", label, str(e)[:50])


# ══════════════════════════════════════════════════════════
# 메인 루프
# ══════════════════════════════════════════════════════════
def main():
    log.info("=" * 70)
    log.info("  Digital Twin 지속 반복 개선 트레이너")
    log.info("  ★ CPU/RAM 모니터링으로 RL 학습에 영향 최소화 ★")
    log.info("  CPU < %.0f%% 일 때만 작업 시작", CPU_SAFE_THRESHOLD)
    log.info("  Ctrl+C 로 현재 작업 완료 후 종료")
    log.info("=" * 70)

    state = load_state()
    log.info("복원: cycle=%d, fuel_R²=%.4f, sar_mAP=%.4f, whatif=%d개",
             state["cycle"],
             state["fuel"].get("best_r2", 0),
             state["sar"].get("best_map50", 0),
             state["whatif"].get("best_scenarios", 0))

    # 초기 리소스 확인
    res = get_resources()
    log.info("현재 시스템: CPU=%.1f%%, RAM여유=%.1fGB", res["cpu"], res["ram_free_gb"])

    # 작업 목록 (CPU 부담 낮은 것부터)
    # What-if(API 호출) → Fuel(XGBoost, 중간) → SAR(YOLOv8, 무거움)
    TASKS = [
        ("WHAT-IF", run_whatif_cycle),
        ("FUEL",    run_fuel_cycle),
        ("SAR",     run_sar_cycle),
    ]

    while not _stop:
        state["cycle"] += 1
        cycle = state["cycle"]

        log.info("\n" + "═" * 70)
        log.info("  ▶ 전체 사이클 %d  (%s)", cycle, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        log.info("═" * 70)

        log_rl_status()

        res = get_resources()
        log.info("시스템: CPU=%.1f%%, RAM여유=%.1fGB", res["cpu"], res["ram_free_gb"])

        for task_name, task_fn in TASKS:
            if _stop:
                break

            t0 = time.time()
            result = task_fn(state, cycle)
            elapsed = time.time() - t0

            ok = "✓" if result.get("success") else f"✗ {result.get('error','')[:50]}"
            log.info("  [%s] 완료 %s  (%.0f초)", task_name, ok, elapsed)

            save_state(state)

            if _stop:
                break

            # 작업 사이 CPU 확인 후 필요시 대기
            check_cpu_during(task_name)
            if not _stop:
                # 짧은 냉각 대기
                for _ in range(10):
                    if _stop: break
                    time.sleep(1)

        if _stop:
            break

        save_state(state)

        # 사이클 요약
        log.info("\n" + "─" * 70)
        log.info("  사이클 %d 완료  %s", cycle, datetime.now().strftime("%H:%M:%S"))
        log.info("  Fuel:    누적 %d사이클, 최고 R²=%.4f → 다음 목표 R²≥%.3f",
                 state["fuel"]["cycles"], state["fuel"]["best_r2"], state["fuel"]["target_r2"])
        log.info("  SAR:     누적 %d사이클, 최고 mAP50=%.4f → 다음 목표 mAP50≥%.2f",
                 state["sar"]["cycles"], state["sar"]["best_map50"], state["sar"]["target_map50"])
        log.info("  What-if: 누적 %d사이클, 최고 시나리오=%d개 → 다음 목표 %d개",
                 state["whatif"]["cycles"], state["whatif"]["best_scenarios"], state["whatif"]["target_scenarios"])
        log.info("─" * 70)

        # 다음 사이클 전 대기 (10초)
        for _ in range(10):
            if _stop: break
            time.sleep(1)

    log.info("종료. 총 %d사이클 완료.", state["cycle"])
    save_state(state)


if __name__ == "__main__":
    main()
