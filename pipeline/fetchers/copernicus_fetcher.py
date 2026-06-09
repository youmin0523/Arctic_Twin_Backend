#!/usr/bin/env python3
"""
Copernicus Marine Service - Arctic Sea Ice 자동 수집 파이프라인

최초 실행 전:
  1. pip install copernicusmarine
  2. 계정 가입: https://data.marine.copernicus.eu/register (무료)
  3. 인증 설정: copernicusmarine login
     → ~/.copernicusmarine/.copernicusmarine-credentials 에 저장됨

사용법:
  python copernicus_fetcher.py                   # 최신 데이터 1회 수집
  python copernicus_fetcher.py --schedule        # 매일 06:00 UTC 자동 실행
  python copernicus_fetcher.py --date 2025-03-20 # 특정 날짜 수집
  python copernicus_fetcher.py --dry-run         # 실제 다운로드 없이 설정만 확인

출력: output/realIceData_latest.json (+ output/archive/daily/realIceData_YYYYMMDD.json)
"""

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path


def _atomic_write_json(path, data, **dump_kwargs) -> None:
    """임시파일로 쓴 뒤 os.replace로 원자적 교체 (디스크풀 시 기존 파일 보존)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# ─── 로깅 설정 ────────────────────────────────────────────────────
LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "ice_fetcher.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ice_fetcher")

# ─── 설정 ────────────────────────────────────────────────────────────
VARIABLE = "sea_ice_area_fraction"
# 대체 변수명 후보 (데이터셋 버전에 따라 다를 수 있음)
VARIABLE_CANDIDATES = [
    "sea_ice_area_fraction",
    "siconc",
    "ice_concentration",
    "sic",
]

# ── 반구별 데이터셋 설정 ─────────────────────────────────────────────────
# 북극: Arctic 전용 6km 분석예보 product.
# 남극: 남극 전용 product 부재 → 전지구 물리예보(GLOBAL_ANALYSISFORECAST)의
#   siconc 변수를 남위 -50° 이남으로 잘라 사용(아라온 ROSS/PENINSULA 항로 커버).
HEMISPHERES = {
    "north": {
        "dataset_id": "cmems_mod_arc_phy_anfc_6km_detided_P1D-m",
        "product_id": "ARCTIC_ANALYSISFORECAST_PHY_ICE_002_011",
        "lat_min": 60.0,
        "lat_max": 90.0,
        "out_latest": "realIceData_latest.json",
    },
    "south": {
        "dataset_id": "cmems_mod_glo_phy_anfc_0.083deg_P1D-m",
        "product_id": "GLOBAL_ANALYSISFORECAST_PHY_001_024",
        "lat_min": -90.0,
        "lat_max": -50.0,
        "out_latest": "realIceData_south_latest.json",
    },
}

# 하위호환: 기존 참조(DATASET_ID/PRODUCT_ID/MIN_LAT)는 북극 기본값으로 유지.
DATASET_ID = HEMISPHERES["north"]["dataset_id"]
PRODUCT_ID = HEMISPHERES["north"]["product_id"]
MIN_LAT = HEMISPHERES["north"]["lat_min"]
STEP = 3  # 6km * 3 = 18km 간격 다운샘플링 (파일 크기 관리)
MIN_CONC = 0.05
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
ARCHIVE_DIR = os.path.join(OUTPUT_DIR, "archive", "daily")


def check_credentials():
    """Copernicus Marine 인증 상태 확인 (env var 또는 파일)."""
    # Node.js 스케줄러가 주입하는 env var 방식
    if os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME"):
        return True
    # CLI login으로 저장된 파일 방식
    cred_paths = [
        Path.home() / ".copernicusmarine" / ".copernicusmarine-credentials",
        Path.home() / ".copernicusmarine" / "credentials",
        Path.home() / ".motuclient-python" / "motuclient-python.ini",
    ]
    for p in cred_paths:
        if p.exists():
            return True
    return False


def print_setup_guide():
    """최초 설정 가이드 출력."""
    guide = """
============================================================
  Copernicus Marine Service 계정 설정 필요
============================================================

1. 무료 가입:
   https://data.marine.copernicus.eu/register

2. 인증 설정 (터미널에서 실행):
   copernicusmarine login

3. 이후 이 스크립트를 다시 실행하세요.

참고: 상업 사용 허용, 출처 표기 필수
  "Generated using Copernicus Marine Service information"
============================================================
"""
    print(guide)


def find_variable(ds):
    """데이터셋에서 해빙 농도 변수를 자동 탐지."""
    for name in VARIABLE_CANDIDATES:
        if name in ds.data_vars:
            return name
    for name in ds.data_vars:
        nl = name.lower()
        if "ice" in nl and ("frac" in nl or "conc" in nl or "area" in nl):
            return name
    raise ValueError(
        f"Sea ice variable not found. Available: {list(ds.data_vars)}"
    )


def fetch_copernicus(target_date=None, step=STEP, dry_run=False, hemisphere="north"):
    """Copernicus Marine Service에서 해빙 데이터 수집.

    hemisphere="north"(기본, 북극) | "south"(남극). 남극은 전지구 product 의
    siconc 를 남위 -50° 이남으로 잘라 사용.
    """
    cfg = HEMISPHERES.get(hemisphere, HEMISPHERES["north"])
    dataset_id = cfg["dataset_id"]
    product_id = cfg["product_id"]
    lat_min = cfg["lat_min"]
    lat_max = cfg["lat_max"]

    try:
        import copernicusmarine
    except ImportError:
        log.error("copernicusmarine not installed. Run: pip install copernicusmarine")
        sys.exit(1)

    if not check_credentials():
        print_setup_guide()
        sys.exit(1)

    import numpy as np

    # 날짜 결정
    if target_date:
        dt = datetime.strptime(target_date, "%Y-%m-%d")
    else:
        # 최신 데이터는 보통 1~2일 전까지 가용
        dt = datetime.utcnow() - timedelta(days=1)

    date_str = dt.strftime("%Y-%m-%d")
    date_compact = dt.strftime("%Y%m%d")
    log.info(f"[{hemisphere}] Target date: {date_str}")

    if dry_run:
        log.info(f"[DRY-RUN][{hemisphere}] dataset={dataset_id} variable={VARIABLE} "
                 f"date={date_str} lat=[{lat_min},{lat_max}] step={step}(~{6*step}km)")
        return None

    # 데이터셋 열기
    log.info(f"Opening dataset: {dataset_id}")
    try:
        ds = copernicusmarine.open_dataset(
            dataset_id=dataset_id,
            variables=[VARIABLE],
            minimum_latitude=lat_min,
            maximum_latitude=lat_max,
            minimum_longitude=-180,
            maximum_longitude=180,
            start_datetime=f"{date_str}T00:00:00",
            end_datetime=f"{date_str}T23:59:59",
        )
    except Exception as e:
        err_msg = str(e)
        # 변수명이 달라서 실패했을 수 있음 — 변수 지정 없이 재시도
        log.warning(f"First attempt failed: {err_msg}")
        log.info("Retrying without variable filter...")
        try:
            ds = copernicusmarine.open_dataset(
                dataset_id=dataset_id,
                minimum_latitude=lat_min,
                maximum_latitude=lat_max,
                start_datetime=f"{date_str}T00:00:00",
                end_datetime=f"{date_str}T23:59:59",
            )
        except Exception as e2:
            log.error(f"Failed to open dataset: {e2}")
            return None

    var_name = find_variable(ds)
    log.info(f"Variable: {var_name}")

    conc = ds[var_name].values
    # 차원 정리 (time, depth, y, x) → (y, x)
    while conc.ndim > 2:
        conc = conc[0]

    # 좌표 추출
    lat_coords = ds.latitude.values if "latitude" in ds.coords else ds.lat.values
    lon_coords = ds.longitude.values if "longitude" in ds.coords else ds.lon.values

    # 다운샘플링
    conc_ds = conc[::step, ::step]
    lats = lat_coords[::step]
    lons = lon_coords[::step]

    # 스케일 정규화 (일부 데이터셋은 0-100 또는 0-1)
    max_val = float(np.nanmax(conc_ds))
    if max_val > 1.5:
        conc_ds = conc_ds / 100.0

    # 셀 추출 — 반구별 위도 범위([lat_min, lat_max]) 안의 셀만.
    cells = []
    for i, lat in enumerate(lats):
        if not (lat_min <= lat <= lat_max):
            continue
        for j, lon in enumerate(lons):
            c = float(conc_ds[i, j])
            if np.isfinite(c) and MIN_CONC <= c <= 1.0:
                cells.append({
                    "lon": round(float(lon), 3),
                    "lat": round(float(lat), 3),
                    "concentration": round(c, 4),
                })

    result = {
        "source": f"Copernicus Marine Service {product_id}",
        "provider": "EU Copernicus / Mercator Ocean",
        "hemisphere": hemisphere,
        "date": date_compact,
        "month": dt.month,
        "grid_resolution_km": 6 * step,
        "cell_count": len(cells),
        "cells": cells,
    }

    ds.close()
    log.info(f"Cells extracted: {len(cells)}")
    return result


def save_json(data, output_dir=OUTPUT_DIR, archive_dir=ARCHIVE_DIR):
    """JSON 저장: latest + 날짜별 아카이브. 남극은 별도 파일(realIceData_south_*)."""
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(archive_dir, exist_ok=True)

    date_str = data["date"]
    hemi = data.get("hemisphere", "north")
    cfg = HEMISPHERES.get(hemi, HEMISPHERES["north"])
    latest_name = cfg["out_latest"]
    archive_prefix = "realIceData_south" if hemi == "south" else "realIceData"

    # latest (항상 덮어씀)
    latest_path = os.path.join(output_dir, latest_name)
    _atomic_write_json(latest_path, data, ensure_ascii=False)
    size_kb = os.path.getsize(latest_path) / 1024
    log.info(f"Saved: {latest_path} ({size_kb:.0f} KB, {data['cell_count']} cells)")

    # 날짜별 아카이브 (기존 파일 보존)
    archive_path = os.path.join(archive_dir, f"{archive_prefix}_{date_str}.json")
    if not os.path.exists(archive_path):
        _atomic_write_json(archive_path, data, ensure_ascii=False)
        log.info(f"Archived: {archive_path}")
    else:
        log.info(f"Archive already exists, skipping: {archive_path}")

    return latest_path


def run_once(target_date=None, step=STEP, dry_run=False, hemisphere="north"):
    """1회 수집."""
    data = fetch_copernicus(
        target_date=target_date, step=step, dry_run=dry_run, hemisphere=hemisphere
    )
    if data and not dry_run:
        path = save_json(data)
        # HTML 폴더에도 복사 (반구별 파일명 유지)
        html_dir = os.path.dirname(os.path.abspath(__file__))
        html_latest = os.path.join(html_dir, os.path.basename(path))
        import shutil
        shutil.copy2(path, html_latest)
        log.info(f"Copied to: {html_latest}")
    return data


def run_scheduled():
    """매일 06:00 UTC에 자동 실행."""
    try:
        import schedule  # type: ignore[import]
        import time
    except ImportError:
        log.error("schedule not installed. Run: pip install schedule")
        sys.exit(1)

    log.info("Starting daily fetch at 06:00 UTC (press Ctrl+C to stop)")

    # 시작 시 1회 즉시 실행
    run_once()

    schedule.every().day.at("06:00").do(run_once)

    while True:
        schedule.run_pending()
        time.sleep(60)


def main():
    parser = argparse.ArgumentParser(
        description="Copernicus Marine Service Arctic Sea Ice fetcher"
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Target date (YYYY-MM-DD). Default: yesterday"
    )
    parser.add_argument(
        "--schedule", action="store_true",
        help="Run daily at 06:00 UTC"
    )
    parser.add_argument(
        "--step", type=int, default=STEP,
        help=f"Downsample step (default: {STEP} = {6*STEP}km)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Check config without downloading"
    )
    parser.add_argument(
        "--hemisphere", choices=["north", "south"], default="north",
        help="north=북극(Arctic, 기본) | south=남극(Antarctic, ROSS/PENINSULA)"
    )

    args = parser.parse_args()

    if args.schedule:
        run_scheduled()
    else:
        data = run_once(target_date=args.date, step=args.step, dry_run=args.dry_run,
                        hemisphere=args.hemisphere)
        if data:
            log.info(f"Done! {data['cell_count']} cells saved.")
        elif not args.dry_run:
            log.error("Fetch failed. Check credentials and network.")
            sys.exit(1)


if __name__ == "__main__":
    main()
