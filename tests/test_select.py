"""Tests for BeemAI select entities (battery mode + charge power)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.beem_ai.select import (
    BeemAIBatteryModeSelect,
    BeemAIChargePowerSelect,
)


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


# ------------------------------------------------------------------
# Battery Mode Select
# ------------------------------------------------------------------


class TestBatteryModeSelect:
    def test_current_option_reads_from_control_state(self, coordinator, entry):
        select = BeemAIBatteryModeSelect(coordinator, entry)
        assert select.current_option == "auto"

    def test_current_option_after_update(self, coordinator, entry, state_store):
        state_store.update_control(mode="advanced")
        select = BeemAIBatteryModeSelect(coordinator, entry)
        assert select.current_option == "advanced"

    def test_current_option_pause(self, coordinator, entry, state_store):
        state_store.update_control(mode="pause")
        select = BeemAIBatteryModeSelect(coordinator, entry)
        assert select.current_option == "pause"

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

    def test_options_includes_pause(self, coordinator, entry):
        select = BeemAIBatteryModeSelect(coordinator, entry)
        assert select._attr_options == ["auto", "pause", "advanced"]

    def test_available_when_can_change_mode(self, coordinator, entry, state_store):
        state_store.update_control(can_change_mode=True)
        select = BeemAIBatteryModeSelect(coordinator, entry)
        assert select.available is True

    def test_unavailable_when_cannot_change_mode(self, coordinator, entry, state_store):
        state_store.update_control(can_change_mode=False)
        select = BeemAIBatteryModeSelect(coordinator, entry)
        assert select.available is False


# ------------------------------------------------------------------
# Charge Power Select
# ------------------------------------------------------------------


class TestChargePowerSelect:
    def test_current_option_reads_control_state(self, coordinator, entry):
        select = BeemAIChargePowerSelect(coordinator, entry)
        assert select.current_option == "0"

    def test_current_option_after_update(self, coordinator, entry, state_store):
        state_store.update_control(charge_from_grid_max_power=2500)
        select = BeemAIChargePowerSelect(coordinator, entry)
        assert select.current_option == "2500"

    @pytest.mark.asyncio
    async def test_select_option_calls_coordinator(self, coordinator, entry):
        select = BeemAIChargePowerSelect(coordinator, entry)
        select.async_write_ha_state = MagicMock()

        await select.async_select_option("5000")

        coordinator.async_set_battery_control.assert_awaited_once_with(
            charge_from_grid_max_power=5000
        )

    def test_unique_id(self, coordinator, entry):
        select = BeemAIChargePowerSelect(coordinator, entry)
        assert select._attr_unique_id == "test-entry-123_charge_power"

    def test_options(self, coordinator, entry):
        select = BeemAIChargePowerSelect(coordinator, entry)
        assert select._attr_options == ["500", "1000", "2500", "5000"]

    def test_available_when_advanced(self, coordinator, entry, state_store):
        state_store.update_control(mode="advanced")
        select = BeemAIChargePowerSelect(coordinator, entry)
        assert select.available is True

    def test_unavailable_when_auto(self, coordinator, entry, state_store):
        select = BeemAIChargePowerSelect(coordinator, entry)
        assert select.available is False  # default mode is "auto"

    def test_unavailable_when_pause(self, coordinator, entry, state_store):
        state_store.update_control(mode="pause")
        select = BeemAIChargePowerSelect(coordinator, entry)
        assert select.available is False
