"""
models.py
=========
Icebreaker escort system — data models & initial fleet.

Pure Python TypedDict / Literal definitions to match the existing
arctic_master_router.py style (no Pydantic dependency).
"""

from __future__ import annotations
from typing import TypedDict, Literal


IcebreakerStatus = Literal[
    "idle",          # 모항 대기
    "dispatched",    # 본선 향해 이동 중
    "rendezvous",    # 합류 지점 도달, 대기
    "escorting",     # 동행 쇄빙 중
    "released",      # 해제됨 — 모항 복귀 중
]


class Position(TypedDict):
    lat: float
    lon: float


class Icebreaker(TypedDict):
    id: str
    name_ko: str
    position: Position
    home_port: str
    status: IcebreakerStatus
    speed_knots: float
    ice_class: str                   # "Arc7" | "Arc9" (RMRS 등급)
    breakable_thickness_m: float
    escorting_ship_id: str | None


# ═══════════════════════════════════════════════════════════════════════════════
# RMRS Arc ↔ IACS Polar Class 근사 매핑
#
# 러시아 해사선급(RMRS)의 Arc 등급과 IACS Polar Class는 1:1 동치가 아니며,
# 빙해항행 능력(운항 가능 빙질/두께/계절)을 기준으로 한 근사 매핑임.
# RIO 계산 시 calculate_rio()는 PC 키를 요구하므로 변환 필요.
# ═══════════════════════════════════════════════════════════════════════════════

ARC_TO_PC: dict[str, str] = {
    "Arc9": "PC3",   # year-round, multi-year ice
    "Arc7": "PC4",   # year-round, thick FY + old ice
    "Arc6": "PC5",
    "Arc5": "PC6",
    "Arc4": "PC7",
}


def arc_to_pc(arc_class: str) -> str:
    """RMRS Arc 등급을 IACS PC 등급으로 변환. 미매핑 입력은 그대로 반환."""
    return ARC_TO_PC.get(arc_class, arc_class)


# ═══════════════════════════════════════════════════════════════════════════════
# 초기 쇄빙선 함대 — 한국 보유 쇄빙선 1척(아라온호), 사전 배치 시나리오
#
# 한국은 현재 극지연구선 '아라온(Araon)' 1척을 운용 중.
#
# 아라온호 공개 제원 (KOPRI 극지연구소)
#   - 취역: 2009, 운용: 극지연구소
#   - 실제 모항: 인천항 (37.4513°N, 126.5970°E)
#   - 최대 항속: 약 16 knots
#   - 연속쇄빙 능력: 1.0m 두께 @ 3knots
#   - Korean Register ice class PL-10 (IACS PC 근사: PC5~PC6 중간)
#
# ── 사전 배치 시나리오 (Pre-positioning Scenario) ─────────────────────────────
# 현재 아라온 실제 모항은 인천이지만, NSR 항해 지원을 위한 사전 배치 거점으로
# Wrangel Island (브랑겔 섬, 71.0°N 179.5°E) 을 가정. NSR 경로는 실제로
# 브랑겔 섬 북방 ~150km 를 통과하므로, Araon 이 섬 북안에 사전 정박해 있다가
# 본선 진입 시점에 단거리 이동으로 합류 가능.
#
# Nome 배치도 검토했으나, Nome-Chukchi 직선 추격 시 Araon 16kn vs 본선 15kn
# 의 closing rate 이 경로 각도 상 1kn 이하로 떨어져 사실상 따라잡기 불가
# (first run: 605km → 622km 로 거리가 오히려 벌어짐). 사전 배치는 경로상
# 거점이어야만 유효.
#
# 근거 / 내러티브:
#   - 러시아는 실제로 Wrangel Island 에 기상관측소 및 국경수비대 기지 운영
#   - "한-러 북극 연구 협력 거점" 가정 (가상) — 극지연구소가 브랑겔에 임시
#     전진기지를 두고 Araon 을 NSR 항로 서포트용으로 순환 배치
#   - POLARIS 상 Arc4 이하 본선이 NSR 동부(척치/동시베리아) 통과 시 에스코트
#     수요가 가장 높은 구간과 일치
# ═══════════════════════════════════════════════════════════════════════════════

INITIAL_ICEBREAKERS: list[Icebreaker] = [
    {
        "id": "ib-araon",
        "name_ko": "아라온",
        # //* [Modified] Wrangel 북방 '연안' 거점으로 재배치 (71.0 → 71.7).
        #   프론트 ESCORT_ASSETS.NSR.home 과 일치시켜 trace/Live 아라온이
        #   서로 다른 좌표(71.0 vs 71.7)에 2척으로 보이던 중복 제거.
        "position": {"lat": 71.7, "lon": 179.5},  # Wrangel 북방 연안
        "home_port": "Wrangel Is. (사전배치)",
        "status": "idle",
        "speed_knots": 16.0,
        "ice_class": "Arc6",   # KR PL-10 ≈ Arc6
        "breakable_thickness_m": 1.0,
        "escorting_ship_id": None,
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
# 항로별 호위 함대 (Route-aware fleet)
#
# 각 북극항로는 관할·지리가 달라 호위 자산과 사전배치 모항이 다르다. 프론트엔드
# useAraonControl.js 의 ESCORT_ASSETS 와 좌표·자산을 일치시킨다(단일 진실원).
#   NSR → 아라온(KOPRI)        @ Wrangel 북방 연안 (71.7N, 179.5E)
#   NWP → CCGS 쇄빙선(캐 해경)  @ Resolute Passage (74.55N, -94.9W)
#   TSR → 원자력 쇄빙선(Rosatom) @ Isfjorden/Longyearbyen (78.28N, 15.2E)
#
# 모항은 각 항로가 실제로 ~400km 내로 근접 통과하는 실재 위치여야 호위가 발동된다
# (dispatcher 의 사전배치 거점 근접 판정). 재생성 후 summary 의 calls>0 로 확인.
# ═══════════════════════════════════════════════════════════════════════════════

FLEET_BY_ROUTE: dict[str, list[Icebreaker]] = {
    "NSR": INITIAL_ICEBREAKERS,
    "NWP": [
        {
            "id": "ib-ccgs",
            "name_ko": "CCGS 쇄빙선",
            "position": {"lat": 74.55, "lon": -94.9},  # Resolute Passage
            "home_port": "Resolute Passage (사전배치)",
            "status": "idle",
            "speed_knots": 15.0,
            "ice_class": "Arc7",   # CCGS Louis S. St-Laurent 급 ≈ Arc7
            "breakable_thickness_m": 1.2,
            "escorting_ship_id": None,
        },
    ],
    "TSR": [
        {
            "id": "ib-rosatom",
            "name_ko": "원자력 쇄빙선",
            "position": {"lat": 78.28, "lon": 15.2},  # Isfjorden(Longyearbyen)
            "home_port": "Isfjorden/Longyearbyen (사전배치)",
            "status": "idle",
            "speed_knots": 18.0,
            "ice_class": "Arc9",   # Rosatomflot Arktika 급 원자력 ≈ Arc9
            "breakable_thickness_m": 2.5,
            "escorting_ship_id": None,
        },
    ],
    # ── 남극(양극 운항) 함대 — 아라온이 남반구 여름에 양극 운항 ───────────────
    # 한국 극지선은 아라온 1척이라 북극(NSR)·남극(ROSS/PENINSULA)을 계절별로 오간다.
    # 남극 항로에서도 동일 아라온이 호위하되 사전배치 거점만 각 항로 해빙역 입구에 둔다.
    # 프론트 ESCORT_ASSETS.ROSS/PENINSULA 의 home 좌표와 일치(단일 진실원).
    "ROSS": [
        {
            "id": "ib-araon-ross",
            "name_ko": "아라온",
            "position": {"lat": -72.0, "lon": 168.5},  # 빅토리아랜드 외해(로스해 입구)
            "home_port": "빅토리아랜드 외해 (남극 전진)",
            "status": "idle",
            "speed_knots": 16.0,
            "ice_class": "Arc6",   # KR PL-10 ≈ Arc6
            "breakable_thickness_m": 1.0,
            "escorting_ship_id": None,
        },
    ],
    "PENINSULA": [
        {
            "id": "ib-araon-pen",
            "name_ko": "아라온",
            "position": {"lat": -61.4, "lon": -60.2},  # 사우스셰틀랜드 북방
            "home_port": "사우스셰틀랜드 북방 (남극 전진)",
            "status": "idle",
            "speed_knots": 16.0,
            "ice_class": "Arc6",   # KR PL-10 ≈ Arc6
            "breakable_thickness_m": 1.0,
            "escorting_ship_id": None,
        },
    ],
}


def fleet_for_route(route_name: str) -> list[Icebreaker]:
    """항로 키에 대응하는 초기 함대 반환. 미등록 항로는 NSR(아라온) 폴백."""
    return FLEET_BY_ROUTE.get(route_name, INITIAL_ICEBREAKERS)
