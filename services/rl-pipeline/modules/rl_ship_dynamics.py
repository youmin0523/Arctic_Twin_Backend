"""
rl_ship_dynamics.py -- 선박 역학 모델

RL 환경에서 사용하는 선박의 운동 역학을 시뮬레이션합니다.
- 위치 업데이트 (위경도 기반 대권 항해)
- 선회 반경/관성 제약
- 속도 가감속 모델
- 해빙 저항에 의한 속도 감소
"""
from __future__ import annotations

import math
import numpy as np
from dataclasses import dataclass, field


# ── 상수 ───────────────────────────────────────────────────
DEG2RAD = math.pi / 180.0
RAD2DEG = 180.0 / math.pi
EARTH_R_KM = 6_371.0          # 지구 반지름 (km)
KM_PER_DEG_LAT = 111.32       # 위도 1° ≈ 111.32 km
NM_TO_KM = 1.852              # 1 해리 = 1.852 km
KNOTS_TO_KMS = NM_TO_KM / 3600.0  # 1 knot -> km/s


@dataclass
class ShipState:
    """선박 상태 벡터"""
    lon: float = 0.0           # 경도 (°)
    lat: float = 0.0           # 위도 (°)
    heading: float = 0.0       # 선수방위 (° true north, 0=N, 90=E)
    speed_knots: float = 14.0  # 대수속력 (knots)
    target_speed: float = 14.0 # 목표 속력 (knots)


@dataclass
class ShipParams:
    """선박 역학 파라미터"""
    max_speed_knots: float = 15.0      # 최대 속력
    min_speed_knots: float = 3.0       # 최소 속력 (정지 불가, 조종 가능 최저)
    max_turn_rate_deg_s: float = 1.5   # 최대 선회율 (°/s) -- 대형 화물선 기준
    speed_accel_knots_s: float = 0.02  # 가속도 (knots/s)
    speed_decel_knots_s: float = 0.05  # 감속도 (knots/s) -- 감속이 더 빠름
    turn_rate_speed_factor: float = 0.7  # 저속 시 선회 성능 감소 계수
    ice_drag_factor: float = 0.4       # 해빙 농도 1.0일 때 속도 감소 비율


def normalize_angle(deg: float) -> float:
    """각도를 -180 ~ +180 범위로 정규화"""
    deg = deg % 360.0
    if deg > 180.0:
        deg -= 360.0
    return deg


def km_per_deg_lon(lat: float) -> float:
    """특정 위도에서 경도 1°의 km 거리"""
    return KM_PER_DEG_LAT * math.cos(lat * DEG2RAD)


def approx_dist_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 지점 간 Equirectangular 근사 거리 (km)"""
    d_lat = (lat2 - lat1) * KM_PER_DEG_LAT
    d_lon = (lon2 - lon1) * km_per_deg_lon((lat1 + lat2) / 2.0)
    return math.sqrt(d_lat * d_lat + d_lon * d_lon)


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """지점 1에서 지점 2로의 방위 (° true north)"""
    d_lon = (lon2 - lon1) * km_per_deg_lon((lat1 + lat2) / 2.0)
    d_lat = (lat2 - lat1) * KM_PER_DEG_LAT
    return normalize_angle(math.atan2(d_lon, d_lat) * RAD2DEG)


def step_ship(state: ShipState, params: ShipParams,
              heading_delta_deg: float, speed_factor: float,
              ice_concentration: float, dt: float) -> ShipState:
    """
    한 타임스텝만큼 선박 상태를 전진시킵니다.

    Args:
        state:              현재 선박 상태
        params:             선박 역학 파라미터
        heading_delta_deg:  RL 에이전트가 결정한 방향 변화 (°, + = 우현)
        speed_factor:       RL 에이전트가 결정한 속도 계수 (0.5 ~ 1.0)
        ice_concentration:  현재 위치의 해빙 농도 (0.0 ~ 1.0)
        dt:                 타임스텝 (초)

    Returns:
        갱신된 ShipState
    """
    # 1. 속도 업데이트
    #    목표 속도 = 최대 속도 * speed_factor * 해빙 저항
    ice_drag = 1.0 - params.ice_drag_factor * min(ice_concentration, 1.0)
    target = params.max_speed_knots * speed_factor * ice_drag
    target = max(params.min_speed_knots, min(params.max_speed_knots, target))

    speed = state.speed_knots
    if speed < target:
        speed = min(target, speed + params.speed_accel_knots_s * dt)
    else:
        speed = max(target, speed - params.speed_decel_knots_s * dt)

    # 2. 선회 업데이트
    #    선회율은 속도에 비례 (저속 시 조종 어려움)
    speed_ratio = speed / params.max_speed_knots
    effective_turn_rate = params.max_turn_rate_deg_s * (
        params.turn_rate_speed_factor + (1.0 - params.turn_rate_speed_factor) * speed_ratio
    )
    # 요청된 선회를 dt 내에 가능한 범위로 클램프
    max_turn = effective_turn_rate * dt
    actual_turn = max(-max_turn, min(max_turn, heading_delta_deg))
    heading = normalize_angle(state.heading + actual_turn)

    # 3. 위치 업데이트 (Equirectangular 전진)
    speed_km_s = speed * KNOTS_TO_KMS
    dist_km = speed_km_s * dt

    heading_rad = heading * DEG2RAD
    d_north_km = dist_km * math.cos(heading_rad)
    d_east_km = dist_km * math.sin(heading_rad)

    d_lat = d_north_km / KM_PER_DEG_LAT
    cos_lat = max(0.01, math.cos(state.lat * DEG2RAD))
    d_lon = d_east_km / (KM_PER_DEG_LAT * cos_lat)

    lat = state.lat + d_lat
    lon = state.lon + d_lon

    # 극점/경도 래핑 처리
    lat = max(-89.9, min(89.9, lat))
    if lon > 180.0:
        lon -= 360.0
    elif lon < -180.0:
        lon += 360.0

    return ShipState(
        lon=lon,
        lat=lat,
        heading=heading,
        speed_knots=speed,
        target_speed=target,
    )


def project_future_positions(
    state: ShipState,
    params: ShipParams,
    heading_deltas: list[float],
    speed_factors: list[float],
    ice_concentrations: list[float],
    dt: float,
) -> list[tuple[float, float]]:
    """
    연속된 행동을 적용하여 미래 위치 시퀀스를 생성합니다.
    RL 추론 결과를 웨이포인트 시퀀스로 변환할 때 사용.

    Returns:
        [(lon, lat), ...] 리스트
    """
    positions = []
    s = ShipState(
        lon=state.lon, lat=state.lat,
        heading=state.heading, speed_knots=state.speed_knots,
        target_speed=state.target_speed,
    )
    n = len(heading_deltas)
    for i in range(n):
        ice_c = ice_concentrations[i] if i < len(ice_concentrations) else 0.0
        s = step_ship(s, params, heading_deltas[i], speed_factors[i], ice_c, dt)
        positions.append((s.lon, s.lat))
    return positions
