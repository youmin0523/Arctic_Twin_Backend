"""
existing_rl_client.py — (C) 기존 SAC :8001 인퍼런스 클라이언트
=============================================================
rl-pipeline의 /api/rl/infer 를 호출하여
각 Arctic 구간의 빙산 회피 난이도를 예측한다.
"""

import logging

import httpx

logger = logging.getLogger("report-service.rl.existing_rl_client")

RL_BASE_URL = "http://127.0.0.1:8001"

# NSR 7구간 대표 좌표 (중심점)
SEGMENT_COORDS = {
    "베링해협": {"lat": 65.5, "lon": -169.0, "heading": 270, "speed_knots": 12},
    "척치해": {"lat": 69.5, "lon": -164.5, "heading": 270, "speed_knots": 10},
    "동시베리아해": {"lat": 73.0, "lon": 157.5, "heading": 270, "speed_knots": 10},
    "랍테프해": {"lat": 75.0, "lon": 122.5, "heading": 270, "speed_knots": 10},
    "빌키츠키해협": {"lat": 77.0, "lon": 103.5, "heading": 270, "speed_knots": 8},
    "카라해": {"lat": 74.0, "lon": 77.5, "heading": 270, "speed_knots": 10},
    "바렌츠해": {"lat": 74.0, "lon": 37.5, "heading": 270, "speed_knots": 12},
}

DEFAULT_DIFFICULTY = 0.5  # SAC 미가동 시 기본값


async def check_rl_health() -> bool:
    """RL 서버 상태 확인."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{RL_BASE_URL}/api/rl/health")
            return resp.status_code == 200
    except Exception:
        return False


async def get_segment_avoidance_difficulty(
    segment_name: str,
    ice_concentration: float,
    icebergs: list[dict],
    weather: dict,
) -> float:
    """구간별 빙산 회피 난이도 예측.

    기존 SAC 모델에 구간 대표 좌표를 입력하여
    회피 행동의 크기(heading_change + speed_change)를 난이도 지수로 변환.

    Returns
    -------
    float : 0.0(쉬움) ~ 1.0(어려움)
    """
    coords = SEGMENT_COORDS.get(segment_name)
    if not coords:
        return DEFAULT_DIFFICULTY

    # 구간 내 빙산 필터
    seg_icebergs = []
    for berg in icebergs:
        blat = berg.get("lat", 0)
        blon = berg.get("lon", 0)
        if abs(blat - coords["lat"]) < 5 and abs(blon - coords["lon"]) < 15:
            seg_icebergs.append({
                "lat": blat,
                "lon": blon,
                "length_m": berg.get("length_m", 100),
            })

    payload = {
        "ship_state": {
            "lon": coords["lon"],
            "lat": coords["lat"],
            "heading": coords["heading"],
            "speed_knots": coords["speed_knots"],
            "ice_class": "PC5",
            "progress": 0.5,
        },
        "icebergs": seg_icebergs[:10],  # 최대 10개
        "ice_data": {"concentration": ice_concentration},
        "weather": {
            "visibility_km": weather.get("visibility_km", 10),
            "wave_height_m": weather.get("wave_height_m", 1.5),
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{RL_BASE_URL}/api/rl/infer", json=payload
            )
            if resp.status_code != 200:
                return DEFAULT_DIFFICULTY

            result = resp.json()
            # action은 [heading_delta, speed_factor] 리스트로 반환됨
            action = result.get("action", [0.0, 1.0])
            if isinstance(action, list) and len(action) >= 2:
                heading_change = abs(float(action[0]))
                speed_factor = float(action[1])
            else:
                # 혹시 dict 형태로 오면 fallback
                heading_change = abs(float(action.get("heading_delta", 0))) if isinstance(action, dict) else 0.0
                speed_factor = float(action.get("speed_factor", 1.0)) if isinstance(action, dict) else 1.0

            # 행동 크기를 난이도로 변환 (0~1): heading 최대 15°, speed 최소 0.5
            difficulty = min(1.0, (heading_change / 15.0 + abs(1.0 - speed_factor) / 0.5) / 2.0)
            return round(difficulty, 4)

    except Exception as e:
        logger.debug("RL 인퍼런스 실패 (%s): %s", segment_name, e)
        return DEFAULT_DIFFICULTY


async def get_all_segment_difficulties(
    ice_data: dict,
    icebergs: list[dict],
    weather: dict,
) -> dict[str, float]:
    """전체 NSR 7구간의 회피 난이도 조회."""
    # 먼저 RL 서버 확인
    if not await check_rl_health():
        logger.info("RL 서버 미가동 — 기본 난이도(0.5) 사용")
        return {name: DEFAULT_DIFFICULTY for name in SEGMENT_COORDS}

    results = {}
    for seg_name in SEGMENT_COORDS:
        difficulty = await get_segment_avoidance_difficulty(
            seg_name,
            ice_concentration=ice_data.get("mean_conc", 0.5),
            icebergs=icebergs,
            weather=weather,
        )
        results[seg_name] = difficulty

    return results
