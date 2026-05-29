#!/usr/bin/env python3
"""
Copernicus Marine Service - Sea Surface Temperature (SST) Fetcher

각 웨이포인트의 해수면 온도(SST)를 Copernicus 해양물리 제품에서 조회.
기온(temperature_2m)과 SST 차이로 해무 위험도를 추정할 수 있음.

1차: Arctic Physics (고위도, 6.25km) — 북극 해역
2차: Global Physics (전역, 0.083°)  — 나머지 전역

Product (Arctic) : ARCTIC_ANALYSISFORECAST_PHY_002_001
Dataset (Arctic) : cmems_mod_arc_phy_anfc_6km_detided_P1D-m
Product (Global) : GLOBAL_ANALYSISFORECAST_PHY_001_024
Dataset (Global) : cmems_mod_glo_phy-thetao_anfc_0.083deg_P1D-m
Variable         : thetao (Sea water potential temperature, °C)
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path


# ─── 설정 ────────────────────────────────────────────────────────────
ARCTIC_DATASET_ID = "cmems_mod_arc_phy_anfc_6km_detided_P1D-m"
GLOBAL_DATASET_ID = "cmems_mod_glo_phy-thetao_anfc_0.083deg_P1D-m"
SST_VARIABLE = "thetao"


def _copernicus_available() -> bool:
    """Copernicus Marine 인증 상태 확인."""
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


def _open_sst_dataset(dataset_id: str, wps: list[dict], target_dt: datetime):
    """Copernicus SST 데이터셋 오픈 (lazy loading)."""
    import copernicusmarine

    min_lat, max_lat, min_lon, max_lon = _compute_bbox(wps)
    time_str = target_dt.strftime("%Y-%m-%dT%H:%M:%S")

    return copernicusmarine.open_dataset(
        dataset_id=dataset_id,
        variables=[SST_VARIABLE],
        minimum_latitude=min_lat,
        maximum_latitude=max_lat,
        minimum_longitude=min_lon,
        maximum_longitude=max_lon,
        start_datetime=time_str,
        end_datetime=time_str,
    )


def _nearest_sst(ds, lat: float, lon: float) -> float | None:
    """데이터셋에서 (lat, lon) 최근접 격자점의 SST 값 추출."""
    import numpy as np

    try:
        val = ds[SST_VARIABLE].sel(
            latitude=lat, longitude=lon, method="nearest"
        )
        if "time" in val.dims:
            val = val.isel(time=-1)
        if "depth" in val.dims:
            val = val.isel(depth=0)
        v = float(val.values)
        if np.isfinite(v) and -5.0 <= v <= 45.0:
            return round(v, 1)
    except Exception:
        pass
    return None


def _fill_from_dataset(dataset_id: str, label: str,
                       wp_results: list[dict], target_indices: list[int],
                       target_dt: datetime) -> list[int]:
    """주어진 데이터셋으로 SST 조회. 실패한 인덱스 반환."""
    target_wps = [wp_results[i] for i in target_indices]
    try:
        print(f"    [{label}] querying SST for {len(target_wps)} waypoints...")
        ds = _open_sst_dataset(dataset_id, target_wps, target_dt)

        filled = 0
        still_null = []
        for i in target_indices:
            wp = wp_results[i]
            val = _nearest_sst(ds, wp["lat"], wp["lon"])
            if val is not None:
                wp["sst_c"] = val
                filled += 1
            else:
                still_null.append(i)

        ds.close()
        print(f"    [{label}] filled {filled}/{len(target_wps)} waypoints")
        return still_null
    except Exception as e:
        print(f"    [WARN] {label} SST failed: {e}")
        return target_indices


def fill_sst(wp_results: list[dict]) -> list[dict]:
    """전체 웨이포인트의 SST를 Copernicus에서 조회.

    1차: Arctic Physics (lat >= 60°N, 6.25km 고해상도)
    2차: Global Physics (전역, 0.083° 해상도) — 1차에서 못 채운 지점
    실패 시 sst_c = None 그대로 반환.
    """
    all_indices = list(range(len(wp_results)))

    # sst_c 필드 초기화
    for wp in wp_results:
        if "sst_c" not in wp:
            wp["sst_c"] = None

    try:
        import copernicusmarine  # noqa: F401
    except ImportError:
        print("    [WARN] copernicusmarine not installed - skipping SST fetch")
        return wp_results

    if not _copernicus_available():
        print("    [WARN] Copernicus credentials missing - skipping SST fetch")
        return wp_results

    target_dt = datetime.now(timezone.utc) - timedelta(days=1)

    # 1차: Arctic Physics (lat >= 60°N)
    arctic_indices = [i for i in all_indices if wp_results[i]["lat"] >= 60.0]
    other_indices = [i for i in all_indices if wp_results[i]["lat"] < 60.0]

    still_null = []
    if arctic_indices:
        still_null = _fill_from_dataset(
            ARCTIC_DATASET_ID, "Copernicus Arctic SST",
            wp_results, arctic_indices, target_dt,
        )
    still_null.extend(other_indices)

    # 2차: Global Physics — 나머지 전역
    if still_null:
        still_null = _fill_from_dataset(
            GLOBAL_DATASET_ID, "Copernicus Global SST",
            wp_results, still_null, target_dt,
        )

    return wp_results
