"""
multi_model_trainer.py — 빙급 × 선종 다중 모델 병렬 반복 학습

반복 학습 시작 버튼 하나로 정의된 모든 (ice_class, ship_type) 조합을
ThreadPoolExecutor로 동시에 학습합니다.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from modules.rl.departure_iterative_trainer import DepartureIterativeTrainer

logger = logging.getLogger("report-service.rl.multi_model_trainer")

# ── 지원 빙급 ────────────────────────────────────────────────
ICE_CLASSES: list[str] = [
    "PC7", "PC6", "PC5", "PC4", "PC3", "IA Super", "IA",
]

# ── 선종 정의 ─────────────────────────────────────────────────
#   transit_days : 북극 항로 기준 예상 항해 일수
#   label        : 한글 표시명
SHIP_TYPES: dict[str, dict] = {
    "bulk":      {"label": "벌크선",     "transit_days": 16},
    "tanker":    {"label": "탱커",       "transit_days": 14},
    "container": {"label": "컨테이너선", "transit_days": 12},
    "lng":       {"label": "LNG선",      "transit_days": 13},
}

# ── (ice_class, ship_type) 전체 조합 ─────────────────────────
ALL_COMBINATIONS: list[tuple[str, str]] = [
    (ic, st) for ic in ICE_CLASSES for st in SHIP_TYPES
]


def _model_label(ice_class: str, ship_type: str) -> str:
    return f"{ice_class} / {SHIP_TYPES[ship_type]['label']}"


# ── 개별 모델 상태 ────────────────────────────────────────────
@dataclass
class ModelStatus:
    ice_class: str
    ship_type: str
    label: str
    is_running: bool = False
    current_iteration: int = 0
    latest_metrics: Optional[dict] = None
    converged: bool = False
    error: Optional[str] = None

    def __post_init__(self):
        if self.latest_metrics is None:
            self.latest_metrics = {}

    def to_dict(self) -> dict:
        return {
            "ice_class": self.ice_class,
            "ship_type": self.ship_type,
            "label": self.label,
            "is_running": self.is_running,
            "current_iteration": self.current_iteration,
            "latest_metrics": self.latest_metrics,
            "converged": self.converged,
            "error": self.error,
        }


# ── 다중 모델 트레이너 ─────────────────────────────────────────
class MultiModelIterativeTrainer:
    """
    모든 (ice_class, ship_type) 조합을 ThreadPoolExecutor로 동시 학습.
    각 조합은 독립적인 DepartureIterativeTrainer + DepartureAgent를 사용합니다.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._statuses: dict[str, ModelStatus] = {}
        self._futures: dict[str, Future] = {}
        self._executor: Optional[ThreadPoolExecutor] = None
        self.is_running = False
        self.stop_requested = False
        # 조합별 트레이너 인스턴스 (stop 전달용)
        self._trainers: dict[str, "DepartureIterativeTrainer"] = {}

    # ── 학습 시작 ─────────────────────────────────────────────
    def start(self,
              monthly_ice: dict,
              weather_data: dict,
              route_scorer,
              base_timesteps: int = 100_000,
              max_iterations: int = 10,
              target_success_rate: float = 0.80,
              target_prohibitive_rate: float = 0.10,
              eval_episodes: int = 50,
              forecast_days: int = 30) -> None:
        """ALL_COMBINATIONS 전체를 동시에 학습 시작."""
        if self.is_running:
            raise RuntimeError("이미 다중 모델 학습이 진행 중입니다.")

        self.stop_requested = False
        self.is_running = True

        with self._lock:
            self._statuses.clear()
            self._futures.clear()
            self._trainers.clear()
            for ic, st in ALL_COMBINATIONS:
                key = _combo_key(ic, st)
                self._statuses[key] = ModelStatus(
                    ice_class=ic, ship_type=st, label=_model_label(ic, st)
                )

        import os
        # rl-pipeline과 코어 공유: 최소 2, 최대 2 (28개 조합, 부하 분산)
        cpu_workers = max(2, min(2, (os.cpu_count() or 8) // 10))
        n = min(cpu_workers, len(ALL_COMBINATIONS))
        self._executor = ThreadPoolExecutor(max_workers=n, thread_name_prefix="rl_train")
        logger.info("[MultiModel] 학습 시작: %d개 조합, 동시 실행: %d개", len(ALL_COMBINATIONS), n)

        for ic, st in ALL_COMBINATIONS:
            key = _combo_key(ic, st)
            future = self._executor.submit(
                self._train_one,
                ice_class=ic,
                ship_type=st,
                monthly_ice=monthly_ice,
                weather_data=weather_data,
                route_scorer=route_scorer,
                base_timesteps=base_timesteps,
                max_iterations=max_iterations,
                target_success_rate=target_success_rate,
                target_prohibitive_rate=target_prohibitive_rate,
                eval_episodes=eval_episodes,
                forecast_days=forecast_days,
            )
            with self._lock:
                self._futures[key] = future

        # 모든 작업 완료 감시 스레드
        threading.Thread(target=self._watch_all, daemon=True).start()

    # ── 개별 조합 학습 ────────────────────────────────────────
    def _train_one(self, ice_class: str, ship_type: str,
                   monthly_ice, weather_data, route_scorer,
                   base_timesteps, max_iterations,
                   target_success_rate, target_prohibitive_rate,
                   eval_episodes, forecast_days) -> None:
        from modules.rl.departure_trainer import DepartureTrainer
        from modules.rl.departure_iterative_trainer import DepartureIterativeTrainer
        from modules.rl.departure_env import DepartureRewardWeights

        import json as _json
        from pathlib import Path as _Path
        key = _combo_key(ice_class, ship_type)
        transit_days = SHIP_TYPES[ship_type]["transit_days"]

        # 수렴 완료된 조합만 스킵 (미수렴이면 재학습)
        _data_dir = _Path(__file__).resolve().parents[2] / "data"
        _hist_key = key.replace(" ", "_").replace("/", "_")
        history_path = _data_dir / f"departure_iterative_history_{_hist_key}.json"
        if history_path.exists():
            try:
                existing = _json.loads(history_path.read_text(encoding="utf-8"))
                already_converged = existing and existing[-1].get("converged", False)
                if already_converged:
                    logger.info("[MultiModel] 스킵 (수렴 완료): %s (%d회)", key, len(existing))
                    with self._lock:
                        st = self._statuses.get(key)
                        if st:
                            st.current_iteration = len(existing)
                            st.converged = True
                            st.is_running = False
                    return
                # 미수렴이면 히스토리 초기화 후 재학습
                logger.info("[MultiModel] 미수렴 재학습 (이전 %d회): %s", len(existing), key)
                history_path.unlink(missing_ok=True)
            except Exception:
                pass

        with self._lock:
            self._statuses[key].is_running = True

        logger.info("[MultiModel] 시작: %s", key)

        base_trainer = DepartureTrainer()
        iterative_trainer = DepartureIterativeTrainer(
            departure_trainer=base_trainer,
            ice_class=ice_class,
            ship_type=ship_type,
        )

        with self._lock:
            self._trainers[key] = iterative_trainer

        # 진행 상황을 status에 반영하는 콜백 주입
        original_save = iterative_trainer._save_history

        def _patched_save():
            original_save()
            with self._lock:
                st = self._statuses.get(key)
                if st and iterative_trainer.history:
                    last = iterative_trainer.history[-1]
                    st.current_iteration = last.iteration
                    st.latest_metrics = last.post_metrics
                    st.converged = last.converged

        iterative_trainer._save_history = _patched_save

        try:
            iterative_trainer.run(
                monthly_ice=monthly_ice,
                weather_data=weather_data,
                route_scorer=route_scorer,
                ice_class=ice_class,
                forecast_days=forecast_days,
                transit_days=transit_days,
                base_timesteps=base_timesteps,
                max_iterations=max_iterations,
                target_success_rate=target_success_rate,
                target_prohibitive_rate=target_prohibitive_rate,
                eval_episodes=eval_episodes,
            )
        except Exception as e:
            logger.error("[MultiModel] 오류 (%s): %s", key, e, exc_info=True)
            with self._lock:
                if key in self._statuses:
                    self._statuses[key].error = str(e)
        finally:
            with self._lock:
                if key in self._statuses:
                    self._statuses[key].is_running = False
            logger.info("[MultiModel] 완료: %s", key)

    # ── 전체 완료 감시 ────────────────────────────────────────
    def _watch_all(self):
        if self._executor:
            self._executor.shutdown(wait=True)
        self.is_running = False
        logger.info("[MultiModel] 전체 학습 완료")

    # ── 중단 ─────────────────────────────────────────────────
    def stop(self) -> None:
        self.stop_requested = True
        with self._lock:
            for trainer in self._trainers.values():
                try:
                    trainer.stop()
                except Exception:
                    pass
        logger.info("[MultiModel] 중단 요청 전파 완료")

    # ── 상태 조회 ─────────────────────────────────────────────
    def get_status(self) -> dict:
        with self._lock:
            models = {k: v.to_dict() for k, v in self._statuses.items()}
            running_count = sum(1 for v in self._statuses.values() if v.is_running)
            converged_count = sum(1 for v in self._statuses.values() if v.converged)

        return {
            "is_running": self.is_running,
            "total_models": len(ALL_COMBINATIONS),
            "running_models": running_count,
            "converged_models": converged_count,
            "models": models,
        }


def _combo_key(ice_class: str, ship_type: str) -> str:
    return f"{ice_class}_{ship_type}".replace(" ", "_")
