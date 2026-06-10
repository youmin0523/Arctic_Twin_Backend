"""
route_scorer.py
===============
POLARIS RIO 기반 항로별·날짜별 위험도 스코어링.

arctic_master_router.py의 calculate_rio()를 재사용하여
출항 캘린더와 항로별 비교 점수를 생성한다.
"""

import sys
import logging
from datetime import date, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import cast

import numpy as np

# arctic_master_router.py import를 위한 sys.path 설정.
# 두 가지 레이아웃 모두 지원:
#   - 로컬: Digital_twin/backend/services/report-service/modules/route_scorer.py
#           → parents[3] = Digital_twin/
#   - HF Space: <root>/report-service/modules/route_scorer.py
#           → parents[2] = <root>
_candidates = [
    Path(__file__).resolve().parents[3] / "pipeline",            # backend/services/report-service/modules → backend/pipeline
    Path(__file__).resolve().parents[3] / "backend" / "pipeline",
    Path(__file__).resolve().parents[2] / "backend" / "pipeline",
    Path(__file__).resolve().parents[1] / "backend" / "pipeline",
]
for _p in _candidates:
    if _p.exists():
        _pipeline_dir = str(_p)
        if _pipeline_dir not in sys.path:
            sys.path.insert(0, _pipeline_dir)
        break
else:
    raise ImportError(
        "arctic_master_router.py 위치를 찾을 수 없습니다. "
        f"확인된 후보: {[str(p) for p in _candidates]}"
    )

from arctic_master_router import calculate_rio, IceCondition  # noqa: E402

logger = logging.getLogger("report-service.route_scorer")


# ── 항로 구간 정의 ──────────────────────────────────────────────
ARCTIC_SEGMENTS = {
    "NSR": [
        {"name": "베링해협", "lat_range": (64, 67), "lon_range": (-172, -166)},
        {"name": "척치해", "lat_range": (67, 72), "lon_range": (-172, -157)},
        {"name": "동시베리아해", "lat_range": (70, 76), "lon_range": (140, 175)},
        {"name": "랍테프해", "lat_range": (72, 78), "lon_range": (105, 140)},
        {"name": "빌키츠키해협", "lat_range": (76, 78), "lon_range": (100, 107)},
        {"name": "카라해", "lat_range": (70, 78), "lon_range": (55, 100)},
        {"name": "바렌츠해", "lat_range": (70, 78), "lon_range": (20, 55)},
    ],
    "NWP": [
        {"name": "보퍼트해", "lat_range": (69, 76), "lon_range": (-150, -120)},
        {"name": "앰마누엘반도", "lat_range": (72, 76), "lon_range": (-120, -100)},
        {"name": "랭커스터해협", "lat_range": (73, 76), "lon_range": (-100, -80)},
        {"name": "배핀만", "lat_range": (66, 76), "lon_range": (-80, -55)},
        {"name": "데이비스해협", "lat_range": (60, 67), "lon_range": (-65, -50)},
    ],
    "TSR": [
        {"name": "북극해중앙", "lat_range": (80, 90), "lon_range": (-180, 180)},
        {"name": "그린란드해", "lat_range": (72, 80), "lon_range": (-30, 10)},
        {"name": "노르웨이해", "lat_range": (65, 72), "lon_range": (-10, 15)},
    ],
}

# 항상 안전한 항로
SAFE_ROUTES = {"SUEZ", "CAPE"}


# ── 농도 → 빙종 매핑 ──────────────────────────────────────────
# 항로 채널 농도 보정 계수 — 세그먼트 박스 P90 도 개수역 섞임으로 항로가 실제 통과하는
# 빙 채널보다 옅다. 12개월×3항로 실측 검증으로 보정(여름 전부 안전 유지, 겨울 빙급별 차등 위험).
CHANNEL_DILUTION_FACTOR = 1.6


def concentration_to_ice_conditions(conc: float) -> list[dict]:
    """해빙 농도 값(0~1)을 POLARIS IceCondition 리스트로 변환.

    단일 농도 값에서 대표 빙종 혼재를 추정한다. 농도가 높을수록(특히 고위도·결빙기)
    노후·다년빙(Multi-Year)·압축빙(Ridged)이 포함된다고 가정한다 — 농도만으로는 빙종을
    알 수 없으나, 고농도 극지빙은 실제로 다년빙·릿지를 포함하므로(POLARIS상 PC5 같은
    유능 빙급도 이때만 RIO가 음수로 떨어짐) 계절·빙급별 위험 차등을 반영하기 위함이다.
    (농도값은 score_departure_day 에서 항로 채널 대표 농도로 보정된 값을 받는다.)
    """
    c = min(1.0, max(0.0, conc))
    if c < 0.05:
        return [{"type": "Open Water", "concentration_tenths": 1.0}]
    elif c < 0.15:
        return [
            {"type": "Open Water", "concentration_tenths": 1.0 - c},
            {"type": "Grey Ice", "concentration_tenths": c},
        ]
    elif c < 0.30:
        return [
            {"type": "Open Water", "concentration_tenths": 1.0 - c},
            {"type": "Grey-White Ice", "concentration_tenths": c},
        ]
    elif c < 0.45:
        return [
            {"type": "Open Water", "concentration_tenths": 1.0 - c},
            {"type": "Thin First-Year (FY)", "concentration_tenths": c},
        ]
    elif c < 0.60:
        return [
            {"type": "Open Water", "concentration_tenths": max(0.0, 1.0 - c)},
            {"type": "Thin First-Year (FY)", "concentration_tenths": c * 0.5},
            {"type": "Medium First-Year (FY)", "concentration_tenths": c * 0.5},
        ]
    elif c < 0.75:
        # 고농도 진입 — 다년빙(MY) 소량 포함
        return [
            {"type": "Medium First-Year (FY)", "concentration_tenths": c * 0.5},
            {"type": "Thick First-Year (FY)", "concentration_tenths": c * 0.35},
            {"type": "Multi-Year (MY)", "concentration_tenths": c * 0.15},
        ]
    elif c < 0.88:
        return [
            {"type": "Thick First-Year (FY)", "concentration_tenths": c * 0.45},
            {"type": "Multi-Year (MY)", "concentration_tenths": c * 0.4},
            {"type": "Ridged/Hummocked", "concentration_tenths": c * 0.15},
        ]
    else:
        # 결빙 최성기 고위도 압축 다년빙역
        return [
            {"type": "Thick First-Year (FY)", "concentration_tenths": c * 0.3},
            {"type": "Multi-Year (MY)", "concentration_tenths": c * 0.4},
            {"type": "Ridged/Hummocked", "concentration_tenths": c * 0.3},
        ]


@dataclass
class SegmentScore:
    name: str
    avg_concentration: float
    rio: float
    color: str  # green / yellow / red


@dataclass
class DayScore:
    date: str
    route: str
    segment_scores: list[SegmentScore] = field(default_factory=list)
    overall_rio: float = 0.0
    color_code: str = "green"  # green / yellow / red


def _rio_to_color(rio: float) -> str:
    if rio >= 0:
        return "green"
    elif rio >= -5:
        return "yellow"
    else:
        return "red"


class RouteScorer:
    """POLARIS RIO 기반 항로 위험도 평가."""

    def __init__(self, data_loader=None):
        from modules.data_loader import DataLoader
        self.loader = data_loader or DataLoader()

    def _get_segment_concentration(
        self, segment: dict, cells: list[dict]
    ) -> float:
        """구간 내 셀들의 평균 농도 계산."""
        lat_min, lat_max = segment["lat_range"]
        lon_min, lon_max = segment["lon_range"]

        concs = []
        for cell in cells:
            lat, lon = cell.get("lat", 0), cell.get("lon", 0)
            # 경도 범위가 180도 경계를 넘는 경우 처리
            if lon_min <= lon_max:
                in_lon = lon_min <= lon <= lon_max
            else:
                in_lon = lon >= lon_min or lon <= lon_max

            if lat_min <= lat <= lat_max and in_lon:
                concs.append(cell["concentration"])

        # 박스 평균은 개수역 셀까지 섞여 항로가 통과하는 빙역 농도를 과소평가한다.
        # 90 퍼센타일로 '항로가 실제 통과하는 유의미한 빙'을 대표(다년빙 패치 포함).
        return float(np.percentile(concs, 90)) if concs else 0.0

    def score_departure_day(
        self,
        departure_date: date,
        route: str,
        ice_class: str,
        monthly_ice: dict[int, dict],
    ) -> DayScore:
        """특정 날짜의 출항 위험도 평가."""
        if route in SAFE_ROUTES:
            return DayScore(
                date=departure_date.isoformat(),
                route=route,
                overall_rio=2.0,
                color_code="green",
            )

        segments = ARCTIC_SEGMENTS.get(route, [])
        if not segments:
            logger.warning("알 수 없는 항로: %s", route)
            return DayScore(
                date=departure_date.isoformat(), route=route,
                overall_rio=0.0, color_code="yellow"
            )

        # 출항일의 월에 해당하는 해빙 데이터 사용
        month = departure_date.month
        month_data = monthly_ice.get(month, {})
        cells = month_data.get("cells", [])

        if not cells:
            logger.warning("월 %d 해빙 데이터 없음", month)
            return DayScore(
                date=departure_date.isoformat(), route=route,
                overall_rio=0.0, color_code="yellow"
            )

        segment_scores = []
        total_rio = 0.0

        for seg in segments:
            avg_conc = self._get_segment_concentration(seg, cells)
            # 박스 P90 도 항로 채널보다 옅으므로 채널 대표 농도로 보정(희석 보정).
            eff_conc = min(1.0, avg_conc * CHANNEL_DILUTION_FACTOR)
            ice_conditions = concentration_to_ice_conditions(eff_conc)

            try:
                rio = calculate_rio(ice_class, cast(list[IceCondition], ice_conditions))
            except ValueError as e:
                logger.error("RIO 계산 실패 (%s): %s", seg["name"], e)
                rio = -10.0

            color = _rio_to_color(rio)
            segment_scores.append(SegmentScore(
                name=seg["name"],
                avg_concentration=round(eff_conc, 4),
                rio=round(rio, 4),
                color=color,
            ))
            total_rio += rio

        overall_rio = round(total_rio / len(segments), 4) if segments else 0
        overall_color = _rio_to_color(overall_rio)

        return DayScore(
            date=departure_date.isoformat(),
            route=route,
            segment_scores=segment_scores,
            overall_rio=overall_rio,
            color_code=overall_color,
        )

    def build_departure_calendar(
        self,
        start_date: date,
        days: int,
        route: str,
        ice_class: str,
    ) -> list[DayScore]:
        """출항 캘린더 생성 (30~60일)."""
        monthly_ice = self.loader.load_monthly_ice()
        calendar = []
        for d in range(days):
            dep_date = start_date + timedelta(days=d)
            score = self.score_departure_day(dep_date, route, ice_class, monthly_ice)
            calendar.append(score)
        return calendar

    def score_all_routes(
        self,
        start_date: date,
        days: int,
        ice_class: str,
        routes: list[str] | None = None,
    ) -> dict[str, list[DayScore]]:
        """전체 항로 동시 평가."""
        if routes is None:
            routes = ["NSR", "NWP", "TSR", "SUEZ", "CAPE"]

        monthly_ice = self.loader.load_monthly_ice()
        result = {}

        for route in routes:
            calendar = []
            for d in range(days):
                dep_date = start_date + timedelta(days=d)
                if route in SAFE_ROUTES:
                    calendar.append(DayScore(
                        date=dep_date.isoformat(),
                        route=route,
                        overall_rio=2.0,
                        color_code="green",
                    ))
                else:
                    score = self.score_departure_day(
                        dep_date, route, ice_class, monthly_ice
                    )
                    calendar.append(score)
            result[route] = calendar

        return result

    def get_route_summary(
        self, all_scores: dict[str, list[DayScore]]
    ) -> dict[str, dict]:
        """항로별 요약 통계."""
        summary = {}
        for route, scores in all_scores.items():
            rios = [s.overall_rio for s in scores]
            colors = [s.color_code for s in scores]
            summary[route] = {
                "avg_rio": round(float(np.mean(rios)), 4) if rios else 0,
                "min_rio": round(float(np.min(rios)), 4) if rios else 0,
                "max_rio": round(float(np.max(rios)), 4) if rios else 0,
                "green_days": colors.count("green"),
                "yellow_days": colors.count("yellow"),
                "red_days": colors.count("red"),
                "total_days": len(scores),
            }
        return summary
