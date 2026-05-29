#!/usr/bin/env python3
"""
실측 빙산 데이터 수집 파이프라인

소스:
  1순위: US National Ice Center (NIC) — 주간 갱신, CSV, 대형 빙산 추적
  2순위: Joel-Hanson GitHub JSON — 일간 갱신, BYU/NASA 스크래핑
  3순위: NSIDC IIP 시즌 CSV — 북대서양 빙산 개별 관측

사용법:
  python iceberg_fetcher.py              # 최신 빙산 데이터 수집
  python iceberg_fetcher.py --dry-run    # 설정 확인만
"""

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
FILENAME = "realBergData_latest.json"

# ── NIC CSV (1순위) ─────────────────────────────────────────────────
NIC_URL = "https://usicecenter.gov/File/DownloadCurrent?pId=134"

def fetch_nic():
    """US National Ice Center CSV → 빙산 리스트."""
    import requests
    print("[NIC] fetching:", NIC_URL)
    resp = requests.get(NIC_URL, timeout=60)
    if resp.status_code != 200:
        print(f"  HTTP {resp.status_code}")
        return None

    text = resp.text
    reader = csv.DictReader(io.StringIO(text))
    bergs = []
    for row in reader:
        try:
            name = row.get("Iceberg", "").strip()
            lat = float(row.get("Latitude", 0))
            lon = float(row.get("Longitude", 0))
            length_nm = float(row.get("Length (NM)", 0))
            width_nm = float(row.get("Width (NM)", 0))
            date_str = row.get("Last Update", "").strip()

            length_m = round(length_nm * 1852)
            width_m = round(width_nm * 1852)

            # 크기 기반 타입 분류
            if length_nm >= 20:
                berg_type = "tabular"
            elif length_nm >= 10:
                berg_type = "large"
            elif length_nm >= 5:
                berg_type = "medium"
            else:
                berg_type = "small"

            bergs.append({
                "id": name,
                "lon": round(lon, 3),
                "lat": round(lat, 3),
                "length_m": length_m,
                "width_m": width_m,
                "type": berg_type,
                "last_update": date_str,
            })
        except (ValueError, KeyError) as e:
            print(f"  skip row: {e}")
            continue

    if bergs:
        print(f"[NIC] {len(bergs)} icebergs parsed")
    return bergs if bergs else None


# ── Joel-Hanson GitHub JSON (2순위) ─────────────────────────────────
GITHUB_URL = "https://raw.githubusercontent.com/Joel-hanson/Iceberg-locations/main/api/latest.json"

def parse_dms(dms_str):
    """DMS 문자열 (예: '48 54\\'S') → 십진 도."""
    m = re.match(r"(\d+)\s+(\d+)'?\s*([NSEW])", dms_str.strip())
    if not m:
        return None
    deg = int(m.group(1))
    minutes = int(m.group(2))
    direction = m.group(3)
    dd = deg + minutes / 60.0
    if direction in ('S', 'W'):
        dd = -dd
    return round(dd, 3)

def fetch_github():
    """GitHub JSON → 빙산 리스트."""
    import requests
    print("[GitHub] fetching:", GITHUB_URL)
    resp = requests.get(GITHUB_URL, timeout=30)
    if resp.status_code != 200:
        print(f"  HTTP {resp.status_code}")
        return None

    data = resp.json()
    bergs = []
    for ib in data.get("icebergs", []):
        lat = parse_dms(ib.get("dms_lattitude", ""))
        lon = parse_dms(ib.get("dms_longitude", ""))
        if lat is None or lon is None:
            continue
        name = ib.get("iceberg", "").upper()
        bergs.append({
            "id": name,
            "lon": lon,
            "lat": lat,
            "length_m": 5000,   # GitHub 소스에 크기 없음 → 기본값
            "width_m": 2000,
            "type": "large",
            "last_update": ib.get("recent_observation", ""),
        })

    if bergs:
        print(f"[GitHub] {len(bergs)} icebergs parsed")
    return bergs if bergs else None


# ── NSIDC IIP 시즌 CSV (3순위) ──────────────────────────────────────
NSIDC_BASE = "https://noaadata.apps.nsidc.org/NOAA/G00807/"

def fetch_nsidc_iip(year=2021):
    """NSIDC IIP 시즌 CSV → 빙산 리스트 (마지막 100개 관측)."""
    import requests
    url = f"{NSIDC_BASE}IIP_{year}IcebergSeason.csv"
    print(f"[NSIDC-IIP] fetching: {url}")
    resp = requests.get(url, timeout=60)
    if resp.status_code != 200:
        print(f"  HTTP {resp.status_code}")
        return None

    reader = csv.DictReader(io.StringIO(resp.text))
    all_rows = list(reader)
    # 마지막 100개만 (최신 관측)
    recent = all_rows[-100:] if len(all_rows) > 100 else all_rows
    bergs = []
    seen = set()
    for row in recent:
        try:
            lat = float(row.get("SIGHTING_LATITUDE", 0))
            lon = float(row.get("SIGHTING_LONGITUDE", 0))
            yr = row.get("ICEBERG_YEAR", "")
            num = row.get("ICEBERG_NUMBER", "")
            name = f"IIP-{yr}-{num}"
            if name in seen:
                continue
            seen.add(name)

            size_class = row.get("SIZE", "MED").strip().upper()
            if "LRG" in size_class or "VLG" in size_class:
                length_m, width_m, btype = 3000, 1200, "large"
            elif "MED" in size_class:
                length_m, width_m, btype = 1500, 600, "medium"
            else:
                length_m, width_m, btype = 500, 200, "small"

            bergs.append({
                "id": name,
                "lon": round(lon, 3),
                "lat": round(lat, 3),
                "length_m": length_m,
                "width_m": width_m,
                "type": btype,
                "last_update": row.get("SIGHTING_DATE", ""),
            })
        except (ValueError, KeyError):
            continue

    if bergs:
        print(f"[NSIDC-IIP] {len(bergs)} icebergs parsed")
    return bergs if bergs else None


# ── 메인 로직 ───────────────────────────────────────────────────────
def fetch_all():
    """3개 소스를 순차 시도, 성공한 것 합산."""
    import requests  # noqa: F811 — 상단 함수에서 이미 lazy import

    all_bergs = []
    sources = []

    # 1순위: NIC
    nic = fetch_nic()
    if nic:
        all_bergs.extend(nic)
        sources.append("US National Ice Center")

    # 2순위: GitHub (NIC와 중복 가능 → ID 기준 중복 제거)
    gh = fetch_github()
    if gh:
        existing_ids = {b["id"] for b in all_bergs}
        for b in gh:
            if b["id"] not in existing_ids:
                all_bergs.append(b)
                existing_ids.add(b["id"])
        sources.append("BYU/NASA via GitHub")

    # 3순위: NSIDC IIP (북대서양)
    iip = fetch_nsidc_iip()
    if iip:
        all_bergs.extend(iip)
        sources.append("NSIDC IIP")

    if not all_bergs:
        print("[ERROR] No iceberg data from any source")
        return None

    result = {
        "source": " / ".join(sources),
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "berg_count": len(all_bergs),
        "bergs": all_bergs,
    }
    print(f"\n[TOTAL] {len(all_bergs)} icebergs from: {', '.join(sources)}")
    return result


def save_json(data):
    """JSON 저장."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, FILENAME)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"[saved] {out_path} ({size_kb:.0f} KB)")

    # HTML 폴더에도 복사
    html_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(html_dir, FILENAME)
    shutil.copy2(out_path, html_path)
    print(f"[copied] {html_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Real iceberg tracking data fetcher (NIC / GitHub / NSIDC-IIP)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Check URLs only")
    args = parser.parse_args()

    if args.dry_run:
        print("[DRY-RUN] Sources:")
        print(f"  NIC:    {NIC_URL}")
        print(f"  GitHub: {GITHUB_URL}")
        print(f"  NSIDC:  {NSIDC_BASE}IIP_2021IcebergSeason.csv")
        return

    data = fetch_all()
    if data:
        save_json(data)
        print(f"\nDone! {data['berg_count']} icebergs saved to {FILENAME}")
    else:
        print("\nFailed to fetch any iceberg data.")
        sys.exit(1)


if __name__ == "__main__":
    main()
