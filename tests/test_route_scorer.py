"""route_scorer 순수 로직 테스트.

농도→빙종 매핑(concentration_to_ice_conditions)과 RIO→색상 분류(_rio_to_color)는
출항 캘린더/항로 비교 점수의 토대다. DataLoader/DB 를 건드리지 않는 모듈 레벨
순수 함수만 검증한다.
"""
import pytest
from modules.route_scorer import concentration_to_ice_conditions, _rio_to_color


class TestConcentrationToIceConditions:
    def test_returns_list_of_conditions(self):
        out = concentration_to_ice_conditions(0.5)
        assert isinstance(out, list) and len(out) >= 1
        for c in out:
            assert "type" in c and "concentration_tenths" in c

    def test_very_low_is_open_water(self):
        out = concentration_to_ice_conditions(0.01)
        assert out == [{"type": "Open Water", "concentration_tenths": 1.0}]

    @pytest.mark.parametrize("conc", [0.0, 0.1, 0.3, 0.5, 0.8, 0.95, 1.0])
    def test_concentration_tenths_in_valid_range(self, conc):
        """모든 분해 결과의 농도가 [0,1] 범위 (calculate_rio 계약 충족)."""
        for c in concentration_to_ice_conditions(conc):
            assert 0.0 <= c["concentration_tenths"] <= 1.0

    def test_higher_concentration_introduces_thicker_ice(self):
        low_types = {c["type"] for c in concentration_to_ice_conditions(0.1)}
        high_types = {c["type"] for c in concentration_to_ice_conditions(0.95)}
        # 고농도엔 두꺼운 1년생 빙이 등장, 저농도엔 없음
        assert "Thick First-Year (FY)" in high_types
        assert "Thick First-Year (FY)" not in low_types

    def test_resulting_conditions_compute_valid_rio(self):
        """분해 결과를 실제 RIO 계산에 넣어도 예외 없이 동작 (통합 계약)."""
        from arctic_master_router import calculate_rio
        for conc in (0.0, 0.25, 0.5, 0.75, 1.0):
            rio = calculate_rio("PC5", concentration_to_ice_conditions(conc))
            assert isinstance(rio, float)


class TestRioToColor:
    @pytest.mark.parametrize("rio,color", [
        (5.0, "green"), (0.0, "green"),
        (-1.0, "yellow"), (-5.0, "yellow"),
        (-6.0, "red"), (-20.0, "red"),
    ])
    def test_color_thresholds(self, rio, color):
        assert _rio_to_color(rio) == color

    def test_monotonic_safety(self):
        """RIO 가 높을수록 더 안전한(또는 같은) 색상."""
        order = {"green": 2, "yellow": 1, "red": 0}
        rios = [-20, -6, -5, -1, 0, 5]
        colors = [order[_rio_to_color(r)] for r in rios]
        assert colors == sorted(colors)
