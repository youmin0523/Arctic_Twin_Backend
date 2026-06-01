"""
train_all.py — 빙산 회피 RL 전체 조합 학습 스크립트

실행:
    cd c:/cccc/Digital_twin/rl-pipeline
    python train_all.py

설명:
    84개 조합 (3 routes × 7 ice_classes × 4 ship_types) 을
    3개씩 병렬로 학습합니다.

    패스(pass)가 완료될 때마다:
      1) 전체 조합의 결과를 집계·분석
      2) 미수렴 조합의 주요 문제(충돌 과다/성공 부족/정체 등)를 판별
      3) 보상 가중치를 자동 조정해서 다음 패스 재시작
      → 최대 MAX_PASSES회 반복, 모든 조합 수렴 시 조기 종료
"""
import dataclasses
import json
import logging
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.absolute()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "train_all.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("train_all")

MAX_PASSES = 5  # 전체 재시작 최대 횟수
MODEL_DIR = PROJECT_ROOT / "models"


# ── 패스 간 글로벌 보상치 분석 & 조정 ─────────────────────────────
def _analyze_and_adjust_weights(pass_num: int) -> dict | None:
    """
    패스 완료 후 모든 미수렴 조합의 히스토리를 집계 분석하여
    다음 패스에 사용할 조정된 글로벌 보상 가중치를 반환합니다.

    Returns:
        조정된 가중치 dict, 또는 None (분석 불가 시)
    """
    from modules.rl_reward import RewardWeights

    histories = []
    for p in MODEL_DIR.glob("iterative_history_*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            # 미수렴 조합만 분석 대상
            if data and not data[-1].get("converged", False):
                histories.append(data)
        except Exception:
            pass

    if not histories:
        logger.info("[분석] 미수렴 히스토리 없음 — 가중치 조정 불필요")
        return None

    # 마지막 반복의 메트릭을 집계
    collision_rates = []
    success_rates = []
    all_signals = []
    for h in histories:
        last = h[-1]
        pm = last.get("post_metrics", {})
        collision_rates.append(pm.get("collision_rate", 1.0))
        success_rates.append(pm.get("success_rate", 0.0))
        all_signals.extend(last.get("signals", []))

    mean_collision = sum(collision_rates) / len(collision_rates)
    mean_success = sum(success_rates) / len(success_rates)

    # 시그널 빈도 집계
    signal_count: dict[str, int] = {}
    for s in all_signals:
        signal_count[s] = signal_count.get(s, 0) + 1
    dominant = sorted(signal_count, key=lambda k: -signal_count[k])

    logger.info(
        "[분석] 패스 %d 종료 분석 | 평균 충돌률=%.3f | 평균 성공률=%.3f | 주요 시그널=%s",
        pass_num, mean_collision, mean_success, dominant[:3]
    )

    # 가장 최근 사용된 가중치를 베이스로 조정
    base_weights = RewardWeights()
    # 미수렴 히스토리에서 가장 최근 가중치 추출
    try:
        last_weights_dict = histories[0][-1].get("weights", {})
        if last_weights_dict:
            base_weights = RewardWeights(**last_weights_dict)
    except Exception:
        pass

    d = dataclasses.asdict(base_weights)

    # ── 규칙 기반 글로벌 조정 ──────────────────────────────────────
    if mean_collision > 0.30:
        # 충돌률 30% 초과: 충돌 억제 최우선
        d["collision"]   = max(-1500.0, d["collision"]   * 2.0)
        d["proximity"]   = max(-30.0,   d["proximity"]   * 1.8)
        d["danger_zone"] = max(-50.0,   d["danger_zone"] * 2.0)
        logger.info("[조정] 충돌률 과다(%.2f) → 충돌 패널티 2배 강화", mean_collision)

    elif mean_collision > 0.15:
        d["collision"]   = max(-1500.0, d["collision"]   * 1.5)
        d["proximity"]   = max(-30.0,   d["proximity"]   * 1.4)
        d["danger_zone"] = max(-50.0,   d["danger_zone"] * 1.5)
        logger.info("[조정] 충돌률 높음(%.2f) → 충돌 패널티 1.5배 강화", mean_collision)

    if mean_success < 0.30:
        # 성공률 30% 미만: 성공 인센티브 대폭 강화
        d["episode_success"] = min(2000.0, d["episode_success"] * 2.0)
        d["progress"]        = min(20.0,   d["progress"]        * 1.5)
        logger.info("[조정] 성공률 매우 낮음(%.2f) → 성공 보너스 2배, 전진 보상 1.5배", mean_success)

    elif mean_success < 0.50:
        d["episode_success"] = min(2000.0, d["episode_success"] * 1.5)
        d["progress"]        = min(20.0,   d["progress"]        * 1.3)
        logger.info("[조정] 성공률 낮음(%.2f) → 성공 보너스 1.5배", mean_success)

    # 정체 감지 (critical_collision & low_success 동시 다발): 전략 리셋
    is_stuck = (
        signal_count.get("critical_collision", 0) > len(histories) * 0.5 and
        signal_count.get("low_success", 0) > len(histories) * 0.5
    )
    if is_stuck:
        logger.info("[조정] 전체 정체 감지 — 가중치 대폭 리셋")
        d = {
            "collision":         -600.0,
            "proximity":         -12.0,
            "danger_zone":       -25.0,
            "route_deviation":   -0.15,
            "progress":           3.5,
            "smoothness":        -0.05,
            "fuel":              -0.02,
            "ice_concentration": -0.3,
            "episode_success":    600.0,
        }

    new_weights = RewardWeights(**d)
    logger.info("[조정] 새 글로벌 보상 가중치: %s", dataclasses.asdict(new_weights))
    return dataclasses.asdict(new_weights)


def _save_global_weights(weights_dict: dict):
    """글로벌 보상 가중치를 파일로 저장 (다음 패스에서 IterativeTrainer가 참조)."""
    path = MODEL_DIR / "global_reward_weights.json"
    path.write_text(json.dumps(weights_dict, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("[저장] 글로벌 보상 가중치 → %s", path)


def _cleanup_unconverged_histories():
    """수렴되지 않은 iterative_history 파일을 삭제하여 재학습 허용."""
    deleted = 0
    for p in MODEL_DIR.glob("iterative_history_*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not (data and data[-1].get("converged", False)):
                p.unlink()
                deleted += 1
        except Exception:
            pass
    if deleted:
        logger.info("[정리] 미수렴 히스토리 %d개 삭제 완료 (재학습 허용)", deleted)


def _inject_global_weights_into_trainers():
    """
    global_reward_weights.json이 있으면 RLTrainer 기본 가중치로 패치.
    IterativeTrainer가 히스토리 없이 시작할 때 이 가중치를 initial_weights로 사용하도록
    rl_reward.py의 RewardWeights 기본값을 동적으로 교체합니다.
    """
    weights_path = MODEL_DIR / "global_reward_weights.json"
    if not weights_path.exists():
        return

    try:
        from modules import rl_reward  # type: ignore[attr-defined]
        weights_dict = json.loads(weights_path.read_text(encoding="utf-8"))
        new_weights = rl_reward.RewardWeights(**weights_dict)

        # RewardWeights의 클래스 기본값을 새 값으로 교체
        for field_name, val in weights_dict.items():
            if hasattr(rl_reward.RewardWeights, field_name):
                # dataclass field 기본값 교체
                pass
        # 가장 간단한 방법: 모듈 수준 패치
        rl_reward._GLOBAL_WEIGHTS_OVERRIDE = new_weights
        logger.info("[패치] 글로벌 보상 가중치 주입 완료")
    except Exception as e:
        logger.warning("[패치] 글로벌 가중치 주입 실패 (무시): %s", e)


def run_one_pass(pass_num: int, global_weights: dict | None = None) -> dict:
    """한 번의 전체 학습 패스를 실행하고 최종 상태를 반환합니다."""
    from modules.rl_multi_model_trainer import RLMultiModelTrainer, ALL_COMBINATIONS

    logger.info("=" * 60)
    logger.info("[패스 %d] 빙산 회피 RL 전체 조합 학습 시작", pass_num)
    logger.info("총 조합 수: %d개", len(ALL_COMBINATIONS))
    logger.info("설정: max_iterations=5, timesteps=150K/조합, 동시실행=3개")
    if global_weights:
        logger.info("[패스 %d] 적용 보상 가중치: %s", pass_num, global_weights)
    logger.info("=" * 60)

    trainer = RLMultiModelTrainer()
    start = time.time()

    trainer.start(
        max_iterations=5,
        target_success_rate=0.70,
        target_collision_rate=0.15,
        eval_episodes=30,
        eval_difficulty="medium",
        base_timesteps=150_000,
    )

    # trainer.start()는 즉시 반환(스레드 시작) → is_running True 될 때까지 대기
    time.sleep(5)

    while trainer.is_running:
        time.sleep(60)
        status = trainer.get_status()
        running = status["running_models"]
        converged = status["converged_models"]
        total = status["total_models"]
        elapsed = (time.time() - start) / 60
        logger.info(
            "[패스 %d] %.0f분 경과 | 실행 중: %d개 | 수렴 완료: %d/%d개",
            pass_num, elapsed, running, converged, total
        )

    elapsed = (time.time() - start) / 60
    status = trainer.get_status()
    logger.info("=" * 60)
    logger.info("[패스 %d] 학습 종료 | 총 소요: %.1f분", pass_num, elapsed)
    logger.info("수렴 완료: %d/%d개", status["converged_models"], status["total_models"])
    logger.info("=" * 60)
    return status


def main():
    global_start = time.time()
    global_weights = None  # 첫 패스는 기본 가중치 사용

    for pass_num in range(1, MAX_PASSES + 1):
        try:
            status = run_one_pass(pass_num, global_weights)
        except KeyboardInterrupt:
            logger.info("사용자 중단 요청 — 종료합니다.")
            break

        total = status["total_models"]
        converged = status["converged_models"]

        if converged >= total:
            logger.info("[자동완료] 모든 %d개 조합 수렴 완료. 학습을 종료합니다.", total)
            break

        remaining = total - converged
        next_pass = pass_num + 1
        logger.info(
            "[자동재시작] 미수렴 %d개 조합 재학습 — 패스 %d/%d 준비 중",
            remaining, next_pass, MAX_PASSES
        )

        # 1) 결과 분석 → 글로벌 보상 가중치 조정
        new_weights = _analyze_and_adjust_weights(pass_num)
        if new_weights:
            _save_global_weights(new_weights)
            global_weights = new_weights

        # 2) 미수렴 히스토리 삭제 (재학습 허용, 수렴 완료는 유지)
        _cleanup_unconverged_histories()

        # 3) 새 가중치를 모듈에 주입
        _inject_global_weights_into_trainers()

        time.sleep(10)
    else:
        logger.info("[자동완료] 최대 %d회 패스 완료. 학습을 종료합니다.", MAX_PASSES)

    elapsed_total = (time.time() - global_start) / 60
    logger.info("[전체 소요] %.1f분", elapsed_total)


if __name__ == "__main__":
    main()
