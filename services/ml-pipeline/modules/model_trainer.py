"""
XGBoost 회귀 모델 학습·평가·저장 모듈

- Train/Test 분리 후 RMSE, R², MAE 평가
- Feature Importance 시각화 (PNG 저장)
- 학습된 모델을 joblib으로 .pkl 저장
"""

import os

import joblib
import matplotlib
matplotlib.use("Agg")  # GUI 없는 환경 대응
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import train_test_split
from xgboost import XGBRegressor

from . import config as cfg


class FuelModelTrainer:
    """XGBoost 연료 소모량 예측 모델 학습기.

    Target(fuel_per_nm)에 log 변환을 적용하여 학습하고,
    예측 시 exp()로 역변환하여 원래 스케일의 값을 반환한다.
    (연료 소모량이 지수적으로 분포하므로 log 변환이 필수적)
    """

    def __init__(self):
        self.model: XGBRegressor | None = None
        self.X_train = None
        self.X_test = None
        self.y_train_log = None   # log 변환된 target (학습용)
        self.y_test_raw = None    # 원본 target (평가용)
        self.y_test_log = None    # log 변환된 target (평가용)
        self.metrics: dict = {}

    # ─── 데이터 준비 ───────────────────────────

    def prepare(self, df: pd.DataFrame):
        """DataFrame에서 Feature/Target을 분리하고 Train/Test로 나눈다."""
        X = df[cfg.FEATURE_COLUMNS].values
        y = df[cfg.TARGET_COLUMN].values

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=cfg.TEST_SIZE, random_state=cfg.RANDOM_SEED
        )

        self.X_train = X_train
        self.X_test = X_test
        self.y_train_log = np.log(y_train)
        self.y_test_raw = y_test
        self.y_test_log = np.log(y_test)

        print(
            f"[데이터 분할] Train: {len(self.X_train)}건 / Test: {len(self.X_test)}건"
        )

    # ─── 학습 ──────────────────────────────────

    def train(self):
        """XGBoost Regressor를 log(fuel_per_nm)에 대해 학습시킨다."""
        self.model = XGBRegressor(**cfg.XGBOOST_PARAMS)
        self.model.fit(
            self.X_train,
            self.y_train_log,
            eval_set=[(self.X_test, self.y_test_log)],
            verbose=False,
        )
        print("[학습 완료] XGBoost Regressor 학습 완료 (log-transformed target)")

    # ─── 평가 ──────────────────────────────────

    def evaluate(self) -> dict:
        """Test 데이터로 모델 성능을 평가하고 결과를 반환한다.

        log 스케일과 원본 스케일 모두에서 성능을 측정한다.
        """
        assert self.model is not None
        y_pred_log = self.model.predict(self.X_test)
        y_pred = np.exp(y_pred_log)  # 원본 스케일로 역변환

        # 원본 스케일 평가 (실질적 예측 성능)
        rmse = float(np.sqrt(mean_squared_error(self.y_test_raw, y_pred)))
        r2 = float(r2_score(self.y_test_raw, y_pred))
        mae = float(mean_absolute_error(self.y_test_raw, y_pred))

        # log 스케일 평가 (모델이 실제 학습한 공간)
        r2_log = float(r2_score(self.y_test_log, y_pred_log))

        self.metrics = {"RMSE": rmse, "R2": r2, "MAE": mae, "R2_log": r2_log}

        print("=" * 50)
        print("  모델 평가 결과")
        print("=" * 50)
        print(f"  RMSE      : {rmse:.6f} tons/nm")
        print(f"  R² (원본)  : {r2:.4f}")
        print(f"  R² (log)  : {r2_log:.4f}")
        print(f"  MAE       : {mae:.6f} tons/nm")
        print("=" * 50)

        return self.metrics

    # ─── Feature Importance ────────────────────

    def plot_feature_importance(self, save_path: str | None = None):
        """Feature Importance를 막대 그래프로 시각화하고 저장한다."""
        assert self.model is not None
        importances = self.model.feature_importances_
        feature_names = cfg.FEATURE_COLUMNS

        sorted_idx = np.argsort(importances)[::-1]
        sorted_names = [feature_names[i] for i in sorted_idx]
        sorted_values = importances[sorted_idx]

        fig, ax = plt.subplots(figsize=(10, 6))
        bars = ax.barh(
            range(len(sorted_names)),
            sorted_values,
            color="#2196F3",
            edgecolor="#1565C0",
        )
        ax.set_yticks(range(len(sorted_names)))
        ax.set_yticklabels(sorted_names, fontsize=12)
        ax.set_xlabel("Importance", fontsize=12)
        ax.set_title("XGBoost Feature Importance — Fuel per NM Prediction", fontsize=14)
        ax.invert_yaxis()

        # 값 표시
        for bar, val in zip(bars, sorted_values):
            ax.text(
                bar.get_width() + 0.005,
                bar.get_y() + bar.get_height() / 2,
                f"{val:.3f}",
                va="center",
                fontsize=11,
            )

        plt.tight_layout()

        if save_path:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            fig.savefig(save_path, dpi=150)
            print(f"[Feature Importance] 차트 저장 → {save_path}")

        plt.close(fig)

        # dict로도 반환
        return dict(zip(sorted_names, sorted_values.tolist()))

    # ─── 모델 저장 ─────────────────────────────

    def save_model(self, save_path: str):
        """학습된 모델을 joblib으로 .pkl 파일로 저장한다.

        log 변환 메타정보를 포함하여 저장하므로,
        로드 후 predict()만 호출하면 원본 스케일 값이 반환된다.
        """
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        artifact = {
            "model": self.model,
            "log_transformed": True,
            "feature_columns": cfg.FEATURE_COLUMNS,
            "metrics": self.metrics,
        }
        joblib.dump(artifact, save_path)
        print(f"[모델 저장] {save_path}")

    # ─── 모델 로드 및 예측 ─────────────────────

    @staticmethod
    def load_model(path: str):
        """저장된 .pkl 모델을 로드한다."""
        artifact = joblib.load(path)
        print(f"[모델 로드] {path}")
        return artifact

    @staticmethod
    def predict(artifact: dict, X: np.ndarray) -> np.ndarray:
        """로드된 모델 artifact로 예측한다 (자동 역변환 포함)."""
        y_pred_log = artifact["model"].predict(X)
        if artifact.get("log_transformed", False):
            return np.exp(y_pred_log)
        return y_pred_log
