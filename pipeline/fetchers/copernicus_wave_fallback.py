#!/usr/bin/env python3
"""
Copernicus Marine Service - Wave Fallback

Open-Meteo Marine API가 커버하지 못하는 해역의 파 데이터를
Copernicus 제품으로 보충.

1차: Arctic Wave (3km, 1시간) — 고위도 북극 해역
2차: Global Wave (0.083°, 3시간) — 나머지 전역

Product (Arctic) : ARCTIC_ANALYSIS_FORECAST_WAV_002_014
Dataset (Arctic) : dataset-wam-arctic-1hr3km-be
Product (Global) : GLOBAL_ANALYSISFORECAST_WAV_001_027
Dataset (Global) : cmems_mod_glo_wav_anfc_0.083deg_PT3H-i_202411

Variables:
  VHM0  : Spectral significant wave height (m)
  VMDR  : Mean wave direction from (deg, 0=north)
  VTM02 : Spectral moments mean wave period (s)
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─── 설정 ────────────────────────────────────────────────────────────
ARCTIC_DATASET_ID = "dataset-wam-arctic-1hr3km-be"
GLOBAL_DATASET_ID = "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i"

WAVE_HEIGHT_VAR = "VHM0"
WAVE_DIRECTION_VAR = "VMDR"
WAVE_PERIOD_VAR = "VTM02"

WAVE_VARIABLES = [WAVE_HEIGHT_VAR, WAVE_DIRECTION_VAR, WAVE_PERIOD_VAR]


def _copernicus_available() -> bool:
    """Copernicus Marine 인증 상태 확인.

    헤드리스 서버(AWS)는 자격증명 파일 없이 환경변수로만 인증하므로
    env var(COPERNICUSMARINE_SERVICE_USERNAME/PASSWORD)도 함께 확인한다.
    (copernicus_fetcher.py 와 동일한 기준 — 파일만 확인하면 운영에서 폴백이 조용히 스킵됨)
    """
    if os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME") and \
       os.environ.get("COPERNICUSMARINE_SERVICE_PASSWORD"):
        return True
    cred_paths = [
        Path.home() / ".copernicusmarine" / ".copernicusmarine-credentials",
        Path.home() / ".copernicusmarine" / "credentials",
        Path.home() / ".motuclient-python" / "motuclient-python.ini",
    ]
    return any(p.exists() for p in cred_paths)


def _compute_bbox(wps: list[dict]) -> tuple[float, float, float, float]:
    """웨이포인트 목록에서 bounding box 계산 (안티메리디안 고려)."""
    lats = [wp["lat"] for wp in wps]
    lons = [wp["lon"] for wp in wps]

    min_lat = min(lats) - 0.5
    max_lat = max(lats) + 0.5

    has_pos = any(ln > 0 for ln in lons)
    has_neg = any(ln < 0 for ln in lons)
    if has_pos and has_neg and (max(lons) - min(lons)) > 180:
        min_lon, max_lon = -180.0, 180.0
    else:
        min_lon = min(lons) - 0.5
        max_lon = max(lons) + 0.5

    return min_lat, max_lat, min_lon, max_lon


def _open_wave_dataset(dataset_id: str, wps: list[dict], target_dt: datetime):
    """Copernicus Wave 데이터셋 오픈 — 파고·파향·파주기 3변수."""
    import copernicusmarine

    min_lat, max_lat, min_lon, max_lon = _compute_bbox(wps)
    time_str = target_dt.strftime("%Y-%m-%dT%H:%M:%S")

    return copernicusmarine.open_dataset(
        dataset_id=dataset_id,
        variables=WAVE_VARIABLES,
        minimum_latitude=min_lat,
        maximum_latitude=max_lat,
        minimum_longitude=min_lon,
        maximum_longitude=max_lon,
        start_datetime=time_str,
        end_datetime=time_str,
    )


def _nearest_scalar(ds, var_name: str, lat: float, lon: float,
                    allow_negative: bool = False) -> float | None:
    """데이터셋에서 (lat, lon) 최근접 격자점의 변수값 추출."""
    import numpy as np

    if var_name not in ds.variables:
        return None
    try:
        val = ds[var_name].sel(latitude=lat, longitude=lon, method="nearest")
        if "time" in val.dims:
            val = val.isel(time=-1)
        v = float(val.values)
        if not np.isfinite(v):
            return None
        if not allow_negative and v < 0:
            return None
        return v
    except Exception:
        return None


def _fill_from_dataset(dataset_id: str, label: str,
                       wp_results: list[dict], null_indices: list[int],
                       target_dt: datetime) -> list[int]:
    """주어진 데이터셋으로 null 웨이포인트 보충. 여전히 height null인 인덱스 반환.

    파고(height)가 null이었던 지점에 대해 height·direction·period를 함께 채운다.
    이미 direction·period만 null이고 height가 있던 지점은 대상에서 제외 (caller가 결정).
    """
    null_wps = [wp_results[i] for i in null_indices]
    try:
        print(f"    [{label}] querying {len(null_wps)} null waypoints...")
        ds = _open_wave_dataset(dataset_id, null_wps, target_dt)

        filled_h = 0
        filled_d = 0
        filled_p = 0
        still_null = []
        for i in null_indices:
            wp = wp_results[i]
            h = _nearest_scalar(ds, WAVE_HEIGHT_VAR, wp["lat"], wp["lon"])
            d = _nearest_scalar(ds, WAVE_DIRECTION_VAR, wp["lat"], wp["lon"],
                                allow_negative=False)
            p = _nearest_scalar(ds, WAVE_PERIOD_VAR, wp["lat"], wp["lon"])

            if h is not None:
                wp["wave_height_m"] = round(h, 2)
                wp["wave_source"] = "copernicus"
                filled_h += 1
            else:
                still_null.append(i)

            if d is not None and wp.get("wave_direction_deg") is None:
                wp["wave_direction_deg"] = round(d % 360.0, 1)
                filled_d += 1
            if p is not None and wp.get("wave_period_s") is None and p > 0:
                wp["wave_period_s"] = round(p, 2)
                filled_p += 1

        ds.close()
        print(
            f"    [{label}] filled height={filled_h}/{len(null_wps)}, "
            f"direction={filled_d}, period={filled_p}"
        )
        return still_null
    except Exception as e:
        print(f"    [WARN] {label} failed: {e}")
        return null_indices


def fill_wave_heights(wp_results: list[dict]) -> list[dict]:
    """Open-Meteo에서 null인 웨이포인트의 파 데이터를 Copernicus로 보충.

    파고(height)가 null인 지점에 대해 Arctic → Global 순서로 fallback.
    동시에 파향(direction)·파주기(period)도 채운다 (해당 지점에 null일 경우).

    1차: Arctic Wave (고위도 전용, 3km 고해상도)
    2차: Global Wave (전역, 0.083° 해상도) — 1차에서 못 채운 지점
    실패 시 원본을 그대로 반환.
    """
    null_indices = [i for i, wp in enumerate(wp_results)
                    if wp.get("wave_height_m") is None]
    if not null_indices:
        return wp_results

    try:
        import copernicusmarine  # noqa: F401
    except ImportError:
        print("    [WARN] copernicusmarine not installed - skipping wave fallback")
        return wp_results

    if not _copernicus_available():
        print("    [WARN] Copernicus credentials missing - skipping wave fallback")
        return wp_results

    target_dt = datetime.now(timezone.utc) - timedelta(hours=3)

    # 1차: Arctic Wave (lat >= 41°N 커버리지)
    arctic_indices = [i for i in null_indices if wp_results[i]["lat"] >= 41.0]
    other_indices = [i for i in null_indices if wp_results[i]["lat"] < 41.0]

    still_null = []
    if arctic_indices:
        still_null = _fill_from_dataset(
            ARCTIC_DATASET_ID, "Copernicus Arctic Wave",
            wp_results, arctic_indices, target_dt,
        )
    still_null.extend(other_indices)

    # 2차: Global Wave — 아직 null인 지점 보충
    if still_null:
        still_null = _fill_from_dataset(
            GLOBAL_DATASET_ID, "Copernicus Global Wave",
            wp_results, still_null, target_dt,
        )

    return wp_results
