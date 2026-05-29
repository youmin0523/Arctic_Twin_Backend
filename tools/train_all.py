import os
import sys
import logging
import time
from pathlib import Path

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger("train_all")

# 경로 설정
BASE_DIR = Path(__file__).resolve().parent.parent  # tools/ → 프로젝트 루트
RL_PIPELINE_PATH = BASE_DIR / "backend/services/rl-pipeline"
REPORT_SERVICE_PATH = BASE_DIR / "backend/services/report-service"

# PYTHONPATH 추가
sys.path.append(str(RL_PIPELINE_PATH))
sys.path.append(str(REPORT_SERVICE_PATH))

def train_collision_avoidance():
    print(">>> Collision Avoidance (RL-C) 학습 시작 시도...")
    logger.info("=== Collision Avoidance (RL-C) 학습 시작 ===")
    try:
        from modules.rl_trainer import RLTrainer
        trainer = RLTrainer()
        print(">>> RLTrainer 초기화 완료. train_curriculum 실행...")
        results = trainer.train_curriculum()
        print(f">>> Collision Avoidance 학습 완료: {results}")
        logger.info(f"Collision Avoidance 학습 완료: {results}")
    except Exception as e:
        print(f">>> Collision Avoidance 학습 실패: {e}")
        logger.error(f"Collision Avoidance 학습 실패: {e}", exc_info=True)

def train_trend_report():
    print(">>> Trend Report (RL-A) 학습 시작 시도...")
    logger.info("=== Trend Report / Departure Scheduling (RL-A) 학습 시작 ===")
    try:
        from modules.rl.departure_trainer import DepartureTrainer
        print(">>> DepartureTrainer 임포트 완료...")
        from modules.data_loader import DataLoader
        from modules.route_scorer import RouteScorer
        
        # 의존성 초기화
        data_loader = DataLoader()
        route_scorer = RouteScorer(data_loader)
        monthly_ice = data_loader.load_monthly_ice()
        weather = data_loader.load_weather()
        
        trainer = DepartureTrainer()
        trainer.train_curriculum(
            monthly_ice=monthly_ice,
            weather_data=weather,
            route_scorer=route_scorer,
            ice_class="PC5"
        )
        logger.info("Trend Report 학습 완료")
    except Exception as e:
        logger.error(f"Trend Report 학습 실패: {e}", exc_info=True)

if __name__ == "__main__":
    # 두 학습을 순차적으로 진행 (필요시 병렬 처리 가능하나 안정성을 위해 순차 진행)
    train_collision_avoidance()
    train_trend_report()
