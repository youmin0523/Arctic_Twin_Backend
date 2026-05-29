"""
departure_env.py — (A) 출항 스케줄링 RL 환경
=============================================
Gymnasium 환경: 최적 출항 날짜를 선택하는 연속 액션 RL.

State (28-dim):
  - month_sin, month_cos (2)
  - segment_conc × 7 (7)
  - segment_wave × 7 (7)
  - segment_visibility × 7 (7)
  - ice_class_encoded (1)
  - transit_days_normalized (1)
  - forecast_window_day (1)
  - day_of_week_sin, day_of_week_cos (2)

Action (1-dim, continuous):
  - departure_day_offset [-1, +1] → 0~forecast_days 스케일

Reward:
  - Σ POLARIS RIO per segment during transit
  - -10 for each prohibitive segment (RIO < -10)
  - +50 for successful passage (no prohibitive segment)
  - -5 per transit day above baseline
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from modules.route_scorer import ARCTIC_SEGMENTS

logger = logging.getLogger("report-service.rl.departure_env")

@dataclass
class DepartureRewardWeights:
    """출항 최적화 RL 보상 가중치."""
    prohibitive_penalty: float = -10.0   # 통행 불가 구간 페널티 (per segment)
    success_bonus: float = 50.0          # 통행 불가 구간 없이 완전 통과 보너스
    efficiency_penalty: float = -5.0     # 기준(14일) 초과 일수당 패널티


ICE_CLASS_MAP = {
    "None": 0.0, "IC": 0.15, "IB": 0.25, "IA": 0.35, "IA Super": 0.45,
    "PC7": 0.55, "PC6": 0.65, "PC5": 0.75, "PC4": 0.85, "PC3": 0.90,
    "PC2": 0.95, "PC1": 1.0,
}

NUM_SEGMENTS = 7  # NSR 기준


class DepartureSchedulingEnv(gym.Env):
    """출항 날짜 최적화 Gymnasium 환경."""

    metadata = {"render_modes": []}

    def __init__(
        self,
        monthly_ice: Optional[dict] = None,
        weather_data: Optional[dict] = None,
        route_scorer=None,
        ice_class: str = "PC5",
        forecast_days: int = 30,
        transit_days: int = 14,
        start_date: Optional[date] = None,
        difficulty: str = "medium",
        reward_weights: Optional[DepartureRewardWeights] = None,
    ):
        super().__init__()

        self.monthly_ice = monthly_ice or {}
        self.weather_data = weather_data or {}
        self.route_scorer = route_scorer
        self.ice_class = ice_class
        self.forecast_days = forecast_days
        self.transit_days = transit_days
        self.start_date = start_date or date.today()
        self.difficulty = difficulty
        self.reward_weights = reward_weights if reward_weights is not None else DepartureRewardWeights()

        # 관측 공간: 28차원 (month_sin/cos(2) + conc(7) + wave(7) + vis(7) + ice(1) + transit(1) + forecast(1) + dow_sin/cos(2) = 28)
        self.observation_space = spaces.Box(
            low=-1.0, high=1.0, shape=(28,), dtype=np.float32
        )
        # 행동 공간: 출항 날짜 오프셋 [-1, 1]
        self.action_space = spaces.Box(
            low=-1.0, high=1.0, shape=(1,), dtype=np.float32
        )

        self._current_day_offset = 0
        self._episode_reward = 0.0

    def _get_difficulty_months(self) -> list[int]:
        """난이도별 탐색 월 범위."""
        if self.difficulty == "easy":
            return [6, 7, 8, 9]
        elif self.difficulty == "medium":
            return [4, 5, 10, 11]
        else:  # hard
            return [11, 12, 1, 2, 3]

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)

        # 난이도에 따른 랜덤 시작 월 선택
        months = self._get_difficulty_months()
        month = self.np_random.choice(months)
        year = self.start_date.year
        self.start_date = date(year, month, 1)
        self._current_day_offset = 0
        self._episode_reward = 0.0

        obs = self._build_observation(0)
        return obs, {}

    def _build_observation(self, day_offset: int) -> np.ndarray:
        """상태 벡터 구성 (28차원: month_sin/cos(2) + conc(7) + wave(7) + vis(7) + ice(1) + transit(1) + forecast(1) + dow_sin/cos(2))."""
        dep_date = self.start_date + timedelta(days=day_offset)
        month = dep_date.month

        # 계절 인코딩
        month_sin = np.sin(2 * np.pi * month / 12)
        month_cos = np.cos(2 * np.pi * month / 12)

        # 구간별 농도 (7)
        segment_concs = self._get_segment_concentrations(month)

        # 구간별 파고/가시거리 (각 7) — weather에서 추출 또는 기본값
        segment_waves = np.full(NUM_SEGMENTS, 0.3)
        segment_vis = np.full(NUM_SEGMENTS, 0.8)

        nsr_weather = self.weather_data.get("routes", {}).get("NSR", {})
        if "summary" in nsr_weather:
            s = nsr_weather["summary"]
            if s.get("avg_wave_m") is not None:
                segment_waves[:] = min(s["avg_wave_m"] / 10.0, 1.0)
            if s.get("min_vis_km") is not None:
                segment_vis[:] = min(s["min_vis_km"] / 50.0, 1.0)

        # 빙급 인코딩
        ice_enc = ICE_CLASS_MAP.get(self.ice_class, 0.5)

        # 항행일수 정규화
        transit_norm = min(self.transit_days / 30.0, 1.0)

        # 현재 탐색 날짜 오프셋
        forecast_norm = day_offset / max(self.forecast_days, 1)

        # 요일 인코딩
        dow = dep_date.weekday()
        dow_sin = np.sin(2 * np.pi * dow / 7)
        dow_cos = np.cos(2 * np.pi * dow / 7)

        obs = np.array([
            month_sin, month_cos,
            *segment_concs,
            *segment_waves,
            *segment_vis,
            ice_enc,
            transit_norm,
            forecast_norm,
            dow_sin, dow_cos,
        ], dtype=np.float32)

        return obs

    def _get_segment_concentrations(self, month: int) -> np.ndarray:
        """해당 월의 NSR 7구간 평균 농도."""
        concs = np.full(NUM_SEGMENTS, 0.5)

        month_data = self.monthly_ice.get(month)
        if month_data is None or not self.route_scorer:
            return concs

        cells = month_data.get("cells", [])
        segments = ARCTIC_SEGMENTS.get("NSR", [])

        for i, seg in enumerate(segments[:NUM_SEGMENTS]):
            c = self.route_scorer._get_segment_concentration(seg, cells)
            concs[i] = c

        return concs

    def step(self, action):
        # 액션을 날짜 오프셋으로 변환
        offset = (action[0] + 1) / 2 * self.forecast_days
        day_offset = int(np.clip(offset, 0, self.forecast_days - 1))

        dep_date = self.start_date + timedelta(days=day_offset)

        # POLARIS RIO 계산
        reward = 0.0
        has_prohibitive = False

        if self.route_scorer:
            day_score = self.route_scorer.score_departure_day(
                dep_date, "NSR", self.ice_class, self.monthly_ice
            )
            for seg in day_score.segment_scores:
                reward += seg.rio
                if seg.rio < -10:
                    reward += self.reward_weights.prohibitive_penalty
                    has_prohibitive = True

            if not has_prohibitive:
                reward += self.reward_weights.success_bonus

        # 효율 패널티
        reward += max(0, self.transit_days - 14) * self.reward_weights.efficiency_penalty

        self._episode_reward += reward
        self._current_day_offset = day_offset

        obs = self._build_observation(day_offset)
        terminated = True  # 1스텝 에피소드
        truncated = False

        info = {
            "departure_date": dep_date.isoformat(),
            "day_offset": day_offset,
            "episode_reward": self._episode_reward,
            "has_prohibitive": has_prohibitive,
        }

        return obs, reward, terminated, truncated, info
