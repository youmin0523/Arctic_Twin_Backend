"""
generate_czml.py
================
12개월 NOAA/NSIDC 해빙 격자 JSON → 단일 CZML 타임랩스 파일 변환

사용법:
    python generate_czml.py

출력:
    ../data/arctic_ice_timeseries.czml
"""

import json
import calendar
from pathlib import Path

# ── 경로 설정 ─────────────────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
OUTPUT_FILE = DATA_DIR / 'arctic_ice_timeseries.czml'

# ── 설정값 ────────────────────────────────────────────────────
LON_STEP = 3          # 경도 버킷 크기 (도)
SMOOTH_WINDOW = 5     # 이동 평균 윈도우 (양쪽 합산)
DEFAULT_LAT = 89.0    # 데이터 없는 경도 버킷의 기본 위도 (극점 부근)
MIN_LAT_FILTER = 50   # 이 위도 미만 셀은 무시

# ── 농도 임계값별 레이어 정의 ──────────────────────────────────
LAYERS = [
    {
        'threshold': 0.15,
        'id_prefix': 'ice_extent',
        'name': '해빙 경계',
        'fill':    [135, 206, 250, 60],     # 연한 하늘색, 반투명
        'outline': [70,  150, 220, 180],
    },
    {
        'threshold': 0.50,
        'id_prefix': 'ice_pack',
        'name': '유빙대',
        'fill':    [160, 210, 245, 90],     # 중간 파랑
        'outline': [100, 160, 237, 200],
    },
    {
        'threshold': 0.80,
        'id_prefix': 'ice_core',
        'name': '밀빙역',
        'fill':    [210, 235, 255, 130],    # 거의 흰색
        'outline': [190, 215, 250, 230],
    },
]

MONTH_NAMES = [
    '1월', '2월', '3월', '4월', '5월', '6월',
    '7월', '8월', '9월', '10월', '11월', '12월',
]


def month_interval(m: int) -> str:
    """월(1~12)에 대한 ISO 8601 시간 구간 반환."""
    last_day = calendar.monthrange(2023, m)[1]
    return f"2023-{m:02d}-01T00:00:00Z/2023-{m:02d}-{last_day}T23:59:59Z"


def load_month(m: int) -> dict:
    """월별 JSON 파일 로드."""
    path = DATA_DIR / 'monthly' / f'realIceData_month{m:02d}.json'
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def compute_ice_edge(cells: list, threshold: float) -> list:
    """
    격자 셀에서 해빙 경계 폴리곤 좌표를 추출한다.

    알고리즘:
    1. 경도를 LON_STEP 단위로 버킷화
    2. 각 버킷에서 threshold 이상인 셀 중 최남단 위도 추출
    3. 빈 버킷은 이웃 보간
    4. 이동 평균으로 스무싱
    """
    # ── 1. 버킷별 최남단 위도 수집 ──────────────────────────────
    buckets = {}
    for c in cells:
        conc = c['concentration']
        lat = c['lat']
        lon = c['lon']

        if conc < threshold or lat < MIN_LAT_FILTER:
            continue

        bucket = round(lon / LON_STEP) * LON_STEP
        # -180 ~ 177 범위로 정규화
        if bucket >= 180:
            bucket -= 360
        if bucket < -180:
            bucket += 360

        if bucket not in buckets or lat < buckets[bucket]:
            buckets[bucket] = lat

    if len(buckets) < 10:
        return []

    # ── 2. 전체 경도 범위에 대해 보간 채우기 ─────────────────────
    all_lons = list(range(-180, 180, LON_STEP))
    filled = {}

    for lon in all_lons:
        if lon in buckets:
            filled[lon] = buckets[lon]
        else:
            # 가장 가까운 좌/우 값으로 보간
            prev_lat = None
            next_lat = None
            for delta in range(1, len(all_lons)):
                check_left = lon - delta * LON_STEP
                if check_left < -180:
                    check_left += 360
                if check_left in buckets and prev_lat is None:
                    prev_lat = buckets[check_left]

                check_right = lon + delta * LON_STEP
                if check_right >= 180:
                    check_right -= 360
                if check_right in buckets and next_lat is None:
                    next_lat = buckets[check_right]

                if prev_lat is not None and next_lat is not None:
                    break

            if prev_lat and next_lat:
                filled[lon] = (prev_lat + next_lat) / 2
            elif prev_lat:
                filled[lon] = prev_lat
            elif next_lat:
                filled[lon] = next_lat
            else:
                filled[lon] = DEFAULT_LAT

    # ── 3. 이동 평균 스무싱 ─────────────────────────────────────
    raw = [(lon, filled[lon]) for lon in all_lons]
    n = len(raw)
    half = SMOOTH_WINDOW // 2
    smoothed = []

    for i in range(n):
        lat_sum = 0.0
        count = 0
        for j in range(-half, half + 1):
            idx = (i + j) % n
            lat_sum += raw[idx][1]
            count += 1
        smoothed.append((raw[i][0], lat_sum / count))

    return smoothed


def points_to_cartographic(pts: list) -> list:
    """[(lon, lat), ...] → CZML cartographicDegrees [lon, lat, h, ...]"""
    arr = []
    for lon, lat in pts:
        arr.extend([lon, lat, 0])
    return arr


def generate():
    """메인: CZML 파일 생성."""
    print("=" * 60)
    print("  Arctic Sea Ice CZML Generator")
    print("=" * 60)

    czml = [{
        "id": "document",
        "name": "Arctic Sea Ice — Annual Cycle 2023",
        "version": "1.0",
        "clock": {
            "interval": "2023-01-01T00:00:00Z/2023-12-31T23:59:59Z",
            "currentTime": "2023-01-01T00:00:00Z",
            "multiplier": 604800,       # 1초 = 1주일
            "range": "LOOP_STOP",
            "step": "SYSTEM_CLOCK_MULTIPLIER"
        }
    }]

    entity_count = 0

    for m in range(1, 13):
        data = load_month(m)
        cells = data.get('cells', [])
        avail = month_interval(m)
        cell_count = len(cells)

        print(f"\n  Month {m:02d}  |  cells: {cell_count:,}", end='')

        for layer in LAYERS:
            edge = compute_ice_edge(cells, layer['threshold'])

            if len(edge) < 10:
                print(f"  |  {layer['id_prefix']}: SKIP", end='')
                continue

            entity = {
                "id": f"{layer['id_prefix']}_m{m:02d}",
                "name": f"{layer['name']} — {MONTH_NAMES[m-1]}",
                "availability": avail,
                "polygon": {
                    "positions": {
                        "cartographicDegrees": points_to_cartographic(edge)
                    },
                    "material": {
                        "solidColor": {
                            "color": {"rgba": layer['fill']}
                        }
                    },
                    "height": 0,
                    "outline": True,
                    "outlineColor": {"rgba": layer['outline']},
                    "outlineWidth": 1,
                    "perPositionHeight": False,
                }
            }
            czml.append(entity)
            entity_count += 1

        print(f"  |  OK")

    # -- CZML file output --
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(czml, f, ensure_ascii=False)

    size_kb = OUTPUT_FILE.stat().st_size / 1024

    print(f"\n{'=' * 60}")
    print(f"  DONE: {OUTPUT_FILE}")
    print(f"  Entities: {entity_count}")
    print(f"  Size: {size_kb:.1f} KB")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    generate()
