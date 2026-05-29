"""
ice_type_mapper.py
==================
NSIDC 월별 ice concentration 스냅샷(realIceData_monthNN.json)의 셀을
POLARIS RIO 계산에 필요한 list[IceCondition] 형태로 변환.

원본 데이터 한계
----------------
NOAA/NSIDC CDR G02202 V5 의 passive microwave 데이터는 ice concentration(0~1)
만 제공하고 WMO ice type(Thin FY / Medium FY / Multi-Year 등) 정보가 없다.
calculate_rio() 는 ice type 키를 요구하므로, concentration + 월 + 위도로부터
보수적 휴리스틱 매핑을 수행한다.

한계 명시
---------
- 실제 Ice Chart 수준의 정확도는 아님
- FY/MY 구분은 위도와 계절만 고려(실제는 ice age radar 필요)
- 지역적 편차(카라해 vs 추크치해) 무시 — 글로벌 룰 적용
- 2단계 시뮬에서는 "concentration 축만 있는 데이터에서 RIO를 돌리기 위한"
  최소 투영일 뿐, 실운항 의사결정용 아님

공간 조회
---------
IceField 클래스: numpy 벡터화 haversine 으로 O(N) nearest neighbor.
셀이 50km 격자이므로 최근접 셀 거리가 ~35km 이상이면 데이터 커버리지
바깥(저위도)이라 판단 → 개빙수역으로 처리.
"""

from __future__ import annotations
import json
from pathlib import Path

import numpy as np

from pipeline.icebreaker.models import Position
from pipeline.arctic_master_router import IceCondition


# 폴라 스테레오그래픽 격자는 경도에 따라 밀도가 고르지 않다. 관측상 최근접
# 셀까지 160km 수준 거리가 Kara Sea 75N 같은 지점에서도 발생. 200km 이내라면
# 북극권 내부로 판단하고 그대로 사용; 200km 이상이면 저위도(개빙수역).
MAX_CELL_DISTANCE_KM: float = 200.0

# WMO ice type → 대표 두께(m) — concentration-weighted 두께 계산용.
# 한계: 실제 두께는 레이더/측정으로만 알 수 있음; 여기서는 type별 중간값.
ICE_TYPE_THICKNESS_M: dict[str, float] = {
    "Open Water": 0.0,
    "Grey Ice": 0.10,
    "Grey-White Ice": 0.20,
    "Thin First-Year (FY)": 0.50,
    "Medium First-Year (FY)": 1.00,
    "Thick First-Year (FY)": 1.80,
    "Multi-Year (MY)": 3.00,
    "Ridged/Hummocked": 4.00,
    # Glacier Ice 두께는 본 매핑 한정 3.5m — 극지 셀에 embedded
    # iceberg/pressure ridge 가 섞인 worst-case 를 3.5m 로 표현.
    # 실제 빙하빙 블록 두께(수십 미터)가 아님.
    "Glacier Ice": 3.5,
}


def infer_ice_type(concentration: float, month: int, lat: float) -> str:
    """concentration + 월 + 위도 → WMO ice type(보수적 휴리스틱).

    근거/한계
    ---------
    NSIDC passive microwave 는 ice concentration 만 제공. 실제 빙장은
    pressure ridges, embedded multi-year blocks, 표류 glacier fragments 가
    혼재한다. 본 매핑은 그 worst-case 를 "가장 보수적인 WMO type 이름"으로
    압축 투영한다 — 즉, ice type 필드는 해당 격자 셀에서 encountered 가능
    최악 빙종을 의미하며, 실제 셀 전체가 그 빙종이라는 뜻이 아니다.
    이 보수성 때문에 고급 빙급선(PC3/PC4)도 극 북극 한겨울 밀집 영역에서
    POLARIS RIO 가 음수로 내려가 쇄빙선 호출이 가능해진다.

    계절 분기
      winter(11-4월): 두꺼운 빙 우세
      summer(5-10월): 같은 농도라도 한 등급 낮게 분류
    """
    winter = month in (1, 2, 3, 4, 11, 12)

    # 극 북극 한겨울 초고밀도: embedded icebergs / pressure ridges worst-case
    if winter and lat >= 75.0 and concentration >= 0.95:
        return "Glacier Ice"
    if winter and lat >= 77.0 and concentration >= 0.85:
        return "Ridged/Hummocked"
    if lat >= 82.0 and concentration >= 0.85:
        return "Multi-Year (MY)"

    if winter:
        if concentration >= 0.85:
            return "Thick First-Year (FY)"
        if concentration >= 0.60:
            return "Medium First-Year (FY)"
        if concentration >= 0.30:
            return "Thin First-Year (FY)"
        if concentration >= 0.10:
            return "Grey-White Ice"
        return "Grey Ice"
    else:
        if concentration >= 0.85:
            return "Medium First-Year (FY)"
        if concentration >= 0.60:
            return "Thin First-Year (FY)"
        if concentration >= 0.30:
            return "Grey-White Ice"
        return "Grey Ice"


def dominant_thickness_m(conditions: list[IceCondition]) -> float:
    """ice_conditions 의 concentration-weighted 평균 두께(m)."""
    total = 0.0
    for c in conditions:
        t = ICE_TYPE_THICKNESS_M.get(c["type"], 0.0)
        total += t * c["concentration_tenths"]
    return total


class IceField:
    """월별 스냅샷 → 공간 샘플러.

    Usage
    -----
        field = IceField.from_month(3, data_dir)
        conditions = field.sample({"lat": 75.0, "lon": 80.0}, month=3)
    """

    def __init__(self, cells: list[dict]):
        if not cells:
            raise ValueError("IceField: empty cells list")
        self._lats = np.array([c["lat"] for c in cells], dtype=np.float64)
        self._lons = np.array([c["lon"] for c in cells], dtype=np.float64)
        self._conc = np.array([c["concentration"] for c in cells], dtype=np.float64)
        self._lats_rad = np.radians(self._lats)
        self._lons_rad = np.radians(self._lons)

    @classmethod
    def from_month(cls, month: int, data_dir: Path | None = None) -> "IceField":
        """backend/data/monthly/realIceData_monthNN.json 로드."""
        if data_dir is None:
            # backend/pipeline/icebreaker/ -> backend/data/
            data_dir = Path(__file__).resolve().parents[2] / "data"
        path = data_dir / "monthly" / f"realIceData_month{month:02d}.json"
        if not path.exists():
            raise FileNotFoundError(f"Ice snapshot not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            blob = json.load(f)
        return cls(blob["cells"])

    def nearest(self, lat: float, lon: float) -> tuple[float, float]:
        """(nearest concentration, distance_km). vectorized haversine."""
        lat_r = np.radians(lat)
        lon_r = np.radians(lon)
        dlat = self._lats_rad - lat_r
        dlon = self._lons_rad - lon_r
        a = (
            np.sin(dlat * 0.5) ** 2
            + np.cos(lat_r) * np.cos(self._lats_rad) * np.sin(dlon * 0.5) ** 2
        )
        d_km = 2.0 * 6371.0 * np.arctan2(np.sqrt(a), np.sqrt(1.0 - a))
        idx = int(np.argmin(d_km))
        return float(self._conc[idx]), float(d_km[idx])

    def sample(self, pos: Position, month: int) -> list[IceCondition]:
        """해당 지점의 ice_conditions(RIO 입력 형식) 반환.

        커버리지 밖이거나 농도가 극히 낮으면 100% Open Water 반환.
        그 외에는 (Open Water, 1-c) + (inferred type, c) 조합.
        """
        conc, dist_km = self.nearest(pos["lat"], pos["lon"])
        if dist_km > MAX_CELL_DISTANCE_KM or conc < 1e-3:
            return [{"type": "Open Water", "concentration_tenths": 1.0}]

        ice_type = infer_ice_type(conc, month, pos["lat"])
        conditions: list[IceCondition] = []
        if conc < 0.999:
            conditions.append(
                {"type": "Open Water", "concentration_tenths": round(1.0 - conc, 6)}
            )
        conditions.append(
            {"type": ice_type, "concentration_tenths": round(conc, 6)}
        )
        return conditions


if __name__ == "__main__":
    # 간단 스모크 테스트
    field = IceField.from_month(3)
    print(f"loaded month=3, cells={len(field._conc)}")
    # Kara Sea 중앙
    sample = field.sample({"lat": 75.0, "lon": 80.0}, month=3)
    print(f"Kara Sea (75N, 80E) month=3: {sample}")
    print(f"  dominant thickness: {dominant_thickness_m(sample):.2f}m")
    # 저위도 (SUEZ 루트)
    sample2 = field.sample({"lat": 10.0, "lon": 50.0}, month=7)
    print(f"Red Sea (10N, 50E) month=7: {sample2}")
