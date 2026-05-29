"""
rl_agent.py -- SAC 에이전트 설정 및 추론

stable-baselines3의 SAC 알고리즘을 래핑하여
빙산 회피 모델의 학습/추론/저장/로드를 관리합니다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np

try:
    from stable_baselines3 import SAC
    from stable_baselines3.common.callbacks import BaseCallback
    HAS_SB3 = True
except ImportError:
    HAS_SB3 = False
    SAC = None

    # SB3 미설치 시 더미 BaseCallback (서버 기동용)
    class BaseCallback:
        def __init__(self, verbose=0): pass
        def _on_step(self): return True

from gymnasium.wrappers import RecordEpisodeStatistics
from .rl_environment import IcebergAvoidanceEnv
from .rl_reward import RewardWeights


class _StopTraining(Exception):
    """중단 요청 시 model.learn()을 강제 종료하는 sentinel 예외."""
    pass

logger = logging.getLogger(__name__)

# 중앙 모델 폴더 우선 (backend/model/avoidance-route/models), 없으면 로컬 폴백.
# 두 가지 레이아웃 모두 지원:
#   - 로컬: Digital_twin/backend/services/rl-pipeline/modules/rl_agent.py
#           → parents[3] = Digital_twin/
#   - HF Space: <root>/rl-pipeline/modules/rl_agent.py
#           → parents[2] = <root>
_CENTRAL_CANDIDATES = [
    Path(__file__).resolve().parents[3] / "model" / "avoidance-route" / "models",            # backend/services/rl-pipeline/modules → backend/model
    Path(__file__).resolve().parents[3] / "backend" / "model" / "avoidance-route" / "models",
    Path(__file__).resolve().parents[2] / "backend" / "model" / "avoidance-route" / "models",
]
_CENTRAL_BASE = next((p for p in _CENTRAL_CANDIDATES if p.exists()), _CENTRAL_CANDIDATES[0])
_LOCAL_BASE = Path(__file__).resolve().parent.parent / "models"
_BASE_MODEL_DIR = _CENTRAL_BASE if _CENTRAL_BASE.exists() else _LOCAL_BASE

# 기본 모델: 중앙 폴더면 NSR_normal, 로컬이면 sac_iceberg (구 동작 유지)
_DEFAULT_NAME = "NSR_normal" if _CENTRAL_BASE.exists() else "sac_iceberg"
MODEL_DIR = _BASE_MODEL_DIR / _DEFAULT_NAME
MODEL_DIR.mkdir(parents=True, exist_ok=True)


def _model_dir_for(model_key: str) -> Path:
    """model_key별 전용 디렉토리 반환.

    중앙 폴더 사용 시: model_key 가 "<route>_<difficulty>" (예: NSR_normal) 그대로 폴더명.
    로컬 폴더 사용 시: 기존 "sac_<key>" 형식 유지.
    """
    if model_key == "default":
        return MODEL_DIR
    if _BASE_MODEL_DIR == _CENTRAL_BASE:
        d = _BASE_MODEL_DIR / model_key
    else:
        d = _BASE_MODEL_DIR / f"sac_{model_key}"
    d.mkdir(parents=True, exist_ok=True)
    return d

DEFAULT_HYPERPARAMS = {
    "learning_rate": 3e-4,        # [수정] 1e-4 → 3e-4: 학습률 복원 (수렴 속도 개선)
    "buffer_size": 500_000,       # [수정] 1M → 500K: 메모리 절약 + 최근 경험 비중 증가
    "batch_size": 256,            # [수정] 512 → 256: 더 잦은 업데이트
    "gamma": 0.99,                # [수정] 0.995 → 0.99: 단기 보상(progress) 중시
    "tau": 0.005,
    "ent_coef": "auto",
    "train_freq": 1,
    "gradient_steps": 1,
    "learning_starts": 5_000,     # [수정] 20K → 5K: 빠른 학습 시작 (easy stage에서 성공 경험 축적)
    "policy_kwargs": {
        "net_arch": [256, 256],   # [수정] [400,300] → [256,256]: 표준 크기 (overfitting 방지)
    },
}


class TrainingMetricsCallback(BaseCallback):
    """학습 중 메트릭 수집 콜백"""

    def __init__(self, log_interval: int = 1000, verbose: int = 0):
        super().__init__(verbose)
        self.log_interval = log_interval
        self.episode_rewards = []
        self.episode_lengths = []
        self.collision_count = 0
        self.success_count = 0
        self.total_episodes = 0
        self.metrics_history = []

    _MAX_EPISODE_HISTORY = 2000  # 메모리 누수 방지

    def _on_step(self) -> bool:
        infos = self.locals.get("infos", [])
        for info in infos:
            if "episode" in info:
                self.total_episodes += 1
                self.episode_rewards.append(info["episode"]["r"])
                self.episode_lengths.append(info["episode"]["l"])
                # 리스트 무한 증가 방지 (72시간 학습 대비)
                if len(self.episode_rewards) > self._MAX_EPISODE_HISTORY:
                    self.episode_rewards = self.episode_rewards[-self._MAX_EPISODE_HISTORY:]
                    self.episode_lengths = self.episode_lengths[-self._MAX_EPISODE_HISTORY:]
            if info.get("collision", False):
                self.collision_count += 1
            if info.get("success", False):
                self.success_count += 1

        if self.num_timesteps % self.log_interval == 0 and self.total_episodes > 0:
            recent_rewards = self.episode_rewards[-100:]
            metrics = {
                "timestep": self.num_timesteps,
                "episodes": self.total_episodes,
                "mean_reward_100": float(np.mean(recent_rewards)) if recent_rewards else 0,
                "collision_rate": self.collision_count / max(1, self.total_episodes),
                "success_rate": self.success_count / max(1, self.total_episodes),
            }
            self.metrics_history.append(metrics)
            logger.info(
                f"[RL] Step {self.num_timesteps}: "
                f"reward={metrics['mean_reward_100']:.2f}, "
                f"collision={metrics['collision_rate']:.3f}, "
                f"success={metrics['success_rate']:.3f}"
            )
        return True

    def get_latest_metrics(self) -> dict:
        if self.metrics_history:
            return self.metrics_history[-1]
        return {"timestep": 0, "episodes": 0, "mean_reward_100": 0,
                "collision_rate": 0, "success_rate": 0}


class IcebergAvoidanceAgent:
    """빙산 회피 SAC 에이전트

    model_key를 지정하면 경로(NSR/NWP/TSR), 빙급, 선종별로
    독립된 모델 디렉토리에 저장/로드합니다.
    """

    def __init__(self, hyperparams: dict | None = None, model_key: str = "default"):
        self.hyperparams = {**DEFAULT_HYPERPARAMS, **(hyperparams or {})}
        self.model_key = model_key
        self.model_dir = _model_dir_for(model_key)
        self.model: Optional[SAC] = None
        self.env: Optional[IcebergAvoidanceEnv] = None
        self.callback = TrainingMetricsCallback()
        self._model_version = self._detect_version()

    def _detect_version(self) -> int:
        """기존 저장된 모델에서 최대 버전 번호 감지 (재시작 시 덮어쓰기 방지)."""
        versions = sorted(self.model_dir.glob("sac_v*.zip"))
        if not versions:
            return 0
        try:
            return max(int(p.stem.split("_v")[1]) for p in versions)
        except (ValueError, IndexError):
            return len(versions)

    def create_env(self, difficulty: str = "medium",
                   reward_weights: RewardWeights | None = None,
                   fixed_route: str | None = None,
                   fixed_ice_class: str | None = None,
                   ship_params=None):
        raw_env = IcebergAvoidanceEnv(
            difficulty=difficulty,
            reward_weights=reward_weights,
            fixed_route=fixed_route,
            fixed_ice_class=fixed_ice_class,
            ship_params=ship_params,
        )
        self.env = RecordEpisodeStatistics(raw_env)
        return self.env

    def build_model(self, difficulty: str = "medium",
                    reward_weights: RewardWeights | None = None):
        if not HAS_SB3:
            raise ImportError(
                "stable-baselines3가 설치되지 않았습니다. "
                "pip install stable-baselines3[extra] torch 를 실행하세요."
            )
        if self.env is None:
            self.create_env(difficulty, reward_weights=reward_weights)

        self.model = SAC(
            "MlpPolicy",
            self.env,
            learning_rate=self.hyperparams["learning_rate"],
            buffer_size=self.hyperparams["buffer_size"],
            batch_size=self.hyperparams["batch_size"],
            gamma=self.hyperparams["gamma"],
            tau=self.hyperparams["tau"],
            ent_coef=self.hyperparams["ent_coef"],
            train_freq=self.hyperparams["train_freq"],
            gradient_steps=self.hyperparams["gradient_steps"],
            learning_starts=self.hyperparams["learning_starts"],
            policy_kwargs=self.hyperparams["policy_kwargs"],
            verbose=1,
        )
        logger.info("[RL] SAC 모델 생성 완료")
        return self.model

    def train(self, total_timesteps: int = 500_000, extra_callback=None) -> dict:
        if self.model is None:
            self.build_model()

        self.callback = TrainingMetricsCallback(log_interval=5000)
        logger.info(f"[RL] 학습 시작: {total_timesteps} 스텝")

        from stable_baselines3.common.callbacks import CallbackList, CheckpointCallback

        # 10,000 스텝마다 체크포인트 저장 — 서버 강제 종료 시에도 복구 가능
        checkpoint_cb = CheckpointCallback(
            save_freq=10_000,
            save_path=str(self.model_dir / "checkpoints"),
            name_prefix="sac_ckpt",
            verbose=0,
        )

        cb_list = [self.callback, checkpoint_cb]
        if extra_callback:
            cb_list.append(extra_callback)
        callbacks = CallbackList(cb_list)

        try:
            self.model.learn(
                total_timesteps=total_timesteps,
                callback=callbacks,
                progress_bar=False,
            )
        except _StopTraining:
            # 중단 요청 — 정상 종료로 처리 (finally에서 저장)
            logger.info("[RL] 중단 요청으로 학습 종료")
        finally:
            # 정상 완료, 사용자 중단, 예외, 서버 종료 모든 케이스에서 저장
            self._model_version += 1
            self.save()
            self._cleanup_checkpoints(keep=3)
            logger.info(f"[RL] 모델 자동 저장 완료 (v{self._model_version})")

        metrics = self.callback.get_latest_metrics()
        logger.info(f"[RL] 학습 완료: {metrics}")
        return metrics

    def predict(self, obs: np.ndarray, deterministic: bool = True) -> tuple[np.ndarray, float]:
        if self.model is None:
            raise RuntimeError("모델이 로드되지 않았습니다.")

        action, _states = self.model.predict(obs, deterministic=deterministic)

        try:
            import torch
            obs_tensor = self.model.policy.obs_to_tensor(obs.reshape(1, -1))[0]
            with torch.no_grad():
                q_values = self.model.critic(
                    obs_tensor,
                    torch.tensor(action.reshape(1, -1), dtype=torch.float32),
                )
                value_estimate = float(min(q.item() for q in q_values))
        except Exception:
            value_estimate = 0.0

        return action, value_estimate

    def predict_sequence(
        self,
        initial_obs: np.ndarray,
        env: IcebergAvoidanceEnv,
        n_steps: int = 20,
        deterministic: bool = True,
    ) -> list[dict]:
        if self.model is None:
            raise RuntimeError("모델이 로드되지 않았습니다.")

        sequence = []
        obs = initial_obs.copy()

        for _ in range(n_steps):
            action, value = self.predict(obs, deterministic=deterministic)
            obs, reward, terminated, truncated, info = env.step(action)

            state = env.get_ship_state()
            sequence.append({
                "lon": state["lon"],
                "lat": state["lat"],
                "heading": state["heading"],
                "speed_knots": state["speed_knots"],
                "action": action.tolist(),
                "value": value,
                "reward": float(reward),
            })

            if terminated or truncated:
                break

        return sequence

    def _cleanup_checkpoints(self, keep: int = 3):
        """오래된 체크포인트 삭제 — 최근 keep개만 유지 (디스크 절약)."""
        ckpt_dir = self.model_dir / "checkpoints"
        if not ckpt_dir.exists():
            return
        ckpts = sorted(ckpt_dir.glob("sac_ckpt_*.zip"))
        for old in ckpts[:-keep]:
            try:
                old.unlink()
                meta = old.with_name(old.stem + "_meta.json")
                if meta.exists():
                    meta.unlink()
            except Exception:
                pass

    def save(self, path: str | None = None):
        if self.model is None:
            return
        save_path = path or str(self.model_dir / f"sac_v{self._model_version}")
        self.model.save(save_path)

        meta = {
            "version": self._model_version,
            "model_key": self.model_key,
            "hyperparams": {k: str(v) for k, v in self.hyperparams.items()},
            "metrics": self.callback.get_latest_metrics(),
        }
        with open(f"{save_path}_meta.json", "w") as f:
            json.dump(meta, f, indent=2)

        logger.info(f"[RL] 모델 저장: {save_path}")

    def load(self, path: str | None = None) -> bool:
        if path:
            load_path = path
        else:
            # 우선순위: sac_v*.zip (versioned) > final.zip > latest.zip
            versions = sorted(self.model_dir.glob("sac_v*.zip"))
            if versions:
                load_path = str(versions[-1]).replace(".zip", "")
            elif (self.model_dir / "final.zip").exists():
                load_path = str(self.model_dir / "final")
            elif (self.model_dir / "latest.zip").exists():
                load_path = str(self.model_dir / "latest")
            else:
                logger.warning(f"[RL] 저장된 모델 없음: {self.model_dir}")
                return False

        try:
            if self.env is None:
                self.create_env()
            self.model = SAC.load(load_path, env=self.env)
            logger.info(f"[RL] 모델 로드: {load_path}")
            return True
        except Exception as e:
            logger.error(f"[RL] 모델 로드 실패: {e}")
            return False

    def get_training_status(self) -> dict:
        return {
            "model_loaded": self.model is not None,
            "version": self._model_version,
            "metrics": self.callback.get_latest_metrics(),
            "metrics_history": self.callback.metrics_history[-50:],
        }
