"""Tests for the SolarForecast ensemble aggregator (async)."""

from unittest.mock import MagicMock

import pytest

from custom_components.beem_ai.forecasting.solar_forecast import (
    P10_SCALE,
    P90_SCALE,
    SolarForecast,
)


# ---------------------------------------------------------------------------
# Mock source helper
# ---------------------------------------------------------------------------

class MockSource:
    """Deterministic async forecast source for testing."""

    def __init__(self, name, data):
        self.name = name
        self._data = data

    async def fetch(self):
        return self._data


class FailingSource:
    """Source that always raises an exception."""

    def __init__(self, name="failing"):
        self.name = name

    async def fetch(self):
        raise RuntimeError("API exploded")


class EmptySource:
    """Source that returns an empty dict."""

    def __init__(self, name="empty"):
        self.name = name

    async def fetch(self):
        return {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_source_data(today_hours, tomorrow_hours, today_kwh, tomorrow_kwh, **extra):
    """Build a source-data dict from simple inputs."""
    return {
        "today": today_hours,
        "tomorrow": tomorrow_hours,
        "today_kwh": today_kwh,
        "tomorrow_kwh": tomorrow_kwh,
        **extra,
    }


@pytest.fixture
def source_a():
    return MockSource(
        "source_a",
        _make_source_data(
            today_hours={10: 500.0, 11: 800.0, 12: 1000.0},
            tomorrow_hours={10: 600.0, 11: 900.0, 12: 1100.0},
            today_kwh=5.0,
            tomorrow_kwh=6.0,
        ),
    )


@pytest.fixture
def source_b():
    return MockSource(
        "source_b",
        _make_source_data(
            today_hours={10: 400.0, 11: 600.0, 12: 800.0},
            tomorrow_hours={10: 500.0, 11: 700.0, 12: 900.0},
            today_kwh=4.0,
            tomorrow_kwh=5.0,
        ),
    )


@pytest.fixture
def source_c():
    return MockSource(
        "source_c",
        _make_source_data(
            today_hours={10: 600.0, 11: 900.0, 12: 1200.0},
            tomorrow_hours={10: 700.0, 11: 1000.0, 12: 1300.0},
            today_kwh=6.0,
            tomorrow_kwh=7.0,
        ),
    )


@pytest.fixture
def solcast_source():
    """MockSource that returns Solcast-style data with P10/P90."""
    return MockSource(
        "solcast",
        _make_source_data(
            today_hours={10: 500.0, 11: 800.0},
            tomorrow_hours={10: 600.0, 11: 900.0},
            today_kwh=5.0,
            tomorrow_kwh=6.0,
            today_p10={10: 350.0, 11: 560.0},
            today_p90={10: 650.0, 11: 1040.0},
            tomorrow_p10={10: 420.0, 11: 630.0},
            tomorrow_p90={10: 780.0, 11: 1170.0},
        ),
    )


# ---------------------------------------------------------------------------
# Tests -- Weighted average merge
# ---------------------------------------------------------------------------

class TestWeightedAverage:
    """Verify the ensemble weighted-average merge logic."""

    @pytest.mark.asyncio
    async def test_equal_weights_two_sources(self, state_store, source_a, source_b):
        sf = SolarForecast(state_store, [source_a, source_b])
        await sf.refresh()

        forecast = state_store.forecast
        # Equal weights -> simple average
        assert forecast.solar_today[10] == pytest.approx(450.0, abs=0.1)
        assert forecast.solar_today[11] == pytest.approx(700.0, abs=0.1)
        assert forecast.solar_today[12] == pytest.approx(900.0, abs=0.1)

        assert forecast.solar_today_kwh == pytest.approx(4.5, abs=0.01)
        assert forecast.solar_tomorrow_kwh == pytest.approx(5.5, abs=0.01)

    @pytest.mark.asyncio
    async def test_equal_weights_three_sources(
        self, state_store, source_a, source_b, source_c
    ):
        sf = SolarForecast(state_store, [source_a, source_b, source_c])
        await sf.refresh()

        forecast = state_store.forecast
        assert forecast.solar_today[10] == pytest.approx(500.0, abs=0.1)
        assert forecast.solar_today[11] == pytest.approx(766.7, abs=0.1)

    @pytest.mark.asyncio
    async def test_set_weights_changes_output(self, state_store, source_a, source_b):
        sf = SolarForecast(state_store, [source_a, source_b])

        sf.set_weights({"source_a": 0.75, "source_b": 0.25})
        await sf.refresh()

        forecast = state_store.forecast
        assert forecast.solar_today[10] == pytest.approx(475.0, abs=0.1)


# ---------------------------------------------------------------------------
# Tests -- Graceful degradation
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    """Ensure the aggregator handles failing and empty sources."""

    @pytest.mark.asyncio
    async def test_source_exception_is_skipped(self, state_store, source_a):
        failing = FailingSource("bad_source")
        sf = SolarForecast(state_store, [source_a, failing])
        await sf.refresh()

        forecast = state_store.forecast
        assert forecast.solar_today[10] == pytest.approx(500.0, abs=0.1)
        assert "bad_source" not in sf.sources_used
        assert "source_a" in sf.sources_used

    @pytest.mark.asyncio
    async def test_source_empty_dict_is_skipped(self, state_store, source_a):
        empty = EmptySource("empty_source")
        sf = SolarForecast(state_store, [source_a, empty])
        await sf.refresh()

        forecast = state_store.forecast
        assert forecast.solar_today[10] == pytest.approx(500.0, abs=0.1)
        assert "empty_source" not in sf.sources_used

    @pytest.mark.asyncio
    async def test_all_sources_fail(self, state_store):
        failing = FailingSource("f1")
        empty = EmptySource("f2")
        sf = SolarForecast(state_store, [failing, empty])
        await sf.refresh()

        assert sf.sources_used == []


# ---------------------------------------------------------------------------
# Tests -- Confidence levels
# ---------------------------------------------------------------------------

class TestConfidence:
    """Verify confidence mapping: 3=high, 2=medium, 1=low, 0=low."""

    @pytest.mark.asyncio
    async def test_three_sources_high(
        self, state_store, source_a, source_b, source_c
    ):
        sf = SolarForecast(state_store, [source_a, source_b, source_c])
        await sf.refresh()
        assert state_store.forecast.confidence == "high"

    @pytest.mark.asyncio
    async def test_two_sources_medium(self, state_store, source_a, source_b):
        sf = SolarForecast(state_store, [source_a, source_b])
        await sf.refresh()
        assert state_store.forecast.confidence == "medium"

    @pytest.mark.asyncio
    async def test_one_source_low(self, state_store, source_a):
        sf = SolarForecast(state_store, [source_a])
        await sf.refresh()
        assert state_store.forecast.confidence == "low"

    @pytest.mark.asyncio
    async def test_zero_sources_low(self, state_store):
        sf = SolarForecast(state_store, [FailingSource()])
        await sf.refresh()
        assert state_store.forecast.confidence == "low"


# ---------------------------------------------------------------------------
# Tests -- P10/P90 confidence intervals
# ---------------------------------------------------------------------------

class TestConfidenceIntervals:
    """Verify P10/P90 estimation with and without Solcast."""

    @pytest.mark.asyncio
    async def test_p10_p90_from_solcast(self, state_store, source_a, solcast_source):
        sf = SolarForecast(state_store, [source_a, solcast_source])
        await sf.refresh()

        forecast = state_store.forecast
        assert forecast.solar_today_p10 == {10: 350.0, 11: 560.0}
        assert forecast.solar_today_p90 == {10: 650.0, 11: 1040.0}
        assert forecast.solar_tomorrow_p10 == {10: 420.0, 11: 630.0}
        assert forecast.solar_tomorrow_p90 == {10: 780.0, 11: 1170.0}

    @pytest.mark.asyncio
    async def test_p10_p90_scaled_without_solcast(self, state_store, source_a, source_b):
        sf = SolarForecast(state_store, [source_a, source_b])
        await sf.refresh()

        forecast = state_store.forecast
        for hour, merged_val in forecast.solar_today.items():
            expected_p10 = round(merged_val * P10_SCALE, 1)
            expected_p90 = round(merged_val * P90_SCALE, 1)
            assert forecast.solar_today_p10[hour] == pytest.approx(expected_p10, abs=0.1)
            assert forecast.solar_today_p90[hour] == pytest.approx(expected_p90, abs=0.1)


# ---------------------------------------------------------------------------
# Tests -- sources_used tracking
# ---------------------------------------------------------------------------

class TestSourcesUsed:
    """Verify the sources_used list is correctly populated."""

    @pytest.mark.asyncio
    async def test_all_sources_succeed(self, state_store, source_a, source_b):
        sf = SolarForecast(state_store, [source_a, source_b])
        await sf.refresh()
        assert sorted(sf.sources_used) == ["source_a", "source_b"]
        assert sorted(state_store.forecast.sources_used) == ["source_a", "source_b"]

    @pytest.mark.asyncio
    async def test_partial_sources(self, state_store, source_a):
        failing = FailingSource("dead")
        sf = SolarForecast(state_store, [source_a, failing])
        await sf.refresh()
        assert sf.sources_used == ["source_a"]

    @pytest.mark.asyncio
    async def test_no_sources(self, state_store):
        sf = SolarForecast(state_store, [])
        await sf.refresh()
        assert sf.sources_used == []


# ---------------------------------------------------------------------------
# Tests -- Reconfigure propagation
# ---------------------------------------------------------------------------

class TestReconfigure:
    """Verify reconfigure() propagates to sources."""

    def test_reconfigure_calls_sources(self, state_store):
        """reconfigure() should call reconfigure on each source that supports it."""
        source = MockSource("src", _make_source_data({10: 500}, {10: 600}, 5.0, 6.0))
        source.reconfigure = MagicMock()

        sf = SolarForecast(state_store, [source])
        config = {"location_lat": 49.0, "panel_arrays": [{"tilt": 30, "azimuth": 180, "kwp": 5.0}]}
        sf.reconfigure(config)

        source.reconfigure.assert_called_once_with(config)

    def test_reconfigure_skips_sources_without_method(self, state_store):
        """reconfigure() should not crash if a source lacks reconfigure()."""
        source = MockSource("src", _make_source_data({10: 500}, {10: 600}, 5.0, 6.0))
        sf = SolarForecast(state_store, [source])
        sf.reconfigure({"location_lat": 49.0})  # Should not raise
