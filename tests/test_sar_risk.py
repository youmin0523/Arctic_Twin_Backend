"""SAR 탐지 → What-If 정량 위험 신호 변환 테스트 (sar_risk)."""
import pytest
from modules.sar_risk import assess_sar_risk, freshness_hours


class TestFreshnessHours:
    def test_basic_delta(self):
        h = freshness_hours("2026-06-01T00:00:00Z", "2026-06-01T06:00:00Z")
        assert h == pytest.approx(6.0)

    def test_missing_inputs_none(self):
        assert freshness_hours(None, "2026-06-01T00:00:00Z") is None
        assert freshness_hours("2026-06-01T00:00:00Z", None) is None

    def test_invalid_string_none(self):
        assert freshness_hours("not-a-date", "2026-06-01T00:00:00Z") is None

    def test_never_negative(self):
        h = freshness_hours("2026-06-02T00:00:00Z", "2026-06-01T00:00:00Z")
        assert h == 0.0


class TestAssessSarRisk:
    def test_none_input_is_unknown(self):
        r = assess_sar_risk(None)
        assert r["level"] == "unknown"
        assert "없음" in r["note"]

    def test_zero_detected_is_none_level(self):
        r = assess_sar_risk(0)
        assert r["level"] == "none"

    def test_few_detections_low(self):
        r = assess_sar_risk(3)
        assert r["level"] == "low"
        assert r["detected"] == 3

    def test_moderate_threshold(self):
        assert assess_sar_risk(10)["level"] == "moderate"

    def test_high_threshold(self):
        assert assess_sar_risk(30)["level"] == "high"

    def test_density_escalates_level(self):
        # 적은 절대수라도 산출물당 밀도가 높으면 등급 상승
        r = assess_sar_risk(9, products_processed=1)  # density 9 ≥ 3 → moderate
        assert r["density"] == 9.0
        assert r["level"] in ("moderate", "high")

    def test_stale_data_downgrades(self):
        fresh = assess_sar_risk(30, detection_time="2026-06-01T00:00:00Z",
                                now_iso="2026-06-01T01:00:00Z")
        stale = assess_sar_risk(30, detection_time="2026-06-01T00:00:00Z",
                                now_iso="2026-06-10T00:00:00Z")
        assert fresh["level"] == "high"
        assert stale["level"] == "moderate"   # 오래된 데이터 → 한 단계 하향
        assert stale["stale"] is True

    def test_age_hours_reported(self):
        r = assess_sar_risk(5, detection_time="2026-06-01T00:00:00Z",
                            now_iso="2026-06-01T12:00:00Z")
        assert r["age_hours"] == pytest.approx(12.0)

    def test_note_always_present(self):
        for n in (None, 0, 5, 15, 50):
            assert isinstance(assess_sar_risk(n)["note"], str)
            assert len(assess_sar_risk(n)["note"]) > 0

    def test_negative_normalized_to_zero(self):
        assert assess_sar_risk(-5)["detected"] == 0
