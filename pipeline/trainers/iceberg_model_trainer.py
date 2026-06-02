#!/usr/bin/env python3
"""
iceberg_model_trainer.py
========================
YOLOv8n 기반 SAR 빙산 탐지 모델 학습기.

파이프라인:
  1. YOLO 포맷 데이터셋 로드 (sar_dataset_builder로 생성)
  2. YOLOv8n 사전학습 가중치에서 전이학습
  3. 3채널 SAR 입력 (VV, VH, VV/VH) 처리
  4. 학습된 모델을 models/ 디렉토리에 저장

사용법:
  python iceberg_model_trainer.py                        # 기본 학습
  python iceberg_model_trainer.py --epochs 100           # 에폭 지정
  python iceberg_model_trainer.py --dataset path/to/data.yaml  # 커스텀 데이터셋
  python iceberg_model_trainer.py --resume               # 이전 학습 이어서
"""

import argparse
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger("iceberg_model_trainer")

PIPELINE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = PIPELINE_DIR / "models"
DATA_DIR = PIPELINE_DIR.parent / "data"
DEFAULT_DATASET = DATA_DIR / "datasets" / "sar_icebergs" / "data.yaml"


class IcebergModelTrainer:
    """YOLOv8 기반 SAR 빙산 탐지 모델 학습기."""

    def __init__(
        self,
        dataset_yaml: Path = DEFAULT_DATASET,
        model_name: str = "yolov8n",
        epochs: int = 50,
        batch_size: int = 8,
        img_size: int = 640,
        device: str = "cpu",
        resume: bool | None = None,  # None = 자동 감지
    ):
        self.dataset_yaml = dataset_yaml
        self.model_name = model_name
        self.epochs = epochs
        self.batch_size = batch_size
        self.img_size = img_size
        self.device = device
        self.model = None
        self.results = None
        self._last_pt = MODELS_DIR / "runs" / "iceberg_detect" / "weights" / "last.pt"
        self._completed_epochs = self._count_completed_epochs()
        # resume=None이면 last.pt 존재 + 미완료 시 자동 재개
        if resume is None:
            self.resume = self._last_pt.exists() and self._completed_epochs < self.epochs
        else:
            self.resume = resume
        if self.resume:
            log.info("YOLOv8 이어서 학습: %d/%d epoch 완료 → last.pt 에서 재개",
                     self._completed_epochs, self.epochs)

    def _count_completed_epochs(self) -> int:
        """results.csv 에서 완료된 epoch 수 반환."""
        csv = MODELS_DIR / "runs" / "iceberg_detect" / "results.csv"
        if not csv.exists():
            return 0
        try:
            lines = [l for l in csv.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
            return max(0, len(lines) - 1)
        except Exception:
            return 0

    def setup(self):
        """YOLOv8 모델 초기화."""
        try:
            from ultralytics import YOLO
        except ImportError:
            raise ImportError(
                "ultralytics가 필요합니다: pip install ultralytics\n"
                "PyTorch도 필요합니다: pip install torch torchvision"
            )

        # 사전학습 모델 로드
        pretrained = f"{self.model_name}.pt"
        log.info("YOLOv8 모델 초기화: %s (사전학습: %s)", self.model_name, pretrained)
        self.model = YOLO(pretrained)

        # 데이터셋 확인
        if not self.dataset_yaml.exists():
            raise FileNotFoundError(
                f"데이터셋을 찾을 수 없습니다: {self.dataset_yaml}\n"
                f"먼저 sar_dataset_builder.py를 실행하세요."
            )

        log.info("데이터셋: %s", self.dataset_yaml)
        log.info("디바이스: %s, 에폭: %d, 배치: %d", self.device, self.epochs, self.batch_size)

    def train(self) -> dict:
        """
        모델 학습을 실행합니다.

        Returns:
            학습 결과 메타데이터 딕셔너리
        """
        log.info("=" * 60)
        log.info("  SAR 빙산 탐지 모델 학습 시작 (resume=%s, 완료=%d/%d epoch)",
                 self.resume, self._completed_epochs, self.epochs)
        log.info("=" * 60)

        start_time = datetime.now()

        if self.resume and self._last_pt.exists():
            # last.pt 에서 이어서 학습 (YOLO resume 모드)
            from ultralytics import YOLO
            self.model = YOLO(str(self._last_pt))
            self.results = self.model.train(resume=True)
        else:
            if not self.model:
                self.setup()
            assert self.model is not None
            self.results = self.model.train(
                data=str(self.dataset_yaml),
                epochs=self.epochs,
                batch=self.batch_size,
                imgsz=self.img_size,
                device=self.device,
                project=str(MODELS_DIR / "runs"),
                name="iceberg_detect",
                exist_ok=True,
                hsv_h=0.0,
                hsv_s=0.0,
                hsv_v=0.3,
                flipud=0.5,
                fliplr=0.5,
                degrees=180,
                scale=0.3,
                mosaic=0.5,
                verbose=True,
            )

        elapsed = (datetime.now() - start_time).total_seconds()

        # 최적 가중치를 models/ 디렉토리에 복사
        best_pt = MODELS_DIR / "runs" / "iceberg_detect" / "weights" / "best.pt"
        target_pt = MODELS_DIR / "iceberg_yolov8.pt"

        if best_pt.exists():
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(best_pt, target_pt)
            log.info("최적 가중치 저장: %s", target_pt)
        else:
            log.warning("best.pt를 찾을 수 없습니다: %s", best_pt)

        # 메타데이터 저장
        metadata = {
            "model": self.model_name,
            "epochs": self.epochs,
            "batch_size": self.batch_size,
            "img_size": self.img_size,
            "device": self.device,
            "dataset": str(self.dataset_yaml),
            "training_time_seconds": round(elapsed, 1),
            "trained_at": datetime.now().isoformat(),
            "weights_path": str(target_pt) if target_pt.exists() else None,
            "classes": ["iceberg"],
            "input_channels": "3 (VV, VH, VV/VH)",
        }

        meta_path = MODELS_DIR / "iceberg_yolov8_meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        log.info("학습 완료: %.1f초, 메타데이터: %s", elapsed, meta_path)
        return metadata

    def evaluate(self) -> dict:
        """학습된 모델을 검증 세트로 평가합니다."""
        if not self.model:
            self.setup()

        assert self.model is not None
        log.info("모델 평가 시작...")
        metrics = self.model.val(
            data=str(self.dataset_yaml),
            device=self.device,
        )

        eval_results = {
            "mAP50": float(metrics.box.map50) if hasattr(metrics.box, "map50") else None,
            "mAP50_95": float(metrics.box.map) if hasattr(metrics.box, "map") else None,
            "precision": float(metrics.box.mp) if hasattr(metrics.box, "mp") else None,
            "recall": float(metrics.box.mr) if hasattr(metrics.box, "mr") else None,
        }

        log.info("평가 결과: %s", eval_results)
        return eval_results

    @staticmethod
    def get_model_info() -> dict:
        """저장된 모델 메타데이터를 반환합니다."""
        meta_path = MODELS_DIR / "iceberg_yolov8_meta.json"
        if meta_path.exists():
            with open(meta_path, encoding="utf-8") as f:
                return json.load(f)
        return {"exists": False, "message": "학습된 모델이 없습니다."}

    @staticmethod
    def model_exists() -> bool:
        """학습된 모델 파일이 존재하는지 확인합니다."""
        return (MODELS_DIR / "iceberg_yolov8.pt").exists()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="SAR 빙산 탐지 모델 학습")
    parser.add_argument("--epochs", type=int, default=50, help="학습 에폭 (기본: 50)")
    parser.add_argument("--batch", type=int, default=8, help="배치 크기 (기본: 8)")
    parser.add_argument("--dataset", type=str, default=None, help="data.yaml 경로")
    parser.add_argument("--device", type=str, default="cpu", help="디바이스 (cpu/cuda)")
    parser.add_argument("--eval-only", action="store_true", help="평가만 실행")
    args = parser.parse_args()

    dataset = Path(args.dataset) if args.dataset else DEFAULT_DATASET

    trainer = IcebergModelTrainer(
        dataset_yaml=dataset,
        epochs=args.epochs,
        batch_size=args.batch,
        device=args.device,
    )

    if args.eval_only:
        results = trainer.evaluate()
    else:
        # 데이터셋이 없으면 자동 생성
        if not dataset.exists():
            log.info("데이터셋이 없습니다. 합성 데이터셋을 자동 생성합니다...")
            from sar_dataset_builder import build_dataset
            build_dataset(synthetic_count=200)

        results = trainer.train()

    print(json.dumps(results, indent=2, ensure_ascii=False))
