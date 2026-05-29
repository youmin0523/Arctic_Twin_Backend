"""
rl_environment.py -- Gymnasium 환경

빙산 회피를 위한 강화학습 환경.
- 선박이 경로의 일부 구간을 항행하며 빙산을 회피
- 상태: 22차원 (선박 상태 + 빙산 + 해빙 + 날씨 + 경로)
- 행동: 2차원 연속 (방향 변화, 속도 계수)
- 에피소드 종료: 위험 구간 통과, 충돌, 이탈, 타임아웃
"""
from __future__ import annotations

import math
import random
from typing import Any

import numpy as np
import gymnasium as gym
from gymnasium import spaces

from .rl_ship_dynamics import (
    ShipState, ShipParams, step_ship,
    approx_dist_km, bearing_deg, normalize_angle,
    KM_PER_DEG_LAT, km_per_deg_lon,
)
from .rl_reward import (
    RewardContext, RewardWeights, compute_reward,
    compute_dynamic_safety_radius,
)
from .rl_land_mask import LandMask
from .config import ROUTE_WAYPOINTS, MAX_SAFE_CONCENTRATION


# ── 빙산 데이터 구조 ──────────────────────────────────────
class Iceberg:
    __slots__ = ("lat", "lon", "length_m", "width_m")

    def __init__(self, lat: float, lon: float, length_m: float = 5000.0, width_m: float = 3000.0):
        self.lat = lat
        self.lon = lon
        self.length_m = length_m
        self.width_m = width_m


# ── 빙산 생성 유틸 ────────────────────────────────────────
def _random_icebergs_along_segment(
    lat1: float, lon1: float, lat2: float, lon2: float,
    count: int, spread_km: float = 30.0,
) -> list[Iceberg]:
    """경로 구간 주변에 랜덤 빙산 생성"""
    bergs = []
    for _ in range(count):
        t = random.random()
        center_lat = lat1 + t * (lat2 - lat1)
        center_lon = lon1 + t * (lon2 - lon1)
        offset_lat = random.gauss(0, spread_km / KM_PER_DEG_LAT / 3)
        offset_lon = random.gauss(0, spread_km / max(1, km_per_deg_lon(center_lat)) / 3)
        size_type = random.choices(
            ["small", "medium", "large", "tabular"],
            weights=[0.4, 0.3, 0.2, 0.1],
        )[0]
        sizes = {
            "small": (random.uniform(25, 80), random.uniform(15, 50)),
            "medium": (random.uniform(80, 200), random.uniform(50, 120)),
            "large": (random.uniform(200, 500), random.uniform(100, 300)),
            "tabular": (random.uniform(500, 2000), random.uniform(300, 1000)),
        }
        length_m, width_m = sizes[size_type]
        bergs.append(Iceberg(
            lat=center_lat + offset_lat,
            lon=center_lon + offset_lon,
            length_m=length_m,
            width_m=width_m,
        ))
    return bergs


# ── 교차 트랙 오류 계산 ──────────────────────────────────
def _cross_track_error(ship_lat: float, ship_lon: float,
                       wp1: tuple, wp2: tuple) -> float:
    """선박 위치에서 경로 구간까지의 교차 트랙 거리 (km, 부호 있음)"""
    lat1, lon1 = wp1
    lat2, lon2 = wp2
    dAB_n = (lat2 - lat1) * KM_PER_DEG_LAT
    dAB_e = (lon2 - lon1) * km_per_deg_lon((lat1 + lat2) / 2)
    dAP_n = (ship_lat - lat1) * KM_PER_DEG_LAT
    dAP_e = (ship_lon - lon1) * km_per_deg_lon((lat1 + ship_lat) / 2)
    AB_len = math.sqrt(dAB_n ** 2 + dAB_e ** 2)
    if AB_len < 1e-6:
        return approx_dist_km(ship_lat, ship_lon, lat1, lon1)
    return (dAP_e * dAB_n - dAP_n * dAB_e) / AB_len


def _along_track_fraction(ship_lat: float, ship_lon: float,
                          wp1: tuple, wp2: tuple) -> float:
    """선박 위치의 구간 내 진행 비율 (0~1)"""
    lat1, lon1 = wp1
    lat2, lon2 = wp2
    dAB_n = (lat2 - lat1) * KM_PER_DEG_LAT
    dAB_e = (lon2 - lon1) * km_per_deg_lon((lat1 + lat2) / 2)
    dAP_n = (ship_lat - lat1) * KM_PER_DEG_LAT
    dAP_e = (ship_lon - lon1) * km_per_deg_lon((lat1 + ship_lat) / 2)
    AB2 = dAB_n ** 2 + dAB_e ** 2
    if AB2 < 1e-6:
        return 0.0
    return max(0.0, min(1.0, (dAP_n * dAB_n + dAP_e * dAB_e) / AB2))


# ── Gymnasium 환경 ────────────────────────────────────────
class IcebergAvoidanceEnv(gym.Env):
    """
    빙산 회피 Gymnasium 환경.

    에피소드:
      1) 랜덤 항로/구간/빙급 선택
      2) 경로 전방에 3~15개 빙산 배치
      3) 선박이 위험 구간 시작점에서 출발
      4) 위험 구간 종료점 도달 시 성공 종료
      5) 충돌/이탈/타임아웃 시 실패 종료
    """

    metadata = {"render_modes": ["human"]}

    MAX_STEPS = 3000     # 에피소드 최대 스텝 (DT=2s 기준 100분)
    DT = 2.0             # 타임스텝 (초)
    MAX_DEVIATION_KM = 50.0
    COLLISION_RADIUS_KM = 0.5
    MAX_NEARBY_ICEBERGS = 3
    SEGMENT_MAX_DIST_KM = 80.0   # [수정] 100km → 80km: 3000스텝(100분)×0.051km/step=153km이나
                                  # 우회 여유분 고려, 직선거리 80km가 현실적 완주 가능 범위
    SUCCESS_PROGRESS = 0.90       # [수정] 0.98 → 0.90: 더 달성 가능한 성공 기준

    def __init__(self, render_mode=None, difficulty: str = "medium",
                 reward_weights: RewardWeights | None = None,
                 fixed_route: str | None = None,
                 fixed_ice_class: str | None = None,
                 ship_params: ShipParams | None = None):
        super().__init__()
        self.render_mode = render_mode
        self.difficulty = difficulty
        self._fixed_route = fixed_route          # None이면 에피소드마다 랜덤
        self._fixed_ice_class = fixed_ice_class  # None이면 에피소드마다 랜덤
        self._custom_ship_params = ship_params   # None이면 기본 ShipParams()

        self.action_space = spaces.Box(
            low=np.array([-15.0, 0.5], dtype=np.float32),
            high=np.array([15.0, 1.0], dtype=np.float32),
        )
        self.observation_space = spaces.Box(
            low=-np.ones(22, dtype=np.float32) * 2.0,
            high=np.ones(22, dtype=np.float32) * 2.0,
        )

        self.ship: ShipState | None = None
        self.ship_params = self._custom_ship_params if self._custom_ship_params is not None else ShipParams()
        self.reward_weights = reward_weights if reward_weights is not None else RewardWeights()
        self.icebergs: list[Iceberg] = []
        self.route_wps: list[tuple[float, float]] = []
        self.segment_start_idx: int = 0
        self.segment_end_idx: int = 0
        self.ice_class: str = "PC5"
        self.max_safe_conc: float = 0.7
        self.ice_concentration: float = 0.0
        self.visibility_km: float = 10.0
        self.wave_height_m: float = 1.0
        self.step_count: int = 0
        self.prev_progress: float = 0.0
        self.land_mask = LandMask()

    def _get_difficulty_params(self) -> dict:
        if self.difficulty == "easy":
            return dict(berg_count=(0, 0), ice_conc=(0.0, 0.0),
                        visibility=(15, 20), wave=(0.0, 0.5))
        elif self.difficulty == "medium":
            return dict(berg_count=(1, 4), ice_conc=(0.0, 0.3),
                        visibility=(5, 15), wave=(0.5, 2.0))
        else:
            return dict(berg_count=(4, 10), ice_conc=(0.2, 0.6),
                        visibility=(2, 8), wave=(1.0, 4.0))

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)

        route_key = self._fixed_route if self._fixed_route else random.choice(list(ROUTE_WAYPOINTS.keys()))
        self.route_wps = ROUTE_WAYPOINTS[route_key]

        n = len(self.route_wps)
        # 구간 거리가 SEGMENT_MAX_DIST_KM 이하가 되도록 start/end 선택
        # → 14knots × 100분 ≈ 140km 이내 완주 가능
        for _attempt in range(20):
            seg_len = random.randint(1, min(3, n - 1))
            start_idx = random.randint(0, n - seg_len - 1)
            end_idx = start_idx + seg_len
            seg_dist = sum(
                approx_dist_km(
                    self.route_wps[i][0], self.route_wps[i][1],
                    self.route_wps[i+1][0], self.route_wps[i+1][1]
                )
                for i in range(start_idx, end_idx)
            )
            if seg_dist <= self.SEGMENT_MAX_DIST_KM:
                break
        self.segment_start_idx = start_idx
        self.segment_end_idx = end_idx

        self.ice_class = self._fixed_ice_class if self._fixed_ice_class else random.choice(["PC3", "PC5", "PC7", "IA Super", "IA"])
        self.max_safe_conc = MAX_SAFE_CONCENTRATION.get(self.ice_class, 0.7)

        dp = self._get_difficulty_params()
        berg_count = random.randint(*dp["berg_count"])
        self.ice_concentration = random.uniform(*dp["ice_conc"])
        self.visibility_km = random.uniform(*dp["visibility"])
        self.wave_height_m = random.uniform(*dp["wave"])

        self.icebergs = []
        if berg_count > 0:
            for i in range(self.segment_start_idx, self.segment_end_idx):
                wp1 = self.route_wps[i]
                wp2 = self.route_wps[i + 1]
                count_in_seg = max(1, berg_count // seg_len)
                self.icebergs.extend(_random_icebergs_along_segment(
                    wp1[0], wp1[1], wp2[0], wp2[1],
                    count_in_seg, spread_km=20.0,
                ))

        start_wp = self.route_wps[self.segment_start_idx]
        next_wp = self.route_wps[self.segment_start_idx + 1]
        initial_heading = bearing_deg(start_wp[0], start_wp[1], next_wp[0], next_wp[1])

        self.ship = ShipState(
            lon=start_wp[1], lat=start_wp[0],
            heading=initial_heading,
            speed_knots=14.0, target_speed=14.0,
        )

        self.step_count = 0
        self.prev_progress = 0.0

        return self._get_obs(), {}

    def _get_progress(self) -> float:
        # 각 sub-segment별 거리와 along-track fraction 수집
        seg_dists: list[float] = []
        seg_fracs: list[float] = []
        total_dist = 0.0
        for i in range(self.segment_start_idx, self.segment_end_idx):
            wp1 = self.route_wps[i]
            wp2 = self.route_wps[i + 1]
            d = float(approx_dist_km(wp1[0], wp1[1], wp2[0], wp2[1]))
            frac = float(_along_track_fraction(self.ship.lat, self.ship.lon, wp1, wp2))
            seg_dists.append(d)
            seg_fracs.append(frac)
            total_dist += d

        if total_dist < 1e-3:
            return 0.0

        # [수정] 각 segment에서 달성 가능한 절대 진행 거리를 계산해
        # 가장 많이 전진한 segment 기준으로 progress를 결정
        # (기존: cross-track 비교 시 abs 누락 버그 → progress가 역행하는 문제 수정)
        best_cum = 0.0
        cum_before = 0.0
        n_segs = len(seg_dists)
        for k in range(n_segs):
            candidate = cum_before + seg_fracs[k] * seg_dists[k]
            if candidate > best_cum:
                best_cum = candidate
            cum_before += seg_dists[k]

        return min(1.0, best_cum / total_dist)

    def _get_cross_track(self) -> float:
        best_xt = float("inf")
        for i in range(self.segment_start_idx, self.segment_end_idx):
            wp1 = self.route_wps[i]
            wp2 = self.route_wps[i + 1]
            xt = _cross_track_error(self.ship.lat, self.ship.lon, wp1, wp2)
            if abs(xt) < abs(best_xt):
                best_xt = xt
        return best_xt

    def _nearest_icebergs(self, n: int = 3) -> list[tuple[float, float, float]]:
        dists = []
        for berg in self.icebergs:
            d = approx_dist_km(self.ship.lat, self.ship.lon, berg.lat, berg.lon)
            b = bearing_deg(self.ship.lat, self.ship.lon, berg.lat, berg.lon)
            rel_bearing = normalize_angle(b - self.ship.heading)
            dists.append((rel_bearing, d, berg.length_m))
        dists.sort(key=lambda x: x[1])
        result = dists[:n]
        while len(result) < n:
            result.append((0.0, 999.0, 0.0))
        return result

    def _get_obs(self) -> np.ndarray:
        obs = np.zeros(22, dtype=np.float32)

        obs[0] = self.ship.lon / 180.0
        obs[1] = self.ship.lat / 90.0

        h_rad = self.ship.heading * math.pi / 180.0
        obs[2] = math.sin(h_rad)
        obs[3] = math.cos(h_rad)

        obs[4] = self.ship.speed_knots / self.ship_params.max_speed_knots

        progress = self._get_progress()
        target_idx = min(self.segment_end_idx, self.segment_start_idx + 1)
        for i in range(self.segment_start_idx + 1, self.segment_end_idx + 1):
            wp = self.route_wps[i]
            frac = _along_track_fraction(
                self.ship.lat, self.ship.lon,
                self.route_wps[i - 1], wp)
            if frac < 0.95:
                target_idx = i
                break
        target_wp = self.route_wps[min(target_idx, len(self.route_wps) - 1)]

        obs[5] = (target_wp[1] - self.ship.lon) * km_per_deg_lon(self.ship.lat) / 100.0
        obs[6] = (target_wp[0] - self.ship.lat) * KM_PER_DEG_LAT / 100.0
        dist_to_wp = approx_dist_km(self.ship.lat, self.ship.lon, target_wp[0], target_wp[1])
        bearing_to_wp = bearing_deg(self.ship.lat, self.ship.lon, target_wp[0], target_wp[1])
        rel_bearing_wp = normalize_angle(bearing_to_wp - self.ship.heading)
        obs[7] = rel_bearing_wp / 180.0
        obs[8] = min(1.0, dist_to_wp / 200.0)

        nearest = self._nearest_icebergs(self.MAX_NEARBY_ICEBERGS)
        for i, (rel_b, dist, size) in enumerate(nearest):
            obs[9 + i * 2] = rel_b / 180.0
            obs[10 + i * 2] = min(1.0, dist / 50.0)

        obs[15] = min(1.0, self.ice_concentration)
        obs[16] = min(1.0, self.max_safe_conc)  # 빙급별 허용 최대 농도 (obs[15]와 구분)
        obs[17] = min(1.0, self.visibility_km / 20.0)
        obs[18] = min(1.0, self.wave_height_m / 8.0)
        obs[19] = self.max_safe_conc
        obs[20] = progress
        xt = self._get_cross_track()
        obs[21] = max(-1.0, min(1.0, xt / self.MAX_DEVIATION_KM))

        return obs

    def step(self, action: np.ndarray):
        heading_delta = float(np.clip(action[0], -15.0, 15.0))
        speed_factor = float(np.clip(action[1], 0.5, 1.0))

        self.ship = step_ship(
            self.ship, self.ship_params,
            heading_delta, speed_factor,
            self.ice_concentration, self.DT,
        )
        self.step_count += 1

        collision = False
        iceberg_dists = []
        iceberg_sizes = []
        for berg in self.icebergs:
            d = approx_dist_km(self.ship.lat, self.ship.lon, berg.lat, berg.lon)
            iceberg_dists.append(d)
            iceberg_sizes.append(berg.length_m)
            collision_r = max(self.COLLISION_RADIUS_KM, berg.length_m / 1000.0 / 2.0)
            if d < collision_r:
                collision = True
        
        # 육지 충돌 체크
        if not collision and self.land_mask.is_land(self.ship.lat, self.ship.lon):
            collision = True

        current_progress = self._get_progress()
        delta_progress = current_progress - self.prev_progress
        self.prev_progress = current_progress

        xt_km = abs(self._get_cross_track())

        terminated = False
        truncated = False
        success = False

        if collision:
            terminated = True
        elif current_progress >= self.SUCCESS_PROGRESS:  # [수정] 0.98 → 0.90
            terminated = True
            success = True
        elif xt_km > self.MAX_DEVIATION_KM:
            terminated = True
        elif self.step_count >= self.MAX_STEPS:
            truncated = True

        ctx = RewardContext(
            ship_lat=self.ship.lat,
            ship_lon=self.ship.lon,
            ship_speed_knots=self.ship.speed_knots,
            heading_change_deg=heading_delta,
            speed_factor=speed_factor,
            iceberg_distances_km=iceberg_dists,
            iceberg_sizes_m=iceberg_sizes,
            cross_track_error_km=xt_km,
            along_track_progress=delta_progress,
            max_allowed_deviation_km=self.MAX_DEVIATION_KM,
            ice_concentration=self.ice_concentration,
            max_safe_concentration=self.max_safe_conc,
            visibility_km=self.visibility_km,
            wave_height_m=self.wave_height_m,
            collision=collision,
            episode_done_success=success,
        )
        reward, reward_components = compute_reward(ctx, self.reward_weights)

        obs = self._get_obs()
        info = {
            "collision": collision,
            "success": success,
            "progress": current_progress,
            "cross_track_km": xt_km,
            "reward_components": reward_components,
            "step": self.step_count,
        }

        return obs, reward, terminated, truncated, info

    def get_ship_state(self) -> dict:
        return {
            "lon": self.ship.lon,
            "lat": self.ship.lat,
            "heading": self.ship.heading,
            "speed_knots": self.ship.speed_knots,
        }

    def get_icebergs(self) -> list[dict]:
        return [
            {"lat": b.lat, "lon": b.lon, "length_m": b.length_m, "width_m": b.width_m}
            for b in self.icebergs
        ]
