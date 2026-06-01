"""
rl_iterative_trainer.py -- 자동화 반복 학습 파이프라인

학습 완료 후 성능 분석 → 보상 가중치 자동 조정 → 재학습을 반복해
성공률과 충돌 회피율이 목표치에 도달할 때까지 모델을 개선합니다.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, cast

from .rl_reward import RewardWeights
from .rl_trainer import RLTrainer

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
HISTORY_PATH = MODEL_DIR / "iterative_history.json"


def _history_path_for(route: str, ice_class: str, ship_type: str) -> Path:
    key = f"{route}_{ice_class}_{ship_type}".replace(" ", "_")
    return MODEL_DIR / f"iterative_history_{key}.json"

# ── 보상 가중치 Clamping 범위 ─────────────────────────────
WEIGHT_BOUNDS: dict[str, tuple[float, float]] = {
    "collision":         (-1500.0, -100.0),  # 하한 확장: 충돌 패널티 최대 강화 허용
    "proximity":         (-30.0,   -1.0),    # 하한 확장: 근접 패널티 강화 허용
    "danger_zone":       (-50.0,   -5.0),    # 신규: 위험 구역 경보 범위
    "route_deviation":   (-2.0,    -0.05),
    "progress":          (0.5,     5.0),     # 상한 확장: 전진 보상 강화 허용
    "smoothness":        (-0.5,    -0.01),
    "fuel":              (-0.2,    -0.01),
    "ice_concentration": (-5.0,    -0.1),
    "episode_success":   (100.0,   1000.0),  # 상한 대폭 확장: 성공 보너스 최대 강화
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _apply_bounds(w: RewardWeights) -> RewardWeights:
    d = dataclasses.asdict(w)
    for key, (lo, hi) in WEIGHT_BOUNDS.items():
        if key in d:
            d[key] = _clamp(d[key], lo, hi)
    return RewardWeights(**d)


# ── 분석 및 조정 클래스 ────────────────────────────────────
class RewardAdjuster:
    """평가 메트릭을 분석해 보상 가중치를 자동 조정합니다."""

    def analyze(self, metrics: dict) -> list[str]:
        """메트릭 → 활성 시그널 목록 반환 (우선순위 순)."""
        signals: list[str] = []
        cr = metrics.get("collision_rate", 1.0)
        sr = metrics.get("success_rate", 0.0)
        dev = metrics.get("mean_max_deviation_km", 0.0)

        if cr > 0.20:
            signals.append("critical_collision")
        elif cr > 0.10:
            signals.append("high_collision")

        if sr < 0.40:
            signals.append("low_success")
        elif sr < 0.70:
            signals.append("moderate_success")

        if dev > 30.0:
            signals.append("high_deviation")

        if cr <= 0.05 and sr >= 0.70:
            signals.append("converging")

        # 우선순위 기준 최대 3개
        priority = [
            "critical_collision", "high_collision",
            "low_success", "moderate_success",
            "high_deviation", "converging",
        ]
        ordered = [s for s in priority if s in signals]
        return ordered[:3]

    def adjust(self, weights: RewardWeights, signals: list[str]) -> RewardWeights:
        """시그널에 따라 가중치를 조정하고 clamping 적용."""
        d = dataclasses.asdict(weights)

        for sig in signals:
            if sig == "critical_collision":
                d["collision"]    *= 2.0   # 1.5 → 2.0: 더 강력한 충돌 억제
                d["proximity"]    *= 1.8   # 1.3 → 1.8
                d["danger_zone"]  *= 2.0   # 위험 구역 경보도 동시 강화
            elif sig == "high_collision":
                d["collision"]    *= 1.6   # 1.25 → 1.6
                d["proximity"]    *= 1.4   # 1.15 → 1.4
                d["danger_zone"]  *= 1.5
            elif sig == "low_success":
                d["episode_success"] *= 1.8  # 1.3 → 1.8: 성공 보너스 대폭 강화
                d["progress"]        *= 1.5  # 1.2 → 1.5
            elif sig == "moderate_success":
                d["episode_success"] *= 1.3  # 1.15 → 1.3
                d["progress"]        *= 1.2
            elif sig == "high_deviation":
                d["route_deviation"] *= 1.5  # 1.3 → 1.5
                d["progress"]        *= 0.85
            elif sig == "converging":
                d["smoothness"] *= 0.9
                d["fuel"]       *= 0.9

        new_w = RewardWeights(**d)
        return _apply_bounds(new_w)

    def check_plateau(self, history: list[dict], field: str = "collision_rate",
                      threshold: float = 0.02, window: int = 3) -> bool:
        """최근 window 회 연속으로 field 개선이 threshold 미만이면 True."""
        if len(history) < window + 1:
            return False
        improvements = [
            abs(history[-(i+1)]["post_metrics"].get(field, 0) -
                history[-(i+2)]["post_metrics"].get(field, 0))
            for i in range(window)
        ]
        return all(imp < threshold for imp in improvements)


# ── 반복 학습 기록 ─────────────────────────────────────────
@dataclass
class IterationRecord:
    iteration: int
    weights: dict
    pre_metrics: dict
    post_metrics: dict
    signals: list[str]
    duration_seconds: float
    converged: bool


def _clean_nan(obj):
    """JSON serialization 지원을 위해 NaN을 0.0으로 변환."""
    if isinstance(obj, float) and math.isnan(obj):
        return 0.0
    if isinstance(obj, dict):
        return {k: _clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_nan(v) for v in obj]
    return obj


# ── 메인 반복 학습기 ───────────────────────────────────────
class IterativeTrainer:
    """학습→평가→보상 조정→재학습 루프를 자동으로 실행합니다."""

    def __init__(self, base_trainer: RLTrainer,
                 history_path: Path | None = None,
                 route: str | None = None,
                 ice_class: str | None = None,
                 ship_type: str | None = None):
        self.base_trainer = base_trainer
        self.route = route
        self.ice_class = ice_class
        self.ship_type = ship_type
        self.history_path = history_path or (
            _history_path_for(route, ice_class, ship_type)
            if route and ice_class and ship_type
            else HISTORY_PATH
        )
        self.history: list[IterationRecord] = []
        self.is_running = False
        self._load_history()  # 기존 히스토리 복원
        self.stop_requested = False
        self.current_iteration = 0
        self.current_weights: Optional[RewardWeights] = None
        self.adjuster = RewardAdjuster()

    # ── 수렴 판단 ─────────────────────────────────────────
    def _converged(self, metrics: dict,
                   target_success: float,
                   target_collision: float) -> bool:
        return (metrics.get("success_rate", 0.0) >= target_success and
                metrics.get("collision_rate", 1.0) <= target_collision)

    # ── 히스토리 저장 (crash-safe, Windows 파일 잠금 재시도 포함) ─
    def _load_history(self):
        """디스크에서 기존 히스토리를 불러와 self.history에 복원."""
        if not self.history_path.exists():
            return
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            self.history = [IterationRecord(**r) for r in data]
            if self.history:
                logger.info("[IterativeTrainer] 기존 히스토리 복원: %d회 완료", len(self.history))
        except Exception as e:
            logger.warning("[IterativeTrainer] 히스토리 로드 실패 (초기화): %s", e)
            self.history = []

    def _save_history(self):
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        data = [dataclasses.asdict(r) for r in self.history]
        data = _clean_nan(data)
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        tmp = self.history_path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        for attempt in range(3):
            try:
                os.replace(tmp, self.history_path)
                return
            except OSError:
                if attempt < 2:
                    time.sleep(0.15)
                else:
                    # 최후 수단: 직접 덮어쓰기
                    self.history_path.write_text(payload, encoding="utf-8")

    # ── 메인 루프 ─────────────────────────────────────────
    def run(self,
            max_iterations: int = 5,
            target_success_rate: float = 0.70,
            target_collision_rate: float = 0.15,
            eval_episodes: int = 30,
            eval_difficulty: str = "medium",
            initial_weights: RewardWeights | None = None,
            base_timesteps: int | None = None) -> dict:

        self.is_running = True
        self.stop_requested = False
        # 기존 히스토리가 없으면 초기화, 있으면 이어서 진행
        if not self.history:
            self._load_history()
        completed = len(self.history)

        if completed >= max_iterations:
            logger.info("[IterativeTrainer] 이미 %d/%d회 완료 — 스킵", completed, max_iterations)
            self.is_running = False
            return {"iterations_completed": completed, "skipped": True}

        # 마지막 완료된 가중치에서 이어서 시작
        if self.history and initial_weights is None:
            last = self.history[-1]
            current_weights = RewardWeights(**last.weights)
            logger.info("[IterativeTrainer] %d회부터 이어서 재개 (이전 %d회 완료)", completed + 1, completed)
        else:
            current_weights = initial_weights or RewardWeights()
        self.current_weights = current_weights

        logger.info(f"[IterativeTrainer] 반복 학습 시작 (max={max_iterations}, "
                    f"target_success={target_success_rate}, "
                    f"target_collision={target_collision_rate})")

        def _eval_difficulty_for(iteration: int, max_iter: int) -> str:
            """반복 진행에 따라 평가 난이도를 점진적으로 높임."""
            ratio = iteration / max(max_iter, 1)
            if ratio <= 0.3:
                return "easy"
            elif ratio <= 0.7:
                return "medium"
            else:
                return eval_difficulty  # hard (기본값) 또는 호출자 지정

        try:
            for i in range(completed + 1, max_iterations + 1):
                if self.stop_requested:
                    logger.info("[IterativeTrainer] 중단 요청으로 종료")
                    break

                self.current_iteration = i
                cur_eval_difficulty = _eval_difficulty_for(i, max_iterations)
                logger.info(f"[IterativeTrainer] ===== 반복 {i}/{max_iterations} (평가난이도: {cur_eval_difficulty}) =====")
                logger.info(f"[IterativeTrainer] 현재 가중치: {dataclasses.asdict(current_weights)}")

                iter_start = time.time()

                # 1. 학습 전 평가 (iteration 1이고 모델이 없으면 스킵)
                pre_metrics: dict = {}
                if self.base_trainer.agent.model is not None:
                    logger.info("[IterativeTrainer] 학습 전 평가 중...")
                    try:
                        pre_metrics = self.base_trainer.evaluate(
                            n_episodes=eval_episodes, difficulty=cur_eval_difficulty)
                        logger.info(f"[IterativeTrainer] 사전 평가: {pre_metrics}")
                    except Exception as e:
                        logger.error("[IterativeTrainer] 사전 평가 실패, 스킵: %s", e, exc_info=True)

                    # 첫 번째 반복은 무조건 학습 실행 (기존 모델 개선 목적)
                    if i > 1 and pre_metrics and self._converged(pre_metrics, target_success_rate, target_collision_rate):
                        logger.info("[IterativeTrainer] 수렴 조건 달성 — 조기 종료")
                        record = IterationRecord(
                            iteration=i, weights=dataclasses.asdict(current_weights),
                            pre_metrics=pre_metrics, post_metrics=pre_metrics,
                            signals=[], duration_seconds=0.0, converged=True)
                        self.history.append(record)
                        self._save_history()
                        break

                # 2. 커리큘럼 학습
                logger.info("[IterativeTrainer] 커리큘럼 학습 시작...")
                try:
                    self.base_trainer.train_curriculum(reward_weights=current_weights)
                except Exception as e:
                    logger.error("[IterativeTrainer] 커리큘럼 학습 예외 발생: %s", e, exc_info=True)
                    # 학습 실패 시 이번 반복을 스킵하고 다음으로 진행
                    continue

                # 3. 학습 후 평가
                logger.info("[IterativeTrainer] 학습 후 평가 중...")
                try:
                    post_metrics = self.base_trainer.evaluate(
                        n_episodes=eval_episodes, difficulty=cur_eval_difficulty)
                    logger.info(f"[IterativeTrainer] 사후 평가: {post_metrics}")
                except Exception as e:
                    logger.error("[IterativeTrainer] 사후 평가 실패: %s", e, exc_info=True)
                    post_metrics = pre_metrics or {}

                # 4. 시그널 분석 및 가중치 조정
                signals = self.adjuster.analyze(post_metrics)

                # Plateau 감지 — 개선이 없으면 전략 전환
                history_dicts = [dataclasses.asdict(r) for r in self.history]
                collision_plateau = ("critical_collision" in signals or "high_collision" in signals) and \
                    self.adjuster.check_plateau(history_dicts, field="collision_rate")
                success_plateau = ("low_success" in signals) and \
                    self.adjuster.check_plateau(history_dicts, field="success_rate")

                if collision_plateau and success_plateau:
                    # 양쪽 모두 정체: 전체 가중치 리셋 후 재시작
                    logger.info("[IterativeTrainer] 완전 Plateau 감지: 가중치 대폭 리셋")
                    next_weights = RewardWeights(
                        collision=-500.0, proximity=-10.0, danger_zone=-20.0,
                        route_deviation=-0.2, progress=3.0, smoothness=-0.05,
                        fuel=-0.02, ice_concentration=-0.3, episode_success=500.0,
                    )
                elif collision_plateau:
                    logger.info("[IterativeTrainer] Collision Plateau 감지: danger_zone 집중 강화")
                    signals = [s for s in signals
                               if s not in ("critical_collision", "high_collision")]
                    d = dataclasses.asdict(current_weights)
                    d["danger_zone"] *= 2.5
                    d["proximity"]   *= 1.5
                    next_weights = _apply_bounds(RewardWeights(**d))
                elif success_plateau:
                    logger.info("[IterativeTrainer] Success Plateau 감지: episode_success 집중 강화")
                    signals = [s for s in signals if s not in ("low_success", "moderate_success")]
                    d = dataclasses.asdict(current_weights)
                    d["episode_success"] *= 2.0
                    d["progress"]        *= 2.0
                    next_weights = _apply_bounds(RewardWeights(**d))
                else:
                    next_weights = self.adjuster.adjust(current_weights, signals)

                converged = self._converged(
                    post_metrics, target_success_rate, target_collision_rate)
                elapsed = time.time() - iter_start

                record = IterationRecord(
                    iteration=i,
                    weights=dataclasses.asdict(current_weights),
                    pre_metrics=pre_metrics,
                    post_metrics=post_metrics,
                    signals=signals,
                    duration_seconds=elapsed,
                    converged=converged,
                )
                self.history.append(record)
                self._save_history()

                logger.info(f"[IterativeTrainer] 반복 {i} 완료 | "
                            f"success={post_metrics.get('success_rate', 0):.3f} | "
                            f"collision={post_metrics.get('collision_rate', 0):.3f} | "
                            f"signals={signals} | converged={converged}")

                if converged:
                    logger.info("[IterativeTrainer] 수렴 조건 달성 — 학습 종료")
                    break

                current_weights = next_weights
                self.current_weights = current_weights

        finally:
            # 반복 학습 중단/완료/예외 모든 케이스에서 히스토리 및 모델 저장
            if self.base_trainer.agent.model is not None:
                try:
                    self.base_trainer.agent.save()
                    logger.info("[IterativeTrainer] 중단 시점 모델 자동 저장 완료")
                except Exception as e:
                    logger.error("[IterativeTrainer] 중단 시점 모델 저장 실패: %s", e)
            if self.history:
                try:
                    self._save_history()
                    logger.info("[IterativeTrainer] 중단 시점 히스토리 자동 저장 완료")
                except Exception as e:
                    logger.error("[IterativeTrainer] 히스토리 저장 실패: %s", e)
            self.is_running = False
            self.current_iteration = 0

        final_metrics = self.history[-1].post_metrics if self.history else {}
        return {
            "iterations_completed": len(self.history),
            "converged": self.history[-1].converged if self.history else False,
            "final_metrics": final_metrics,
            "final_weights": dataclasses.asdict(self.current_weights or RewardWeights()),
            "history_path": str(self.history_path),
        }

    def stop(self):
        self.stop_requested = True
        self.base_trainer.stop_requested = True

    def get_status(self) -> dict:
        status = {
            "is_running": self.is_running,
            "current_iteration": self.current_iteration,
            "current_weights": (dataclasses.asdict(self.current_weights)
                                if self.current_weights else {}),
            "latest_metrics": (self.history[-1].post_metrics
                               if self.history else {}),
            "history": [dataclasses.asdict(r) for r in self.history[-5:]],
        }
        return cast(dict, _clean_nan(status))
