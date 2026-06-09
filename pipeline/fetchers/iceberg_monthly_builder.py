#!/usr/bin/env python3
"""
월별 빙산 레퍼런스 빌더 (NSIDC IIP · North Atlantic)

빙하 아카이브의 월별(1월~12월) 선택 시 빙산이 계절에 따라 실제로 변하도록,
NSIDC IIP(International Ice Patrol, G00807) 시즌 CSV 다년치를 내려받아
**달력 월별로 집계·공간 다운샘플**한 realBergData_month01~12.json 을 생성한다.

배경:
  - 실시간 fetcher(iceberg_fetcher.py)는 IIP 시즌 CSV 의 "마지막 100건"만 쓰고
    6,000여 건의 dated 관측을 버린다. 그 전체에는 월별로 펼쳐진 북대서양(48~62°N)
    빙산 관측이 들어 있어, 월별 기후값(climatology) 레퍼런스로 재활용할 수 있다.
  - 산출물은 특정 연도가 아니라 다년 누적 월별 대표 분포(레퍼런스 성격)다.
    → 월별 아카이브(1월~12월)는 본디 계절 레퍼런스이므로 의미가 정확히 맞는다.

한계:
  - 커버리지는 IIP 작전구역(래브라도·뉴펀들랜드·데이비스 해협 일대)에 집중.
    고위도 북극해(보퍼트·랍테프·NSR 핵심)는 포함되지 않는다.
  - NSIDC 공개분은 현재 2021 시즌까지(2022+ 미게시). 신규 시즌 게시 시 자동 포함.

사용법:
  python iceberg_monthly_builder.py            # 기본 연도창 집계 → monthly/ 생성
  python iceberg_monthly_builder.py --dry-run  # 다운로드만, 파일 미기록
"""

import argparse
import csv
import io
import json
import os
import ssl
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

NSIDC_BASE = "https://noaadata.apps.nsidc.org/NOAA/G00807/"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "monthly")

# 집계 연도창: 최신 연도부터 과거로 내려가며 200 응답 연도만 사용(404 자동 스킵).
# 다년 누적으로 월별 대표 분포의 안정성을 확보한다(최대 MAX_YEARS 시즌).
YEAR_FROM = datetime.now(timezone.utc).year   # 신규 시즌 게시 시 자동 반영
YEAR_TO = 2012                                # IIP 공개 하한(현재 기준)
MAX_YEARS = 8

# 월별 공간 다운샘플: 0.05° 격자당 1점으로 thinning 후, 월당 최대 CAP 개로 stride 축소.
GRID_DEG = 0.05
CAP_PER_MONTH = 600

_SSL_CTX = ssl.create_default_context()


def _http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "arctic-twin-monthly-builder"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return r.status, r.read()


def _classify(size_class):
    s = (size_class or "").strip().upper()
    if "LRG" in s or "VLG" in s:
        return 3000, 1200, "large"
    if "MED" in s:
        return 1500, 600, "medium"
    return 500, 200, "small"


def _parse_month(date_str):
    """SIGHTING_DATE('M/D/YYYY') → 달력 월(1~12) 또는 None."""
    m = (date_str or "").strip().split("/")
    if not m or not m[0].isdigit():
        return None
    mo = int(m[0])
    return mo if 1 <= mo <= 12 else None


def fetch_year(year):
    url = f"{NSIDC_BASE}IIP_{year}IcebergSeason.csv"
    print(f"[IIP] trying {url}")
    try:
        status, body = _http_get(url)
    except Exception as e:  # noqa: BLE001
        print(f"  error: {type(e).__name__}: {str(e)[:60]}")
        return None
    if status != 200 or len(body) < 1000:
        print(f"  HTTP {status} ({len(body)} bytes) — skip")
        return None
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8", "replace"))))
    print(f"  {len(rows)} rows")
    return rows


def build():
    # 월별 누적 버킷: month → { grid_key → berg }
    buckets = {mo: {} for mo in range(1, 13)}
    used_years = []
    year = YEAR_FROM
    while year >= YEAR_TO and len(used_years) < MAX_YEARS:
        rows = fetch_year(year)
        if rows:
            used_years.append(year)
            for row in rows:
                try:
                    lat = float(row.get("SIGHTING_LATITUDE", 0))
                    lon = float(row.get("SIGHTING_LONGITUDE", 0))
                except (TypeError, ValueError):
                    continue
                if lat <= 0:  # 북반구만 (IIP 는 전부 북대서양이지만 방어)
                    continue
                mo = _parse_month(row.get("SIGHTING_DATE", ""))
                if mo is None:
                    continue
                # 0.05° 격자당 1점으로 thinning (반복 관측·군집 축소)
                gkey = (round(lat / GRID_DEG), round(lon / GRID_DEG))
                if gkey in buckets[mo]:
                    continue
                length_m, width_m, btype = _classify(row.get("SIZE", ""))
                iy = row.get("ICEBERG_YEAR", "")
                num = row.get("ICEBERG_NUMBER", "")
                buckets[mo][gkey] = {
                    "id": f"IIP-{iy}-{num}",
                    "lon": round(lon, 3),
                    "lat": round(lat, 3),
                    "length_m": length_m,
                    "width_m": width_m,
                    "type": btype,
                    "last_update": (row.get("SIGHTING_DATE", "") or "").strip(),
                }
        year -= 1

    if not used_years:
        print("[ERROR] 사용 가능한 IIP 시즌 CSV 없음")
        return None

    out = {}
    for mo in range(1, 13):
        bergs = list(buckets[mo].values())
        # 월당 상한: 초과 시 결정적 stride 다운샘플(공간 격자 thinning 후라 분포 유지)
        if len(bergs) > CAP_PER_MONTH:
            step = len(bergs) // CAP_PER_MONTH + 1
            bergs = bergs[::step][:CAP_PER_MONTH]
        out[mo] = {
            "source": "NSIDC International Ice Patrol (G00807)",
            "month": f"{mo:02d}",
            "years": used_years,
            "note": "다년 누적 월별 대표 분포(climatology) · 북대서양 IIP 구역",
            "berg_count": len(bergs),
            "bergs": bergs,
        }
    return out


def _atomic_write_json(path, data):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main():
    ap = argparse.ArgumentParser(description="NSIDC IIP 월별 빙산 레퍼런스 빌더")
    ap.add_argument("--dry-run", action="store_true", help="다운로드만, 파일 미기록")
    args = ap.parse_args()

    out = build()
    if not out:
        sys.exit(1)

    print("\n[월별 집계]")
    for mo in range(1, 13):
        print(f"  month{mo:02d}: {out[mo]['berg_count']:4d} bergs (years {out[mo]['years']})")

    if args.dry_run:
        print("\n[DRY-RUN] 파일 미기록")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for mo in range(1, 13):
        path = os.path.join(OUTPUT_DIR, f"realBergData_month{mo:02d}.json")
        _atomic_write_json(path, out[mo])
        print(f"[saved] {path}")
    print("\nDone! 월별 빙산 레퍼런스 12개 생성 완료.")


if __name__ == "__main__":
    main()
