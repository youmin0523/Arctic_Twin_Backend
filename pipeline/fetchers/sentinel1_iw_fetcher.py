#!/usr/bin/env python3
"""
Sentinel-1 IW GRD 빙하 아카이브 자동 수집 파이프라인

CDSE (Copernicus Data Space Ecosystem) OData API를 통해
Arctic 빙하 지역의 Sentinel-1 IW GRD 제품을 다운로드합니다.

최초 실행 전:
  1. CDSE 무료 가입: https://dataspace.copernicus.eu
  2. .env 파일에 CDSE_USER / CDSE_PASSWORD 설정
     또는 환경변수로 직접 전달

사용법:
  python sentinel1_iw_fetcher.py                   # 최신 데이터 1회 수집
  python sentinel1_iw_fetcher.py --date 2026-04-07 # 특정 날짜 수집
  python sentinel1_iw_fetcher.py --dry-run         # 실제 다운로드 없이 확인
  python sentinel1_iw_fetcher.py --backfill 30     # 최근 30일 일괄 수집
  python sentinel1_iw_fetcher.py --max-disk-gb 100 # 디스크 제한 변경

출력:
  data/sentinel1_catalog_latest.json   (메타데이터 카탈로그)
  data/sentinel1_archive/YYYY/MM/      (GRD .zip 파일)
"""

import argparse
import json
import logging
import os
import random
import shutil
import sys
import tempfile
import time
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
        logging.FileHandler(LOG_DIR / "sentinel1_iw_fetcher.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("sentinel1_iw")

# ─── 설정 ────────────────────────────────────────────────────────────
CDSE_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu"
    "/auth/realms/CDSE/protocol/openid-connect/token"
)
CDSE_ODATA_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
CDSE_DOWNLOAD_BASE = "https://zipper.dataspace.copernicus.eu/odata/v1/Products"

# 재시도 설정
MAX_RETRIES = 5
BASE_BACKOFF = 2.0  # 초
MAX_BACKOFF = 300.0  # 5분 캡
RETRYABLE_CODES = {429, 500, 502, 503, 529}

# 디스크 관리
# 주의: 이 값은 루트 디스크(40GB) 용량보다 충분히 작아야 한다. 과거 50GB(>실디스크 40GB)로
# 설정돼 정리가 영영 트리거되지 않아 archive 가 디스크를 100% 채운 사고가 있었다.
# 도커 이미지 재빌드(~13GB transient) + OS(~6GB) + swap(6GB) + 레포 를 빼면
# SAR raw 에 안전하게 줄 수 있는 건 ~6GB. raw zip 은 탐지(iceberg_detector)용 중간 입력일
# 뿐이라 최근 몇 패스만 있으면 충분하고, 동향용 산출물(작은 JSON)은 별도로 무한 보관된다.
DEFAULT_MAX_DISK_GB = 6

# Arctic 빙하 AOI (관심 지역)
ARCTIC_AOIS = {
    "svalbard": {
        "name": "Svalbard",
        "bbox": (10, 76, 35, 81),
        "description": "Austfonna, Kronebreen 등 스발바르 빙하",
    },
    "greenland_east": {
        "name": "East Greenland",
        "bbox": (-45, 65, -15, 80),
        "description": "동부 그린란드 주요 유출 빙하",
    },
    "jakobshavn": {
        "name": "Jakobshavn / Ilulissat",
        "bbox": (-55, 68, -45, 72),
        "description": "야콥스하운 이스브래 (세계 최대 유출 빙하)",
    },
    "novaya_zemlya": {
        "name": "Novaya Zemlya",
        "bbox": (48, 70, 62, 77),
        "description": "노바야제믈랴 북극 빙하",
    },
}

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "data"
ARCHIVE_DIR = OUTPUT_DIR / "sentinel1_archive"
CATALOG_FILE = OUTPUT_DIR / "sentinel1_catalog_latest.json"

# ─── .env 파일 로드 (독립 실행 시) ────────────────────────────────────
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"
if _ENV_FILE.exists() and not os.environ.get("CDSE_USER"):
    with open(_ENV_FILE, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                os.environ.setdefault(_key.strip(), _val.strip())


# ─── CDSE OAuth2 인증 ────────────────────────────────────────────────
class CDSEAuth:
    """Copernicus Data Space Ecosystem OAuth2 인증 관리."""

    def __init__(self):
        self.username = os.environ.get("CDSE_USER", "")
        self.password = os.environ.get("CDSE_PASSWORD", "")
        self._token = None
        self._token_expiry = 0

        if not self.username or not self.password:
            self._print_setup_guide()
            raise RuntimeError("CDSE 자격증명이 설정되지 않았습니다.")

    @staticmethod
    def _print_setup_guide():
        guide = """
============================================================
  CDSE (Copernicus Data Space Ecosystem) 계정 설정 필요
============================================================

1. 무료 가입:
   https://dataspace.copernicus.eu

2. .env 파일에 추가:
   CDSE_USER=your_email@example.com
   CDSE_PASSWORD=your_password

3. 이후 이 스크립트를 다시 실행하세요.

참고: Copernicus Marine Service와 별도 플랫폼입니다.
      같은 이메일로 가입할 수 있지만 비밀번호가 다를 수 있습니다.
============================================================
"""
        print(guide)

    def get_token(self):
        """유효한 액세스 토큰 반환. 만료 임박 시 자동 갱신."""
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        return self._refresh_token()

    def _refresh_token(self):
        """CDSE에서 새 토큰 발급."""
        import requests

        log.info("CDSE 토큰 발급 요청...")
        try:
            resp = requests.post(
                CDSE_TOKEN_URL,
                data={
                    "grant_type": "password",
                    "username": self.username,
                    "password": self.password,
                    "client_id": "cdse-public",
                },
                timeout=30,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"CDSE 토큰 요청 실패: {e}") from e

        if resp.status_code == 401:
            log.error("CDSE 인증 실패 - 이메일/비밀번호를 확인하세요.")
            log.error("Copernicus Marine Service와 별도 계정일 수 있습니다.")
            log.error("가입: https://dataspace.copernicus.eu")
            raise RuntimeError("CDSE 인증 실패 (401)")

        if resp.status_code != 200:
            raise RuntimeError(
                f"CDSE 토큰 발급 실패: HTTP {resp.status_code} — {resp.text[:200]}"
            )

        data = resp.json()
        self._token = data["access_token"]
        expires_in = data.get("expires_in", 600)
        self._token_expiry = time.time() + expires_in
        log.info(f"CDSE 토큰 발급 완료 (만료: {expires_in}초)")
        return self._token


# ─── 재시도 로직 ─────────────────────────────────────────────────────
def request_with_retry(session, method, url, auth=None, **kwargs):
    """
    HTTP 요청 + 지수 백오프 재시도.

    529 (서버 과부하), 429 (Rate Limit), 5xx 에러에 대해
    지수 백오프 + 랜덤 jitter로 재시도합니다.
    """
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            # 토큰 갱신 (매 요청 전 확인)
            if auth:
                token = auth.get_token()
                kwargs.setdefault("headers", {})
                kwargs["headers"]["Authorization"] = f"Bearer {token}"

            resp = session.request(method, url, **kwargs)

            if resp.status_code == 200:
                return resp

            if resp.status_code in RETRYABLE_CODES:
                wait = min(
                    BASE_BACKOFF * (2 ** attempt) + random.uniform(0, BASE_BACKOFF),
                    MAX_BACKOFF,
                )
                log.warning(
                    f"HTTP {resp.status_code} ← {url[:80]}... "
                    f"재시도 {attempt + 1}/{MAX_RETRIES} ({wait:.1f}초 대기)"
                )
                time.sleep(wait)
                continue

            if resp.status_code == 401 and auth:
                log.warning("토큰 만료 감지, 갱신 후 재시도...")
                auth._token = None  # 강제 갱신
                continue

            resp.raise_for_status()

        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                wait = min(
                    BASE_BACKOFF * (2 ** attempt) + random.uniform(0, BASE_BACKOFF),
                    MAX_BACKOFF,
                )
                log.warning(
                    f"요청 에러: {e} — "
                    f"재시도 {attempt + 1}/{MAX_RETRIES} ({wait:.1f}초 대기)"
                )
                time.sleep(wait)
            else:
                raise RuntimeError(
                    f"{MAX_RETRIES}회 재시도 후 실패: {url[:100]}"
                ) from last_error

    raise RuntimeError(f"{MAX_RETRIES}회 재시도 후 실패: {url[:100]}")


# ─── OData 카탈로그 쿼리 ─────────────────────────────────────────────
def _bbox_to_wkt(bbox):
    """(min_lon, min_lat, max_lon, max_lat) → WKT POLYGON."""
    lon1, lat1, lon2, lat2 = bbox
    return (
        f"{lon1} {lat1},{lon2} {lat1},{lon2} {lat2},{lon1} {lat2},{lon1} {lat1}"
    )


def query_sentinel1_products(session, auth, aoi_key, start_date, end_date):
    """
    CDSE OData API로 Sentinel-1 IW GRD 제품 검색.

    Returns: list of product dicts (Id, Name, ContentLength, footprint 등)
    """
    aoi = ARCTIC_AOIS[aoi_key]
    wkt = _bbox_to_wkt(aoi["bbox"])

    # OData 필터 구성
    filter_parts = [
        "Collection/Name eq 'SENTINEL-1'",
        (
            "Attributes/OData.CSC.StringAttribute/any("
            "att:att/Name eq 'operationalMode' and "
            "att/OData.CSC.StringAttribute/Value eq 'IW')"
        ),
        (
            "Attributes/OData.CSC.StringAttribute/any("
            "att:att/Name eq 'productType' and "
            "att/OData.CSC.StringAttribute/Value eq 'GRD')"
        ),
        f"ContentDate/Start gt {start_date}T00:00:00.000Z",
        f"ContentDate/Start lt {end_date}T23:59:59.999Z",
        f"OData.CSC.Intersects(area=geography'SRID=4326;POLYGON(({wkt}))')",
    ]
    odata_filter = " and ".join(filter_parts)

    all_products = []
    skip = 0
    page_size = 100

    while True:
        params = {
            "$filter": odata_filter,
            "$orderby": "ContentDate/Start desc",
            "$top": page_size,
            "$skip": skip,
        }

        resp = request_with_retry(
            session, "GET", CDSE_ODATA_URL, auth=auth,
            params=params, timeout=60,
        )
        data = resp.json()
        products = data.get("value", [])

        if not products:
            break

        for p in products:
            p["_aoi"] = aoi_key
        all_products.extend(products)

        log.info(
            f"  [{aoi['name']}] 페이지 {skip // page_size + 1}: "
            f"{len(products)}건 (누적 {len(all_products)}건)"
        )

        # 다음 페이지 확인
        if len(products) < page_size:
            break
        skip += page_size

    return all_products


# ─── 다운로드 ────────────────────────────────────────────────────────
def _get_product_path(product_name):
    """제품명에서 저장 경로 결정."""
    # S1A_IW_GRDH_1SDV_20260407T053210_... → 2026/04/
    try:
        date_part = product_name.split("_")[4]  # 20260407T053210
        year = date_part[:4]
        month = date_part[4:6]
    except (IndexError, ValueError):
        year = datetime.utcnow().strftime("%Y")
        month = datetime.utcnow().strftime("%m")

    return ARCHIVE_DIR / year / month / f"{product_name}.zip"


def download_product(session, auth, product, dry_run=False):
    """
    단일 Sentinel-1 제품 다운로드.

    - 이미 존재하는 파일은 skip
    - HTTP Range로 부분 다운로드 이어받기
    - 임시 파일 → 원자적 rename
    """
    product_id = product["Id"]
    product_name = product["Name"]
    content_length = product.get("ContentLength", 0)
    size_mb = content_length / (1024 * 1024) if content_length else 0

    dest_path = _get_product_path(product_name)

    # 이미 완료된 파일 skip
    if dest_path.exists() and dest_path.stat().st_size > 0:
        if content_length and dest_path.stat().st_size >= content_length:
            log.info(f"  [SKIP] {product_name} (이미 존재, {size_mb:.0f}MB)")
            return str(dest_path)

    if dry_run:
        log.info(f"  [DRY-RUN] {product_name} ({size_mb:.0f}MB)")
        return None

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_suffix(".zip.tmp")

    download_url = f"{CDSE_DOWNLOAD_BASE}({product_id})/$value"

    # 부분 다운로드 이어받기
    headers = {}
    existing_size = 0
    if temp_path.exists():
        existing_size = temp_path.stat().st_size
        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"
            log.info(
                f"  [RESUME] {product_name}: {existing_size / 1024 / 1024:.0f}MB부터 이어받기"
            )

    log.info(f"  [DOWN] {product_name} ({size_mb:.0f}MB) → {dest_path.name}")

    resp = request_with_retry(
        session, "GET", download_url, auth=auth,
        headers=headers, stream=True, timeout=1800,
    )

    # 스트리밍 저장
    mode = "ab" if existing_size > 0 and resp.status_code == 206 else "wb"
    downloaded = existing_size if mode == "ab" else 0

    with open(temp_path, mode) as f:
        for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):  # 8MB chunks
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)

    # 원자적 rename
    shutil.move(str(temp_path), str(dest_path))
    final_mb = downloaded / (1024 * 1024)
    log.info(f"  [OK] {product_name} ({final_mb:.0f}MB)")
    return str(dest_path)


# ─── 카탈로그 관리 ────────────────────────────────────────────────────
def _extract_metadata(product, file_path):
    """OData 제품 정보에서 카탈로그 엔트리 생성."""
    name = product.get("Name", "")
    attrs = {}
    for attr in product.get("Attributes", []):
        attr_name = attr.get("Name", "")
        attr_val = attr.get("Value", "")
        if attr_name in (
            "orbitDirection", "polarisationChannels",
            "instrumentShortName", "operationalMode",
        ):
            attrs[attr_name] = attr_val

    # 촬영 시간 추출
    content_date = product.get("ContentDate", {})
    sensing_start = content_date.get("Start", "")
    sensing_stop = content_date.get("End", "")

    # footprint
    footprint = ""
    geo_footprint = product.get("GeoFootprint", {})
    if geo_footprint:
        footprint = json.dumps(geo_footprint)

    return {
        "id": product.get("Id", ""),
        "name": name,
        "sensing_start": sensing_start,
        "sensing_stop": sensing_stop,
        "aoi": product.get("_aoi", ""),
        "orbit_direction": attrs.get("orbitDirection", ""),
        "polarization": attrs.get("polarisationChannels", ""),
        "file_path": str(file_path) if file_path else None,
        "file_size_mb": round(
            product.get("ContentLength", 0) / (1024 * 1024), 1
        ),
        "download_timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def load_catalog():
    """기존 카탈로그 로드."""
    if CATALOG_FILE.exists():
        try:
            with open(CATALOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "source": "Copernicus Data Space Ecosystem (CDSE)",
        "data_type": "Sentinel-1 IW GRD",
        "updated_at": "",
        "product_count": 0,
        "aois": list(ARCTIC_AOIS.keys()),
        "products": [],
    }


def save_catalog(catalog):
    """카탈로그 JSON 저장."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    catalog["updated_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    catalog["product_count"] = len(catalog["products"])

    _atomic_write_json(CATALOG_FILE, catalog, ensure_ascii=False, indent=2)

    size_kb = CATALOG_FILE.stat().st_size / 1024
    log.info(
        f"카탈로그 저장: {CATALOG_FILE.name} "
        f"({catalog['product_count']}건, {size_kb:.0f}KB)"
    )


# ─── 디스크 관리 ─────────────────────────────────────────────────────
def get_archive_size_gb():
    """sentinel1_archive 디렉토리 총 크기 (GB) — 완성 .zip + 끊긴 .zip.tmp 등 모든 파일 합산.

    과거엔 *.zip 만 셌는데, 끊긴 부분 다운로드(.zip.tmp, 개당 최대 ~2GB)가 합산에서
    빠져 캡(DEFAULT_MAX_DISK_GB)을 우회 → 14개 누적으로 디스크 100% / 빌드 실패 사고 발생.
    이제 모든 파일을 합산해 캡이 실제 디스크 사용량을 정확히 강제한다.
    """
    if not ARCHIVE_DIR.exists():
        return 0.0
    total = sum(f.stat().st_size for f in ARCHIVE_DIR.rglob("*") if f.is_file())
    return total / (1024 ** 3)


def cleanup_old_products(max_gb):
    """디스크 제한 초과 시 가장 오래된 파일부터 삭제 (FIFO)."""
    # 끊긴 다운로드 찌꺼기(.zip.tmp)는 get_archive_size_gb 가 세지 않아 영영 누수된다.
    # 매 실행 시 무조건 정리한다.
    for tmp in ARCHIVE_DIR.rglob("*.zip.tmp"):
        try:
            size_mb = tmp.stat().st_size / (1024 * 1024)
            tmp.unlink()
            log.info(f"  [DELETE tmp] {tmp.name} ({size_mb:.0f}MB)")
        except OSError:
            pass

    current_gb = get_archive_size_gb()
    if current_gb <= max_gb:
        return

    log.warning(
        f"디스크 사용량 {current_gb:.1f}GB > 제한 {max_gb}GB — 정리 시작"
    )

    # 수정 시간 기준 오래된 순 정렬
    zip_files = sorted(ARCHIVE_DIR.rglob("*.zip"), key=lambda f: f.stat().st_mtime)

    for zf in zip_files:
        if get_archive_size_gb() <= max_gb * 0.8:  # 80% 이하로 정리
            break
        size_mb = zf.stat().st_size / (1024 * 1024)
        log.info(f"  [DELETE] {zf.name} ({size_mb:.0f}MB)")
        zf.unlink()

    # 빈 디렉토리 정리
    for d in sorted(ARCHIVE_DIR.rglob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()

    log.info(f"디스크 정리 완료: {get_archive_size_gb():.1f}GB")


# ─── 메인 실행 ────────────────────────────────────────────────────────
def run_once(target_date=None, dry_run=False, backfill_days=0, max_disk_gb=DEFAULT_MAX_DISK_GB):
    """1회 수집 실행."""
    import requests

    # 인증
    try:
        auth = CDSEAuth()
    except RuntimeError as e:
        log.error(str(e))
        return False

    session = requests.Session()
    session.headers.update({"User-Agent": "ArcticDigitalTwin/1.0"})

    # 날짜 범위 결정
    if target_date:
        end_dt = datetime.strptime(target_date, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=max(backfill_days, 1) - 1)
    elif backfill_days > 0:
        end_dt = datetime.utcnow() - timedelta(days=1)
        start_dt = end_dt - timedelta(days=backfill_days - 1)
    else:
        # 기본: 최근 12일 (Sentinel-1 재방문 주기 ~6일 × 2)
        end_dt = datetime.utcnow() - timedelta(days=1)
        start_dt = end_dt - timedelta(days=11)

    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")
    log.info(f"검색 기간: {start_str} ~ {end_str}")

    if dry_run:
        log.info("[DRY-RUN] 실제 다운로드 없이 카탈로그 쿼리만 수행")

    # 디스크 정리 (다운로드 전)
    if not dry_run:
        cleanup_old_products(max_disk_gb)

    # 카탈로그 로드
    catalog = load_catalog()
    existing_ids = {p["id"] for p in catalog["products"]}

    total_new = 0
    total_downloaded = 0

    # AOI별 검색 + 다운로드
    for aoi_key in ARCTIC_AOIS:
        aoi_name = ARCTIC_AOIS[aoi_key]["name"]
        log.info(f"\n{'─'*40}")
        log.info(f"AOI: {aoi_name} ({aoi_key})")
        log.info(f"{'─'*40}")

        try:
            products = query_sentinel1_products(
                session, auth, aoi_key, start_str, end_str,
            )
        except Exception as e:
            log.error(f"[{aoi_name}] 카탈로그 쿼리 실패: {e}")
            continue

        log.info(f"[{aoi_name}] 검색 결과: {len(products)}건")

        for product in products:
            pid = product.get("Id", "")
            if pid in existing_ids:
                continue

            total_new += 1

            try:
                file_path = download_product(session, auth, product, dry_run=dry_run)
            except Exception as e:
                log.error(f"다운로드 실패: {product.get('Name', '?')} - {e}")
                continue

            if file_path:
                total_downloaded += 1

            # 카탈로그에 추가
            entry = _extract_metadata(product, file_path)
            catalog["products"].append(entry)
            existing_ids.add(pid)

    # 카탈로그 저장
    save_catalog(catalog)

    session.close()

    log.info(f"\n{'='*50}")
    log.info(f"  완료 - 신규 {total_new}건, 다운로드 {total_downloaded}건")
    log.info(f"  디스크 사용: {get_archive_size_gb():.1f}GB / {max_disk_gb}GB")
    log.info(f"  카탈로그: {catalog['product_count']}건")
    log.info(f"{'='*50}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Sentinel-1 IW GRD 빙하 아카이브 자동 수집 (CDSE)"
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="대상 날짜 (YYYY-MM-DD). 기본: 최근 12일",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="실제 다운로드 없이 카탈로그 쿼리만 수행",
    )
    parser.add_argument(
        "--backfill", type=int, default=0,
        help="최근 N일 일괄 수집 (예: --backfill 30)",
    )
    parser.add_argument(
        "--max-disk-gb", type=int, default=DEFAULT_MAX_DISK_GB,
        help=f"아카이브 디스크 제한 (GB, 기본: {DEFAULT_MAX_DISK_GB})",
    )
    parser.add_argument(
        "--cleanup-only", action="store_true",
        help="다운로드 없이 아카이브 정리만 수행 (.zip.tmp 제거 + 캡 초과분 FIFO 삭제). 스케줄러 주기 정리용",
    )
    args = parser.parse_args()

    # 정리 전용 모드: fetch 없이 cleanup_old_products 만 실행 (스케줄러가 주기 호출)
    if args.cleanup_only:
        before = get_archive_size_gb()
        log.info("Sentinel-1 아카이브 정리 시작 (cleanup-only) — 현재 %.1fGB / 캡 %dGB", before, args.max_disk_gb)
        cleanup_old_products(args.max_disk_gb)
        after = get_archive_size_gb()
        log.info("Sentinel-1 아카이브 정리 완료 — %.1fGB → %.1fGB", before, after)
        return

    log.info("Sentinel-1 IW GRD 빙하 아카이브 수집 시작")

    ok = run_once(
        target_date=args.date,
        dry_run=args.dry_run,
        backfill_days=args.backfill,
        max_disk_gb=args.max_disk_gb,
    )

    if not ok:
        log.error("수집 실패. 자격증명 및 네트워크를 확인하세요.")
        sys.exit(1)


if __name__ == "__main__":
    main()
