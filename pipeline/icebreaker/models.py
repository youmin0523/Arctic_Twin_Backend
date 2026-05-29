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
        "position": {"lat": 71.0, "lon": 179.5},  # Wrangel Island 북안
        "home_port": "Wrangel Is. (사전배치)",
        "status": "idle",
        "speed_knots": 16.0,
        "ice_class": "Arc6",   # KR PL-10 ≈ Arc6
        "breakable_thickness_m": 1.0,
        "escorting_ship_id": None,
    },
]
