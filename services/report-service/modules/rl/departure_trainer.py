"""
departure_trainer.py — (A) 커리큘럼 학습 오케스트레이터
=====================================================
3단계 커리큘럼으로 출항 스케줄링 RL을 학습한다.

| 단계   | 조건              | 스텝  |
|--------|-------------------|-------|
| Easy   | 하절기(6-9월)     | 50K   |
| Medium | 춘추(4,5,10,11월) | 100K  |
| Hard   | 동절기(11-3월)    | 100K  |
"""

import logging
import time
from datetime import date

from stable_baselines3.common.callbacks import BaseCallback
from modules.rl.departure_agent import _StopTraining

logger = logging.getLogger("report-service.rl.departure_trainer")

CURRICULUM_DEFAULTS = [
    {"difficulty": "easy", "timesteps": 50_000},
    {"difficulty": "medium", "timesteps": 100_000},
    {"difficulty": "hard", "timesteps": 100_000},
]


class _ProgressCallback(BaseCallback):
    """학습 중 실시간 진행률을 trainer에 반영하는 콜백."""

    def __init__(self, trainer: "DepartureTrainer", stage_start: int):
        super().__init__(verbose=0)
        self.trainer = trainer
        self.stage_start = stage_start

    def _on_step(self) -> bool:
        done = self.stage_start + self.num_timesteps
        self.trainer.total_timesteps_done = done
        if self.trainer.total_timesteps_target > 0:
            self.trainer.progress = int(done / self.trainer.total_timesteps_target * 100)
        if self.trainer.stop_requested:
            raise _StopTraining("사용자 중단 요청")
        return True


class DepartureTrainer:
    """출항 RL 커리큘럼 학습 관리자."""

    def __init__(self):
        self.is_training = False
        self.stop_requested = False
        self.current_stage = ""
        self.progress = 0  # 0~100
        self.total_timesteps_done = 0
        self.total_timesteps_target = 0
        self.training_history = []
        self.start_time = None

    def train_curriculum(
        self,
        monthly_ice: dict,
        weather_data: dict,
        route_scorer,
        ice_class: str = "PC5",
        forecast_days: int = 30,
        transit_days: int = 14,
        base_timesteps: int = 100_000,
        reward_weights=None,
    ):
        """3단계 커리큘럼 학습 실행."""
        from modules.rl.departure_env import DepartureSchedulingEnv, DepartureRewardWeights
        from modules.rl.departure_agent import DepartureAgent

        # 동적 타겟 설정
        stages = [
            {"difficulty": "easy", "timesteps": int(base_timesteps * 0.5)},
            {"difficulty": "medium", "timesteps": base_timesteps},
            {"difficulty": "hard", "timesteps": base_timesteps},
        ]
        self.total_timesteps_target = sum(s["timesteps"] for s in stages)
        self.is_training = True
        self.stop_requested = False
        self.progress = 0
        self.total_timesteps_done = 0
        self.start_time = time.time()

        agent = DepartureAgent()

        try:
            for i, stage in enumerate(stages):
                if self.stop_requested:
                    logger.info("학습 중단 요청으로 커리큘럼 중단")
                    break
                self.current_stage = f"{stage['difficulty']} ({i+1}/{len(stages)})"
                logger.info("커리큘럼 단계 시작: %s", self.current_stage)

                env = DepartureSchedulingEnv(
                    monthly_ice=monthly_ice,
                    weather_data=weather_data,
                    route_scorer=route_scorer,
                    ice_class=ice_class,
                    forecast_days=forecast_days,
                    transit_days=transit_days,
                    start_date=date.today(),
                    difficulty=stage["difficulty"],
                    reward_weights=reward_weights,
                )

                try:
                    stage_start = self.total_timesteps_done
                    cb = _ProgressCallback(self, stage_start)
                    agent.train(env, timesteps=stage["timesteps"], callback=cb)
                    self.total_timesteps_done = stage_start + stage["timesteps"]
                    self.progress = int(
                        self.total_timesteps_done / self.total_timesteps_target * 100
                    )
                    self.training_history.append({
                        "stage": stage["difficulty"],
                        "timesteps": stage["timesteps"],
                        "completed": True,
                    })
                except Exception as e:
                    logger.error("학습 실패 (단계 %s): %s", stage["difficulty"], e, exc_info=True)
                    self.training_history.append({
                        "stage": stage["difficulty"],
                        "timesteps": stage["timesteps"],
                        "completed": False,
                        "error": str(e),
                    })
                finally:
                    env.close()

                if self.stop_requested:
                    break
        finally:
            self.is_training = False
            self.progress = 100
            elapsed = time.time() - self.start_time
            logger.info("커리큘럼 학습 완료 (%.1f초)", elapsed)

    def train_single(
        self,
        difficulty: str,
        timesteps: int,
        monthly_ice: dict,
        weather_data: dict,
        route_scorer,
        ice_class: str = "PC5",
        reward_weights=None,
    ):
        """단일 난이도 학습."""
        from modules.rl.departure_env import DepartureSchedulingEnv
        from modules.rl.departure_agent import DepartureAgent

        self.is_training = True
        self.current_stage = difficulty
        self.progress = 0
        self.total_timesteps_done = 0
        self.total_timesteps_target = timesteps
        self.start_time = time.time()

        env = DepartureSchedulingEnv(
            monthly_ice=monthly_ice,
            weather_data=weather_data,
            route_scorer=route_scorer,
            ice_class=ice_class,
            difficulty=difficulty,
            reward_weights=reward_weights,
        )

        try:
            agent = DepartureAgent()
            cb = _ProgressCallback(self, 0)
            agent.train(env, timesteps=timesteps, callback=cb)
            self.progress = 100
        except Exception as e:
            logger.error("단일 학습 실패: %s", e, exc_info=True)
        finally:
            env.close()
            self.is_training = False
            elapsed = time.time() - self.start_time if self.start_time else 0
            logger.info("단일 학습 종료 (%.1f초)", elapsed)

    def get_status(self) -> dict:
        """학습 상태 조회."""
        elapsed = time.time() - self.start_time if self.start_time else 0
        return {
            "is_training": self.is_training,
            "current_stage": self.current_stage,
            "progress": self.progress,
            "total_timesteps_done": self.total_timesteps_done,
            "total_timesteps_target": self.total_timesteps_target,
            "elapsed_seconds": round(elapsed, 1),
            "history": self.training_history,
        }
