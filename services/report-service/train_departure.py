"""
train_departure.py — 출항 스케줄 RL 전체 조합 학습 스크립트

실행:
    cd c:/cccc/Digital_twin/report-service
    python train_departure.py

설명:
    28개 조합 (7 ice_classes × 4 ship_types) 을 병렬로 학습합니다.

    패스(pass)가 완료될 때마다:
      1) 전체 조합의 결과를 집계·분석
      2) 미수렴 조합의 주요 문제(통행불가 과다/성공 부족/정체 등)를 판별
      3) 보상 가중치를 자동 조정해서 다음 패스 재시작
      → 최대 MAX_PASSES회 반복, 모든 조합 수렴 시 조기 종료
"""
import dataclasses
import json
import logging
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
        logging.FileHandler(LOG_DIR / "train_departure.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("train_departure")

MAX_PASSES = 5
DATA_DIR = PROJECT_ROOT / "data"


# ── 패스 간 글로벌 보상치 분석 & 조정 ─────────────────────────────
def _analyze_and_adjust_weights(pass_num: int) -> dict | None:
    """
    패스 완료 후 모든 미수렴 조합의 히스토리를 집계 분석하여
    다음 패스에 사용할 조정된 글로벌 보상 가중치를 반환합니다.
    """
    from modules.rl.departure_env import DepartureRewardWeights

    histories = []
    for p in DATA_DIR.glob("departure_iterative_history_*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data and not data[-1].get("converged", False):
                histories.append(data)
        except Exception:
            pass

    if not histories:
        logger.info("[분석] 미수렴 히스토리 없음 — 가중치 조정 불필요")
        return None

    # 마지막 반복의 메트릭 집계
    success_rates = []
    prohibitive_rates = []
    all_signals = []
    for h in histories:
        last = h[-1]
        pm = last.get("post_metrics", {})
        success_rates.append(pm.get("success_rate", 0.0))
        prohibitive_rates.append(pm.get("prohibitive_rate", 1.0))
        all_signals.extend(last.get("signals", []))

    mean_success = sum(success_rates) / len(success_rates)
    mean_prohibitive = sum(prohibitive_rates) / len(prohibitive_rates)

    signal_count: dict[str, int] = {}
    for s in all_signals:
        signal_count[s] = signal_count.get(s, 0) + 1
    dominant = sorted(signal_count, key=lambda k: -signal_count[k])

    logger.info(
        "[분석] 패스 %d 종료 분석 | 평균 성공률=%.3f | 평균 통행불가율=%.3f | 주요 시그널=%s",
        pass_num, mean_success, mean_prohibitive, dominant[:3]
    )

    # 가장 최근 사용된 가중치를 베이스로 조정
    base_weights = DepartureRewardWeights()
    try:
        last_weights_dict = histories[0][-1].get("weights", {})
        if last_weights_dict:
            base_weights = DepartureRewardWeights(**last_weights_dict)
    except Exception:
        pass

    d = dataclasses.asdict(base_weights)

    # ── 규칙 기반 글로벌 조정 ──────────────────────────────────────
    if mean_prohibitive > 0.40:
        d["prohibitive_penalty"] = max(-100.0, d["prohibitive_penalty"] * 2.0)
        d["success_bonus"]       = min(200.0,  d["success_bonus"]       * 1.3)
        logger.info("[조정] 통행불가율 과다(%.2f) → 금지 패널티 2배 강화", mean_prohibitive)
    elif mean_prohibitive > 0.20:
        d["prohibitive_penalty"] = max(-100.0, d["prohibitive_penalty"] * 1.5)
        logger.info("[조정] 통행불가율 높음(%.2f) → 금지 패널티 1.5배 강화", mean_prohibitive)

    if mean_success < 0.30:
        d["success_bonus"] = min(200.0, d["success_bonus"] * 2.0)
        logger.info("[조정] 성공률 매우 낮음(%.2f) → 성공 보너스 2배", mean_success)
    elif mean_success < 0.50:
        d["success_bonus"] = min(200.0, d["success_bonus"] * 1.5)
        logger.info("[조정] 성공률 낮음(%.2f) → 성공 보너스 1.5배", mean_success)

    # 전체 정체 감지: prohibitive & success 모두 다발
    n = len(histories)
    is_stuck = (
        signal_count.get("high_prohibitive", 0) > n * 0.5 and
        signal_count.get("low_success", 0) > n * 0.5
    )
    if is_stuck:
        logger.info("[조정] 전체 정체 감지 — 가중치 대폭 리셋")
        d = {
            "prohibitive_penalty": -40.0,
            "success_bonus":       120.0,
            "efficiency_penalty":  -5.0,
        }

    new_weights = DepartureRewardWeights(**d)
    logger.info("[조정] 새 글로벌 보상 가중치: %s", dataclasses.asdict(new_weights))
    return dataclasses.asdict(new_weights)


def _save_global_weights(weights_dict: dict):
    """글로벌 보상 가중치를 파일로 저장."""
    path = DATA_DIR / "departure_global_reward_weights.json"
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(weights_dict, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("[저장] 글로벌 보상 가중치 → %s", path)


def _cleanup_unconverged_histories():
    """수렴되지 않은 departure_iterative_history 파일을 삭제하여 재학습 허용."""
    deleted = 0
    for p in DATA_DIR.glob("departure_iterative_history_*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not (data and data[-1].get("converged", False)):
                p.unlink()
                deleted += 1
        except Exception:
            pass
    if deleted:
        logger.info("[정리] 미수렴 히스토리 %d개 삭제 완료 (재학습 허용)", deleted)


def run_one_pass(pass_num: int, monthly_ice: dict, weather_data: dict,
                 route_scorer, global_weights: dict | None = None) -> dict:
    """한 번의 전체 학습 패스를 실행하고 최종 상태를 반환합니다."""
    from modules.rl.multi_model_trainer import MultiModelIterativeTrainer, ALL_COMBINATIONS

    logger.info("=" * 60)
    logger.info("[패스 %d] 출항 스케줄 RL 전체 조합 학습 시작", pass_num)
    logger.info("총 조합 수: %d개 (7 ice_class × 4 ship_type)", len(ALL_COMBINATIONS))
    logger.info("설정: max_iterations=5, timesteps=100K/조합, 동시실행=2개")
    if global_weights:
        logger.info("[패스 %d] 적용 보상 가중치: %s", pass_num, global_weights)
    logger.info("=" * 60)

    trainer = MultiModelIterativeTrainer()
    start = time.time()

    trainer.start(
        monthly_ice=monthly_ice,
        weather_data=weather_data,
        route_scorer=route_scorer,
        base_timesteps=100_000,
        max_iterations=5,
        target_success_rate=0.70,
        target_prohibitive_rate=0.15,
        eval_episodes=30,
        forecast_days=30,
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
    from modules.data_loader import DataLoader
    from modules.route_scorer import RouteScorer

    logger.info("데이터 로딩 중...")
    data_loader = DataLoader()
    route_scorer = RouteScorer(data_loader)
    monthly_ice = data_loader.load_monthly_ice()
    weather_data = data_loader.load_weather()
    logger.info("월별 해빙 데이터: %d개월, 기상 데이터 로드 완료", len(monthly_ice))

    global_start = time.time()
    global_weights = None  # 첫 패스는 기본 가중치 사용

    for pass_num in range(1, MAX_PASSES + 1):
        try:
            status = run_one_pass(pass_num, monthly_ice, weather_data,
                                  route_scorer, global_weights)
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

        # 2) 미수렴 히스토리 삭제 (수렴 완료는 유지)
        _cleanup_unconverged_histories()

        time.sleep(10)
    else:
        logger.info("[자동완료] 최대 %d회 패스 완료. 학습을 종료합니다.", MAX_PASSES)

    elapsed_total = (time.time() - global_start) / 60
    logger.info("[전체 소요] %.1f분", elapsed_total)


if __name__ == "__main__":
    main()
