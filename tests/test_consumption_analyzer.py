"""Tests for the ConsumptionAnalyzer module."""

from datetime import datetime
from unittest.mock import patch

import pytest

from custom_components.beem_ai.consumption_analyzer import (
    ConsumptionAnalyzer,
    _ANOMALY_STDDEV_THRESHOLD,
    _DEFAULT_CONSUMPTION_W,
    _EMA_ALPHA,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def analyzer(tmp_path):
    """Create a ConsumptionAnalyzer with a temporary data directory."""
    return ConsumptionAnalyzer(data_dir=tmp_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fixed_datetime(day_of_week: int, hour: int):
    """Return a datetime with a specific weekday and hour.

    Monday=0 ... Sunday=6.  We pick a known date that falls on the
    requested weekday (2026-02-23 is a Monday).
    """
    # 2026-02-23 is a Monday (weekday=0)
    base_day = 23 + day_of_week  # Mon=23, Tue=24, ...
    return datetime(2026, 2, base_day, hour, 30, 0)


# ---------------------------------------------------------------------------
# Tests — Default initialisation
# ---------------------------------------------------------------------------

class TestDefaults:
    """All 168 buckets should initialise to the default 500 W."""

    def test_all_buckets_exist(self, analyzer):
        for day in range(7):
            hourly = analyzer.get_hourly_forecast(day)
            assert len(hourly) == 24
            for h in range(24):
                assert hourly[h] == pytest.approx(_DEFAULT_CONSUMPTION_W)

    def test_missing_day_returns_empty(self, analyzer):
        assert analyzer.get_hourly_forecast(99) == {}


# ---------------------------------------------------------------------------
# Tests — record_consumption and EMA
# ---------------------------------------------------------------------------

class TestRecordConsumption:
    """Verify EMA updates correctly for the current day/hour slot."""

    def test_single_update(self, analyzer):
        now = _fixed_datetime(0, 14)  # Monday 14:00
        with patch("custom_components.beem_ai.consumption_analyzer.datetime") as mock_dt:
            mock_dt.now.return_value = now
            analyzer.record_consumption(1000.0)

        hourly = analyzer.get_hourly_forecast(0)
        # EMA: 0.1 * 1000 + 0.9 * 500 = 550
        assert hourly[14] == pytest.approx(550.0)
        # Other hours remain default
        assert hourly[13] == pytest.approx(_DEFAULT_CONSUMPTION_W)

    def test_ema_convergence(self, analyzer):
        """Repeatedly recording 800 W should make the EMA converge toward 800."""
        now = _fixed_datetime(2, 10)  # Wednesday 10:00
        with patch("custom_components.beem_ai.consumption_analyzer.datetime") as mock_dt:
            mock_dt.now.return_value = now
            for _ in range(200):
                analyzer.record_consumption(800.0)

        hourly = analyzer.get_hourly_forecast(2)
        assert hourly[10] == pytest.approx(800.0, abs=1.0)


# ---------------------------------------------------------------------------
# Tests — Forecasts
# ---------------------------------------------------------------------------

class TestForecasts:
    def test_get_forecast_kwh_tomorrow(self, analyzer):
        now = _fixed_datetime(0, 12)  # Monday -> tomorrow is Tuesday (1)

        # Set a known value for Tuesday (day=1)
        for h in range(24):
            analyzer._ema[1][h] = 1000.0  # 1000 W each hour

        with patch("custom_components.beem_ai.consumption_analyzer.datetime") as mock_dt:
            mock_dt.now.return_value = now
            result = analyzer.get_forecast_kwh_tomorrow()

        # 24 hours * 1000 W / 1000 = 24.0 kWh
        assert result == pytest.approx(24.0)

    def test_get_forecast_kwh_today_remaining(self, analyzer):
        now = _fixed_datetime(0, 20)  # Monday 20:00
        with patch("custom_components.beem_ai.consumption_analyzer.datetime") as mock_dt:
            mock_dt.now.return_value = now

            # Set known values for Monday
            for h in range(24):
                analyzer._ema[0][h] = 1000.0  # 1000 W each hour

            result = analyzer.get_forecast_kwh_today_remaining()

        # Hours 21, 22, 23 => 3 hours * 1000 W / 1000 = 3.0 kWh
        assert result == pytest.approx(3.0)

    def test_get_hourly_forecast_returns_copy(self, analyzer):
        """Modifying the returned dict should not affect internal state."""
        hourly = analyzer.get_hourly_forecast(0)
        hourly[0] = 99999.0
        assert analyzer.get_hourly_forecast(0)[0] == pytest.approx(_DEFAULT_CONSUMPTION_W)


# ---------------------------------------------------------------------------
# Tests — Anomaly detection
# ---------------------------------------------------------------------------

class TestAnomaly:
    def test_insufficient_data_returns_false(self, analyzer):
        now = _fixed_datetime(0, 10)
        with patch("custom_components.beem_ai.consumption_analyzer.datetime") as mock_dt:
            mock_dt.now.return_value = now
            # Only 1 sample — need >= 2
            analyzer.record_consumption(500.0)
            assert analyzer.is_anomaly(99999.0) is False

    def test_normal_value_not_anomaly(self, analyzer):
        now = _fixed_datetime(0, 10)
        with patch("custom_components.beem_ai.consumption_analyzer.datetime") as mock_dt:
            mock_dt.now.return_value = now
            # Record many similar values to build statistics
            for _ in range(50):
                analyzer.record_consumption(500.0)

            # A value close to mean should not be an anomaly
            assert analyzer.is_anomaly(510.0) is False

    def test_extreme_value_is_anomaly(self, analyzer):
        now = _fixed_datetime(0, 10)
        with patch("custom_components.beem_ai.consumption_analyzer.datetime") as mock_dt:
            mock_dt.now.return_value = now
            # Build up stats with values around 500
            for v in [490, 500, 510, 500, 490, 510, 500, 505, 495, 500]:
                analyzer.record_consumption(float(v))

            # A value far from mean (5000 W) should be anomalous
            assert analyzer.is_anomaly(5000.0) is True


# ---------------------------------------------------------------------------
# Tests — Save / Load roundtrip
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        analyzer1 = ConsumptionAnalyzer(data_dir=tmp_path)

        now = _fixed_datetime(3, 18)  # Thursday 18:00
        with patch("custom_components.beem_ai.consumption_analyzer.datetime") as mock_dt:
            mock_dt.now.return_value = now
            for _ in range(20):
                analyzer1.record_consumption(750.0)

        analyzer1.save()

        # Load into a fresh instance
        analyzer2 = ConsumptionAnalyzer(data_dir=tmp_path)
        analyzer2.load()

        h1 = analyzer1.get_hourly_forecast(3)
        h2 = analyzer2.get_hourly_forecast(3)
        assert h1[18] == pytest.approx(h2[18])

        # Welford stats should also survive
        assert analyzer2._count[3][18] == analyzer1._count[3][18]
        assert analyzer2._mean[3][18] == pytest.approx(analyzer1._mean[3][18])
        assert analyzer2._m2[3][18] == pytest.approx(analyzer1._m2[3][18])

    def test_load_missing_file_uses_defaults(self, tmp_path):
        analyzer = ConsumptionAnalyzer(data_dir=tmp_path)
        analyzer.load()  # No file exists — should not crash
        assert analyzer.get_hourly_forecast(0)[0] == pytest.approx(_DEFAULT_CONSUMPTION_W)
