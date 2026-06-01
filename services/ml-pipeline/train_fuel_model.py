"""
XGBoost 연료 소모량 예측 모델 — 메인 학습 스크립트

실행 방법:
    cd Digital_twin/ml-pipeline
    python train_fuel_model.py

실행 결과:
    1. data/fuel_dataset.csv        — 가상 학습 데이터 (1,200건)
    2. models/fuel_xgb_model.pkl    — 학습된 XGBoost 모델
    3. models/feature_importance.png — Feature Importance 차트
"""

import os
import sys

import numpy as np

# 프로젝트 루트 기준 모듈 임포트
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules import config as cfg  # type: ignore[attr-defined]
from modules.data_generator import generate_dataset
from modules.model_trainer import FuelModelTrainer


def main():
    print("=" * 60)
    print("  북극항로 연료 소모량 예측 모델 (XGBoost Regression)")
    print("=" * 60)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_path = os.path.join(base_dir, cfg.DATA_DIR, cfg.DATASET_FILENAME)
    model_path = os.path.join(base_dir, cfg.MODEL_DIR, cfg.MODEL_FILENAME)
    fi_path = os.path.join(
        base_dir, cfg.MODEL_DIR, cfg.FEATURE_IMPORTANCE_FILENAME
    )

    # ── Step 1: 가상 데이터 생성 ──────────────────────────────
    print("\n[Step 1] 가상 데이터 생성 중...")
    df = generate_dataset(save_path=data_path)

    # 데이터 요약
    print(f"\n  전체 샘플 수 : {len(df)}")
    print(f"  선종별 분포  :")
    for vtype, count in df["vessel_type"].value_counts().items():
        print(f"    - {vtype}: {count}건")
    print(f"\n  fuel_per_nm 통계:")
    print(f"    min  = {df['fuel_per_nm'].min():.6f}")
    print(f"    max  = {df['fuel_per_nm'].max():.4f}")
    print(f"    mean = {df['fuel_per_nm'].mean():.6f}")
    print(f"    std  = {df['fuel_per_nm'].std():.6f}")

    # ── Step 2: 모델 학습 ─────────────────────────────────────
    print("\n[Step 2] XGBoost 모델 학습 중...")
    trainer = FuelModelTrainer()
    trainer.prepare(df)
    trainer.train()

    # ── Step 3: 모델 평가 ─────────────────────────────────────
    print("\n[Step 3] 모델 평가...")
    metrics = trainer.evaluate()

    # ── Step 4: Feature Importance ─────────────────────────────
    print("\n[Step 4] Feature Importance 시각화...")
    fi = trainer.plot_feature_importance(save_path=fi_path)
    print("  Feature Importance 순위:")
    for rank, (name, score) in enumerate(fi.items(), 1):
        print(f"    {rank}. {name}: {score:.4f}")

    # ── Step 5: 모델 저장 ─────────────────────────────────────
    print("\n[Step 5] 학습된 모델 저장...")
    trainer.save_model(model_path)

    # ── Step 6: 예측 테스트 (모델 로드 후 샘플 예측) ───────────
    print("\n[Step 6] 저장된 모델 로드 및 예측 테스트...")
    artifact = FuelModelTrainer.load_model(model_path)

    test_cases = [
        {
            "label": "쇄빙선 / 개수역 (얼음 없음)",
            "input": [20000, 8.5, 32000, 0.0, 0.0, 2],
        },
        {
            "label": "쇄빙선 / 중빙역 (두께 2m, 농도 0.8)",
            "input": [20000, 8.5, 32000, 2.0, 0.8, 2],
        },
        {
            "label": "LNG 운반선 / 개수역",
            "input": [95000, 12.0, 37000, 0.0, 0.0, 4],
        },
        {
            "label": "LNG 운반선 / 중빙역 (두께 1.5m, 농도 0.6)",
            "input": [95000, 12.0, 37000, 1.5, 0.6, 4],
        },
        {
            "label": "컨테이너선 / 개수역",
            "input": [55000, 14.2, 28000, 0.0, 0.0, 0],
        },
        {
            "label": "컨테이너선 / 중빙역 (두께 1.5m, 농도 0.7)",
            "input": [55000, 14.2, 28000, 1.5, 0.7, 0],
        },
    ]

    print("\n  예측 결과:")
    print("  " + "-" * 65)
    for tc in test_cases:
        pred = FuelModelTrainer.predict(artifact, np.array([tc["input"]]))[0]
        print(f"  {tc['label']}")
        print(f"    → 예측 fuel_per_nm = {pred:.6f} tons/nm")
        print()

    # ── 완료 ──────────────────────────────────────────────────
    print("=" * 60)
    print("  학습 파이프라인 완료!")
    print(f"  - 데이터: {data_path}")
    print(f"  - 모델 : {model_path}")
    print(f"  - 차트 : {fi_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
