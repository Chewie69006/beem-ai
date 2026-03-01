"""Tests for battery control switch and number entities."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.beem_ai.switch import (
    BeemAIAllowGridChargeSwitch,
    BeemAIPreventDischargeSwitch,
)
from custom_components.beem_ai.number import (
    BeemAIChargePowerNumber,
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


# ------------------------------------------------------------------
# Charge Power Number
# ------------------------------------------------------------------


class TestChargePowerNumber:
    def test_native_value_reads_control_state(self, coordinator, entry):
        num = BeemAIChargePowerNumber(coordinator, entry)
        assert num.native_value == 0

    def test_native_value_after_update(self, coordinator, entry, state_store):
        state_store.update_control(charge_from_grid_max_power=3000)
        num = BeemAIChargePowerNumber(coordinator, entry)
        assert num.native_value == 3000

    @pytest.mark.asyncio
    async def test_set_value(self, coordinator, entry):
        num = BeemAIChargePowerNumber(coordinator, entry)
        num.async_write_ha_state = MagicMock()
        await num.async_set_native_value(2500.0)
        coordinator.async_set_battery_control.assert_awaited_once_with(
            charge_from_grid_max_power=2500
        )

    def test_unique_id(self, coordinator, entry):
        num = BeemAIChargePowerNumber(coordinator, entry)
        assert num._attr_unique_id == "test-entry-123_charge_power"


# ------------------------------------------------------------------
# Min SoC Number
# ------------------------------------------------------------------


class TestMinSocNumber:
    def test_native_value_default(self, coordinator, entry):
        num = BeemAIMinSocNumber(coordinator, entry)
        assert num.native_value == 20

    @pytest.mark.asyncio
    async def test_set_value(self, coordinator, entry):
        num = BeemAIMinSocNumber(coordinator, entry)
        num.async_write_ha_state = MagicMock()
        await num.async_set_native_value(10.0)
        coordinator.async_set_battery_control.assert_awaited_once_with(min_soc=10)

    def test_unique_id(self, coordinator, entry):
        num = BeemAIMinSocNumber(coordinator, entry)
        assert num._attr_unique_id == "test-entry-123_min_soc"


# ------------------------------------------------------------------
# Max SoC Number
# ------------------------------------------------------------------


class TestMaxSocNumber:
    def test_native_value_default(self, coordinator, entry):
        num = BeemAIMaxSocNumber(coordinator, entry)
        assert num.native_value == 100

    @pytest.mark.asyncio
    async def test_set_value(self, coordinator, entry):
        num = BeemAIMaxSocNumber(coordinator, entry)
        num.async_write_ha_state = MagicMock()
        await num.async_set_native_value(95.0)
        coordinator.async_set_battery_control.assert_awaited_once_with(max_soc=95)

    def test_unique_id(self, coordinator, entry):
        num = BeemAIMaxSocNumber(coordinator, entry)
        assert num._attr_unique_id == "test-entry-123_max_soc"
