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


WINTER_MONTHS: frozenset[int] = frozenset({1, 2, 3, 4, 11, 12})


def infer_ice_type(
    concentration: float,
    month: int,
    lat: float,
    thickness: float | None = None,
) -> str:
    """ice type 추론 — 실측 두께(thickness)가 있으면 그것을 1차 신호로 사용.

    배경
    ----
    monthly 스냅샷(realIceData_monthNN.json)의 각 셀은 concentration 외에
    **실측 thickness(m)** 도 제공한다. 두께는 항행 난이도를 직접 결정하는
    물리량이라 ice type 분류의 가장 정확한 신호다(예: 한겨울 NSR 회랑은
    농도가 0.3~0.5 로 낮게 들어와도 실측 두께가 1~2.7m 인 두꺼운 정착빙/
    압력능선 빙역). 종전 구현은 thickness 를 버리고 농도만으로 분류해
    이런 셀을 전부 "Thin FY(0.5m)" 로 과소평가, RIO 가 음수로 못 내려가
    쇄빙선 호출이 영원히 발생하지 않는 회귀가 있었다.

    분류 정책 (thickness 우선)
    --------------------------
    WMO 해빙 두께 단계 + POLARIS 위험도 보정:
      ≥2.5m & 한겨울 고위도(lat≥75) → Glacier Ice
          (극 북극 한겨울 두꺼운 빙역의 embedded iceberg/glacier fragment
           worst-case — PC3 본선도 RIO 음수가 되어야 하는 구간)
      ≥3.0m              → Ridged/Hummocked
      ≥2.0m              → Multi-Year (MY)
      ≥1.2m              → Thick First-Year (FY)
      ≥0.7m              → Medium First-Year (FY)
      ≥0.3m              → Thin First-Year (FY)
      ≥0.15m             → Grey-White Ice
      ≥0.10m             → Grey Ice
      그 외              → Open Water

    thickness 가 없는 레거시 경로에서는 종전 농도+계절+위도 휴리스틱을 사용.
    """
    winter = month in WINTER_MONTHS

    if thickness is not None and thickness > 0.0:
        if winter and lat >= 75.0 and thickness >= 2.5:
            return "Glacier Ice"
        if thickness >= 3.0:
            return "Ridged/Hummocked"
        if thickness >= 2.0:
            return "Multi-Year (MY)"
        if thickness >= 1.2:
            return "Thick First-Year (FY)"
        if thickness >= 0.7:
            return "Medium First-Year (FY)"
        if thickness >= 0.3:
            return "Thin First-Year (FY)"
        if thickness >= 0.15:
            return "Grey-White Ice"
        if thickness >= 0.10:
            return "Grey Ice"
        return "Open Water"

    # ── 레거시 폴백: thickness 미제공 시 농도 기반(보수적 휴리스틱) ──
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


def winter_effective_concentration(
    raw_conc: float, thickness: float, month: int
) -> float:
    """한겨울 정착빙 보정 — 두꺼운 빙은 밀집빙(고총농도)으로 간주.

    근거
    ----
    이 데이터셋의 passive-microwave 농도 축은 한겨울 NSR 회랑에서도 0.3~0.5
    로 비현실적으로 낮다(실제 3월 척치/동시베리아해 정착빙 총농도는 0.9+).
    반면 실측 thickness 는 1~2.7m 로 물리적으로 타당하다. POLARIS RIO 는
    Σ(부분농도 × RIV) 이므로 농도가 낮으면 두꺼운 빙도 위험도가 희석된다.
    Ice Chart 관행상 한겨울 두꺼운 정착빙역은 총농도가 높게 보고되므로,
    두께에 따른 농도 하한(consolidation floor)을 적용해 RIO 가 실제 항행
    위험을 반영하도록 한다. 여름/박빙역(thickness 낮음)은 보정하지 않아
    개빙수역의 통항성을 그대로 유지한다.
    """
    if month not in WINTER_MONTHS:
        return raw_conc
    if thickness >= 1.2:        # Thick FY 이상 — 한겨울 압밀 정착빙
        floor = 0.97
    elif thickness >= 0.7:      # Medium FY
        floor = 0.90
    elif thickness >= 0.4:      # Thin FY 상단
        floor = 0.80
    else:
        floor = 0.0
    return max(raw_conc, floor)


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
        # 실측 두께(m). 일부 레거시 스냅샷엔 없을 수 있어 0.0 기본값.
        self._thick = np.array(
            [float(c.get("thickness", 0.0)) for c in cells], dtype=np.float64
        )
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

    def nearest(self, lat: float, lon: float) -> tuple[float, float, float]:
        """(nearest concentration, thickness_m, distance_km). vectorized haversine."""
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
        return float(self._conc[idx]), float(self._thick[idx]), float(d_km[idx])

    def sample(self, pos: Position, month: int) -> list[IceCondition]:
        """해당 지점의 ice_conditions(RIO 입력 형식) 반환.

        커버리지 밖이거나 빙이 사실상 없으면 100% Open Water 반환.
        그 외에는 (Open Water, 1-c_eff) + (thickness 기반 inferred type, c_eff) 조합.
        c_eff 는 한겨울 정착빙 보정(winter_effective_concentration)이 적용된 농도.
        """
        conc, thick, dist_km = self.nearest(pos["lat"], pos["lon"])
        if dist_km > MAX_CELL_DISTANCE_KM or (conc < 1e-3 and thick < 0.10):
            return [{"type": "Open Water", "concentration_tenths": 1.0}]

        ice_type = infer_ice_type(conc, month, pos["lat"], thickness=thick)
        if ice_type == "Open Water":
            return [{"type": "Open Water", "concentration_tenths": 1.0}]

        eff_conc = winter_effective_concentration(conc, thick, month)
        conditions: list[IceCondition] = []
        if eff_conc < 0.999:
            conditions.append(
                {"type": "Open Water", "concentration_tenths": round(1.0 - eff_conc, 6)}
            )
        conditions.append(
            {"type": ice_type, "concentration_tenths": round(eff_conc, 6)}
        )
        return conditions

    def sample_thickness(self, pos: Position, _month: int = 0) -> float:
        """해당 지점의 실측 두께(m). 커버리지 밖이면 0.0.

        HUD '얼음 두께' 표시 및 쇄빙 유효두께 계산용 — 종전 type-table 가중
        근사(dominant_thickness_m) 대신 실측값을 직접 노출.
        """
        _conc, thick, dist_km = self.nearest(pos["lat"], pos["lon"])
        if dist_km > MAX_CELL_DISTANCE_KM:
            return 0.0
        return round(thick, 4)

    def high_threat_cells(
        self,
        min_thick: float = 1.2,
        min_conc: float = 0.0,
        min_lat: float = 60.0,
    ) -> list[dict]:
        """항행 위협이 되는 고두께/고농도 셀 좌표 목록 반환.

        RL 빙산 회피 오프라인 베이크(rl_iceberg_bake)에서 "장애물(대리 빙산)"
        소스로 사용. 실측 빙산(realBergData)은 북극에 거의 없으므로 두꺼운
        해빙 셀을 대리 빙산으로 취급한다(프론트 ThreeOverlay 의 surrogateIce 와
        동일 개념). 프라이빗 배열을 직접 읽지 않도록 public 헬퍼로 노출한다.

        Returns: [{"lat", "lon", "thickness", "concentration"}], 두께 내림차순.
        """
        mask = (
            (self._thick >= min_thick)
            & (self._conc >= min_conc)
            & (self._lats >= min_lat)
        )
        idx = np.nonzero(mask)[0]
        cells = [
            {
                "lat": float(self._lats[i]),
                "lon": float(self._lons[i]),
                "thickness": float(self._thick[i]),
                "concentration": float(self._conc[i]),
            }
            for i in idx
        ]
        cells.sort(key=lambda c: c["thickness"], reverse=True)
        return cells


if __name__ == "__main__":
    # 간단 스모크 테스트
    field = IceField.from_month(3)
    print(f"loaded month=3, cells={len(field._conc)}")
    # Kara Sea 중앙
    pos = {"lat": 75.0, "lon": 80.0}
    sample = field.sample(pos, month=3)
    print(f"Kara Sea (75N, 80E) month=3: {sample}")
    print(f"  actual thickness: {field.sample_thickness(pos):.2f}m")
    # 저위도 (SUEZ 루트)
    sample2 = field.sample({"lat": 10.0, "lon": 50.0}, month=7)
    print(f"Red Sea (10N, 50E) month=7: {sample2}")
