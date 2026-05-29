#!/usr/bin/env python3
"""
Arctic Digital Twin - 데이터 파이프라인 통합 실행기

사용법:
  python run_pipeline.py                # 전체 파이프라인 실행
  python run_pipeline.py --ice-only     # 해빙 데이터만 수집
  python run_pipeline.py --berg-only    # 빙산 데이터만 수집
  python run_pipeline.py --nsidc-only   # NSIDC 데이터만 변환
  python run_pipeline.py --weather-only # Open-Meteo 기상 데이터만 수집
"""

import argparse
import subprocess
import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).parent
FETCHERS_DIR = PIPELINE_DIR / "fetchers"
SERVER_DATA_DIR = PIPELINE_DIR.parent / "data"


def run_script(script_path, args=None):
    """Python 스크립트 실행."""
    cmd = [sys.executable, str(script_path)]
    if args:
        cmd.extend(args)
    print(f"\n{'='*60}")
    print(f"  실행: {' '.join(cmd)}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, cwd=str(PIPELINE_DIR))
    return result.returncode


def ensure_data_dir():
    """서버 데이터 디렉토리 생성."""
    SERVER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (SERVER_DATA_DIR / "archive").mkdir(exist_ok=True)
    print(f"[OK] 데이터 디렉토리: {SERVER_DATA_DIR}")


def main():
    parser = argparse.ArgumentParser(
        description="Arctic Digital Twin 데이터 파이프라인"
    )
    parser.add_argument(
        "--ice-only", action="store_true", help="Copernicus 해빙 데이터만 수집"
    )
    parser.add_argument("--berg-only", action="store_true", help="빙산 데이터만 수집")
    parser.add_argument("--nsidc-only", action="store_true", help="NSIDC 데이터만 변환")
    parser.add_argument(
        "--weather-only", action="store_true", help="Open-Meteo 기상 데이터만 수집"
    )
    parser.add_argument(
        "--sentinel1-only", action="store_true", help="Sentinel-1 IW 빙하 아카이브만 수집"
    )
    parser.add_argument(
        "--detect-icebergs", action="store_true", help="SAR CV 빙산 자동 탐지 실행"
    )
    parser.add_argument(
        "--detect-max", type=int, default=5, help="SAR 탐지 시 최대 처리 제품 수 (기본: 5)"
    )
    args = parser.parse_args()

    ensure_data_dir()

    run_all = not (
        args.ice_only or args.berg_only or args.nsidc_only
        or args.weather_only or args.sentinel1_only or args.detect_icebergs
    )

    results = []

    if run_all or args.ice_only:
        rc = run_script(FETCHERS_DIR / "copernicus_fetcher.py")
        results.append(("Copernicus Sea Ice", rc))

    if run_all or args.berg_only:
        rc = run_script(FETCHERS_DIR / "iceberg_fetcher.py")
        results.append(("Iceberg Tracker", rc))

    if run_all or args.nsidc_only:
        rc = run_script(
            FETCHERS_DIR / "nsidc_pipeline.py", ["--year", "2023", "--all-months"]
        )
        results.append(("NSIDC Convert", rc))

    if run_all or args.weather_only:
        rc = run_script(FETCHERS_DIR / "weather_fetcher.py")
        results.append(("MET Norway Weather", rc))

    if run_all or args.sentinel1_only:
        rc = run_script(FETCHERS_DIR / "sentinel1_iw_fetcher.py")
        results.append(("Sentinel-1 IW Glacier Archive", rc))

    if args.detect_icebergs:
        rc = run_script(
            PIPELINE_DIR / "processors" / "iceberg_detector.py",
            ["--latest", "--max-products", str(args.detect_max)],
        )
        results.append(("SAR Iceberg Detection (CV)", rc))

    # 결과 요약
    print(f"\n{'='*60}")
    print("  Pipeline Results")
    print(f"{'='*60}")
    for name, rc in results:
        status = "[OK]" if rc == 0 else "[FAIL]"
        print(f"  {status} | {name}")
    print(f"\n  Data dir: {SERVER_DATA_DIR}")


if __name__ == "__main__":
    main()
