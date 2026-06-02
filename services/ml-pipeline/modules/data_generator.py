"""
가상 데이터 생성 모듈
선박 제원 + 빙하 조건 → 단위 거리당 연료 소모량(fuel_per_nm) 데이터셋 생성

핵심 상관관계:
  - 빙하 두께·농도가 높을수록 연료 소모량이 *지수적*으로 증가
  - 쇄빙선(PC2)은 빙하 저항에 강하지만 개수역에서 연비가 나쁨 (둥근 뱃머리)
  - 컨테이너선(내빙등급 없음)은 중빙역에서 추가 패널티 발생
"""

import math
import os

import numpy as np
import pandas as pd

from . import config as cfg


def _compute_fuel_per_nm(
    displacement: float,
    draft: float,
    engine_power: float,
    ice_thickness: float,
    ice_concentration: float,
    ice_class_code: int,
    open_water_penalty: float,
) -> float:
    """단일 샘플의 fuel_per_nm을 물리 기반 공식으로 계산한다."""

    # 1) 기본 연료 소모 (배수량·흘수에 비례, 비선형)
    base_fuel = cfg.BASE_FUEL_COEFF * (
        (displacement / cfg.DISPLACEMENT_REF) ** cfg.DISPLACEMENT_EXPONENT
        * (draft / cfg.DRAFT_REF) ** cfg.DRAFT_EXPONENT
    )

    # 2) 쇄빙선 개수역 패널티 (둥근 뱃머리 → 항상 적용)
    base_fuel *= open_water_penalty

    # 3) 빙하 저항 — 지수적 증가
    ice_severity = ice_thickness * ice_concentration
    resistance_factor = cfg.ICE_CLASS_RESISTANCE_FACTOR.get(ice_class_code, 1.0)
    effective_ice = ice_severity * resistance_factor
    ice_multiplier = math.exp(cfg.ICE_RESISTANCE_EXPONENT * effective_ice)

    # 4) 컨테이너선 중빙역 추가 패널티 (내빙등급 없음 + 빙하 심한 구간)
    if ice_class_code == 0 and ice_severity > cfg.HEAVY_ICE_THRESHOLD:
        ice_multiplier *= cfg.CONTAINER_HEAVY_ICE_PENALTY

    fuel_per_nm = base_fuel * ice_multiplier
    return fuel_per_nm


def _sample_ice_conditions(rng: np.random.Generator, n: int) -> tuple:
    """빙하 농도·두께를 상관관계를 유지하며 샘플링한다.

    빙하 농도를 먼저 뽑고, 농도에 비례하여 두께의 평균을 결정한다.
    개수역(40%), 중빙역(35%), 중빙역(25%) 층화 샘플링.
    """
    n_open = int(n * 0.40)       # 개수역 (concentration < 0.1)
    n_medium = int(n * 0.35)     # 중간 (0.1 ~ 0.6)
    n_heavy = n - n_open - n_medium  # 중빙역 (0.6 ~ 1.0)

    conc_open = rng.uniform(0.0, 0.10, n_open)
    conc_medium = rng.uniform(0.10, 0.60, n_medium)
    conc_heavy = rng.uniform(0.60, 1.00, n_heavy)
    concentration = np.concatenate([conc_open, conc_medium, conc_heavy])

    # 두께: 농도와 양의 상관 + 노이즈
    mean_thickness = 0.3 + 2.0 * concentration ** 1.5
    thickness = rng.normal(mean_thickness, 0.3)
    thickness = np.clip(thickness, 0.0, cfg.ICE_THICKNESS_MAX)

    # 셔플
    idx = rng.permutation(n)
    return concentration[idx], thickness[idx]


def generate_dataset(save_path: str | None = None) -> pd.DataFrame:
    """가상 연료 소모 데이터셋을 생성하여 DataFrame으로 반환한다.

    Args:
        save_path: CSV 저장 경로. None이면 저장하지 않음.

    Returns:
        생성된 DataFrame (1,200건).
    """
    rng = np.random.default_rng(cfg.RANDOM_SEED)
    records = []

    for vessel_key, vessel in cfg.VESSEL_TYPES.items():
        n = cfg.SAMPLES_PER_VESSEL

        # 선박 제원 샘플링 (균일 분포)
        disp_lo, disp_hi = vessel["displacement_range"]
        draft_lo, draft_hi = vessel["draft_range"]
        power_lo, power_hi = vessel["engine_power_range"]
        displacement = rng.uniform(disp_lo, disp_hi, n)
        draft = rng.uniform(draft_lo, draft_hi, n)
        engine_power = rng.uniform(power_lo, power_hi, n)

        # 빙하 조건 샘플링
        ice_concentration, ice_thickness = _sample_ice_conditions(rng, n)

        ice_class_code = vessel["ice_class_code"]
        open_water_penalty = vessel["open_water_penalty"]

        for i in range(n):
            fuel = _compute_fuel_per_nm(
                displacement[i],
                draft[i],
                engine_power[i],
                ice_thickness[i],
                ice_concentration[i],
                ice_class_code,
                open_water_penalty,
            )
            # 가우시안 노이즈 (±7%)
            noise = rng.normal(1.0, cfg.NOISE_STD)
            fuel = max(0.001, fuel * noise)

            records.append({
                "vessel_type": vessel_key,
                "displacement": round(displacement[i], 1),
                "draft": round(draft[i], 2),
                "engine_power": round(engine_power[i], 1),
                "ice_thickness": round(ice_thickness[i], 3),
                "ice_concentration": round(ice_concentration[i], 4),
                "ice_class_code": ice_class_code,
                "fuel_per_nm": round(fuel, 6),
            })

    df = pd.DataFrame(records)

    if save_path:
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        df.to_csv(save_path, index=False, encoding="utf-8-sig")
        print(f"[데이터 생성] {len(df)}건 저장 완료 → {save_path}")

    return df
