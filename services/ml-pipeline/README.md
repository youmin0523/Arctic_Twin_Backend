# ml-pipeline — 북극항로 연료 소모량 예측 (XGBoost Regression)

북극항로(NSR) 운항 선박의 **빙하 저항에 따른 단위 거리당 연료 소모량(fuel_per_nm)**을 예측하는 머신러닝 회귀 모델입니다.

## 디렉토리 구조

```
ml-pipeline/
├── train_fuel_model.py       # 메인 실행 스크립트
├── requirements.txt          # Python 의존성
├── README.md
├── modules/
│   ├── config.py             # 선박 제원, 모델 하이퍼파라미터, 상수
│   ├── data_generator.py     # 가상 데이터 생성 (1,200건)
│   └── model_trainer.py      # XGBoost 학습, 평가, 저장
├── models/                   # 학습 산출물
│   ├── fuel_xgb_model.pkl    # 저장된 XGBoost 모델 (joblib)
│   └── feature_importance.png
└── data/
    └── fuel_dataset.csv      # 생성된 학습 데이터
```

## 빠른 시작

### 1. 환경 설정

```bash
cd Digital_twin/ml-pipeline

# 가상환경 생성 (권장)
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 의존성 설치
pip install -r requirements.txt
```

### 2. 모델 학습 실행

```bash
python train_fuel_model.py
```

실행하면 아래 작업이 순서대로 수행됩니다:

| 단계 | 설명 | 산출물 |
|------|------|--------|
| Step 1 | 가상 데이터 1,200건 생성 | `data/fuel_dataset.csv` |
| Step 2 | XGBoost Regressor 학습 | — |
| Step 3 | RMSE, R², MAE 평가 출력 | — |
| Step 4 | Feature Importance 시각화 | `models/feature_importance.png` |
| Step 5 | 학습된 모델 저장 | `models/fuel_xgb_model.pkl` |
| Step 6 | 샘플 예측 테스트 | 콘솔 출력 |

### 3. 저장된 모델 사용 (Python)

```python
import joblib
import numpy as np

model = joblib.load("models/fuel_xgb_model.pkl")

# 입력: [배수량(tons), 흘수(m), 엔진출력(kW), 빙하두께(m), 빙하농도(0~1), 내빙등급코드]
# 내빙등급코드: 0=없음, 2=PC2(쇄빙선), 4=PC4(LNG)
sample = np.array([[95000, 12.0, 37000, 1.5, 0.6, 4]])
prediction = model.predict(sample)
print(f"예측 연료 소모량: {prediction[0]:.6f} tons/nm")
```

## 입출력 사양

### Features (X)

| Feature | 단위 | 설명 | 범위 |
|---------|------|------|------|
| `displacement` | tons | 배수량 | 18,000 ~ 110,000 |
| `draft` | m | 흘수 | 7.5 ~ 16.0 |
| `engine_power` | kW | 엔진 출력 | 20,000 ~ 45,000 |
| `ice_thickness` | m | 빙하 두께 | 0.0 ~ 3.0 |
| `ice_concentration` | — | 빙하 농도 | 0.0 ~ 1.0 |
| `ice_class_code` | — | 내빙등급 코드 | 0, 2, 4 |

### Target (y)

| Target | 단위 | 설명 |
|--------|------|------|
| `fuel_per_nm` | tons/nm | 해리당 연료 소모량 |

## 선종별 특성

| 선종 | 내빙등급 | 특징 |
|------|---------|------|
| **쇄빙선** | PC2 (code=2) | 빙하 저항에 강함. 단, 둥근 뱃머리로 개수역 연비 25% 저하 |
| **LNG 운반선** | PC4 (code=4) | 중간 수준의 내빙 성능 |
| **컨테이너선** | 없음 (code=0) | 빙하에 취약. 중빙역(severity > 0.5)에서 추가 50% 패널티 |

## 모델 상세

- **알고리즘**: XGBoost Regressor
- **핵심 공식**: 빙하 저항이 `exp(2.5 × ice_thickness × ice_concentration × class_factor)`로 지수적 증가
- **하이퍼파라미터**: `n_estimators=200`, `max_depth=6`, `learning_rate=0.1`
- **Train/Test 비율**: 80% / 20%
- **평가 지표**: RMSE, R² Score, MAE

## FastAPI 서버 (port 8003)

학습된 모델은 FastAPI 서버를 통해 실시간 예측 API로 제공됩니다.

### 서버 단독 실행

```bash
cd Digital_twin/ml-pipeline
venv/Scripts/activate        # Windows
uvicorn server:app --host 127.0.0.1 --port 8003 --reload
```

### 자동 실행

Express 백엔드(`backend/src/index.js`)가 서버 시작 시 ml-pipeline을 자동으로 기동합니다.
별도 실행이 필요 없습니다.

### API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | `/api/fuel/health` | 모델 로드 상태 확인 |
| POST | `/api/fuel/predict` | 단일 구간 연료 소모량 예측 |
| POST | `/api/fuel/compare` | 북극항로 vs 수에즈 경제성 비교 |

### `/api/fuel/compare` 요청 예시

```json
{
  "displacement": 55000,
  "draft": 14.2,
  "engine_power": 28000,
  "ice_class_code": 0,
  "nsr_ice_thickness": 1.0,
  "nsr_ice_concentration": 0.3,
  "nsr_distance_nm": 7200,
  "suez_distance_nm": 12400,
  "vessel_type": "container",
  "speed_knots": 14.0
}
```

### 응답 예시

```json
{
  "nsr": {
    "total_fuel_tons": 269.13,
    "fuel_cost_usd": 161480,
    "escort_cost_usd": 2069805,
    "insurance_cost_usd": 365260,
    "total_cost_usd": 2596545,
    "transit_days": 24.4
  },
  "suez": {
    "total_fuel_tons": 236.43,
    "fuel_cost_usd": 141857,
    "toll_usd": 300000,
    "total_cost_usd": 461857,
    "transit_days": 36.9
  },
  "comparison": {
    "cost_saving_usd": -2134688,
    "time_saving_days": 12.6,
    "nsr_is_cheaper": false
  }
}
```

## 비용 모델 상세

### 선종별 부대비용 (고정 요율)

| 항목 | 컨테이너선 | LNG 운반선 | 쇄빙선 |
|------|-----------|-----------|--------|
| 쇄빙 에스코트 (USD/일) | $85,000 | $120,000 | **면제** |
| 북극해 보험 (USD/일) | $15,000 | $45,000 | $8,000 |
| 수에즈 통행료 (USD) | $300,000 | $450,000 | $250,000 |
| 수에즈 보안비 (USD) | $20,000 | $35,000 | $15,000 |
| 벙커유 단가 | $600/ton (VLSFO) | | |

### 프론트엔드 연동

BottomPanel의 **Ship Service Info** 패널에 "ML 연료 비용 분석" 섹션이 자동으로 표시됩니다.
선박 제원, 항로, 빙하 농도가 변경되면 실시간으로 API를 호출하여 비교 결과를 업데이트합니다.
