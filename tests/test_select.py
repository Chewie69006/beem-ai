"""Tests for BeemAI select entity (battery mode)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.beem_ai.select import BeemAIBatteryModeSelect


@pytest.fixture
def coordinator(state_store):
    """Minimal coordinator mock with state_store."""
    c = MagicMock()
    c.state_store = state_store
    c.async_set_battery_control = AsyncMock(return_value=True)
    return c


@pytest.fixture
def entry():
    e = MagicMock()
    e.entry_id = "test-entry-123"
    return e


class TestBatteryModeSelect:
    def test_current_option_reads_from_control_state(self, coordinator, entry):
        select = BeemAIBatteryModeSelect(coordinator, entry)
        assert select.current_option == "auto"

    def test_current_option_after_update(self, coordinator, entry, state_store):
        state_store.update_control(mode="advanced")
        select = BeemAIBatteryModeSelect(coordinator, entry)
        assert select.current_option == "advanced"

    @pytest.mark.asyncio
    async def test_select_option_calls_coordinator(self, coordinator, entry):
        select = BeemAIBatteryModeSelect(coordinator, entry)
        select.async_write_ha_state = MagicMock()

        await select.async_select_option("advanced")

        coordinator.async_set_battery_control.assert_awaited_once_with(mode="advanced")
        select.async_write_ha_state.assert_called_once()

    def test_unique_id(self, coordinator, entry):
        select = BeemAIBatteryModeSelect(coordinator, entry)
        assert select._attr_unique_id == "test-entry-123_battery_mode"

    def test_options(self, coordinator, entry):
        select = BeemAIBatteryModeSelect(coordinator, entry)
        assert select._attr_options == ["auto", "advanced"]
