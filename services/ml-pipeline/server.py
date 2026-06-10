"""
ML Fuel Prediction Pipeline -- FastAPI Server

Run: uvicorn server:app --reload --port 8003
(포트 8003: 백엔드 8000, RL 8001, Report 8002 와 충돌 방지)
"""

import os
import logging

import joblib
import numpy as np
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ml-pipeline")

app = FastAPI(
    title="ML Fuel Prediction Pipeline",
    description="빙하 저항 기반 연료 소모량 예측 API (XGBoost)",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 모델 로드 ───────────────────────────────────────────────
# 중앙 모델 폴더 우선, 없으면 서비스 로컬 폴더로 폴백
# 두 가지 레이아웃 지원:
#   - 로컬: Digital_twin/backend/services/ml-pipeline/server.py
#           → ../../backend = backend/services/backend (없음)
#           → ../../../backend = Digital_twin/backend (있음)
#   - HF Space: <root>/ml-pipeline/server.py
#           → ../backend = <root>/backend (있음)
_MODEL_CANDIDATES = [
    # backend/services/ml-pipeline → ../../model = backend/model (현재 레이아웃)
    os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "model",
        "nevigation-service", "fuel_xgb_model.pkl",
    )),
    # 단독 배포(HF) 폴백
    os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "backend", "model",
        "nevigation-service", "fuel_xgb_model.pkl",
    )),
    os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "backend", "model",
        "nevigation-service", "fuel_xgb_model.pkl",
    )),
]
_CENTRAL_MODEL = next((p for p in _MODEL_CANDIDATES if os.path.exists(p)), _MODEL_CANDIDATES[0])
_LOCAL_MODEL = os.path.join(os.path.dirname(__file__), "models", "fuel_xgb_model.pkl")
MODEL_PATH = _CENTRAL_MODEL if os.path.exists(_CENTRAL_MODEL) else _LOCAL_MODEL
artifact = None


@app.on_event("startup")
def load_model():
    global artifact
    if os.path.exists(MODEL_PATH):
        artifact = joblib.load(MODEL_PATH)
        logger.info(f"[ML] 연료 예측 모델 로드 완료: {MODEL_PATH}")
        logger.info(f"[ML] 모델 성능: {artifact.get('metrics', {})}")
    else:
        logger.warning(f"[ML] 모델 파일 없음: {MODEL_PATH}")
        logger.warning("[ML] 먼저 python train_fuel_model.py 를 실행하세요.")


# ── Request / Response Models ───────────────────────────────

class FuelPredictRequest(BaseModel):
    displacement: float       # 배수량 (tons)
    draft: float              # 흘수 (m)
    engine_power: float       # 엔진 출력 (kW)
    ice_thickness: float      # 빙하 두께 (m), 0~3
    ice_concentration: float  # 빙하 농도 (0~1)
    ice_class_code: int       # 내빙등급 코드 (0=없음, 2=PC2, 4=PC4)


class RouteCompareRequest(BaseModel):
    """북극항로 vs 수에즈 비교 요청"""
    displacement: float
    draft: float
    engine_power: float
    ice_class_code: int
    # 북극항로 구간별 빙하 조건 (평균값)
    nsr_ice_thickness: float   # NSR 평균 빙하 두께
    nsr_ice_concentration: float  # NSR 평균 빙하 농도
    nsr_distance_nm: float     # NSR 총 거리 (해리)
    suez_distance_nm: float    # 수에즈 총 거리 (해리)
    # 선종 (비용 계산용)
    vessel_type: str = "container"  # container, lng, icebreaker
    speed_knots: float = 14.0  # 운항 속도 (knots)
    route: str = "NSR"  # 북극항로 키(NSR/NWP/TSR/ROSS/PENINSULA) — 호위비 자국/타국 분기용


# ── 연료 단가 및 부대비용 상수 ──────────────────────────────

# 벙커유(VLSFO) 단가 (USD/ton) — 2024~2025 평균
FUEL_PRICE_USD_PER_TON = 600.0

# 수에즈 운하 통행료 (USD) — 선종별
SUEZ_TOLL = {
    "container": 300_000,   # 컨테이너선 (가장 저렴한 선종)
    "lng": 450_000,         # LNG 운반선
    "icebreaker": 250_000,  # 쇄빙선
}

# 쇄빙선 에스코트 수수료 (USD/일) — 타국 쇄빙선(시장 수수료) 기준
# NWP=캐나다 CCGS, TSR=러시아 Rosatom 등 타국 자산을 빌릴 때 지불하는 시장 수수료.
ICEBREAKER_ESCORT_FEE = {
    "container": 85_000,    # 일반 상선
    "lng": 120_000,         # LNG (위험물 할증)
    "icebreaker": 0,        # 자체 쇄빙 → 면제
}

# 자국(한국 아라온, KOPRI) 쇄빙선이 호위하는 항로 — 타국 시장 수수료가 아니라
# 운영원가만 산정한다(국가 자산이라 수수료 지불 주체가 없음).
OWN_ESCORT_ROUTES = {"NSR", "ROSS", "PENINSULA"}  # 아라온 호위 권역(북동항로 + 남극)

# 자국 쇄빙선 운영원가 (USD/일): 쇄빙선 자체 연료(~20k, 약 33톤/일×$600) + 인건/유지(~10k).
# 시장 수수료(85k+)와 달리 마진이 없는 실비 — 자국 자산 운용 비용만 반영.
OWN_ESCORT_OPERATING_COST_PER_DAY = 30_000

# 항로별 '빙해 노출 구간' 비율 — 전체 거리 중 실제 빙해(빙저항·감속 적용) 비중.
# 나머지(연안·대서양 등)는 개수역으로 기본 연료·명목속도. 전 구간 빙해 가정의 과대평가를 보정한다.
ROUTE_ICE_FRACTION = {
    "NSR": 0.40,   # 카라/랍테프/동시베리아/축치 일부
    "NWP": 0.45,   # 캐나다 군도 다년빙 구간
    "TSR": 0.55,   # 북극점 인근 고위도 — 빙해 비중 최대
    "ROSS": 0.50,
    "PENINSULA": 0.45,
}

# 북극해 특별 보험료 (USD/일) — 선종별
ARCTIC_INSURANCE_PER_DAY = {
    "container": 15_000,
    "lng": 45_000,          # LNG 폭발 위험 → 기하급수적 할증
    "icebreaker": 8_000,
}

# 수에즈 우회 보안비 (해적 대비, 아덴만 통과)
SUEZ_SECURITY_COST = {
    "container": 20_000,
    "lng": 35_000,
    "icebreaker": 15_000,
}


# ── Endpoints ───────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "service": "ML Fuel Prediction Pipeline",
        "model_loaded": artifact is not None,
        "port": 8003,
    }


@app.get("/api/fuel/health")
def health():
    return {
        "status": "ok" if artifact else "no_model",
        "model_loaded": artifact is not None,
        "metrics": artifact.get("metrics", {}) if artifact else {},
    }


@app.post("/api/fuel/predict")
def predict_fuel(req: FuelPredictRequest):
    """단일 구간 연료 소모량 예측"""
    if not artifact:
        return {"error": "모델이 로드되지 않았습니다. train_fuel_model.py를 먼저 실행하세요."}

    X = np.array([[
        req.displacement, req.draft, req.engine_power,
        req.ice_thickness, req.ice_concentration, req.ice_class_code,
    ]])

    y_log = artifact["model"].predict(X)
    fuel_per_nm = float(np.exp(y_log[0])) if artifact.get("log_transformed") else float(y_log[0])

    return {
        "fuel_per_nm": round(fuel_per_nm, 6),
        "unit": "tons/nm",
    }


@app.post("/api/fuel/compare")
def compare_routes(req: RouteCompareRequest):
    """북극항로 vs 수에즈 운하 경제성 비교"""
    if not artifact:
        return {"error": "모델이 로드되지 않았습니다."}

    vtype = req.vessel_type

    # ── 1) nm당 연료율 예측 (ML 모델) ────────────────────────
    # NSR 빙해 연료율 (빙두께·농도 반영)
    X_nsr = np.array([[
        req.displacement, req.draft, req.engine_power,
        req.nsr_ice_thickness, req.nsr_ice_concentration, req.ice_class_code,
    ]])
    y_log = artifact["model"].predict(X_nsr)
    nsr_fuel_per_nm = float(np.exp(y_log[0])) if artifact.get("log_transformed") else float(y_log[0])

    # 개수역(빙하 없음) 연료율 — 수에즈 전체 + 북극항로의 개수역 구간에 공통 적용
    X_open = np.array([[
        req.displacement, req.draft, req.engine_power,
        0.0, 0.0, req.ice_class_code,
    ]])
    y_log_open = artifact["model"].predict(X_open)
    open_fuel_per_nm = float(np.exp(y_log_open[0])) if artifact.get("log_transformed") else float(y_log_open[0])
    suez_fuel_per_nm = open_fuel_per_nm

    # ── 2) NSR 빙해 노출 구간 분리 (전 구간 빙저항 과대평가 보정) ──
    # 북극항로는 일부 구간만 빙해이고 나머지(연안·대서양)는 개수역이다. 빙해 구간 비율만큼만
    # 빙저항 연료·감속을 적용하고, 나머지는 개수역 연료·명목속도로 계산한다.
    ice_frac = ROUTE_ICE_FRACTION.get(req.route, 0.45)
    nsr_ice_nm = req.nsr_distance_nm * ice_frac
    nsr_open_nm = req.nsr_distance_nm * (1.0 - ice_frac)
    nsr_total_fuel = nsr_fuel_per_nm * nsr_ice_nm + open_fuel_per_nm * nsr_open_nm
    suez_total_fuel = suez_fuel_per_nm * req.suez_distance_nm

    # ── 3) 운항 시간 — 빙해 구간만 감속, 개수역은 명목속도 ───
    # 빙급 코드 → 내빙 성능 계수 (0=없음 → 낮음, 2=PC2 → 높음)
    ice_class_perf = {0: 0.0, 2: 0.9, 4: 0.7}.get(req.ice_class_code, 0.5)
    conc_penalty = req.nsr_ice_concentration * (0.5 - 0.3 * ice_class_perf)
    thick_penalty = min(0.3, req.nsr_ice_thickness / 3.0 * (0.3 - 0.15 * ice_class_perf))
    ice_speed_factor = max(0.3, 1.0 - conc_penalty - thick_penalty)
    nsr_effective_speed = req.speed_knots * ice_speed_factor
    arctic_transit_days = nsr_ice_nm / (nsr_effective_speed * 24)        # 북극 빙해 구간 소요일
    nsr_transit_days = arctic_transit_days + nsr_open_nm / (req.speed_knots * 24)

    suez_transit_days = req.suez_distance_nm / (req.speed_knots * 24)

    # ── 4) 비용 계산 ────────────────────────────────────────
    # 연료비
    nsr_fuel_cost = nsr_total_fuel * FUEL_PRICE_USD_PER_TON
    suez_fuel_cost = suez_total_fuel * FUEL_PRICE_USD_PER_TON

    # NSR 부대비용 — 호위비는 항로별 자국/타국 분기
    #   자국(NSR/ROSS/PENINSULA, 한국 아라온): 시장 수수료 대신 운영원가만
    #   타국(NWP=캐 CCGS, TSR=러 Rosatom): 시장 수수료
    if req.route in OWN_ESCORT_ROUTES:
        escort_mode = "own"
        escort_rate = OWN_ESCORT_OPERATING_COST_PER_DAY
    else:
        escort_mode = "foreign"
        escort_rate = ICEBREAKER_ESCORT_FEE.get(vtype, 85_000)
    # 호위·북극보험은 '북극 빙해 구간'에서만 발생한다. 황해·북대서양 등 개수역에선 쇄빙선
    # 호위도, 북극 특별보험도 불필요 — 전 항해일이 아니라 arctic_transit_days 에만 부과한다.
    nsr_escort_cost = escort_rate * arctic_transit_days
    nsr_insurance_cost = ARCTIC_INSURANCE_PER_DAY.get(vtype, 15_000) * arctic_transit_days
    nsr_additional = nsr_escort_cost + nsr_insurance_cost
    nsr_total_cost = nsr_fuel_cost + nsr_additional

    # 수에즈 부대비용
    suez_toll = SUEZ_TOLL.get(vtype, 300_000)
    suez_security = SUEZ_SECURITY_COST.get(vtype, 20_000)
    suez_additional = suez_toll + suez_security
    suez_total_cost = suez_fuel_cost + suez_additional

    # ── 5) 비교 결과 ────────────────────────────────────────
    cost_saving = suez_total_cost - nsr_total_cost
    time_saving = suez_transit_days - nsr_transit_days

    return {
        "nsr": {
            "distance_nm": req.nsr_distance_nm,
            "fuel_per_nm": round(nsr_fuel_per_nm, 6),
            "total_fuel_tons": round(nsr_total_fuel, 2),
            "fuel_cost_usd": round(nsr_fuel_cost, 0),
            "escort_cost_usd": round(nsr_escort_cost, 0),
            "escort_mode": escort_mode,                       # own(자국 아라온 운영원가) | foreign(타국 시장수수료)
            "escort_rate_per_day_usd": round(escort_rate, 0),
            "insurance_cost_usd": round(nsr_insurance_cost, 0),
            "additional_cost_usd": round(nsr_additional, 0),
            "total_cost_usd": round(nsr_total_cost, 0),
            "transit_days": round(nsr_transit_days, 1),              # 부산-로테르담 총 항해 소요일
            "arctic_transit_days": round(arctic_transit_days, 1),    # 그중 북극 빙해 구간(호위·보험 부과 기준)
            "effective_speed_knots": round(nsr_effective_speed, 1),  # 빙해 구간 실효속도
            "ice_route_fraction": ice_frac,                          # 빙해 노출 구간 비율
        },
        "suez": {
            "distance_nm": req.suez_distance_nm,
            "fuel_per_nm": round(suez_fuel_per_nm, 6),
            "total_fuel_tons": round(suez_total_fuel, 2),
            "fuel_cost_usd": round(suez_fuel_cost, 0),
            "toll_usd": round(suez_toll, 0),
            "security_cost_usd": round(suez_security, 0),
            "additional_cost_usd": round(suez_additional, 0),
            "total_cost_usd": round(suez_total_cost, 0),
            "transit_days": round(suez_transit_days, 1),
        },
        "comparison": {
            "cost_saving_usd": round(cost_saving, 0),
            "cost_saving_percent": round(cost_saving / suez_total_cost * 100, 1) if suez_total_cost > 0 else 0,
            "time_saving_days": round(time_saving, 1),  # 양수 = NSR이 그만큼 더 빠름
            "nsr_is_faster": time_saving > 0,
            "fuel_saving_tons": round(suez_total_fuel - nsr_total_fuel, 2),
            "nsr_is_cheaper": cost_saving > 0,
        },
        "vessel_type": vtype,
        "fuel_price_usd_per_ton": FUEL_PRICE_USD_PER_TON,
    }
