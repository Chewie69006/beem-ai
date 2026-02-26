"""Tests for StateStore forecast persistence (save/load roundtrip)."""

import json
import os
from datetime import datetime

import pytest

from custom_components.beem_ai.state_store import ForecastData, StateStore


@pytest.fixture
def data_dir(tmp_path):
    """Provide a temporary data directory."""
    return str(tmp_path)


@pytest.fixture
def store():
    return StateStore()


# ── Forecast persistence ────────────────────────────────────────────


class TestSaveForecast:
    def test_roundtrip_basic(self, store, data_dir):
        store.update_forecast(
            solar_today={8: 500, 12: 3000, 16: 1200},
            solar_tomorrow={9: 800, 13: 2500},
            solar_today_kwh=15.5,
            solar_tomorrow_kwh=12.3,
            consumption_tomorrow_kwh=8.0,
            consumption_hourly={0: 300, 12: 600},
            confidence="high",
            sources_used=["open_meteo", "forecast_solar"],
        )
        store.save_forecast(data_dir)

        store2 = StateStore()
        assert store2.load_forecast(data_dir) is True

        f = store2.forecast
        assert f.solar_today == {8: 500, 12: 3000, 16: 1200}
        assert f.solar_tomorrow == {9: 800, 13: 2500}
        assert f.solar_today_kwh == 15.5
        assert f.solar_tomorrow_kwh == 12.3
        assert f.consumption_tomorrow_kwh == 8.0
        assert f.consumption_hourly == {0: 300, 12: 600}
        assert f.confidence == "high"
        assert f.sources_used == ["open_meteo", "forecast_solar"]

    def test_roundtrip_preserves_last_updated(self, store, data_dir):
        store.update_forecast(solar_today_kwh=10.0)
        original_ts = store.forecast.last_updated
        store.save_forecast(data_dir)

        store2 = StateStore()
        store2.load_forecast(data_dir)
        # Roundtrip through isoformat should preserve to microsecond
        assert store2.forecast.last_updated == original_ts

    def test_roundtrip_p10_p90(self, store, data_dir):
        store.update_forecast(
            solar_today_p10={8: 300, 12: 2000},
            solar_today_p90={8: 700, 12: 4000},
            solar_tomorrow_p10={9: 500},
            solar_tomorrow_p90={9: 1100},
        )
        store.save_forecast(data_dir)

        store2 = StateStore()
        store2.load_forecast(data_dir)
        assert store2.forecast.solar_today_p10 == {8: 300, 12: 2000}
        assert store2.forecast.solar_today_p90 == {8: 700, 12: 4000}

    def test_load_missing_file_returns_false(self, store, data_dir):
        assert store.load_forecast(data_dir) is False
        assert store.forecast.solar_today_kwh == 0.0

    def test_load_corrupt_file_returns_false(self, store, data_dir):
        path = os.path.join(data_dir, "forecast_state.json")
        with open(path, "w") as f:
            f.write("{broken")
        assert store.load_forecast(data_dir) is False

    def test_empty_dicts_roundtrip(self, store, data_dir):
        """Default empty forecast should roundtrip cleanly."""
        store.save_forecast(data_dir)
        store2 = StateStore()
        store2.load_forecast(data_dir)
        assert store2.forecast.solar_today == {}
        assert store2.forecast.solar_tomorrow == {}

    def test_int_keys_restored(self, store, data_dir):
        """Dict keys should be restored as int, not string."""
        store.update_forecast(solar_today={10: 1500})
        store.save_forecast(data_dir)

        store2 = StateStore()
        store2.load_forecast(data_dir)
        keys = list(store2.forecast.solar_today.keys())
        assert all(isinstance(k, int) for k in keys)
