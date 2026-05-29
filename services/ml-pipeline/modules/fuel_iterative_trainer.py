"""
fuel_iterative_trainer.py
==========================
Fuel XGBoost 자동 반복 개선 트레이너.

동작:
  1회 학습 완료 → 메트릭 분석 → 파라미터 조정 → 재학습 반복
  목표: R² ≥ 0.95 AND RMSE 개선율 < 1% (수렴)

조정 전략:
  - R² 낮음 (< 0.85)   → n_estimators ↑, max_depth ↑, 샘플 수 ↑
  - 과적합 감지         → max_depth ↓, subsample ↓, early_stopping 강화
  - 개선 정체 (plateau) → learning_rate ↓, colsample_bytree 조정
  - RMSE 미세 잔차      → noise 감소, 샘플 수 대폭 확대
"""

import json
import logging
import os
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import numpy as np

from . import config as cfg
from .data_generator import generate_dataset
from .model_trainer import FuelModelTrainer

logger = logging.getLogger("ml-training-service.fuel_iterative")

# ── 수렴 기준 ──────────────────────────────────────────────
TARGET_R2        = 0.95     # 목표 R²
TARGET_RMSE_IMPROVE = 0.01  # 이전 대비 RMSE 개선 < 1% → 수렴
MIN_IMPROVE_PCT  = 0.005    # plateau 판정 개선율 임계값
PLATEAU_WINDOW   = 3        # plateau 연속 체크 횟수

# ── 파라미터 범위 ──────────────────────────────────────────
PARAM_BOUNDS = {
    "n_estimators":     (100, 1000),
    "max_depth":        (3, 10),
    "learning_rate":    (0.01, 0.3),
    "subsample":        (0.6, 1.0),
    "colsample_bytree": (0.6, 1.0),
    "samples_per_vessel": (200, 1200),
    "noise_std":        (0.02, 0.12),
}


@dataclass
class IterationRecord:
    iteration: int
    params: dict
    metrics: dict          # RMSE, R2, MAE, R2_log
    signals: list[str]
    improved: bool
    converged: bool
    duration_seconds: float
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class FuelParamAdjuster:
    """메트릭 시그널에 따라 XGBoost 파라미터를 자동 조정한다."""

    def analyze(self, metrics: dict, prev_metrics: dict | None, history: list[IterationRecord]) -> list[str]:
        """현재 성능 기반 시그널 목록 반환."""
        signals = []
        r2      = metrics.get("R2", 0.0)
        rmse    = metrics.get("RMSE", 999.0)
        r2_log  = metrics.get("R2_log", 0.0)

        # 절대 성능 시그널
        if r2 < 0.75:
            signals.append("very_low_r2")
        elif r2 < 0.85:
            signals.append("low_r2")
        elif r2 < TARGET_R2:
            signals.append("moderate_r2")

        # log 공간 과적합 감지: R2_log 높은데 R2 낮음
        if r2_log > 0.98 and r2 < 0.90:
            signals.append("log_overfitting")

        # 전 회차 대비 개선율
        if prev_metrics:
            prev_rmse = prev_metrics.get("RMSE", 999.0)
            if prev_rmse > 0:
                improve_pct = (prev_rmse - rmse) / prev_rmse
                if improve_pct < MIN_IMPROVE_PCT:
                    signals.append("plateau")
                elif improve_pct < 0.03:
                    signals.append("slow_improve")

        # plateau 연속 체크
        if len(history) >= PLATEAU_WINDOW:
            recent = history[-PLATEAU_WINDOW:]
            rmse_vals = [r.metrics.get("RMSE", 999.0) for r in recent]
            if all(abs(rmse_vals[i] - rmse_vals[i-1]) / max(rmse_vals[i-1], 1e-9) < MIN_IMPROVE_PCT
                   for i in range(1, len(rmse_vals))):
                signals.append("persistent_plateau")

        if not signals:
            signals.append("converging")

        return signals

    def adjust(self, params: dict, signals: list[str], iteration: int) -> dict:
        """시그널에 따라 파라미터 조정 후 반환 (bounds 클리핑 포함)."""
        p = deepcopy(params)

        for sig in signals:
            if sig == "very_low_r2":
                # 모델 용량과 데이터를 대폭 확대
                p["n_estimators"]      = min(p["n_estimators"] + 200, PARAM_BOUNDS["n_estimators"][1])
                p["max_depth"]         = min(p["max_depth"] + 2, PARAM_BOUNDS["max_depth"][1])
                p["samples_per_vessel"] = min(p["samples_per_vessel"] + 300, PARAM_BOUNDS["samples_per_vessel"][1])
                p["learning_rate"]     = max(p["learning_rate"] * 0.8, PARAM_BOUNDS["learning_rate"][0])

            elif sig == "low_r2":
                p["n_estimators"]      = min(p["n_estimators"] + 100, PARAM_BOUNDS["n_estimators"][1])
                p["max_depth"]         = min(p["max_depth"] + 1, PARAM_BOUNDS["max_depth"][1])
                p["samples_per_vessel"] = min(p["samples_per_vessel"] + 150, PARAM_BOUNDS["samples_per_vessel"][1])

            elif sig == "moderate_r2":
                p["n_estimators"]      = min(p["n_estimators"] + 50, PARAM_BOUNDS["n_estimators"][1])
                p["subsample"]         = min(p["subsample"] + 0.05, PARAM_BOUNDS["subsample"][1])

            elif sig == "log_overfitting":
                # log 공간에서 과적합 → 정규화 강화
                p["max_depth"]         = max(p["max_depth"] - 1, PARAM_BOUNDS["max_depth"][0])
                p["subsample"]         = max(p["subsample"] - 0.1, PARAM_BOUNDS["subsample"][0])
                p["colsample_bytree"]  = max(p["colsample_bytree"] - 0.1, PARAM_BOUNDS["colsample_bytree"][0])
                p["noise_std"]         = min(p.get("noise_std", 0.07) + 0.01, PARAM_BOUNDS["noise_std"][1])

            elif sig == "persistent_plateau":
                # 완전 정체 → 학습률 대폭 낮추고 샘플 확대
                p["learning_rate"]     = max(p["learning_rate"] * 0.5, PARAM_BOUNDS["learning_rate"][0])
                p["n_estimators"]      = min(int(p["n_estimators"] * 1.5), PARAM_BOUNDS["n_estimators"][1])
                p["samples_per_vessel"] = min(p["samples_per_vessel"] + 200, PARAM_BOUNDS["samples_per_vessel"][1])
                p["noise_std"]         = max(p.get("noise_std", 0.07) - 0.01, PARAM_BOUNDS["noise_std"][0])

            elif sig == "plateau":
                p["learning_rate"]     = max(p["learning_rate"] * 0.7, PARAM_BOUNDS["learning_rate"][0])
                p["colsample_bytree"]  = min(p["colsample_bytree"] + 0.05, PARAM_BOUNDS["colsample_bytree"][1])

            elif sig == "slow_improve":
                # 미세 개선 중 → 약한 추가 자원
                p["n_estimators"]      = min(p["n_estimators"] + 30, PARAM_BOUNDS["n_estimators"][1])

        # 반복 횟수가 늘수록 early_stopping 강화 (과적합 방지)
        p["early_stopping_rounds"] = max(10, 30 - iteration * 3)

        return p

    @staticmethod
    def converged(metrics: dict, prev_metrics: dict | None) -> bool:
        """수렴 조건: R² ≥ 목표 AND RMSE 개선 < 1%."""
        r2 = metrics.get("R2", 0.0)
        if r2 < TARGET_R2:
            return False
        if prev_metrics is None:
            return False  # 최소 2회는 실행
        prev_rmse = prev_metrics.get("RMSE", 999.0)
        rmse      = metrics.get("RMSE", 999.0)
        improve   = (prev_rmse - rmse) / max(prev_rmse, 1e-9)
        return improve < TARGET_RMSE_IMPROVE


class FuelIterativeTrainer:
    """Fuel XGBoost 자동 반복 개선 트레이너.

    사용법:
        trainer = FuelIterativeTrainer()
        trainer.run(max_iterations=5)
    """

    HISTORY_PATH = Path(__file__).parent.parent / "models" / "fuel_iterative_history.json"

    def __init__(self, base_dir: Path | None = None, status_callback=None):
        self._base = base_dir or Path(__file__).parent.parent
        self._adjuster = FuelParamAdjuster()
        self._history: list[IterationRecord] = []
        self._status_callback = status_callback  # 진행 상황 알림용 콜백
        self._load_history()

    # ── 히스토리 관리 ─────────────────────────────

    def _load_history(self):
        if self.HISTORY_PATH.exists():
            try:
                data = json.loads(self.HISTORY_PATH.read_text(encoding="utf-8"))
                self._history = [IterationRecord(**r) for r in data]
                logger.info("Fuel 이전 히스토리 %d건 복원", len(self._history))
            except Exception as e:
                logger.warning("히스토리 로드 실패: %s", e)
                self._history = []

    def _save_history(self):
        self.HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(self.HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in self._history], f, indent=2, ensure_ascii=False)

    def _update_status(self, stage: str, progress: int, **extra):
        if self._status_callback:
            self._status_callback(stage=stage, progress=progress, **extra)

    # ── 현재 파라미터 상태 ─────────────────────────

    def _initial_params(self) -> dict:
        """초기 파라미터 (config.py 값 기반)."""
        return {
            "n_estimators":      cfg.XGBOOST_PARAMS["n_estimators"],
            "max_depth":         cfg.XGBOOST_PARAMS["max_depth"],
            "learning_rate":     cfg.XGBOOST_PARAMS["learning_rate"],
            "subsample":         cfg.XGBOOST_PARAMS["subsample"],
            "colsample_bytree":  cfg.XGBOOST_PARAMS["colsample_bytree"],
            "samples_per_vessel": cfg.SAMPLES_PER_VESSEL,
            "noise_std":         cfg.NOISE_STD,
            "early_stopping_rounds": 20,
        }

    def _last_params(self) -> dict:
        """마지막 학습에서 사용한 파라미터 반환."""
        if self._history:
            return deepcopy(self._history[-1].params)
        return self._initial_params()

    def _last_metrics(self) -> dict | None:
        if self._history:
            return self._history[-1].metrics
        return None

    # ── 단일 학습 실행 ─────────────────────────────

    def _run_one(self, params: dict, iteration: int) -> tuple[dict, float]:
        """파라미터로 1회 학습 실행 → (metrics, elapsed_sec) 반환."""
        import time
        t0 = time.time()

        # config 동적 패치
        orig_spv    = cfg.SAMPLES_PER_VESSEL
        orig_noise  = cfg.NOISE_STD
        orig_xgb    = deepcopy(cfg.XGBOOST_PARAMS)

        cfg.SAMPLES_PER_VESSEL              = params["samples_per_vessel"]
        cfg.NOISE_STD                       = params.get("noise_std", cfg.NOISE_STD)
        cfg.XGBOOST_PARAMS["n_estimators"]  = params["n_estimators"]
        cfg.XGBOOST_PARAMS["max_depth"]     = params["max_depth"]
        cfg.XGBOOST_PARAMS["learning_rate"] = params["learning_rate"]
        cfg.XGBOOST_PARAMS["subsample"]     = params["subsample"]
        cfg.XGBOOST_PARAMS["colsample_bytree"] = params["colsample_bytree"]

        try:
            data_path  = str(self._base / cfg.DATA_DIR / cfg.DATASET_FILENAME)
            model_path = str(self._base / cfg.MODEL_DIR / f"fuel_xgb_iter{iteration}.pkl")
            fi_path    = str(self._base / cfg.MODEL_DIR / f"feature_importance_iter{iteration}.png")

            os.makedirs(str(self._base / cfg.DATA_DIR), exist_ok=True)
            os.makedirs(str(self._base / cfg.MODEL_DIR), exist_ok=True)

            df = generate_dataset(save_path=data_path)

            trainer = FuelModelTrainer()
            trainer.prepare(df)
            trainer.train()
            metrics = trainer.evaluate()

            # Feature Importance 저장
            try:
                trainer.plot_feature_importance(save_path=fi_path)
            except Exception:
                pass

            # 현재 모델도 저장
            trainer.save_model(model_path)

            # 가장 좋은 모델을 best로 복사
            best_metrics = self._last_metrics()
            if best_metrics is None or metrics.get("R2", 0) > best_metrics.get("R2", 0):
                best_path = str(self._base / cfg.MODEL_DIR / "fuel_xgb_model.pkl")
                trainer.save_model(best_path)
                logger.info("[Iter %d] 새 최고 모델 저장: R²=%.4f", iteration, metrics.get("R2", 0))

        finally:
            # config 복원
            cfg.SAMPLES_PER_VESSEL = orig_spv
            cfg.NOISE_STD          = orig_noise
            cfg.XGBOOST_PARAMS.update(orig_xgb)

        return metrics, time.time() - t0

    # ── 메인 반복 루프 ─────────────────────────────

    def run(
        self,
        max_iterations: int = 5,
        force_restart: bool = False,
    ) -> dict:
        """자동 반복 개선 실행.

        Args:
            max_iterations: 최대 반복 횟수 (이미 완료된 횟수 포함)
            force_restart:  히스토리 무시하고 처음부터 재시작

        Returns:
            최종 결과 딕셔너리
        """
        if force_restart:
            self._history = []

        start_iter = len(self._history)
        logger.info("=" * 60)
        logger.info("  Fuel 자동 반복 개선 시작 (이미 %d회 완료, 최대 %d회)",
                    start_iter, max_iterations)
        logger.info("=" * 60)

        params = self._last_params()

        for i in range(start_iter, max_iterations):
            iteration_num = i + 1
            logger.info("\n[Iter %d/%d] 학습 파라미터: n_est=%d, depth=%d, lr=%.3f, spv=%d",
                        iteration_num, max_iterations,
                        params["n_estimators"], params["max_depth"],
                        params["learning_rate"], params["samples_per_vessel"])

            self._update_status(
                stage=f"반복 학습 {iteration_num}/{max_iterations} — 데이터 생성 및 학습 중...",
                progress=int(60 + (i / max_iterations) * 35),
                iteration=iteration_num,
                params=params,
            )

            try:
                metrics, elapsed = self._run_one(params, iteration_num)
            except Exception as e:
                logger.error("[Iter %d] 학습 실패: %s", iteration_num, e, exc_info=True)
                break

            prev_metrics = self._last_metrics()
            signals      = self._adjuster.analyze(metrics, prev_metrics, self._history)
            converged    = self._adjuster.converged(metrics, prev_metrics)
            improved     = (prev_metrics is None or
                            metrics.get("R2", 0) > prev_metrics.get("R2", 0))

            record = IterationRecord(
                iteration=iteration_num,
                params=deepcopy(params),
                metrics=metrics,
                signals=signals,
                improved=improved,
                converged=converged,
                duration_seconds=round(elapsed, 1),
            )
            self._history.append(record)
            self._save_history()

            logger.info("[Iter %d] R²=%.4f, RMSE=%.6f, MAE=%.6f | 시그널: %s | 수렴: %s",
                        iteration_num,
                        metrics.get("R2", 0), metrics.get("RMSE", 0), metrics.get("MAE", 0),
                        signals, converged)

            self._update_status(
                stage=f"반복 {iteration_num} 완료 — R²={metrics.get('R2',0):.4f}, RMSE={metrics.get('RMSE',0):.6f}",
                progress=int(60 + (iteration_num / max_iterations) * 35),
                iteration=iteration_num,
                metrics=metrics,
                signals=signals,
            )

            if converged:
                logger.info("[FuelIterative] 수렴 달성 — 반복 종료 (iter %d)", iteration_num)
                break

            if i < max_iterations - 1:
                params = self._adjuster.adjust(params, signals, iteration_num)
                logger.info("[Iter %d] 다음 파라미터: n_est=%d, depth=%d, lr=%.4f",
                            iteration_num, params["n_estimators"],
                            params["max_depth"], params["learning_rate"])

        # 최종 요약
        best = max(self._history, key=lambda r: r.metrics.get("R2", 0)) if self._history else None
        result = {
            "total_iterations": len(self._history),
            "converged": self._history[-1].converged if self._history else False,
            "best_metrics": best.metrics if best else {},
            "best_iteration": best.iteration if best else 0,
            "history": [asdict(r) for r in self._history],
            "finished_at": datetime.now().isoformat(),
        }
        logger.info("[FuelIterative] 완료: %d회 반복, 최고 R²=%.4f",
                    result["total_iterations"],
                    result["best_metrics"].get("R2", 0))
        return result

    def get_status(self) -> dict:
        """외부에서 진행 상황 조회용."""
        if not self._history:
            return {"iterations_done": 0, "best_metrics": {}, "history": []}
        best = max(self._history, key=lambda r: r.metrics.get("R2", 0))
        return {
            "iterations_done": len(self._history),
            "converged": self._history[-1].converged,
            "best_metrics": best.metrics,
            "best_iteration": best.iteration,
            "last_signals": self._history[-1].signals,
            "history": [asdict(r) for r in self._history],
        }
