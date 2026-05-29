"""
prediction_calibrator.py — (B) 예측 교정 온라인 RL
==================================================
해빙 농도 → 빙종 매핑 임계값을 실측 데이터와의 오차로 미세 조정.

트리거: 백엔드 데이터 갱신(6시간)마다 POST /api/report/rl/calibrate
보상: -(예측 RIO - 실제 RIO)² per segment
업데이트: 단순 정책 그래디언트
"""

import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("report-service.rl.prediction_calibrator")

STATE_PATH = Path(__file__).resolve().parents[1] / ".." / "data" / "calibration_state.json"

DEFAULT_THRESHOLDS = {
    "open_water": 0.10,
    "grey_white": 0.40,
    "thin_fy": 0.70,
    "medium_fy": 0.85,
}


class PredictionCalibrator:
    """예측 교정 온라인 RL."""

    def __init__(self):
        self.state = self._load_state()

    def _load_state(self) -> dict:
        """calibration_state.json 로드."""
        if STATE_PATH.exists():
            try:
                with open(STATE_PATH, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("교정 상태 로드 실패: %s", e)
        return {
            "thresholds": DEFAULT_THRESHOLDS.copy(),
            "learning_rate": 0.01,
            "episode_count": 0,
        }

    def _save_state(self):
        """calibration_state.json 저장."""
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def get_thresholds(self) -> dict:
        """현재 임계값 반환."""
        return self.state["thresholds"]

    def calibrate(
        self,
        predicted_rios: list[float],
        actual_rios: list[float],
    ) -> dict:
        """예측 RIO와 실제 RIO 간 오차를 기반으로 임계값 조정.

        Parameters
        ----------
        predicted_rios : 현재 임계값으로 계산한 구간별 RIO
        actual_rios : 실측 데이터 기반 구간별 RIO

        Returns
        -------
        업데이트된 임계값
        """
        lr = self.state["learning_rate"]
        thresholds = self.state["thresholds"]

        # 보상 계산: -(오차)²
        errors = np.array(predicted_rios) - np.array(actual_rios)
        mean_sq_error = float(np.mean(errors ** 2))

        # 비례 그래디언트: 오차 크기에 비례해 임계값 조정
        # errors = predicted - actual
        # avg_error > 0 → 예측이 실제보다 높음(낙관적) → 임계값을 낮춰 보수적으로
        # avg_error < 0 → 예측이 실제보다 낮음(비관적) → 임계값을 높임
        avg_error = float(np.mean(errors))
        # np.sign + 상수 조정 대신 오차 크기 비례 조정 (최대 ±0.05 클리핑)
        adjustment = float(np.clip(-lr * avg_error, -0.05, 0.05))

        # 임계값 미세 조정 (범위 제한)
        for key in thresholds:
            thresholds[key] = max(0.01, min(0.99,
                thresholds[key] + adjustment
            ))

        self.state["episode_count"] += 1
        self.state["thresholds"] = thresholds
        self._save_state()

        logger.info(
            "교정 완료 (에피소드 %d): MSE=%.4f, 조정=%.4f",
            self.state["episode_count"], mean_sq_error, adjustment
        )

        return {
            "thresholds": thresholds,
            "episode_count": self.state["episode_count"],
            "mean_squared_error": round(mean_sq_error, 6),
            "adjustment": round(adjustment, 6),
        }

    def get_info(self) -> dict:
        """교정 상태 정보."""
        return {
            "thresholds": self.state["thresholds"],
            "learning_rate": self.state["learning_rate"],
            "episode_count": self.state["episode_count"],
        }
