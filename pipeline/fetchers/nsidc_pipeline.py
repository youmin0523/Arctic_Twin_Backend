#!/usr/bin/env python3
"""
NSIDC-0051 v2 Sea Ice Concentration → JSON 변환 파이프라인

사용법:
  python nsidc_pipeline.py --year 2023 --month 9
  python nsidc_pipeline.py --year 2023 --all-months
  python nsidc_pipeline.py --local-file path/to/file.nc --month 9
  python nsidc_pipeline.py --year 2023 --month 3 --step 3

출력: output/realIceData_month{MM}.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

# ─── NSIDC Polar Stereographic 파라미터 (공식) ─────────────────────
TRUE_LAT = 70.0          # true scale latitude (degrees)
RE = 6378.273            # Earth radius (km)
E = 0.081816153          # eccentricity (Hughes 1980 ellipsoid)
E2 = E * E
CELL_SIZE_KM = 25.0      # native grid spacing

# 북반구 NSIDC 격자 크기
NX, NY = 304, 448

# NOAA CDR (G02202 V5) URL 템플릿 (Earthdata 로그인 불필요)
# NSIDC-0051은 Earthdata 인증 필요 → 대신 동일 데이터의 NOAA CDR 버전 사용
URL_TEMPLATES = [
    # G02202 V5 (F17 위성)
    "https://noaadata.apps.nsidc.org/NOAA/G02202_V5/north/daily/{year}/"
    "sic_psn25_{year}{month:02d}{day:02d}_F17_v05r00.nc",
    # G02202 V5 (am2 위성, 2025~)
    "https://noaadata.apps.nsidc.org/NOAA/G02202_V5/north/daily/{year}/"
    "sic_psn25_{year}{month:02d}{day:02d}_am2_v05r00.nc",
]

# 해빙 농도 변수명 후보 (우선순위 순)
CONC_VAR_CANDIDATES = [
    "cdr_seaice_conc",
    "goddard_nt_seaice_conc",
    "goddard_bt_seaice_conc",
    "seaice_conc_cdr",
    "concentration",
]


def polar_stereo_to_latlon(x_km, y_km):
    """NSIDC 극좌표 (km) → 위도/경도 변환 (북반구).

    References:
        Snyder, J.P., 1987. Map Projections: A Working Manual.
        NSIDC documentation for EASE/Polar Stereographic grids.
    """
    slat = np.radians(TRUE_LAT)
    rho = np.sqrt(x_km**2 + y_km**2)

    # 공식에서 사용하는 스케일 계수
    tc = np.tan(np.pi / 4 - slat / 2) / (
        ((1 - E * np.sin(slat)) / (1 + E * np.sin(slat))) ** (E / 2)
    )
    mc = np.cos(slat) / np.sqrt(1 - E2 * np.sin(slat) ** 2)

    # rho가 0인 경우 (극점)
    t = rho * tc / (RE * mc)

    # 반복법으로 위도 계산 (3회면 충분)
    lat = np.pi / 2 - 2 * np.arctan(t)
    for _ in range(3):
        lat = np.pi / 2 - 2 * np.arctan(
            t * ((1 - E * np.sin(lat)) / (1 + E * np.sin(lat))) ** (E / 2)
        )

    lon = np.arctan2(x_km, -y_km)

    return np.degrees(lat), np.degrees(lon)


def build_xy_grids():
    """NSIDC 304×448 격자의 x, y 좌표(km) 생성."""
    # 격자 원점은 좌하단, 중심은 극점
    # x: -3850 ~ +3750 km,  y: -5350 ~ +5850 km (25km 간격)
    x0 = -3850.0
    y0 = +5850.0
    x = x0 + np.arange(NX) * CELL_SIZE_KM
    y = y0 - np.arange(NY) * CELL_SIZE_KM
    xx, yy = np.meshgrid(x, y)
    return xx, yy


def find_conc_variable(ds):
    """NetCDF 데이터셋에서 해빙 농도 변수를 자동 탐지."""
    for name in CONC_VAR_CANDIDATES:
        if name in ds.data_vars:
            return name
    # 후보에 없으면 'conc' 또는 'ice'가 포함된 변수 탐색
    for name in ds.data_vars:
        if "conc" in name.lower() or "ice" in name.lower():
            return name
    raise ValueError(
        f"해빙 농도 변수를 찾을 수 없습니다. 변수 목록: {list(ds.data_vars)}"
    )


def pick_day_for_month(year, month):
    """월 중간 날짜(15일) 반환. 데이터 없을 수 있으므로 fallback 날짜 리스트."""
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    # 15일 우선, 안 되면 1일, 마지막 날 순서
    return [15, 1, last_day, 10, 20]


def download_nc(year, month, cache_dir="cache"):
    """NOAA CDR 미러에서 NetCDF 다운로드. 캐시 있으면 스킵."""
    import requests

    os.makedirs(cache_dir, exist_ok=True)
    days = pick_day_for_month(year, month)

    # 캐시에 이미 있는 파일 먼저 확인
    for day in days:
        for tmpl in URL_TEMPLATES:
            fname = os.path.basename(tmpl.format(year=year, month=month, day=day))
            local_path = os.path.join(cache_dir, fname)
            if os.path.exists(local_path):
                print(f"[cache] {local_path}")
                return local_path, f"{year}{month:02d}{day:02d}"

    # 다운로드 시도: 날짜 × URL 템플릿 조합
    for day in days:
        for tmpl in URL_TEMPLATES:
            url = tmpl.format(year=year, month=month, day=day)
            fname = os.path.basename(url)
            local_path = os.path.join(cache_dir, fname)

            print(f"[download] {url}")
            try:
                resp = requests.get(url, timeout=120, stream=True)
                if resp.status_code == 200:
                    with open(local_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=1024 * 256):
                            f.write(chunk)
                    size_mb = os.path.getsize(local_path) / (1024 * 1024)
                    print(f"[done] {local_path} ({size_mb:.1f} MB)")
                    return local_path, f"{year}{month:02d}{day:02d}"
                else:
                    print(f"  HTTP {resp.status_code}, next...")
            except requests.RequestException as e:
                print(f"  request failed: {e}, next...")

    raise RuntimeError(
        f"{year}-{month:02d} data unavailable. "
        f"Tried days={days}. Use --local-file option."
    )


def process_nc(nc_path, date_str, month, step=2, min_conc=0.05, min_lat=60.0):
    """NetCDF → JSON dict 변환."""
    import xarray as xr

    print(f"[처리] {nc_path} (step={step})")
    ds = xr.open_dataset(nc_path)

    var_name = find_conc_variable(ds)
    print(f"  변수: {var_name}")

    conc = ds[var_name].values
    # 차원 정리: (time, y, x) → (y, x)
    while conc.ndim > 2:
        conc = conc[0]

    # 유효 범위: 0~1 (일부 데이터셋은 0~100 또는 0~250 스케일)
    if np.nanmax(conc) > 10:
        conc = conc / 250.0  # NSIDC flag: 0–250 → 0–1
    elif np.nanmax(conc) > 1.5:
        conc = conc / 100.0  # percent → fraction

    # 격자 좌표 생성
    xx, yy = build_xy_grids()

    # 극좌표 → 위경도
    lats, lons = polar_stereo_to_latlon(xx, yy)

    # 다운샘플링
    conc_ds = conc[::step, ::step]
    lats_ds = lats[::step, ::step]
    lons_ds = lons[::step, ::step]

    # 필터링
    mask = (
        np.isfinite(conc_ds)
        & (conc_ds >= min_conc)
        & (lats_ds >= min_lat)
        & (conc_ds <= 1.0)
    )

    cells = []
    rows, cols = np.where(mask)
    for r, c in zip(rows, cols):
        cells.append({
            "lon": round(float(lons_ds[r, c]), 3),
            "lat": round(float(lats_ds[r, c]), 3),
            "concentration": round(float(conc_ds[r, c]), 4),
        })

    result = {
        "source": "NOAA/NSIDC CDR G02202 V5",
        "provider": "NOAA/NSIDC",
        "date": date_str,
        "month": month,
        "grid_resolution_km": int(CELL_SIZE_KM * step),
        "cell_count": len(cells),
        "cells": cells,
    }

    ds.close()
    print(f"  셀 수: {len(cells)}")
    return result


def save_json(data, month, output_dir="output"):
    """JSON 파일 저장."""
    os.makedirs(output_dir, exist_ok=True)
    fname = f"realIceData_month{month:02d}.json"
    out_path = os.path.join(output_dir, fname)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f"[저장] {out_path} ({size_mb:.1f} MB, {data['cell_count']} cells)")
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="NSIDC-0051 v2 해빙 농도 → JSON 변환 파이프라인"
    )
    parser.add_argument("--year", type=int, default=2023, help="연도 (기본: 2023)")
    parser.add_argument("--month", type=int, help="월 (1-12)")
    parser.add_argument(
        "--all-months", action="store_true", help="1~12월 전체 처리"
    )
    parser.add_argument(
        "--local-file", type=str, help="로컬 NetCDF 파일 경로 (다운로드 스킵)"
    )
    parser.add_argument(
        "--step", type=int, default=2,
        help="다운샘플링 스텝 (기본: 2 = 50km 간격)"
    )
    parser.add_argument(
        "--output-dir", type=str,
        default=os.path.join(os.path.dirname(__file__), "..", "..", "data"),
        help="출력 디렉터리 (기본: backend/data)"
    )
    parser.add_argument(
        "--min-conc", type=float, default=0.05,
        help="최소 농도 임계값 (기본: 0.05)"
    )

    args = parser.parse_args()

    if not args.month and not args.all_months:
        parser.error("--month 또는 --all-months 중 하나를 지정하세요.")

    months = list(range(1, 13)) if args.all_months else [args.month]

    for m in months:
        if not 1 <= m <= 12:
            print(f"[오류] 잘못된 월: {m}")
            continue

        print(f"\n{'='*60}")
        print(f"  {args.year}년 {m}월 처리 시작")
        print(f"{'='*60}")

        try:
            if args.local_file:
                nc_path = args.local_file
                date_str = f"{args.year}{m:02d}15"
            else:
                nc_path, date_str = download_nc(args.year, m)

            data = process_nc(
                nc_path, date_str, m,
                step=args.step, min_conc=args.min_conc
            )
            save_json(data, m, args.output_dir)

        except Exception as e:
            print(f"[오류] {args.year}년 {m}월 처리 실패: {e}")
            if len(months) == 1:
                sys.exit(1)

    print(f"\n완료! JSON 파일을 arctic-hybrid.html과 같은 폴더에 복사하세요.")
    print(f"  cp {args.output_dir}/realIceData_month*.json <html 폴더>/")


if __name__ == "__main__":
    main()
