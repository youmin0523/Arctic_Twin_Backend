"""
rl_trainer.py -- 학습 오케스트레이션

커리큘럼 학습, 배치 학습, 평가, 실시간 추론을 관리합니다.
"""
from __future__ import annotations

import math
import logging
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

try:
    from stable_baselines3.common.callbacks import BaseCallback as _BaseCallback
except ImportError:
    class _BaseCallback:
        def __init__(self, verbose=0): pass
        def _on_step(self): return True

from .rl_agent import IcebergAvoidanceAgent, _StopTraining
from .rl_environment import IcebergAvoidanceEnv, Iceberg
from .rl_reward import RewardWeights
from .rl_ship_dynamics import approx_dist_km, bearing_deg, normalize_angle, km_per_deg_lon, KM_PER_DEG_LAT
from .config import MAX_SAFE_CONCENTRATION

logger = logging.getLogger(__name__)


class _StopCallback(_BaseCallback):
    """stop_requested 플래그를 확인해 학습을 강제 종료하는 콜백.

    단순히 False를 반환하는 방식은 SB3 버전에 따라 동작하지 않을 수 있어,
    _StopTraining 예외를 발생시켜 model.learn()을 즉시 중단합니다.
    """
    def __init__(self, trainer: "RLTrainer"):
        super().__init__(verbose=0)
        self.trainer = trainer

    def _on_step(self) -> bool:
        if self.trainer.stop_requested:
            raise _StopTraining("사용자 중단 요청")
        return True


@dataclass
class CurriculumStage:
    name: str
    difficulty: str
    timesteps: int
    description: str


CURRICULUM = [
    # [수정] timesteps 대폭 증가: 에이전트가 충분히 경로 완주 경험을 쌓도록
    # easy: 빙산 0개, 짧은 구간 → 성공 경험 축적이 최우선
    CurriculumStage("stage_1_basic",    "easy",   150_000, "단일 빙산, 개방 해수, 좋은 시정 — 경로 완주 경험 축적"),
    CurriculumStage("stage_2_moderate", "medium", 200_000, "다중 빙산, 가벼운 해빙, 보통 시정"),
    CurriculumStage("stage_3_hard",     "hard",   150_000, "밀집 빙산군, 높은 해빙 농도, 낮은 시정"),
]


class RLTrainer:
    """빙산 회피 RL 학습 관리자"""

    def __init__(self, hyperparams: dict | None = None,
                 model_key: str = "default",
                 fixed_route: str | None = None,
                 fixed_ice_class: str | None = None,
                 ship_params=None):
        self.agent = IcebergAvoidanceAgent(hyperparams, model_key=model_key)
        self._fixed_route = fixed_route
        self._fixed_ice_class = fixed_ice_class
        self._ship_params = ship_params
        self.is_training = False
        self.stop_requested = False
        self.current_stage: Optional[str] = None
        self.training_log: list[dict] = []

    def _create_env(self, difficulty: str, reward_weights: RewardWeights | None = None):
        return self.agent.create_env(
            difficulty=difficulty,
            reward_weights=reward_weights,
            fixed_route=self._fixed_route,
            fixed_ice_class=self._fixed_ice_class,
            ship_params=self._ship_params,
        )

    def train_curriculum(self, stages: list[CurriculumStage] | None = None,
                         reward_weights: RewardWeights | None = None) -> dict:
        stages = stages or CURRICULUM
        self.is_training = True
        self.stop_requested = False
        results = []
        try:
            for i, stage in enumerate(stages):
                if self.stop_requested:
                    logger.info("[Trainer] 학습 중단 요청으로 커리큘럼 중단")
                    break
                self.current_stage = stage.name
                logger.info(f"[Trainer] === 커리큘럼 {i+1}/{len(stages)}: {stage.name} ===")

                try:
                    self._create_env(stage.difficulty, reward_weights)
                    if self.agent.model is None:
                        self.agent.build_model(difficulty=stage.difficulty,
                                               reward_weights=reward_weights)
                    else:
                        self.agent.model.set_env(self.agent.env)

                    start_time = time.time()
                    metrics = self.agent.train(total_timesteps=stage.timesteps, extra_callback=_StopCallback(self))
                    elapsed = time.time() - start_time

                    result = {
                        "stage": stage.name, "difficulty": stage.difficulty,
                        "timesteps": stage.timesteps, "elapsed_seconds": elapsed,
                        "metrics": metrics,
                    }
                    results.append(result)
                    self.training_log.append(result)

                    # 중단 요청이 왔으면 스테이지 루프 종료
                    if self.stop_requested:
                        break
                except Exception as e:
                    logger.error(f"[Trainer] 스테이지 {stage.name} 실패: {e}", exc_info=True)
                    if self.stop_requested:
                        break
        finally:
            self.is_training = False
            self.current_stage = None
        return {"stages": results, "total_stages": len(stages)}

    def train_single(self, difficulty: str = "medium", timesteps: int = 100_000,
                     reward_weights: RewardWeights | None = None) -> dict:
        self.is_training = True
        self.stop_requested = False
        self.current_stage = f"single_{difficulty}"
        try:
            self._create_env(difficulty, reward_weights)
            if self.agent.model is None:
                self.agent.build_model(difficulty=difficulty, reward_weights=reward_weights)
            else:
                self.agent.model.set_env(self.agent.env)

            start_time = time.time()
            metrics = self.agent.train(total_timesteps=timesteps, extra_callback=_StopCallback(self))
            elapsed = time.time() - start_time

            result = {"difficulty": difficulty, "timesteps": timesteps,
                      "elapsed_seconds": elapsed, "metrics": metrics}
            self.training_log.append(result)
            return result
        except Exception as e:
            logger.error(f"[Trainer] 단일 학습 실패: {e}", exc_info=True)
            return {"error": str(e)}
        finally:
            self.is_training = False
            self.current_stage = None

    def evaluate(self, n_episodes: int = 100, difficulty: str = "medium") -> dict:
        if self.agent.model is None:
            if not self.agent.load():
                return {"error": "모델이 없습니다. 먼저 학습을 실행하세요."}

        # 학습과 동일한 route/ice_class 환경에서 평가 (불일치 방지)
        env = IcebergAvoidanceEnv(
            difficulty=difficulty,
            fixed_route=self._fixed_route,
            fixed_ice_class=self._fixed_ice_class,
            ship_params=self._ship_params,
        )
        rewards, deviations, episode_lengths = [], [], []
        collisions, successes = 0, 0

        for _ in range(n_episodes):
            if self.stop_requested:
                break
            obs, _ = env.reset()
            total_reward, max_deviation, steps = 0, 0, 0

            while True:
                action, _ = self.agent.predict(obs, deterministic=True)
                obs, reward, terminated, truncated, info = env.step(action)
                total_reward += reward
                max_deviation = max(max_deviation, info.get("cross_track_km", 0))
                steps += 1
                if terminated or truncated:
                    if info.get("collision"): collisions += 1
                    if info.get("success"): successes += 1
                    break

            rewards.append(total_reward)
            deviations.append(max_deviation)
            episode_lengths.append(steps)

        return {
            "episodes": len(rewards),
            "difficulty": difficulty,
            "mean_reward": float(np.mean(rewards)) if rewards else 0.0,
            "collision_rate": collisions / n_episodes if n_episodes > 0 else 0.0,
            "success_rate": successes / n_episodes if n_episodes > 0 else 0.0,
            "mean_max_deviation_km": float(np.mean(deviations)) if deviations else 0.0,
            "mean_episode_length": float(np.mean(episode_lengths)) if episode_lengths else 0.0,
        }

    def infer(self, ship_state: dict, icebergs: list[dict],
              ice_data: dict, weather: dict) -> dict:
        """실시간 추론 -- 프론트엔드 API 호출용"""
        if self.agent.model is None:
            if not self.agent.load():
                return {"error": "모델이 로드되지 않았습니다.", "fallback": True}

        env = IcebergAvoidanceEnv(difficulty="medium")
        try:
            env.reset()

            obs = self._build_obs_from_real_data(ship_state, icebergs, ice_data, weather)
            action, value = self.agent.predict(obs, deterministic=True)

            # 미래 경로 예측용 환경 구성
            env.ship.lon = ship_state["lon"]
            env.ship.lat = ship_state["lat"]
            env.ship.heading = ship_state.get("heading", 0)
            env.ship.speed_knots = ship_state.get("speed_knots", 14)
            env.ice_concentration = ice_data.get("concentration", 0)
            env.visibility_km = weather.get("visibility_km", 10)
            env.icebergs = [
                Iceberg(lat=b["lat"], lon=b["lon"], length_m=b.get("length_m", 5000))
                for b in icebergs
            ]

            sequence = self.agent.predict_sequence(obs, env, n_steps=20)
            projected_path = [{"lon": s["lon"], "lat": s["lat"]} for s in sequence]
            confidence = min(1.0, max(0.0, (value + 50) / 100.0))

            return {
                "action": action.tolist(),
                "heading_delta": float(action[0]),
                "speed_factor": float(action[1]),
                "confidence": confidence,
                "value_estimate": value,
                "projected_path": projected_path,
                "fallback": confidence < 0.3,
            }
        finally:
            env.close()

    def _build_obs_from_real_data(self, ship_state: dict, icebergs: list[dict],
                                  ice_data: dict, weather: dict) -> np.ndarray:
        obs = np.zeros(22, dtype=np.float32)

        lon = ship_state["lon"]
        lat = ship_state["lat"]
        heading = ship_state.get("heading", 0)
        speed = ship_state.get("speed_knots", 14)
        ice_class = ship_state.get("ice_class", "PC5")
        progress = ship_state.get("progress", 0.5)

        obs[0] = lon / 180.0
        obs[1] = lat / 90.0
        h_rad = heading * math.pi / 180.0
        obs[2] = math.sin(h_rad)
        obs[3] = math.cos(h_rad)
        obs[4] = speed / 15.0

        next_wp = ship_state.get("next_waypoint")
        if next_wp:
            obs[5] = (next_wp["lon"] - lon) * km_per_deg_lon(lat) / 100.0
            obs[6] = (next_wp["lat"] - lat) * KM_PER_DEG_LAT / 100.0
            d = approx_dist_km(lat, lon, next_wp["lat"], next_wp["lon"])
            b = bearing_deg(lat, lon, next_wp["lat"], next_wp["lon"])
            obs[7] = normalize_angle(b - heading) / 180.0
            obs[8] = min(1.0, d / 200.0)

        berg_infos = []
        for berg in icebergs:
            d = approx_dist_km(lat, lon, berg["lat"], berg["lon"])
            b = bearing_deg(lat, lon, berg["lat"], berg["lon"])
            berg_infos.append((normalize_angle(b - heading), d, berg.get("length_m", 5000)))
        berg_infos.sort(key=lambda x: x[1])

        for i in range(3):
            if i < len(berg_infos):
                obs[9 + i * 2] = berg_infos[i][0] / 180.0
                obs[10 + i * 2] = min(1.0, berg_infos[i][1] / 50.0)
            else:
                obs[10 + i * 2] = 1.0

        conc = ice_data.get("concentration", 0)
        obs[15] = min(1.0, conc)
        obs[16] = min(1.0, conc)
        obs[17] = min(1.0, weather.get("visibility_km", 10) / 20.0)
        obs[18] = min(1.0, weather.get("wave_height_m", 1) / 8.0)
        obs[19] = MAX_SAFE_CONCENTRATION.get(ice_class, 0.7)
        obs[20] = progress
        obs[21] = ship_state.get("cross_track_km", 0) / 50.0

        return obs

    def get_status(self) -> dict:
        return {
            "is_training": self.is_training,
            "current_stage": self.current_stage,
            "agent_status": self.agent.get_training_status(),
            "training_log": self.training_log[-10:],
        }
