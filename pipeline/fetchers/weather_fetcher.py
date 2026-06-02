#!/usr/bin/env python3
"""
Open-Meteo Global Maritime Weather Fetcher
============================================
5개 항로(NSR, NWP, TSR, SUEZ, CAPE)의 실시간 기상 데이터를 수집합니다.

데이터 소스: Open-Meteo API (무료, API 키 불필요)
  - Marine API      : 유의파고 (wave_height, m)
  - Forecast API    : 기온 (temperature_2m, °C), 가시거리 (visibility, m→km)

사용법:
  python weather_fetcher.py              # 최신 데이터 수집
  python weather_fetcher.py --dry-run    # API 호출 없이 설정만 확인
  python weather_fetcher.py --schedule   # 6시간마다 자동 실행

출력: ../../data/weather_latest.json
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen, Request

try:
    from copernicus_wave_fallback import fill_wave_heights as _cop_fill_waves
except ImportError:
    _cop_fill_waves = None

try:
    from copernicus_sst_fallback import fill_sst as _cop_fill_sst
except ImportError:
    _cop_fill_sst = None


def _ssl_context() -> ssl.SSLContext:
    """Return an SSL context using certifi CA bundle if available."""
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    return ctx


SSL_CTX = _ssl_context()

# --- Configuration -----------------------------------------------------------
OUTPUT_DIR = Path(__file__).parent.parent.parent / "data"

MARINE_API = "https://marine-api.open-meteo.com/v1/marine"
FORECAST_API = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 30  # seconds
CHUNK_SIZE = 50       # max waypoints per batch request

DAILY_CALL_LIMIT = 9_000  # Open-Meteo 일 10,000회 제한 → 안전 마진 1,000 확보
USAGE_FILE = OUTPUT_DIR / "weather_api_usage.json"

# --- 5개 항로 웨이포인트 (arcticRoutes.js 기준) --------------------------------

ROUTE_WAYPOINTS: dict[str, list[dict]] = {
    "NSR": [
        {"name": "부산항", "lat": 35.1, "lon": 129.04},
        {"name": "대한해협 우회", "lat": 35.8, "lon": 130.8},
        {"name": "동해 중앙", "lat": 37.5, "lon": 132.5},
        {"name": "홋카이도 서해안", "lat": 43.0, "lon": 138.0},
        {"name": "소야 해협 접근", "lat": 44.5, "lon": 140.5},
        {"name": "소야 해협", "lat": 45.4, "lon": 141.2},
        {"name": "소야 해협 통과", "lat": 45.65, "lon": 141.93},
        {"name": "소야 동방 외해", "lat": 46.0, "lon": 144.0},
        {"name": "오호츠크해", "lat": 47.0, "lon": 145.5},
        {"name": "부솔 해협 접근", "lat": 48.0, "lon": 148.0},
        {"name": "부솔 해협 (쿠릴 패주)", "lat": 46.5, "lon": 151.3},
        {"name": "북태평양 진입", "lat": 46.0, "lon": 154.0},
        {"name": "캄차카 반도 남방 외해", "lat": 48.5, "lon": 160.0},
        {"name": "캄차카 동해안 남부", "lat": 51.5, "lon": 163.5},
        {"name": "캄차카 동해안 중부", "lat": 53.5, "lon": 165.0},
        {"name": "캄차카 동해안 북부", "lat": 56.0, "lon": 166.0},
        {"name": "베링해 진입", "lat": 58.5, "lon": 167.0},
        {"name": "베링해 중부", "lat": 60.0, "lon": 170.0},
        {"name": "아나디르 만", "lat": 63.0, "lon": 175.0},
        {"name": "날짜변경선", "lat": 64.5, "lon": 180.0},
        {"name": "베링해협 접근", "lat": 65.0, "lon": -175.0},
        {"name": "베링해협 서측", "lat": 65.5, "lon": -170.0},
        {"name": "베링해협 통과", "lat": 66.5, "lon": -168.8},
        {"name": "척치해 진입", "lat": 67.5, "lon": -168.0},
        {"name": "척치해 외곽 북상", "lat": 70.5, "lon": -175.0},
        {"name": "브랑겔 섬 서방 북상", "lat": 72.5, "lon": -179.5},
        {"name": "브랑겔 섬 북방 통과", "lat": 72.8, "lon": 178.5},
        {"name": "브랑겔 섬 완전 통과", "lat": 73.0, "lon": 175.0},
        {"name": "동시베리아해 서진", "lat": 73.0, "lon": 168.0},
        {"name": "동시베리아해 중부", "lat": 73.5, "lon": 160.0},
        {"name": "뉴시베리아 제도 동방 외해", "lat": 73.5, "lon": 153.0},
        {"name": "산니코프 해협 진입", "lat": 73.5, "lon": 148.5},
        {"name": "산니코프 해협 통과", "lat": 73.8, "lon": 145.0},
        {"name": "드미트리 랍테프 해협", "lat": 74.0, "lon": 142.0},
        {"name": "랍테프해 진입", "lat": 74.5, "lon": 140.0},
        {"name": "랍테프해 서부", "lat": 77.5, "lon": 130.0},
        {"name": "타이미르 반도 우회", "lat": 77.5, "lon": 115.0},
        {"name": "빌키츠키 접근", "lat": 77.8, "lon": 110.0},
        {"name": "빌키츠키 통과", "lat": 77.92, "lon": 104.0},
        {"name": "카라해 진입", "lat": 77.5, "lon": 98.0},
        {"name": "카라해 중앙", "lat": 77.0, "lon": 80.0},
        {"name": "노바야젬랴 섬 우회", "lat": 77.5, "lon": 69.0},
        {"name": "바렌츠해 동부", "lat": 76.0, "lon": 60.0},
        {"name": "바렌츠해 중앙", "lat": 73.0, "lon": 45.0},
        {"name": "바렌츠해 서부", "lat": 73.5, "lon": 32.0},
        {"name": "노스케이프 북방 외해", "lat": 73.0, "lon": 20.0},
        {"name": "노르웨이해 북부", "lat": 72.0, "lon": 14.0},
        {"name": "노르웨이해 중부", "lat": 69.0, "lon": 6.0},
        {"name": "노르웨이해 남부", "lat": 64.0, "lon": 2.0},
        {"name": "북해 입구", "lat": 60.0, "lon": 1.0},
        {"name": "북해 중부", "lat": 57.0, "lon": 4.5},
        {"name": "로테르담", "lat": 51.9, "lon": 4.5},
    ],
    "NWP": [
        {"name": "부산항", "lat": 35.1, "lon": 129.04},
        {"name": "대한해협 우회", "lat": 35.8, "lon": 130.8},
        {"name": "홋카이도 외곽", "lat": 43.0, "lon": 138.0},
        {"name": "소야 해협", "lat": 45.65, "lon": 141.93},
        {"name": "소야 동방 외해", "lat": 46.0, "lon": 144.0},
        {"name": "오호츠크해", "lat": 47.0, "lon": 145.5},
        {"name": "부솔 해협 접근", "lat": 48.0, "lon": 148.0},
        {"name": "부솔 해협", "lat": 46.5, "lon": 151.3},
        {"name": "북태평양 진입", "lat": 46.0, "lon": 154.0},
        {"name": "캄차카 반도 남방 외해", "lat": 48.5, "lon": 160.0},
        {"name": "캄차카 동해안 남부", "lat": 51.5, "lon": 163.5},
        {"name": "캄차카 동해안 중부", "lat": 53.5, "lon": 165.0},
        {"name": "캄차카 동해안 북부", "lat": 56.0, "lon": 166.0},
        {"name": "베링해 진입", "lat": 58.5, "lon": 167.0},
        {"name": "날짜변경선", "lat": 64.5, "lon": 180.0},
        {"name": "베링해 동부", "lat": 65.0, "lon": -173.0},
        {"name": "베링해협 서측", "lat": 65.5, "lon": -170.0},
        {"name": "베링해협 통과", "lat": 66.5, "lon": -168.8},
        {"name": "척치-보퍼트", "lat": 69.0, "lon": -165.0},
        {"name": "포인트배로 우회", "lat": 71.8, "lon": -156.0},
        {"name": "보퍼트해 연안 우회", "lat": 72.0, "lon": -140.0},
        {"name": "보퍼트해 북상", "lat": 73.5, "lon": -130.0},
        {"name": "뱅크스 섬 북방 진입", "lat": 74.0, "lon": -124.5},
        {"name": "맥클루어 해협 서부", "lat": 74.8, "lon": -119.0},
        {"name": "맥클루어 해협 중앙", "lat": 75.0, "lon": -115.5},
        {"name": "맥클루어 해협 동부", "lat": 74.8, "lon": -112.0},
        {"name": "바이카운트멜빌 해협 서부", "lat": 74.7, "lon": -109.5},
        {"name": "바이카운트멜빌 해협 중앙", "lat": 74.6, "lon": -106.0},
        {"name": "바이카운트멜빌 해협 동부", "lat": 74.5, "lon": -102.5},
        {"name": "배로우 해협 서부", "lat": 74.0, "lon": -97.0},
        {"name": "배로우 해협 중앙", "lat": 74.0, "lon": -93.5},
        {"name": "랭커스터 해협 서부", "lat": 74.0, "lon": -87.0},
        {"name": "랭커스터 해협 중앙", "lat": 74.0, "lon": -84.0},
        {"name": "배핀 만 입구", "lat": 73.3, "lon": -80.5},
        {"name": "배핀 만 서부", "lat": 72.5, "lon": -75.0},
        {"name": "배핀 만 내해", "lat": 70.0, "lon": -65.0},
        {"name": "데이비스 해협", "lat": 65.0, "lon": -60.0},
        {"name": "래브라도 해", "lat": 60.0, "lon": -50.0},
        {"name": "대서양 중앙", "lat": 55.0, "lon": -30.0},
        {"name": "영국 해협 서측", "lat": 50.0, "lon": -10.0},
        {"name": "도버 해협", "lat": 51.0, "lon": 0.0},
        {"name": "로테르담", "lat": 51.9, "lon": 4.5},
    ],
    "TSR": [
        {"name": "부산항", "lat": 35.1, "lon": 129.04},
        {"name": "대한해협 우회", "lat": 35.8, "lon": 130.8},
        {"name": "소야 해협 통과", "lat": 45.65, "lon": 141.93},
        {"name": "소야 동방 외해", "lat": 46.0, "lon": 144.0},
        {"name": "오호츠크해", "lat": 47.0, "lon": 145.5},
        {"name": "부솔 해협 접근", "lat": 48.0, "lon": 148.0},
        {"name": "부솔 해협", "lat": 46.5, "lon": 151.3},
        {"name": "북태평양 진입", "lat": 46.0, "lon": 154.0},
        {"name": "캄차카 반도 남방 외해", "lat": 48.5, "lon": 160.0},
        {"name": "캄차카 동해안 남부", "lat": 51.5, "lon": 163.5},
        {"name": "캄차카 동해안 중부", "lat": 53.5, "lon": 165.0},
        {"name": "캄차카 동해안 북부", "lat": 56.0, "lon": 166.0},
        {"name": "베링해 진입", "lat": 58.5, "lon": 167.0},
        {"name": "날짜변경선", "lat": 64.5, "lon": 180.0},
        {"name": "베링해 동부", "lat": 65.0, "lon": -173.0},
        {"name": "베링해협 서측", "lat": 65.5, "lon": -170.0},
        {"name": "베링해협 통과", "lat": 66.5, "lon": -168.8},
        {"name": "척치해 북방", "lat": 70.0, "lon": -168.0},
        {"name": "북극해 심해", "lat": 80.0, "lon": 180.0},
        {"name": "북극점 돌파", "lat": 89.9, "lon": 0.0},
        {"name": "스발바르 북방", "lat": 80.0, "lon": 10.0},
        {"name": "노르웨이해", "lat": 70.0, "lon": 10.0},
        {"name": "북해", "lat": 62.0, "lon": 5.0},
        {"name": "로테르담", "lat": 51.9, "lon": 4.5},
    ],
    "SUEZ": [
        {"name": "부산항 출항", "lat": 35.10, "lon": 129.04},
        {"name": "제주도 서방 통과", "lat": 33.50, "lon": 127.00},
        {"name": "동중국해", "lat": 29.00, "lon": 124.00},
        {"name": "대만 해협", "lat": 24.00, "lon": 121.50},
        {"name": "루손 해협 서측 외해", "lat": 20.00, "lon": 118.00},
        {"name": "남중국해 북부", "lat": 16.00, "lon": 114.00},
        {"name": "남중국해 중부", "lat": 10.50, "lon": 110.00},
        {"name": "남중국해 남부", "lat": 6.00, "lon": 107.50},
        {"name": "말라카 해협 북단", "lat": 3.50, "lon": 105.50},
        {"name": "말라카 해협 중앙", "lat": 1.80, "lon": 104.20},
        {"name": "싱가포르 (말라카 해협 출구)", "lat": 1.30, "lon": 103.80},
        {"name": "인도양 북서향", "lat": -1.00, "lon": 98.00},
        {"name": "인도양 중앙부", "lat": 5.00, "lon": 90.00},
        {"name": "스리랑카 남방 외해", "lat": 7.00, "lon": 80.00},
        {"name": "아라비아해 동부", "lat": 10.00, "lon": 75.00},
        {"name": "아라비아해 중앙", "lat": 12.00, "lon": 68.00},
        {"name": "아라비아해 서부", "lat": 13.50, "lon": 62.00},
        {"name": "아덴만 진입", "lat": 12.50, "lon": 55.00},
        {"name": "아덴만 중앙", "lat": 12.00, "lon": 49.00},
        {"name": "아덴만 서부 (예멘 외해)", "lat": 12.00, "lon": 45.50},
        {"name": "바브엘만데브 해협 접근", "lat": 12.80, "lon": 44.00},
        {"name": "바브엘만데브 해협 통과", "lat": 13.80, "lon": 43.00},
        {"name": "홍해 남부", "lat": 15.50, "lon": 43.00},
        {"name": "홍해 중앙", "lat": 18.50, "lon": 42.50},
        {"name": "홍해 중부 북상", "lat": 21.50, "lon": 41.00},
        {"name": "홍해 북부", "lat": 24.00, "lon": 38.50},
        {"name": "홍해 최북단", "lat": 26.50, "lon": 36.80},
        {"name": "수에즈만 남단", "lat": 28.50, "lon": 34.50},
        {"name": "수에즈 운하 남단 (Suez)", "lat": 29.93, "lon": 32.55},
        {"name": "그레이트 비터 호수", "lat": 30.42, "lon": 32.42},
        {"name": "이스마일리아", "lat": 30.62, "lon": 32.27},
        {"name": "수에즈 운하 북단 (Port Said)", "lat": 31.25, "lon": 32.33},
        {"name": "지중해 진입 (동지중해)", "lat": 32.00, "lon": 32.50},
        {"name": "동지중해 북부", "lat": 33.50, "lon": 30.00},
        {"name": "크레타 섬 남방 통과", "lat": 34.80, "lon": 24.00},
        {"name": "시칠리아 해협 접근", "lat": 35.50, "lon": 18.00},
        {"name": "시칠리아 해협 통과", "lat": 37.00, "lon": 12.50},
        {"name": "서지중해 동부", "lat": 38.00, "lon": 9.00},
        {"name": "서지중해 중앙", "lat": 38.50, "lon": 4.50},
        {"name": "알보란해", "lat": 37.00, "lon": -1.50},
        {"name": "지브롤터 해협 통과", "lat": 35.90, "lon": -5.40},
        {"name": "포르투갈 남서 외해", "lat": 38.50, "lon": -9.00},
        {"name": "이베리아 반도 서안 북진", "lat": 43.50, "lon": -9.50},
        {"name": "비스케이만 북동부", "lat": 47.00, "lon": -7.00},
        {"name": "우에상 섬 (브르타뉴 외해)", "lat": 48.50, "lon": -5.50},
        {"name": "영국 해협 서측", "lat": 50.00, "lon": -3.00},
        {"name": "영국 해협 동측", "lat": 51.10, "lon": 1.50},
        {"name": "로테르담 (목적항)", "lat": 51.90, "lon": 4.50},
    ],
    "CAPE": [
        {"name": "부산항 출항", "lat": 35.10, "lon": 129.04},
        {"name": "제주도 서방 통과", "lat": 33.50, "lon": 127.00},
        {"name": "동중국해", "lat": 29.00, "lon": 124.00},
        {"name": "대만 해협", "lat": 24.00, "lon": 121.50},
        {"name": "루손 해협 서측 외해", "lat": 20.00, "lon": 118.00},
        {"name": "남중국해 북부", "lat": 16.00, "lon": 114.00},
        {"name": "남중국해 중부", "lat": 10.50, "lon": 110.00},
        {"name": "남중국해 남부", "lat": 6.00, "lon": 107.50},
        {"name": "말라카 해협 북단", "lat": 3.50, "lon": 105.50},
        {"name": "말라카 해협 중앙", "lat": 1.80, "lon": 104.20},
        {"name": "싱가포르 (말라카 해협 출구)", "lat": 1.30, "lon": 103.80},
        {"name": "자바해 서측", "lat": -4.00, "lon": 107.00},
        {"name": "순다 해협 통과 (자바-수마트라)", "lat": -6.20, "lon": 105.80},
        {"name": "인도양 북동부 진입", "lat": -8.00, "lon": 103.00},
        {"name": "인도양 북부 남하", "lat": -12.00, "lon": 97.00},
        {"name": "인도양 중앙 서남향", "lat": -18.00, "lon": 90.00},
        {"name": "인도양 중남부", "lat": -25.00, "lon": 80.00},
        {"name": "인도양 남부", "lat": -29.00, "lon": 70.00},
        {"name": "인도양 남서부 (마다가스카르 동방)", "lat": -33.00, "lon": 58.00},
        {"name": "아굴하스 곶 동방 외해", "lat": -35.50, "lon": 40.00},
        {"name": "희망봉 동방 접근", "lat": -36.00, "lon": 30.00},
        {"name": "아굴하스 뱅크 통과", "lat": -35.50, "lon": 22.00},
        {"name": "희망봉 (Cape of Good Hope)", "lat": -34.40, "lon": 18.50},
        {"name": "케이프타운 북서방 통과", "lat": -33.00, "lon": 16.00},
        {"name": "대서양 동부 남부 북상", "lat": -28.00, "lon": 12.00},
        {"name": "나미비아 외해", "lat": -20.00, "lon": 8.00},
        {"name": "앙골라 외해", "lat": -12.00, "lon": 5.00},
        {"name": "콩고 외해", "lat": -4.00, "lon": 2.00},
        {"name": "기니만 동부", "lat": 3.00, "lon": 0.00},
        {"name": "기니만 북부", "lat": 8.00, "lon": -2.50},
        {"name": "서아프리카 북상", "lat": 14.00, "lon": -8.00},
        {"name": "서아프리카 서안", "lat": 21.00, "lon": -17.00},
        {"name": "카나리아 제도 외해", "lat": 28.00, "lon": -18.00},
        {"name": "모로코 서방 외해", "lat": 35.00, "lon": -15.50},
        {"name": "포르투갈 남서 외해", "lat": 38.50, "lon": -10.50},
        {"name": "이베리아 반도 서안 북진", "lat": 43.50, "lon": -9.50},
        {"name": "비스케이만 북동부", "lat": 47.00, "lon": -7.00},
        {"name": "우에상 섬 (브르타뉴 외해)", "lat": 48.50, "lon": -5.50},
        {"name": "영국 해협 서측", "lat": 50.00, "lon": -3.00},
        {"name": "영국 해협 동측", "lat": 51.10, "lon": 1.50},
        {"name": "로테르담 (목적항)", "lat": 51.90, "lon": 4.50},
    ],
}


# --- Daily API call counter --------------------------------------------------

def _load_usage() -> dict:
    """일일 API 호출 횟수 파일 로드. 날짜가 바뀌면 자동 리셋."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            usage = json.load(f)
        if usage.get("date") != today:
            return {"date": today, "calls": 0}
        return usage
    except (FileNotFoundError, json.JSONDecodeError):
        return {"date": today, "calls": 0}


def _save_usage(usage: dict) -> None:
    """일일 API 호출 횟수 파일 저장."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(usage, f)


def _check_budget(needed: int) -> bool:
    """needed 만큼의 API 호출이 일일 한도 내에서 가능한지 확인."""
    usage = _load_usage()
    remaining = DAILY_CALL_LIMIT - usage["calls"]
    if needed > remaining:
        print(f"  [LIMIT] 일일 API 호출 한도 도달: "
              f"사용 {usage['calls']}/{DAILY_CALL_LIMIT}, 필요 {needed}, 잔여 {remaining}")
        print(f"  [LIMIT] 이번 주기 건너뜀 - 기존 캐시 데이터 유지")
        return False
    return True


def _record_calls(count: int) -> None:
    """실제 수행한 API 호출 횟수를 기록."""
    usage = _load_usage()
    usage["calls"] += count
    _save_usage(usage)
    print(f"  [USAGE] 오늘 API 호출: {usage['calls']}/{DAILY_CALL_LIMIT}")


def _estimate_calls() -> int:
    """이번 실행에 필요한 예상 API 호출 횟수 계산."""
    total = 0
    for waypoints in ROUTE_WAYPOINTS.values():
        n_chunks = (len(waypoints) + CHUNK_SIZE - 1) // CHUNK_SIZE
        total += n_chunks * 2  # marine + forecast per chunk
    return total


# --- Helpers -----------------------------------------------------------------

def _http_get(url: str) -> dict | list:
    """HTTP GET -> parsed JSON (stdlib only)."""
    try:
        req = Request(url, headers={"User-Agent": "ArcticDigitalTwin/2.0"})
        with urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {url}") from e
    except URLError as e:
        raise RuntimeError(f"URL Error: {e.reason}") from e


def _chunked(lst: list, n: int):
    """Yield successive chunks of size n from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# --- Open-Meteo Batch API calls ---------------------------------------------

def fetch_marine_batch(waypoints: list[dict]) -> list[dict]:
    """Open-Meteo Marine API에서 유의파고(m), 파향(°), 파주기(s)를 배치 조회.

    반환 원소: {"height": float|None, "direction": float|None, "period": float|None}
    - direction: meteorological convention (파가 오는 방향, 0°=북)
    - period: 평균 파주기 (s)
    """
    lats = ",".join(str(wp["lat"]) for wp in waypoints)
    lons = ",".join(str(wp["lon"]) for wp in waypoints)
    url = (
        f"{MARINE_API}?latitude={lats}&longitude={lons}"
        f"&current=wave_height,wave_direction,wave_period"
        f"&cell_selection=nearest"
    )

    data = _http_get(url)
    # 단일 좌표: dict 반환 / 다중 좌표: list 반환
    if isinstance(data, dict):
        data = [data]

    results: list[dict] = []
    for entry in data:
        current = entry.get("current", {})
        h_raw = current.get("wave_height")
        d_raw = current.get("wave_direction")
        p_raw = current.get("wave_period")
        results.append({
            "height": round(float(h_raw), 2) if h_raw is not None else None,
            "direction": round(float(d_raw), 1) if d_raw is not None else None,
            "period": round(float(p_raw), 2) if p_raw is not None else None,
        })

    return results


def fetch_forecast_batch(waypoints: list[dict]) -> list[tuple[float | None, float | None]]:
    """Open-Meteo Forecast API에서 기온(°C)과 가시거리(km)를 배치 조회."""
    lats = ",".join(str(wp["lat"]) for wp in waypoints)
    lons = ",".join(str(wp["lon"]) for wp in waypoints)
    url = f"{FORECAST_API}?latitude={lats}&longitude={lons}&current=temperature_2m,visibility&cell_selection=nearest"

    data = _http_get(url)
    if isinstance(data, dict):
        data = [data]

    results: list[tuple[float | None, float | None]] = []
    for entry in data:
        current = entry.get("current", {})
        temp_raw = current.get("temperature_2m")
        vis_raw = current.get("visibility")

        temp_c = round(float(temp_raw), 1) if temp_raw is not None else None
        # Open-Meteo visibility: meters -> km
        vis_km = round(float(vis_raw) / 1000.0, 2) if vis_raw is not None else None

        results.append((temp_c, vis_km))

    return results


# --- Route-level processing --------------------------------------------------

def fetch_route_weather(route_key: str, waypoints: list[dict], dry_run: bool = False) -> dict:
    """단일 항로의 전체 웨이포인트 기상 데이터 수집."""
    print(f"\n  [{route_key}] {len(waypoints)} waypoints")

    if dry_run:
        wp_results = [
            {"name": wp["name"], "lat": wp["lat"], "lon": wp["lon"],
             "wave_height_m": None, "wave_direction_deg": None, "wave_period_s": None,
             "temperature_c": None, "visibility_km": None}
            for wp in waypoints
        ]
        return {"waypoints": wp_results, "route_summary": compute_route_summary(wp_results)}

    # 배치 marine API (청킹) — 각 원소: {"height","direction","period"}
    wave_data: list[dict] = []
    for chunk in _chunked(waypoints, CHUNK_SIZE):
        try:
            wave_data.extend(fetch_marine_batch(chunk))
        except RuntimeError as e:
            print(f"    [WARN] Marine API: {e}")
            wave_data.extend([{"height": None, "direction": None, "period": None}] * len(chunk))

    # 배치 forecast API (청킹)
    forecast_data: list[tuple[float | None, float | None]] = []
    for chunk in _chunked(waypoints, CHUNK_SIZE):
        try:
            forecast_data.extend(fetch_forecast_batch(chunk))
        except RuntimeError as e:
            print(f"    [WARN] Forecast API: {e}")
            forecast_data.extend([(None, None)] * len(chunk))

    # //! [Original Code] 배치 결과만으로 웨이포인트 결합 (null 다수 발생)
    # //* [Modified Code] null인 좌표를 개별 재시도 + 인접 보간으로 보완
    # ── Stage 1: null인 좌표만 개별 재시도 (Open-Meteo는 개별 호출 시 더 잘 응답) ──
    null_indices = [
        i for i, (temp, vis) in enumerate(forecast_data)
        if temp is None or vis is None
    ]
    if null_indices:
        print(f"    [RETRY] Forecast null at {len(null_indices)}/{len(forecast_data)} points, retrying individually...")
        for idx in null_indices:
            wp = waypoints[idx]
            try:
                retry_result = fetch_forecast_batch([wp])
                if retry_result and retry_result[0] != (None, None):
                    old_temp, old_vis = forecast_data[idx]
                    new_temp, new_vis = retry_result[0]
                    forecast_data[idx] = (
                        new_temp if new_temp is not None else old_temp,
                        new_vis if new_vis is not None else old_vis,
                    )
            except RuntimeError:
                pass  # 개별 재시도 실패 → 보간으로 처리
            time.sleep(0.05)  # rate limit 방지

    # ── Stage 2: 여전히 null인 좌표는 인접 웨이포인트 값으로 선형 보간 ──
    _interpolate_nulls(forecast_data)

    # 웨이포인트별 결합
    wp_results = []
    for i, wp in enumerate(waypoints):
        wave = wave_data[i] if i < len(wave_data) else {"height": None, "direction": None, "period": None}
        temp, vis = forecast_data[i] if i < len(forecast_data) else (None, None)
        wp_results.append({
            "name": wp["name"],
            "lat": wp["lat"],
            "lon": wp["lon"],
            "wave_height_m": wave.get("height"),
            "wave_direction_deg": wave.get("direction"),
            "wave_period_s": wave.get("period"),
            "temperature_c": temp,
            "visibility_km": vis,
        })

    # Open-Meteo 소스 태깅 + Copernicus Arctic wave fallback
    for wp in wp_results:
        if wp["wave_height_m"] is not None:
            wp["wave_source"] = "open-meteo"
    if _cop_fill_waves is not None:
        wp_results = _cop_fill_waves(wp_results)

    # Copernicus SST (해수면 온도) 조회
    if _cop_fill_sst is not None:
        wp_results = _cop_fill_sst(wp_results)

    # 요약 출력
    summary = compute_route_summary(wp_results)
    print(f"    max_wave={summary['max_wave_height_m']}m  "
          f"min_temp={summary['min_temperature_c']}C  "
          f"min_vis={summary['min_visibility_km']}km")

    return {"waypoints": wp_results, "route_summary": summary}


def _interpolate_nulls(data: list[tuple[float | None, float | None]]) -> None:
    """
    forecast_data 리스트 내 null 값을 인접 유효값으로 선형 보간 (in-place).
    양쪽 끝 null은 가장 가까운 유효값으로 채움.
    """
    n = len(data)
    if n == 0:
        return

    # 기온(temp) 보간
    temps = [t for t, _ in data]
    _fill_array(temps)
    # 가시거리(vis) 보간
    vises = [v for _, v in data]
    _fill_array(vises)

    for i in range(n):
        data[i] = (temps[i], vises[i])


def _fill_array(arr: list[float | None]) -> None:
    """1D 배열의 None 값을 인접값 선형 보간으로 채움 (in-place)."""
    n = len(arr)
    # forward fill: 왼쪽 유효값 기록
    last_valid_idx = None
    for i in range(n):
        if arr[i] is not None:
            # 이전 null 구간 보간
            if last_valid_idx is not None and i - last_valid_idx > 1:
                v0, v1 = arr[last_valid_idx], arr[i]
                assert v0 is not None and v1 is not None
                span = i - last_valid_idx
                for j in range(last_valid_idx + 1, i):
                    t = (j - last_valid_idx) / span
                    arr[j] = round(v0 * (1 - t) + v1 * t, 2)
            last_valid_idx = i

    # 양쪽 끝 null 처리: nearest fill
    first_valid = next((i for i in range(n) if arr[i] is not None), None)
    if first_valid is None:
        return  # 전부 null → 포기
    for i in range(first_valid):
        arr[i] = arr[first_valid]
    last_valid = next((i for i in range(n - 1, -1, -1) if arr[i] is not None), None)
    if last_valid is not None:
        for i in range(last_valid + 1, n):
            arr[i] = arr[last_valid]


def compute_route_summary(waypoints: list[dict]) -> dict:
    """항로 전체 최악값 집계."""
    waves = [w["wave_height_m"] for w in waypoints if w["wave_height_m"] is not None]
    temps = [w["temperature_c"] for w in waypoints if w["temperature_c"] is not None]
    visib = [w["visibility_km"] for w in waypoints if w["visibility_km"] is not None]
    ssts = [w["sst_c"] for w in waypoints if w.get("sst_c") is not None]

    max_wave = round(max(waves), 2) if waves else None
    min_temp = round(min(temps), 1) if temps else None
    min_vis = round(min(visib), 2) if visib else None
    min_sst = round(min(ssts), 1) if ssts else None
    max_sst = round(max(ssts), 1) if ssts else None

    return {
        "max_wave_height_m": max_wave,
        "min_temperature_c": min_temp,
        "min_visibility_km": min_vis,
        "min_sst_c": min_sst,
        "max_sst_c": max_sst,
        "is_temp_below_minus_10": (min_temp < -10.0) if min_temp is not None else False,
    }


def compute_global_summary(routes: dict) -> dict:
    """전체 항로 최악값 집계 (하위호환용)."""
    all_waves = []
    all_temps = []
    all_visib = []
    for route_data in routes.values():
        s = route_data.get("route_summary", {})
        if s.get("max_wave_height_m") is not None:
            all_waves.append(s["max_wave_height_m"])
        if s.get("min_temperature_c") is not None:
            all_temps.append(s["min_temperature_c"])
        if s.get("min_visibility_km") is not None:
            all_visib.append(s["min_visibility_km"])

    max_wave = round(max(all_waves), 2) if all_waves else None
    min_temp = round(min(all_temps), 1) if all_temps else None
    min_vis = round(min(all_visib), 2) if all_visib else None

    return {
        "max_wave_height_m": max_wave,
        "min_temperature_c": min_temp,
        "min_visibility_km": min_vis,
        "is_temp_below_minus_10": (min_temp < -10.0) if min_temp is not None else False,
    }


# --- Main --------------------------------------------------------------------

def run(dry_run: bool = False) -> int:
    """메인 실행. 성공 시 0, 실패 시 1 반환."""
    print(f"\n{'=' * 60}")
    print("  Open-Meteo Global Maritime Weather Fetcher")
    print(f"  Routes: {', '.join(ROUTE_WAYPOINTS.keys())}")
    print(f"  Total waypoints: {sum(len(v) for v in ROUTE_WAYPOINTS.values())}")
    print(f"  Dry-run: {dry_run}")
    print(f"{'=' * 60}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_DIR / "weather_latest.json"

    # 일일 API 호출 예산 확인
    estimated = _estimate_calls()
    print(f"\n  Estimated API calls this cycle: {estimated}")
    if not dry_run and not _check_budget(estimated):
        return 0  # 한도 도달 시 기존 캐시 유지, 정상 종료

    print("\n[1/3] Fetching weather for all routes...")
    actual_calls = 0
    routes: dict[str, dict] = {}
    for route_key, waypoints in ROUTE_WAYPOINTS.items():
        routes[route_key] = fetch_route_weather(route_key, waypoints, dry_run=dry_run)
        if not dry_run:
            n_chunks = (len(waypoints) + CHUNK_SIZE - 1) // CHUNK_SIZE
            actual_calls += n_chunks * 2
            time.sleep(0.3)  # polite interval between routes

    # 호출 횟수 기록
    if not dry_run:
        _record_calls(actual_calls)

    print("\n[2/3] Global worst-case aggregation...")
    global_summary = compute_global_summary(routes)
    print(f"  max wave   : {global_summary['max_wave_height_m']}m")
    print(f"  min temp   : {global_summary['min_temperature_c']}C")
    print(f"  min vis    : {global_summary['min_visibility_km']}km")
    print(f"  below -10C : {global_summary['is_temp_below_minus_10']}")

    output = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Open-Meteo (Marine API + Weather Forecast API)",
        "dry_run": dry_run,
        "routes": routes,
        "route_summary": global_summary,  # 하위호환
    }

    output["copernicus_wave_fallback"] = any(
        wp.get("wave_source") == "copernicus"
        for rd in routes.values()
        for wp in rd.get("waypoints", [])
    )

    print(f"\n[3/3] Saving: {out_file}")
    if not dry_run:
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print("  [OK] weather_latest.json saved")
    else:
        print("  (dry-run: file write skipped)")

    return 0


# --- CLI ---------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Open-Meteo Global Maritime Weather Fetcher"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="API 호출 및 파일 저장 없이 설정만 확인"
    )
    parser.add_argument(
        "--schedule", action="store_true",
        help="6시간마다 자동 실행 모드 (무한 루프)"
    )
    args = parser.parse_args()

    if args.schedule:
        import sched
        scheduler = sched.scheduler(time.time, time.sleep)

        def _scheduled_run():
            run(dry_run=False)
            scheduler.enter(21600, 1, _scheduled_run)  # 6시간 = 21600초

        print("[Scheduler] 6시간 주기 기상 데이터 수집 예약됨")
        print("[Scheduler] 즉시 1회 실행 후 6시간마다 반복...")
        scheduler.enter(0, 1, _scheduled_run)
        scheduler.run()
    else:
        rc = run(dry_run=args.dry_run)
        sys.exit(rc)


if __name__ == "__main__":
    main()
