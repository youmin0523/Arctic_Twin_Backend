"""출항 스케줄링 RL 환경(DepartureSchedulingEnv)의 보상 함수 테스트.

보상 = Σ구간RIO + (통행불가 페널티) + (무사통과 보너스) + (효율 페널티) 의 조합이
의도대로 동작하는지 검증한다. route_scorer 는 스텁으로 주입해 DB/DataLoader 없이
순수 보상 로직만 본다. (gymnasium + numpy 필요, torch 불필요)
"""
import sys
from types import SimpleNamespace
import numpy as np
import pytest

# departure_env 는 modules.route_scorer 를 import → conftest 가 report-service 경로 추가
from modules.rl.departure_env import (
    DepartureSchedulingEnv,
    DepartureRewardWeights,
)


def make_scorer(segment_rios):
    """주어진 구간 RIO 리스트를 항상 반환하는 스텁 route_scorer."""
    def score_departure_day(dep_date, route, ice_class, monthly_ice):
        segs = [SimpleNamespace(rio=r) for r in segment_rios]
        return SimpleNamespace(segment_scores=segs)
    return SimpleNamespace(score_departure_day=score_departure_day)


@pytest.fixture
def base_env():
    return DepartureSchedulingEnv(ice_class="PC5", transit_days=14, difficulty="medium")


class TestSpaces:
    def test_observation_space_is_28d(self, base_env):
        assert base_env.observation_space.shape == (28,)

    def test_action_space_is_1d(self, base_env):
        assert base_env.action_space.shape == (1,)

    def test_reset_returns_valid_observation(self, base_env):
        obs, info = base_env.reset(seed=0)
        assert obs.shape == (28,)
        assert np.all(np.isfinite(obs))
        assert isinstance(info, dict)


class TestRewardFunction:
    def test_no_scorer_transit14_zero_reward(self):
        """scorer 없고 14일 이내면 효율 페널티 0 → 보상 0."""
        env = DepartureSchedulingEnv(route_scorer=None, transit_days=14)
        env.reset(seed=1)
        _, reward, terminated, _, _ = env.step(np.array([0.0], dtype=np.float32))
        assert reward == 0.0
        assert terminated is True  # 1스텝 에피소드

    def test_efficiency_penalty_applied_over_14_days(self):
        """transit_days=20 → (20-14)*-5 = -30 효율 페널티."""
        env = DepartureSchedulingEnv(route_scorer=None, transit_days=20)
        env.reset(seed=1)
        _, reward, _, _, _ = env.step(np.array([0.0], dtype=np.float32))
        assert reward == pytest.approx(6 * DepartureRewardWeights().efficiency_penalty)

    def test_all_safe_segments_grant_success_bonus(self):
        """모든 구간 RIO 양수 → 성공 보너스(50) + RIO 합."""
        env = DepartureSchedulingEnv(route_scorer=make_scorer([2, 2, 2]), transit_days=14)
        env.reset(seed=1)
        _, reward, _, _, info = env.step(np.array([0.0], dtype=np.float32))
        assert info["has_prohibitive"] is False
        assert reward == pytest.approx(6 + DepartureRewardWeights().success_bonus)

    def test_prohibitive_segment_blocks_bonus_and_penalizes(self):
        """RIO < -10 구간이 있으면 성공 보너스 없음 + 통행불가 페널티."""
        w = DepartureRewardWeights()
        env = DepartureSchedulingEnv(route_scorer=make_scorer([2, -12, 1]), transit_days=14)
        env.reset(seed=1)
        _, reward, _, _, info = env.step(np.array([0.0], dtype=np.float32))
        assert info["has_prohibitive"] is True
        # 2 + (-12) + 1 + prohibitive_penalty, 성공 보너스 없음
        assert reward == pytest.approx(2 - 12 + 1 + w.prohibitive_penalty)

    def test_safe_route_scores_higher_than_prohibitive(self):
        """안전 경로의 보상이 통행불가 경로보다 항상 높아야 한다(학습 신호 정합성)."""
        env_safe = DepartureSchedulingEnv(route_scorer=make_scorer([2, 2, 2]), transit_days=14)
        env_bad = DepartureSchedulingEnv(route_scorer=make_scorer([2, -15, 2]), transit_days=14)
        env_safe.reset(seed=2); env_bad.reset(seed=2)
        _, r_safe, *_ = env_safe.step(np.array([0.0], dtype=np.float32))
        _, r_bad, *_ = env_bad.step(np.array([0.0], dtype=np.float32))
        assert r_safe > r_bad


class TestActionMapping:
    def test_action_clipped_to_forecast_window(self):
        """행동이 범위를 벗어나도 day_offset 은 [0, forecast_days-1] 로 클립."""
        env = DepartureSchedulingEnv(route_scorer=None, forecast_days=30, transit_days=14)
        env.reset(seed=3)
        _, _, _, _, info_hi = env.step(np.array([5.0], dtype=np.float32))   # +범위 초과
        assert 0 <= info_hi["day_offset"] <= 29
        env.reset(seed=3)
        _, _, _, _, info_lo = env.step(np.array([-5.0], dtype=np.float32))  # -범위 초과
        assert info_lo["day_offset"] == 0

    def test_info_contains_departure_date(self):
        env = DepartureSchedulingEnv(route_scorer=None, transit_days=14)
        env.reset(seed=4)
        _, _, _, _, info = env.step(np.array([0.0], dtype=np.float32))
        assert "departure_date" in info and isinstance(info["departure_date"], str)
