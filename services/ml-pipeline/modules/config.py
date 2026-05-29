"""
ml-pipeline 설정 모듈
선박 타입별 제원 범위, 내빙등급 코드, 연료 소모 관련 상수 정의
"""

# ──────────────────────────────────────────────
# 선박 타입별 제원 범위 (vesselPresets.js 기준)
# ──────────────────────────────────────────────
VESSEL_TYPES = {
    "icebreaker": {
        "label": "쇄빙선 (Icebreaker)",
        "displacement_range": (18_000, 25_000),   # tons
        "draft_range": (7.5, 9.5),                 # m
        "engine_power_range": (25_000, 40_000),    # kW
        "ice_class_code": 2,                        # PC2
        "open_water_penalty": 1.25,                 # 둥근 뱃머리 → 개수역 연비 저하
    },
    "lng": {
        "label": "LNG 운반선 (LNG Carrier)",
        "displacement_range": (80_000, 110_000),
        "draft_range": (10.5, 13.5),
        "engine_power_range": (30_000, 45_000),
        "ice_class_code": 4,                        # PC4
        "open_water_penalty": 1.0,
    },
    "container": {
        "label": "컨테이너선 (Container Ship)",
        "displacement_range": (45_000, 70_000),
        "draft_range": (12.0, 16.0),
        "engine_power_range": (20_000, 35_000),
        "ice_class_code": 0,                        # 내빙등급 없음
        "open_water_penalty": 1.0,
    },
}

# ──────────────────────────────────────────────
# 내빙등급별 빙하 저항 감소 계수
# 높은 등급(PC2)일수록 빙하 저항을 더 잘 견딤
# ──────────────────────────────────────────────
ICE_CLASS_RESISTANCE_FACTOR = {
    0: 1.0,    # 내빙등급 없음 → 빙하 저항 100% 적용
    2: 0.4,    # PC2 (쇄빙선) → 빙하 저항 40%만 적용
    4: 0.65,   # PC4 (LNG 운반선) → 빙하 저항 65% 적용
}

# ──────────────────────────────────────────────
# 빙하 조건 범위
# ──────────────────────────────────────────────
ICE_THICKNESS_MAX = 3.0    # m (다년빙 최대 두께)
ICE_CONCENTRATION_MAX = 1.0

# ──────────────────────────────────────────────
# 연료 소모 공식 상수
# ──────────────────────────────────────────────
BASE_FUEL_COEFF = 0.008           # 기본 연료 계수
DISPLACEMENT_REF = 20_000         # 기준 배수량 (tons)
DRAFT_REF = 10.0                  # 기준 흘수 (m)
DISPLACEMENT_EXPONENT = 0.7       # 배수량 스케일링 지수 (비선형)
DRAFT_EXPONENT = 0.5              # 흘수 스케일링 지수
ICE_RESISTANCE_EXPONENT = 2.5     # 빙하 저항 지수 (exp 내부 계수)
CONTAINER_HEAVY_ICE_PENALTY = 1.5 # 컨테이너선 중빙역 추가 패널티
HEAVY_ICE_THRESHOLD = 0.5         # 중빙역 판단 임계값

# ──────────────────────────────────────────────
# 데이터 생성 설정
# ──────────────────────────────────────────────
SAMPLES_PER_VESSEL = 400          # 선종별 생성 건수
NOISE_STD = 0.07                  # 가우시안 노이즈 표준편차 (7%)
RANDOM_SEED = 42

# ──────────────────────────────────────────────
# 모델 학습 설정
# ──────────────────────────────────────────────
FEATURE_COLUMNS = [
    "displacement",
    "draft",
    "engine_power",
    "ice_thickness",
    "ice_concentration",
    "ice_class_code",
]
TARGET_COLUMN = "fuel_per_nm"

XGBOOST_PARAMS = {
    "n_estimators": 200,
    "max_depth": 6,
    "learning_rate": 0.1,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "objective": "reg:squarederror",
    "random_state": RANDOM_SEED,
}

TEST_SIZE = 0.2

# ──────────────────────────────────────────────
# 파일 경로
# ──────────────────────────────────────────────
DATA_DIR = "data"
MODEL_DIR = "models"
DATASET_FILENAME = "fuel_dataset.csv"
MODEL_FILENAME = "fuel_xgb_model.pkl"
FEATURE_IMPORTANCE_FILENAME = "feature_importance.png"
