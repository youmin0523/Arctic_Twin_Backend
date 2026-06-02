"""
ML Training Server — FastAPI :8004

Fuel XGBoost 학습 + What-if Analysis 백그라운드 실행 서버.
각 작업은 1회 완료 후 자동으로 성능 분석 → 파라미터 조정 → 반복 개선을 수행합니다.

Run: python train_server.py
  또는: venv/Scripts/uvicorn train_server:app --host 0.0.0.0 --port 8004
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

_BASE = Path(__file__).parent
sys.path.insert(0, str(_BASE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ml-training-service")

app = FastAPI(
    title="ML Training Service",
    description="Fuel XGBoost + What-if Analysis 자동 반복 개선 서버 (포트 8004)",
    version="2.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 상태 저장소 ──────────────────────────────────────────────
_STATE_FILE = _BASE / "models" / "training_state.json"
_lock = threading.Lock()

_fuel_state: dict = {
    "is_training":   False,
    "progress":      0,
    "stage":         "대기 중",
    "result":        None,
    "error":         None,
    "started_at":    None,
    "finished_at":   None,
    "metrics":       {},
    # 반복 학습 추가 필드
    "mode":          "idle",     # idle | single | iterative
    "iteration":     0,
    "max_iterations": 0,
    "iterative_status": {},
}

_whatif_state: dict = {
    "is_running":    False,
    "progress":      0,
    "stage":         "대기 중",
    "result":        None,
    "error":         None,
    "started_at":    None,
    "finished_at":   None,
    "scenarios_count": 0,
    "route":         None,
    "ice_class":     None,
    # 반복 학습 추가 필드
    "mode":          "idle",     # idle | single | iterative
    "iteration":     0,
    "max_iterations": 0,
    "iterative_status": {},
}


def _save_state():
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"fuel": _fuel_state, "whatif": _whatif_state},
                      f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning("상태 저장 실패: %s", e)


def _fuel_status_cb(stage: str, progress: int, **extra):
    """FuelIterativeTrainer → state 업데이트 콜백."""
    with _lock:
        _fuel_state["stage"]    = stage
        _fuel_state["progress"] = progress
        if "iteration" in extra:
            _fuel_state["iteration"] = extra["iteration"]
        if "metrics" in extra:
            _fuel_state["metrics"] = extra["metrics"]
        if "params" in extra:
            _fuel_state["iterative_status"]["last_params"] = extra["params"]
        if "signals" in extra:
            _fuel_state["iterative_status"]["last_signals"] = extra["signals"]
    _save_state()


def _whatif_status_cb(stage: str, progress: int, **extra):
    """WhatIfIterativeRunner → state 업데이트 콜백."""
    with _lock:
        _whatif_state["stage"]    = stage
        _whatif_state["progress"] = progress
        if "iteration" in extra:
            _whatif_state["iteration"] = extra["iteration"]
        if "quality" in extra:
            q = extra["quality"]
            _whatif_state["scenarios_count"] = q.get("scenarios_count", 0)
            _whatif_state["iterative_status"]["last_quality"] = q
        if "signals" in extra:
            _whatif_state["iterative_status"]["last_signals"] = extra["signals"]
    _save_state()


# ── Request Models ───────────────────────────────────────────

class FuelTrainRequest(BaseModel):
    samples_per_vessel: int = 400
    n_estimators: int = 200
    max_iterations: int = 5          # 자동 반복 최대 횟수 (1 = 단순 1회)
    force_restart: bool = False       # 히스토리 무시 재시작


class WhatIfRunRequest(BaseModel):
    route: str = "NSR"
    ice_class: str = "PC5"
    departure_date: str = ""
    forecast_days: int = 30
    max_iterations: int = 3          # 자동 반복 최대 횟수 (1 = 단순 1회)
    force_restart: bool = False


# ── Fuel 자동 반복 학습 ──────────────────────────────────────

def _fuel_iterative_sync(req: FuelTrainRequest) -> None:
    with _lock:
        _fuel_state.update(
            is_training=True, progress=0,
            stage="Fuel 반복 학습 초기화 중...", result=None, error=None,
            started_at=datetime.now().isoformat(), finished_at=None,
            mode="iterative" if req.max_iterations > 1 else "single",
            iteration=0, max_iterations=req.max_iterations,
            iterative_status={},
        )
    _save_state()

    try:
        from modules import config as cfg  # type: ignore[attr-defined]
        from copy import deepcopy

        # 초기 파라미터 패치
        orig_spv = cfg.SAMPLES_PER_VESSEL
        orig_xgb = deepcopy(cfg.XGBOOST_PARAMS)
        cfg.SAMPLES_PER_VESSEL = req.samples_per_vessel
        cfg.XGBOOST_PARAMS["n_estimators"] = req.n_estimators

        try:
            from modules.fuel_iterative_trainer import FuelIterativeTrainer
            trainer = FuelIterativeTrainer(
                base_dir=_BASE,
                status_callback=_fuel_status_cb,
            )
            final = trainer.run(
                max_iterations=req.max_iterations,
                force_restart=req.force_restart,
            )
        finally:
            cfg.SAMPLES_PER_VESSEL = orig_spv
            cfg.XGBOOST_PARAMS.update(orig_xgb)

        with _lock:
            _fuel_state.update(
                is_training=False, progress=100,
                stage=("수렴 완료" if final.get("converged")
                       else f"반복 완료 ({final['total_iterations']}회)"),
                result=final,
                metrics=final.get("best_metrics", {}),
                finished_at=datetime.now().isoformat(),
                iterative_status=trainer.get_status(),
            )
        _save_state()

    except Exception as e:
        logger.error("Fuel 반복 학습 오류: %s", e, exc_info=True)
        with _lock:
            _fuel_state.update(
                is_training=False, stage="오류 발생", error=str(e),
                finished_at=datetime.now().isoformat(),
            )
        _save_state()


# ── What-if 자동 반복 실행 ────────────────────────────────────

def _whatif_iterative_sync(req: WhatIfRunRequest) -> None:
    with _lock:
        _whatif_state.update(
            is_running=True, progress=0,
            stage="What-if 반복 분석 초기화 중...", result=None, error=None,
            started_at=datetime.now().isoformat(), finished_at=None,
            route=req.route, ice_class=req.ice_class,
            mode="iterative" if req.max_iterations > 1 else "single",
            iteration=0, max_iterations=req.max_iterations,
            iterative_status={},
        )
    _save_state()

    try:
        import sys as _sys
        report_svc = str(_BASE.parent / "report-service")
        if report_svc not in _sys.path:
            _sys.path.insert(0, report_svc)

        # .env 로드 — backend/.env 우선 (현재 레이아웃: ml-pipeline→services→backend), 단독 폴백
        from pathlib import Path as _Path
        from dotenv import load_dotenv
        for _envp in [_BASE.parent.parent / ".env", _BASE.parent / "backend" / ".env"]:
            if _envp.exists():
                load_dotenv(_envp)
                break

        from modules.data_loader import DataLoader
        from modules.route_scorer import RouteScorer
        from modules.whatif_iterative_runner import WhatIfIterativeRunner

        data_loader  = DataLoader()
        route_scorer = RouteScorer(data_loader)

        runner = WhatIfIterativeRunner(
            route_scorer=route_scorer,
            data_loader=data_loader,
            status_callback=_whatif_status_cb,
        )
        final = runner.run(
            max_iterations=req.max_iterations,
            force_restart=req.force_restart,
        )

        with _lock:
            _whatif_state.update(
                is_running=False, progress=100,
                stage=("완성도 달성" if final.get("converged")
                       else f"반복 완료 ({final['total_iterations']}회)"),
                result=final,
                scenarios_count=final.get("best_quality", {}).get("scenarios_count", 0),
                finished_at=datetime.now().isoformat(),
                iterative_status=runner.get_status(),
            )
        _save_state()

    except Exception as e:
        logger.error("What-if 반복 오류: %s", e, exc_info=True)
        with _lock:
            _whatif_state.update(
                is_running=False, stage="오류 발생", error=str(e),
                finished_at=datetime.now().isoformat(),
            )
        _save_state()


# ── Endpoints ────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "service": "ML Training Service (자동 반복 개선)",
        "port": 8004,
        "docs": "/docs",
    }


@app.get("/api/ml/health")
async def health():
    return {
        "status": "ok",
        "fuel_training": _fuel_state["is_training"],
        "whatif_running": _whatif_state["is_running"],
    }


# ── Fuel 엔드포인트 ──────────────────────────────────────────

@app.post("/api/ml/fuel/train")
async def fuel_train(req: FuelTrainRequest, bg: BackgroundTasks):
    """Fuel XGBoost 학습 시작.
    max_iterations > 1 이면 자동 반복 개선 모드로 실행됩니다."""
    if _fuel_state["is_training"]:
        return JSONResponse(status_code=409,
                            content={"error": "이미 연료 모델 학습이 진행 중입니다."})
    bg.add_task(lambda: threading.Thread(
        target=_fuel_iterative_sync, args=(req,), daemon=True).start())
    mode = "자동 반복 개선" if req.max_iterations > 1 else "단일"
    return {
        "message": f"Fuel XGBoost {mode} 학습 시작",
        "max_iterations": req.max_iterations,
        "samples_per_vessel": req.samples_per_vessel,
        "n_estimators": req.n_estimators,
    }


@app.get("/api/ml/fuel/status")
async def fuel_status():
    return dict(_fuel_state)


@app.get("/api/ml/fuel/iterative/status")
async def fuel_iterative_status():
    """반복 학습 히스토리 및 파라미터 변화 조회."""
    hist_path = _BASE / "models" / "fuel_iterative_history.json"
    if hist_path.exists():
        try:
            return json.loads(hist_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"iterations": [], "message": "히스토리 없음"}


@app.get("/api/ml/fuel/result")
async def fuel_result():
    """마지막 Fuel 학습 결과 반환 (모델 메트릭)."""
    model_path = _BASE / "models" / "fuel_xgb_model.pkl"
    if _fuel_state["result"]:
        return _fuel_state["result"]
    if model_path.exists():
        import joblib
        try:
            artifact = joblib.load(str(model_path))
            return {"exists": True, "metrics": artifact.get("metrics", {})}
        except Exception:
            pass
    return JSONResponse(status_code=404, content={"error": "학습된 모델이 없습니다."})


# ── What-if 엔드포인트 ────────────────────────────────────────

@app.post("/api/ml/whatif/run")
async def whatif_run(req: WhatIfRunRequest, bg: BackgroundTasks):
    """What-if Analysis 시작.
    max_iterations > 1 이면 자동 반복 개선 모드로 실행됩니다."""
    if _whatif_state["is_running"]:
        return JSONResponse(status_code=409,
                            content={"error": "이미 What-if 분석이 진행 중입니다."})
    bg.add_task(lambda: threading.Thread(
        target=_whatif_iterative_sync, args=(req,), daemon=True).start())
    mode = "자동 반복 개선" if req.max_iterations > 1 else "단일"
    return {
        "message": f"What-if Analysis {mode} 시작",
        "route": req.route,
        "ice_class": req.ice_class,
        "max_iterations": req.max_iterations,
    }


@app.get("/api/ml/whatif/status")
async def whatif_status():
    return dict(_whatif_state)


@app.get("/api/ml/whatif/iterative/status")
async def whatif_iterative_status():
    """What-if 반복 히스토리 조회."""
    hist_path = _BASE.parent / "report-service" / "data" / "whatif_iterative_history.json"
    if hist_path.exists():
        try:
            return json.loads(hist_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"iterations": [], "message": "히스토리 없음"}


@app.get("/api/ml/whatif/result")
async def whatif_result():
    """가장 좋은 What-if 결과 반환."""
    out_path = _BASE.parent / "report-service" / "data" / "whatif_latest_result.json"
    if _whatif_state["result"]:
        return _whatif_state["result"]
    if out_path.exists():
        try:
            return json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return JSONResponse(status_code=404, content={"error": "What-if 분석 결과가 없습니다."})


# ── 통합 상태 ─────────────────────────────────────────────────

@app.get("/api/ml/status")
async def all_status():
    """Fuel + What-if 통합 상태 한눈에 보기."""
    return {
        "fuel": {
            "is_training":    _fuel_state["is_training"],
            "mode":           _fuel_state["mode"],
            "progress":       _fuel_state["progress"],
            "stage":          _fuel_state["stage"],
            "iteration":      _fuel_state["iteration"],
            "max_iterations": _fuel_state["max_iterations"],
            "metrics":        _fuel_state["metrics"],
            "error":          _fuel_state["error"],
            "finished_at":    _fuel_state["finished_at"],
            "iterative_status": _fuel_state.get("iterative_status", {}),
        },
        "whatif": {
            "is_running":     _whatif_state["is_running"],
            "mode":           _whatif_state["mode"],
            "progress":       _whatif_state["progress"],
            "stage":          _whatif_state["stage"],
            "iteration":      _whatif_state["iteration"],
            "max_iterations": _whatif_state["max_iterations"],
            "scenarios_count": _whatif_state["scenarios_count"],
            "route":          _whatif_state["route"],
            "ice_class":      _whatif_state["ice_class"],
            "error":          _whatif_state["error"],
            "finished_at":    _whatif_state["finished_at"],
            "iterative_status": _whatif_state.get("iterative_status", {}),
        },
    }


# ── 시작 시 상태 복원 ────────────────────────────────────────

@app.on_event("startup")
def restore_state():
    if _STATE_FILE.exists():
        try:
            saved = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            fuel = saved.get("fuel", {})
            whatif = saved.get("whatif", {})
            if not fuel.get("is_training"):
                _fuel_state.update({k: v for k, v in fuel.items()
                                    if k not in ("is_training",)})
                _fuel_state["is_training"] = False
            if not whatif.get("is_running"):
                _whatif_state.update({k: v for k, v in whatif.items()
                                      if k not in ("is_running",)})
                _whatif_state["is_running"] = False
            logger.info("마지막 상태 복원 완료")
        except Exception as e:
            logger.warning("상태 복원 실패: %s", e)


# ── main ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8004, log_level="info")
