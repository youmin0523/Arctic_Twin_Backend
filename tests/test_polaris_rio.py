"""백엔드 POLARIS 엔진(arctic_master_router) 단위 테스트.

calculate_rio / normalize_ice_class 는 의사결정·항로평가의 핵심이며
의존성이 전혀 없는 순수 함수다. 프론트엔드 polarisRIO.test.js 와 짝을 이룬다.
"""
import pytest
from arctic_master_router import calculate_rio, normalize_ice_class, RIV_TABLE


class TestCalculateRIO:
    def test_open_water_is_safe(self):
        rio = calculate_rio("PC5", [{"type": "Open Water", "concentration_tenths": 1.0}])
        assert rio == RIV_TABLE["PC5"]["Open Water"]  # 1.0 * RIV
        assert rio >= 0

    def test_multiyear_ice_is_hazardous_for_low_class(self):
        rio = calculate_rio("PC7", [{"type": "Multi-Year (MY)", "concentration_tenths": 1.0}])
        assert rio < 0

    def test_rio_is_linear_in_concentration(self):
        half = calculate_rio("PC5", [{"type": "Multi-Year (MY)", "concentration_tenths": 0.5}])
        full = calculate_rio("PC5", [{"type": "Multi-Year (MY)", "concentration_tenths": 1.0}])
        assert full == pytest.approx(2 * half)

    def test_multiple_ice_types_sum(self):
        rio = calculate_rio("PC5", [
            {"type": "Open Water", "concentration_tenths": 0.5},
            {"type": "Multi-Year (MY)", "concentration_tenths": 0.5},
        ])
        expected = 0.5 * RIV_TABLE["PC5"]["Open Water"] + 0.5 * RIV_TABLE["PC5"]["Multi-Year (MY)"]
        assert rio == pytest.approx(expected)

    def test_unknown_ice_class_raises(self):
        with pytest.raises(ValueError):
            calculate_rio("완전허구등급", [{"type": "Open Water", "concentration_tenths": 1.0}])

    def test_unknown_ice_type_raises(self):
        with pytest.raises(ValueError):
            calculate_rio("PC5", [{"type": "존재하지않는빙질", "concentration_tenths": 1.0}])

    def test_out_of_range_concentration_raises(self):
        with pytest.raises(ValueError):
            calculate_rio("PC5", [{"type": "Open Water", "concentration_tenths": 1.5}])

    def test_empty_conditions_is_zero(self):
        assert calculate_rio("PC5", []) == 0.0

    def test_higher_class_never_more_dangerous(self):
        """동일 빙상에서 더 높은 빙급(PC2)이 더 낮은 빙급(PC7)보다 위험할 수 없다."""
        cond = [{"type": "Thick First-Year (FY)", "concentration_tenths": 1.0}]
        assert calculate_rio("PC2", cond) >= calculate_rio("PC7", cond)


class TestNormalizeIceClass:
    def test_standard_key_passthrough(self):
        assert normalize_ice_class("PC5") == "PC5"

    @pytest.mark.parametrize("rmrs,iacs", [
        ("Arc4", "PC7"), ("Arc7", "PC4"), ("Arc9", "PC2"),
        ("Ice1", "IC"), ("Ice3", "IA"),
    ])
    def test_russian_rmrs_mapped_to_iacs(self, rmrs, iacs):
        assert normalize_ice_class(rmrs) == iacs

    def test_case_and_space_insensitive(self):
        assert normalize_ice_class("arc 4") == "PC7"

    def test_unmappable_returned_as_is(self):
        assert normalize_ice_class("UNKNOWN") == "UNKNOWN"

    def test_normalized_class_usable_in_calculate_rio(self):
        # Arc6 → PC5 로 정규화되어 정상 계산되어야 함
        rio = calculate_rio("Arc6", [{"type": "Open Water", "concentration_tenths": 1.0}])
        assert rio == calculate_rio("PC5", [{"type": "Open Water", "concentration_tenths": 1.0}])
