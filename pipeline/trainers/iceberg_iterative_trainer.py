"""
iceberg_iterative_trainer.py
==============================
SAR YOLOv8 빙산 탐지 모델 자동 반복 개선 트레이너.

동작:
  1회 학습 완료 → 메트릭 분석 → 파라미터/데이터 조정 → 재학습 반복
  목표: mAP50 ≥ 0.70 AND precision ≥ 0.65 AND recall ≥ 0.65

조정 전략:
  - mAP50 낮음 (< 0.40)   → 합성 데이터 대폭 확대 + epoch ↑ + augmentation 강화
  - Precision 낮음         → mosaic/fliplr 강화 (다양성 ↑)
  - Recall 낮음            → 작은 빙산 샘플 비중 ↑ + scale 범위 ↑
  - 정체                   → batch_size 조정 + img_size ↑
  - 과적합 감지            → augmentation 강화 + epoch 감소
"""

import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("sar-server.iceberg_iterative")

PIPELINE_DIR  = Path(__file__).resolve().parent.parent
MODELS_DIR    = PIPELINE_DIR / "models"
HISTORY_PATH  = MODELS_DIR / "iceberg_iterative_history.json"

# ── 수렴 기준 ──────────────────────────────────────────────
TARGET_MAP50     = 0.70
TARGET_PRECISION = 0.65
TARGET_RECALL    = 0.65
MIN_MAP_IMPROVE  = 0.02   # mAP 개선 < 2% → plateau
PLATEAU_WINDOW   = 2

# ── 파라미터 범위 ──────────────────────────────────────────
PARAM_BOUNDS = {
    "epochs":           (20, 200),
    "batch_size":       (2, 32),
    "img_size":         (320, 1280),
    "synthetic_count":  (100, 2000),
    "hsv_v":            (0.1, 0.6),
    "flipud":           (0.3, 0.7),
    "fliplr":           (0.3, 0.7),
    "degrees":          (90, 360),
    "scale":            (0.2, 0.6),
    "mosaic":           (0.3, 0.9),
}


@dataclass
class IcebergIterRecord:
    iteration: int
    params: dict
    metrics: dict           # mAP50, mAP50_95, precision, recall
    signals: list[str]
    improved: bool
    converged: bool
    duration_seconds: float
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class IcebergParamAdjuster:
    """메트릭 시그널에 따라 YOLOv8 학습 파라미터를 자동 조정한다."""

    def analyze(self, metrics: dict, prev_metrics: dict | None,
                history: list[IcebergIterRecord]) -> list[str]:
        signals = []
        map50     = metrics.get("mAP50")     or 0.0
        precision = metrics.get("precision") or 0.0
        recall    = metrics.get("recall")    or 0.0

        # 절대 성능 시그널
        if map50 < 0.30:
            signals.append("very_low_map")
        elif map50 < 0.50:
            signals.append("low_map")
        elif map50 < TARGET_MAP50:
            signals.append("moderate_map")

        if precision < 0.50:
            signals.append("low_precision")
        if recall < 0.50:
            signals.append("low_recall")

        # precision > recall 불균형 → recall 부스트
        if precision > 0 and recall > 0:
            ratio = precision / (recall + 1e-9)
            if ratio > 1.5:
                signals.append("recall_deficit")
            elif ratio < 0.67:
                signals.append("precision_deficit")

        # 이전 대비 개선율
        if prev_metrics:
            prev_map = prev_metrics.get("mAP50") or 0.0
            if prev_map > 0:
                improve = map50 - prev_map
                if improve < MIN_MAP_IMPROVE:
                    signals.append("plateau")

        # 연속 plateau
        if len(history) >= PLATEAU_WINDOW:
            recent = history[-PLATEAU_WINDOW:]
            maps = [r.metrics.get("mAP50") or 0.0 for r in recent]
            if all(abs(maps[i] - maps[i-1]) < MIN_MAP_IMPROVE
                   for i in range(1, len(maps))):
                signals.append("persistent_plateau")

        if not signals:
            signals.append("converging")

        return signals

    def adjust(self, params: dict, signals: list[str], iteration: int) -> dict:
        p = deepcopy(params)

        def clip(key, val):
            lo, hi = PARAM_BOUNDS[key]
            return max(lo, min(hi, val))

        for sig in signals:
            if sig == "very_low_map":
                # 데이터와 학습량 대폭 확대
                p["synthetic_count"] = clip("synthetic_count", p["synthetic_count"] + 500)
                p["epochs"]          = clip("epochs", p["epochs"] + 30)
                p["mosaic"]          = clip("mosaic", p["mosaic"] + 0.2)
                p["scale"]           = clip("scale", p["scale"] + 0.1)
                p["hsv_v"]           = clip("hsv_v", p["hsv_v"] + 0.1)

            elif sig == "low_map":
                p["synthetic_count"] = clip("synthetic_count", p["synthetic_count"] + 200)
                p["epochs"]          = clip("epochs", p["epochs"] + 20)
                p["mosaic"]          = clip("mosaic", p["mosaic"] + 0.1)

            elif sig == "moderate_map":
                p["synthetic_count"] = clip("synthetic_count", p["synthetic_count"] + 100)
                p["epochs"]          = clip("epochs", p["epochs"] + 10)

            elif sig == "low_precision":
                # 다양한 패턴 노출로 FP 감소
                p["mosaic"]  = clip("mosaic", p["mosaic"] + 0.15)
                p["degrees"] = clip("degrees", p["degrees"] + 30)
                p["fliplr"]  = clip("fliplr", p["fliplr"] + 0.05)

            elif sig == "recall_deficit":
                # 작은 빙산 검출 향상 → scale 다양화, 이미지 해상도 ↑
                p["scale"]        = clip("scale", p["scale"] + 0.1)
                p["img_size"]     = clip("img_size", p["img_size"] + 160)
                p["flipud"]       = clip("flipud", p["flipud"] + 0.05)
                p["synthetic_count"] = clip("synthetic_count", p["synthetic_count"] + 100)

            elif sig == "precision_deficit":
                # FP 많음 → mosaic 약화, 배경 다양성 감소
                p["mosaic"] = clip("mosaic", max(p["mosaic"] - 0.1, PARAM_BOUNDS["mosaic"][0]))

            elif sig == "persistent_plateau":
                # 완전 정체 → 이미지 해상도 ↑ + 데이터 재생성
                p["img_size"]        = clip("img_size", p["img_size"] + 320)
                p["synthetic_count"] = clip("synthetic_count", p["synthetic_count"] + 300)
                p["epochs"]          = clip("epochs", p["epochs"] + 15)

            elif sig == "plateau":
                p["synthetic_count"] = clip("synthetic_count", p["synthetic_count"] + 150)
                p["epochs"]          = clip("epochs", p["epochs"] + 10)

        return p

    @staticmethod
    def converged(metrics: dict, prev_metrics: dict | None) -> bool:
        map50     = metrics.get("mAP50")     or 0.0
        precision = metrics.get("precision") or 0.0
        recall    = metrics.get("recall")    or 0.0
        if map50 < TARGET_MAP50 or precision < TARGET_PRECISION or recall < TARGET_RECALL:
            return False
        if prev_metrics is None:
            return False
        prev_map = prev_metrics.get("mAP50") or 0.0
        return (map50 - prev_map) < MIN_MAP_IMPROVE


class IcebergIterativeTrainer:
    """SAR YOLOv8 자동 반복 개선 트레이너."""

    def __init__(self, status_callback=None):
        self._adjuster = IcebergParamAdjuster()
        self._history: list[IcebergIterRecord] = []
        self._status_callback = status_callback
        self._initial_override: dict | None = None  # sar_server.py에서 첫 파라미터 주입용
        self._load_history()

    def _load_history(self):
        if HISTORY_PATH.exists():
            try:
                data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
                self._history = [IcebergIterRecord(**r) for r in data]
                logger.info("SAR 이전 히스토리 %d건 복원", len(self._history))
            except Exception as e:
                logger.warning("SAR 히스토리 로드 실패: %s", e)

    def _save_history(self):
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in self._history], f, indent=2, ensure_ascii=False)

    def _update_status(self, stage: str, progress: int, **extra):
        if self._status_callback:
            self._status_callback(stage=stage, progress=progress, **extra)

    def _initial_params(self) -> dict:
        return {
            "epochs":           30,
            "batch_size":       4,
            "img_size":         640,
            "synthetic_count":  200,
            "device":           "cpu",
            "hsv_v":            0.3,
            "flipud":           0.5,
            "fliplr":           0.5,
            "degrees":          180,
            "scale":            0.3,
            "mosaic":           0.5,
        }

    def _last_params(self) -> dict:
        if self._initial_override is not None:
            p = self._initial_params()
            override = self._initial_override
            assert override is not None
            for k, v in override.items():
                p[k] = v
            self._initial_override = None
            return p
        if self._history:
            return deepcopy(self._history[-1].params)
        return self._initial_params()

    def _last_metrics(self) -> dict | None:
        if self._history:
            return self._history[-1].metrics
        return None

    def _run_one(self, params: dict, iteration: int) -> tuple[dict, float]:
        import time
        import sys
        import shutil
        t0 = time.time()

        logger.info("[SAR Iter %d] epochs=%d, synthetic=%d, img=%d, mosaic=%.2f",
                    iteration, params["epochs"], params["synthetic_count"],
                    params["img_size"], params["mosaic"])

        self._update_status(
            stage=f"SAR Iter {iteration} — 합성 데이터셋 생성 중 ({params['synthetic_count']}개)...",
            progress=int(5 + (iteration - 1) * 20),
        )

        # trainers 디렉토리 sys.path에 추가
        trainers_dir = str(Path(__file__).parent)
        if trainers_dir not in sys.path:
            sys.path.insert(0, trainers_dir)

        # 1. 데이터셋 재생성
        from sar_dataset_builder import build_dataset
        yaml_path = build_dataset(synthetic_count=params["synthetic_count"])

        self._update_status(
            stage=f"SAR Iter {iteration} — YOLOv8 학습 중 ({params['epochs']} epoch)...",
            progress=int(10 + (iteration - 1) * 20),
        )

        # 2. YOLOv8 학습
        from iceberg_model_trainer import IcebergModelTrainer
        trainer = IcebergModelTrainer(
            dataset_yaml=yaml_path,
            epochs=params["epochs"],
            batch_size=params["batch_size"],
            img_size=params["img_size"],
            device=params["device"],
        )
        trainer.setup()

        from ultralytics import YOLO
        from pathlib import Path as _Path
        assert trainer.model is not None
        trainer.results = trainer.model.train(
            data=str(yaml_path),
            epochs=params["epochs"],
            batch=params["batch_size"],
            imgsz=params["img_size"],
            device=params["device"],
            project=str(MODELS_DIR / "runs"),
            name=f"iceberg_detect_iter{iteration}",
            exist_ok=True,
            hsv_h=0.0,
            hsv_s=0.0,
            hsv_v=params["hsv_v"],
            flipud=params["flipud"],
            fliplr=params["fliplr"],
            degrees=params["degrees"],
            scale=params["scale"],
            mosaic=params["mosaic"],
            verbose=False,
            patience=10,         # Early Stopping (10 epoch 개선 없으면 종료)
        )

        # 3. 평가
        self._update_status(
            stage=f"SAR Iter {iteration} — 모델 평가 중...",
            progress=int(15 + (iteration - 1) * 20),
        )
        metrics = trainer.evaluate()
        logger.info("[SAR Iter %d] mAP50=%.4f, P=%.4f, R=%.4f",
                    iteration,
                    metrics.get("mAP50") or 0.0,
                    metrics.get("precision") or 0.0,
                    metrics.get("recall") or 0.0)

        # 4. best 가중치 관리
        best_pt = MODELS_DIR / "runs" / f"iceberg_detect_iter{iteration}" / "weights" / "best.pt"
        target_pt = MODELS_DIR / f"iceberg_yolov8_iter{iteration}.pt"
        if best_pt.exists():
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(best_pt, target_pt)

        # 지금까지 최고 모델이면 best로 복사
        prev = self._last_metrics()
        prev_map = (prev.get("mAP50") or 0.0) if prev else 0.0
        cur_map  = metrics.get("mAP50") or 0.0
        if cur_map > prev_map and best_pt.exists():
            shutil.copy2(best_pt, MODELS_DIR / "iceberg_yolov8.pt")
            logger.info("[SAR Iter %d] 새 최고 모델 저장: mAP50=%.4f", iteration, cur_map)

        return metrics, time.time() - t0

    def run(self, max_iterations: int = 3, force_restart: bool = False) -> dict:
        if force_restart:
            self._history = []

        start_iter = len(self._history)
        logger.info("SAR 자동 반복 개선 시작 (이미 %d회, 최대 %d회)", start_iter, max_iterations)

        params = self._last_params()

        for i in range(start_iter, max_iterations):
            iteration_num = i + 1
            try:
                metrics, elapsed = self._run_one(params, iteration_num)
            except Exception as e:
                logger.error("[SAR Iter %d] 실패: %s", iteration_num, e, exc_info=True)
                break

            prev_metrics = self._last_metrics()
            signals      = self._adjuster.analyze(metrics, prev_metrics, self._history)
            converged    = self._adjuster.converged(metrics, prev_metrics)
            improved     = (prev_metrics is None or
                            (metrics.get("mAP50") or 0.0) > (prev_metrics.get("mAP50") or 0.0))

            record = IcebergIterRecord(
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

            logger.info("[SAR Iter %d] 완료 | 시그널: %s | 수렴: %s", iteration_num, signals, converged)

            self._update_status(
                stage=(f"SAR Iter {iteration_num} 완료 — "
                       f"mAP50={metrics.get('mAP50') or 0:.4f}, "
                       f"P={metrics.get('precision') or 0:.4f}, "
                       f"R={metrics.get('recall') or 0:.4f}"),
                progress=int(20 + iteration_num * 20),
                metrics=metrics,
                signals=signals,
            )

            if converged:
                logger.info("[SAR] 수렴 달성 — 반복 종료")
                break

            if i < max_iterations - 1:
                params = self._adjuster.adjust(params, signals, iteration_num)

        best = (max(self._history, key=lambda r: r.metrics.get("mAP50") or 0.0)
                if self._history else None)
        result = {
            "total_iterations": len(self._history),
            "converged": self._history[-1].converged if self._history else False,
            "best_metrics": best.metrics if best else {},
            "best_iteration": best.iteration if best else 0,
            "history": [asdict(r) for r in self._history],
            "finished_at": datetime.now().isoformat(),
        }
        return result

    def get_status(self) -> dict:
        if not self._history:
            return {"iterations_done": 0, "best_metrics": {}, "history": []}
        best = max(self._history, key=lambda r: r.metrics.get("mAP50") or 0.0)
        return {
            "iterations_done": len(self._history),
            "converged": self._history[-1].converged,
            "best_metrics": best.metrics,
            "best_iteration": best.iteration,
            "last_signals": self._history[-1].signals,
            "history": [asdict(r) for r in self._history],
        }
