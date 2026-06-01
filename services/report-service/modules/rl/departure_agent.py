"""
departure_agent.py — (A) SAC 에이전트 래퍼
==========================================
Stable-Baselines3 SAC를 사용한 출항 스케줄링 에이전트.

로드 우선순위:
  1) 중앙 ONNX 폴더 (Digital_twin/model/report-service/*.onnx) — 학습 완료 모델
  2) 로컬 zip (data/departure_rl_model/*.zip) — 진행 중 학습/체크포인트
"""

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("report-service.rl.departure_agent")

MODEL_DIR = Path(__file__).resolve().parents[1] / ".." / "data" / "departure_rl_model"
# 중앙 ONNX 모델 폴더 (학습 완료된 29개 모델). 두 가지 레이아웃 지원:
#   - 로컬: Digital_twin/backend/services/report-service/modules/rl/departure_agent.py
#           → parents[4] = Digital_twin/
#   - HF Space: <root>/report-service/modules/rl/departure_agent.py
#           → parents[3] = <root>
_ONNX_CANDIDATES = [
    Path(__file__).resolve().parents[4] / "model" / "report-service",            # backend/services/report-service/modules/rl → backend/model
    Path(__file__).resolve().parents[4] / "backend" / "model" / "report-service",
    Path(__file__).resolve().parents[3] / "backend" / "model" / "report-service",
]
CENTRAL_ONNX_DIR = next((p for p in _ONNX_CANDIDATES if p.exists()), _ONNX_CANDIDATES[0])


class _StopTraining(Exception):
    """중단 요청 시 model.learn()을 강제 종료하는 sentinel 예외."""
    pass


def _make_model_key(ice_class: str, ship_type: str) -> str:
    """(ice_class, ship_type) → 파일명용 키 (공백·특수문자 제거)."""
    return f"{ice_class}_{ship_type}".replace(" ", "_").replace("/", "_")


class DepartureAgent:
    """출항 스케줄링 SAC 에이전트.

    ice_class / ship_type 조합마다 별도 모델 파일을 사용합니다.
    기본값(ice_class="PC5", ship_type="default")은 기존 경로와 호환됩니다.
    """

    def __init__(self, ice_class: str = "PC5", ship_type: str = "default"):
        self.ice_class = ice_class
        self.ship_type = ship_type
        self.model_key = _make_model_key(ice_class, ship_type)

        # 기존 단일 모델과 호환: default 키는 departure_sac 그대로 사용
        if ship_type == "default" and ice_class == "PC5":
            self.model_path = MODEL_DIR / "departure_sac"
            self._onnx_filename = "departure_sac.onnx"
        else:
            self.model_path = MODEL_DIR / f"departure_sac_{self.model_key}"
            self._onnx_filename = f"departure_sac_{self.model_key}.onnx"

        self.model = None              # SB3 SAC 모델 (zip 로드 시)
        self._onnx_session = None      # onnxruntime InferenceSession (onnx 로드 시)
        self._onnx_input_name = None
        self.is_trained = False
        self._model_version = self._detect_version()
        self._try_load()

    def _detect_version(self) -> int:
        """이 모델 키에 해당하는 버전 파일에서 최대 버전 번호 감지."""
        prefix = self.model_path.stem  # e.g. departure_sac_PC5_bulk
        versions = sorted(MODEL_DIR.glob(f"{prefix}_v*.zip"))
        if not versions:
            return 0
        try:
            return max(int(p.stem.split("_v")[-1]) for p in versions)
        except (ValueError, IndexError):
            return len(versions)

    def _cleanup_checkpoints(self, keep: int = 3):
        """오래된 체크포인트 삭제 — 최근 keep개만 유지."""
        ckpt_dir = MODEL_DIR / "checkpoints"
        if not ckpt_dir.exists():
            return
        ckpts = sorted(ckpt_dir.glob("departure_ckpt_*.zip"))
        for old in ckpts[:-keep]:
            try:
                old.unlink()
            except Exception:
                pass

    def _try_load(self):
        """기존 학습 모델 로드 시도 — ONNX(중앙) 우선, 실패 시 zip(로컬) 폴백."""
        onnx_path = CENTRAL_ONNX_DIR / self._onnx_filename
        if onnx_path.exists():
            try:
                import onnxruntime as ort
                self._onnx_session = ort.InferenceSession(
                    str(onnx_path),
                    providers=["CPUExecutionProvider"],
                )
                self._onnx_input_name = self._onnx_session.get_inputs()[0].name
                self.is_trained = True
                logger.info("출항 RL ONNX 모델 로드: %s", onnx_path)
                return
            except Exception as e:
                logger.warning("ONNX 로드 실패 (%s), zip 폴백 시도: %s", onnx_path, e)

        zip_path = self.model_path.with_suffix(".zip")
        if zip_path.exists():
            try:
                from stable_baselines3 import SAC
                self.model = SAC.load(str(self.model_path))
                self.is_trained = True
                logger.info("출항 RL zip 모델 로드: %s", zip_path)
            except Exception as e:
                logger.warning("출항 RL zip 모델 로드 실패: %s", e)

    def create_model(self, env):
        """새 SAC 모델 생성."""
        from stable_baselines3 import SAC
        self.model = SAC(
            "MlpPolicy",
            env,
            learning_rate=3e-4,
            buffer_size=50000,
            batch_size=256,
            gamma=0.99,
            tau=0.005,
            verbose=0,
        )
        return self.model

    def train(self, env, timesteps: int = 100_000, callback=None):
        """학습 실행."""
        if self.model is None:
            self.create_model(env)
        else:
            self.model.set_env(env)

        try:
            from stable_baselines3.common.callbacks import CheckpointCallback, CallbackList
            checkpoint_cb = CheckpointCallback(
                save_freq=5_000,
                save_path=str(MODEL_DIR / "checkpoints"),
                name_prefix="departure_ckpt",
                verbose=0,
            )
            cb = CallbackList([checkpoint_cb, callback]) if callback else checkpoint_cb
            assert self.model is not None  # train()에서 create_model로 보장됨
            self.model.learn(total_timesteps=timesteps, callback=cb)
        except _StopTraining:
            logger.info("출항 RL 중단 요청으로 학습 종료")
        finally:
            # 정상 완료, 중단, 서버 종료 모든 케이스에서 저장
            self.save()
            self.is_trained = True
            self._cleanup_checkpoints(keep=3)
            logger.info("출항 RL 모델 자동 저장 완료")

    def save(self):
        """모델 저장."""
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        assert self.model is not None  # 학습 후에만 호출됨
        self.model.save(str(self.model_path))
        logger.info("출항 RL 모델 저장: %s", self.model_path)

    def predict(self, obs):
        """추론. ONNX 세션이 있으면 onnxruntime, 없으면 SB3 SAC 사용."""
        if self._onnx_session is not None:
            arr = np.asarray(obs, dtype=np.float32)
            if arr.ndim == 1:
                arr = arr[None, :]  # (28,) -> (1, 28)
            out = self._onnx_session.run(None, {self._onnx_input_name: arr})
            action = out[0][0]  # type: ignore[index]  # (1, 1) -> (1,)
            return action, None
        if self.model is None:
            return None, None
        action, state = self.model.predict(obs, deterministic=True)
        return action, state

    def get_metadata(self) -> dict:
        """모델 메타데이터."""
        zip_path = self.model_path.with_suffix(".zip")
        onnx_path = CENTRAL_ONNX_DIR / self._onnx_filename
        if self._onnx_session is not None:
            active_path = str(onnx_path)
            fmt = "onnx"
        elif self.model is not None:
            active_path = str(zip_path)
            fmt = "zip"
        else:
            active_path = ""
            fmt = "none"
        return {
            "model_exists": zip_path.exists() or onnx_path.exists(),
            "model_path": active_path,
            "model_format": fmt,
            "is_trained": self.is_trained,
            "ice_class": self.ice_class,
            "ship_type": self.ship_type,
            "model_key": self.model_key,
        }
