"""Unit tests for WaterHeaterController (async, HA integration)."""

from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.beem_ai.tariff_manager import (
    TARIFF_HC,
    TARIFF_HP,
    TARIFF_HSC,
    TariffManager,
)
from custom_components.beem_ai.water_heater import (
    WaterHeaterController,
    _DAILY_HEATING_MIN_KWH,
    _HSC_FALLBACK_DEADLINE,
    _SOLAR_SURPLUS_MIN_W,
)
from custom_components.beem_ai.event_bus import Event


@pytest.fixture
def tariff_manager():
    return TariffManager(hp_price=0.27, hc_price=0.20, hsc_price=0.15)


@pytest.fixture
def heater(mock_hass, state_store, event_bus, tariff_manager):
    """Create a WaterHeaterController with sensible defaults."""
    # Set up mock_hass.states.get to return a mock state object
    mock_state = MagicMock()
    mock_state.state = "0"
    mock_hass.states.get = MagicMock(return_value=mock_state)

    return WaterHeaterController(
        hass=mock_hass,
        state_store=state_store,
        event_bus=event_bus,
        tariff_manager=tariff_manager,
        switch_entity="switch.water_heater",
        power_entity="sensor.water_heater_power",
        heater_power_w=2000.0,
    )


# ------------------------------------------------------------------
# Solar surplus -> ON
# ------------------------------------------------------------------


class TestSolarSurplus:
    @pytest.mark.asyncio
    async def test_surplus_turns_on(self, heater, state_store, mock_hass):
        """Solar export above threshold -> heater ON."""
        state_store.update_battery(meter_power_w=-500)

        with patch.object(heater._tariff_manager, "get_current_tariff", return_value=TARIFF_HP):
            decision = await heater.evaluate()

        assert decision == "solar surplus heating"
        assert heater.is_on is True
        mock_hass.services.async_call.assert_called()

    @pytest.mark.asyncio
    async def test_surplus_sets_solar_on_flag(self, heater, state_store, mock_hass):
        """Solar surplus sets _solar_on flag."""
        state_store.update_battery(meter_power_w=-500)

        with patch.object(heater._tariff_manager, "get_current_tariff", return_value=TARIFF_HP):
            await heater.evaluate()

        assert heater._solar_on is True


# ------------------------------------------------------------------
# Solar surplus ended -> OFF
# ------------------------------------------------------------------


class TestSolarSurplusEnded:
    @pytest.mark.asyncio
    async def test_surplus_ended_turns_off(self, heater, state_store, mock_hass):
        """No surplus + was solar-ON -> heater OFF."""
        state_store.update_battery(meter_power_w=100)
        heater._is_on = True
        heater._solar_on = True

        with patch.object(heater._tariff_manager, "get_current_tariff", return_value=TARIFF_HP):
            decision = await heater.evaluate()

        assert decision == "solar surplus ended"
        assert heater.is_on is False
        assert heater._solar_on is False


# ------------------------------------------------------------------
# Off-peak fallback
# ------------------------------------------------------------------


class TestOffPeakFallback:
    @pytest.mark.asyncio
    async def test_hsc_tariff_low_energy_turns_on(self, heater, state_store, mock_hass):
        """HSC tariff + low daily energy -> heater ON."""
        state_store.update_battery(meter_power_w=100)
        heater._daily_energy_kwh = 1.0  # below _DAILY_HEATING_MIN_KWH

        with patch.object(heater._tariff_manager, "get_current_tariff", return_value=TARIFF_HSC):
            decision = await heater.evaluate()

        assert decision == "off-peak fallback"
        assert heater.is_on is True

    @pytest.mark.asyncio
    async def test_hc_tariff_after_deadline_turns_on(self, heater, state_store, mock_hass):
        """HC tariff after 22:00 + low energy -> heater ON."""
        state_store.update_battery(meter_power_w=100)
        heater._daily_energy_kwh = 1.0

        fake_now = datetime(2026, 2, 23, 22, 30)

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        with (
            patch(
                "custom_components.beem_ai.water_heater.datetime", FakeDatetime
            ),
            patch.object(
                heater._tariff_manager,
                "get_current_tariff",
                return_value=TARIFF_HC,
            ),
        ):
            decision = await heater.evaluate()

        assert decision == "off-peak fallback"

    @pytest.mark.asyncio
    async def test_hc_tariff_before_deadline_no_turn_on(self, heater, state_store, mock_hass):
        """HC tariff before 22:00 + low energy -> should NOT trigger fallback."""
        state_store.update_battery(meter_power_w=100)
        heater._daily_energy_kwh = 1.0

        fake_now = datetime(2026, 2, 23, 20, 0)

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        with (
            patch(
                "custom_components.beem_ai.water_heater.datetime", FakeDatetime
            ),
            patch.object(
                heater._tariff_manager,
                "get_current_tariff",
                return_value=TARIFF_HC,
            ),
        ):
            decision = await heater.evaluate()

        assert decision == "maintaining current state"

    @pytest.mark.asyncio
    async def test_enough_energy_no_fallback(self, heater, state_store, mock_hass):
        """Off-peak but already heated enough -> no fallback."""
        state_store.update_battery(meter_power_w=100)
        heater._daily_energy_kwh = 5.0  # above _DAILY_HEATING_MIN_KWH

        with patch.object(heater._tariff_manager, "get_current_tariff", return_value=TARIFF_HSC):
            decision = await heater.evaluate()

        assert decision == "maintaining current state"


# ------------------------------------------------------------------
# Grid import during HP -> OFF
# ------------------------------------------------------------------


class TestGridImportHP:
    @pytest.mark.asyncio
    async def test_importing_during_hp_turns_off(self, heater, state_store, mock_hass):
        """Grid import during peak tariff -> heater OFF."""
        state_store.update_battery(meter_power_w=800)
        heater._is_on = True

        with patch.object(heater._tariff_manager, "get_current_tariff", return_value=TARIFF_HP):
            decision = await heater.evaluate()

        assert decision == "avoiding HP import"
        assert heater.is_on is False


# ------------------------------------------------------------------
# System disabled -> OFF
# ------------------------------------------------------------------


class TestSystemDisabled:
    @pytest.mark.asyncio
    async def test_disabled_turns_off(self, heater, state_store, mock_hass):
        """System disabled flag forces heater OFF."""
        state_store.enabled = False
        heater._is_on = True

        decision = await heater.evaluate()

        assert decision == "system disabled"
        assert heater.is_on is False


# ------------------------------------------------------------------
# reset_daily()
# ------------------------------------------------------------------


class TestResetDaily:
    def test_resets_counters(self, heater):
        """reset_daily() clears daily energy and solar_on flag."""
        heater._daily_energy_kwh = 5.0
        heater._solar_on = True
        heater._last_power_reading_time = datetime.now()

        heater.reset_daily()

        assert heater.daily_energy_kwh == 0.0
        assert heater._solar_on is False
        assert heater._last_power_reading_time is None


# ------------------------------------------------------------------
# reconfigure()
# ------------------------------------------------------------------


class TestReconfigure:
    def test_reconfigure_updates_entities(self, heater):
        """reconfigure() updates switch and power entities."""
        heater.reconfigure({
            "water_heater_switch_entity": "switch.new_heater",
            "water_heater_power_entity": "sensor.new_power",
            "water_heater_power_w": 3000,
        })

        assert heater._switch_entity == "switch.new_heater"
        assert heater._power_entity == "sensor.new_power"
        assert heater._heater_power_w == 3000.0


# ------------------------------------------------------------------
# Event publication on state change
# ------------------------------------------------------------------


class TestEventPublication:
    @pytest.mark.asyncio
    async def test_turn_on_publishes_event(self, heater, event_bus, mock_hass):
        """Turning heater on publishes WATER_HEATER_CHANGED with state=on."""
        received = []
        event_bus.subscribe(
            Event.WATER_HEATER_CHANGED, lambda d: received.append(d)
        )

        await heater._turn_on("test reason")

        assert len(received) == 1
        assert received[0]["state"] == "on"
        assert received[0]["reason"] == "test reason"

    @pytest.mark.asyncio
    async def test_turn_off_publishes_event(self, heater, event_bus, mock_hass):
        """Turning heater off publishes WATER_HEATER_CHANGED with state=off."""
        heater._is_on = True
        received = []
        event_bus.subscribe(
            Event.WATER_HEATER_CHANGED, lambda d: received.append(d)
        )

        await heater._turn_off("test off")

        assert len(received) == 1
        assert received[0]["state"] == "off"

    @pytest.mark.asyncio
    async def test_no_event_when_already_off(self, heater, event_bus):
        """No event if heater is already off and _turn_off is called."""
        received = []
        event_bus.subscribe(
            Event.WATER_HEATER_CHANGED, lambda d: received.append(d)
        )

        await heater._turn_off("already off")

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_no_event_when_already_on(self, heater, event_bus, mock_hass):
        """No event if heater is already on and _turn_on is called."""
        heater._is_on = True
        received = []
        event_bus.subscribe(
            Event.WATER_HEATER_CHANGED, lambda d: received.append(d)
        )

        await heater._turn_on("already on")

        assert len(received) == 0


# ------------------------------------------------------------------
# Energy tracking
# ------------------------------------------------------------------


class TestEnergyTracking:
    def test_energy_accumulates(self, heater, mock_hass):
        """Energy tracking accumulates based on power readings."""
        mock_state = MagicMock()
        mock_state.state = "2000"
        mock_hass.states.get = MagicMock(return_value=mock_state)

        # First call sets baseline
        heater._estimate_daily_energy()
        assert heater.daily_energy_kwh == 0.0

        # Simulate 30 minutes passing
        heater._last_power_reading_time = datetime(2026, 2, 23, 12, 0)
        fake_now = datetime(2026, 2, 23, 12, 30)

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        with patch("custom_components.beem_ai.water_heater.datetime", FakeDatetime):
            heater._estimate_daily_energy()

        # 2000W * 0.5h = 1.0 kWh
        assert heater.daily_energy_kwh == pytest.approx(1.0, abs=0.01)
