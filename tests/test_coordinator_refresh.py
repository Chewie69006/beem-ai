"""Tests for coordinator.async_refresh_battery_from_api() — pre-optimization API refresh (#9)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.beem_ai.coordinator import BeemAICoordinator


@pytest.fixture
def coordinator(mock_hass, state_store):
    """Create a minimal BeemAICoordinator with mocked internals."""
    entry = MagicMock()
    entry.data = {}
    entry.options = {}
    entry.entry_id = "test-entry-123"

    c = BeemAICoordinator(mock_hass, entry)
    c.state_store = state_store
    c._api_client = MagicMock()
    c._api_client.get_battery_state = AsyncMock()
    return c


class TestRefreshBatteryFromApi:
    @pytest.mark.asyncio
    async def test_updates_state_store_with_api_data(self, coordinator, state_store):
        """API response fields are mapped and written to state store."""
        coordinator._api_client.get_battery_state.return_value = {
            "soc": 75.0,
            "solarPower": 3200.0,
            "batteryPower": 1500.0,
            "meterPower": -800.0,
            "inverterPower": 2400.0,
            "globalSoh": 98.5,
        }

        result = await coordinator.async_refresh_battery_from_api()

        assert result is True
        assert state_store.battery.soc == 75.0
        assert state_store.battery.solar_power_w == 3200.0
        assert state_store.battery.battery_power_w == 1500.0
        assert state_store.battery.meter_power_w == -800.0
        assert state_store.battery.inverter_power_w == 2400.0
        assert state_store.battery.soh == 98.5

    @pytest.mark.asyncio
    async def test_returns_false_when_no_api_client(self, coordinator):
        """Without an API client, returns False."""
        coordinator._api_client = None

        result = await coordinator.async_refresh_battery_from_api()

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_api_returns_none(self, coordinator):
        """API failure (None) returns False."""
        coordinator._api_client.get_battery_state.return_value = None

        result = await coordinator.async_refresh_battery_from_api()

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_state_fields(self, coordinator):
        """API response without recognized fields returns False."""
        coordinator._api_client.get_battery_state.return_value = {
            "id": "bat-123",
            "unknownField": 42,
        }

        result = await coordinator.async_refresh_battery_from_api()

        assert result is False

    @pytest.mark.asyncio
    async def test_partial_fields_update_only_what_is_present(
        self, coordinator, state_store
    ):
        """Only fields present in the API response are updated."""
        # Set initial values
        state_store.update_battery(soc=50.0, solar_power_w=1000.0)

        coordinator._api_client.get_battery_state.return_value = {
            "soc": 72.0,
        }

        result = await coordinator.async_refresh_battery_from_api()

        assert result is True
        assert state_store.battery.soc == 72.0
        # solar_power_w unchanged
        assert state_store.battery.solar_power_w == 1000.0

    @pytest.mark.asyncio
    async def test_logs_soc_discrepancy(self, coordinator, state_store, caplog):
        """Logs a warning when MQTT SoC differs from API SoC by >2%."""
        state_store.update_battery(soc=60.0)

        coordinator._api_client.get_battery_state.return_value = {
            "soc": 75.0,
        }

        import logging
        with caplog.at_level(logging.WARNING):
            await coordinator.async_refresh_battery_from_api()

        assert "SoC discrepancy" in caplog.text
        assert "MQTT=60.0%" in caplog.text
        assert "API=75.0%" in caplog.text

    @pytest.mark.asyncio
    async def test_no_discrepancy_log_when_close(self, coordinator, state_store, caplog):
        """No warning when MQTT and API SoC are within 2%."""
        state_store.update_battery(soc=74.0)

        coordinator._api_client.get_battery_state.return_value = {
            "soc": 75.0,
        }

        import logging
        with caplog.at_level(logging.WARNING):
            await coordinator.async_refresh_battery_from_api()

        assert "SoC discrepancy" not in caplog.text

    @pytest.mark.asyncio
    async def test_null_api_fields_are_skipped(self, coordinator, state_store):
        """None values in the API response are not written to state store."""
        state_store.update_battery(soc=50.0)

        coordinator._api_client.get_battery_state.return_value = {
            "soc": None,
            "solarPower": 2000.0,
        }

        await coordinator.async_refresh_battery_from_api()

        # soc should remain unchanged (None was skipped)
        assert state_store.battery.soc == 50.0
        assert state_store.battery.solar_power_w == 2000.0
