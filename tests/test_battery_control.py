"""Tests for battery control switch and number entities."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.beem_ai.switch import (
    BeemAIAllowGridChargeSwitch,
    BeemAIPreventDischargeSwitch,
)
from custom_components.beem_ai.number import (
    BeemAIMinSocNumber,
    BeemAIMaxSocNumber,
)


@pytest.fixture
def coordinator(state_store):
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
# Allow Grid Charge Switch
# ------------------------------------------------------------------


class TestAllowGridChargeSwitch:
    def test_is_on_reads_control_state(self, coordinator, entry):
        sw = BeemAIAllowGridChargeSwitch(coordinator, entry)
        assert sw.is_on is False

    def test_is_on_after_update(self, coordinator, entry, state_store):
        state_store.update_control(allow_charge_from_grid=True)
        sw = BeemAIAllowGridChargeSwitch(coordinator, entry)
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on(self, coordinator, entry):
        sw = BeemAIAllowGridChargeSwitch(coordinator, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coordinator.async_set_battery_control.assert_awaited_once_with(
            allow_charge_from_grid=True
        )

    @pytest.mark.asyncio
    async def test_turn_off(self, coordinator, entry):
        sw = BeemAIAllowGridChargeSwitch(coordinator, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coordinator.async_set_battery_control.assert_awaited_once_with(
            allow_charge_from_grid=False
        )

    def test_unique_id(self, coordinator, entry):
        sw = BeemAIAllowGridChargeSwitch(coordinator, entry)
        assert sw._attr_unique_id == "test-entry-123_allow_grid_charge"

    def test_available_when_advanced(self, coordinator, entry, state_store):
        state_store.update_control(mode="advanced")
        sw = BeemAIAllowGridChargeSwitch(coordinator, entry)
        assert sw.available is True

    def test_unavailable_when_auto(self, coordinator, entry):
        sw = BeemAIAllowGridChargeSwitch(coordinator, entry)
        assert sw.available is False

    def test_unavailable_when_pause(self, coordinator, entry, state_store):
        state_store.update_control(mode="pause")
        sw = BeemAIAllowGridChargeSwitch(coordinator, entry)
        assert sw.available is False


# ------------------------------------------------------------------
# Prevent Discharge Switch
# ------------------------------------------------------------------


class TestPreventDischargeSwitch:
    def test_is_on_reads_control_state(self, coordinator, entry):
        sw = BeemAIPreventDischargeSwitch(coordinator, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on(self, coordinator, entry):
        sw = BeemAIPreventDischargeSwitch(coordinator, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coordinator.async_set_battery_control.assert_awaited_once_with(
            prevent_discharge=True
        )

    def test_unique_id(self, coordinator, entry):
        sw = BeemAIPreventDischargeSwitch(coordinator, entry)
        assert sw._attr_unique_id == "test-entry-123_prevent_discharge"

    def test_available_when_advanced(self, coordinator, entry, state_store):
        state_store.update_control(mode="advanced")
        sw = BeemAIPreventDischargeSwitch(coordinator, entry)
        assert sw.available is True

    def test_unavailable_when_auto(self, coordinator, entry):
        sw = BeemAIPreventDischargeSwitch(coordinator, entry)
        assert sw.available is False


# ------------------------------------------------------------------
# Min SoC Number
# ------------------------------------------------------------------


class TestMinSocNumber:
    def test_native_value_default(self, coordinator, entry):
        num = BeemAIMinSocNumber(coordinator, entry)
        assert num.native_value == 20

    def test_range(self, coordinator, entry):
        num = BeemAIMinSocNumber(coordinator, entry)
        assert num._attr_native_min_value == 10
        assert num._attr_native_max_value == 50
        assert num._attr_native_step == 1

    @pytest.mark.asyncio
    async def test_set_value(self, coordinator, entry):
        num = BeemAIMinSocNumber(coordinator, entry)
        num.async_write_ha_state = MagicMock()
        await num.async_set_native_value(10.0)
        coordinator.async_set_battery_control.assert_awaited_once_with(min_soc=10)

    def test_unique_id(self, coordinator, entry):
        num = BeemAIMinSocNumber(coordinator, entry)
        assert num._attr_unique_id == "test-entry-123_min_soc"

    def test_available_when_advanced(self, coordinator, entry, state_store):
        state_store.update_control(mode="advanced")
        num = BeemAIMinSocNumber(coordinator, entry)
        assert num.available is True

    def test_unavailable_when_auto(self, coordinator, entry):
        num = BeemAIMinSocNumber(coordinator, entry)
        assert num.available is False


# ------------------------------------------------------------------
# Max SoC Number
# ------------------------------------------------------------------


class TestMaxSocNumber:
    def test_native_value_default(self, coordinator, entry):
        num = BeemAIMaxSocNumber(coordinator, entry)
        assert num.native_value == 100

    def test_range(self, coordinator, entry):
        num = BeemAIMaxSocNumber(coordinator, entry)
        assert num._attr_native_min_value == 50
        assert num._attr_native_max_value == 100
        assert num._attr_native_step == 1

    @pytest.mark.asyncio
    async def test_set_value(self, coordinator, entry):
        num = BeemAIMaxSocNumber(coordinator, entry)
        num.async_write_ha_state = MagicMock()
        await num.async_set_native_value(95.0)
        coordinator.async_set_battery_control.assert_awaited_once_with(max_soc=95)

    def test_unique_id(self, coordinator, entry):
        num = BeemAIMaxSocNumber(coordinator, entry)
        assert num._attr_unique_id == "test-entry-123_max_soc"

    def test_available_when_advanced(self, coordinator, entry, state_store):
        state_store.update_control(mode="advanced")
        num = BeemAIMaxSocNumber(coordinator, entry)
        assert num.available is True

    def test_unavailable_when_auto(self, coordinator, entry):
        num = BeemAIMaxSocNumber(coordinator, entry)
        assert num.available is False
