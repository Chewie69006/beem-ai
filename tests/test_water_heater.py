"""Unit tests for WaterHeaterController (async, HA integration)."""

from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.beem_ai.tariff_manager import TariffManager
from custom_components.beem_ai.water_heater import (
    WaterHeaterController,
    _BATTERY_FULL_SOC,
    _BATTERY_FULL_SOC_HYSTERESIS,
    _DAILY_HEATING_MIN_KWH,
    _HC_FALLBACK_DEADLINE,
    _SOLAR_MIN_PRODUCTION_W,
    _SOLAR_SURPLUS_HYSTERESIS_FACTOR,
)
from custom_components.beem_ai.event_bus import Event

_HEATER_POWER_W = 2000.0  # matches fixture below


@pytest.fixture
def tariff_manager():
    return TariffManager(default_price=0.27, periods=[
        {"label": "HC", "start": "23:00", "end": "02:00", "price": 0.20},
        {"label": "HSC", "start": "02:00", "end": "06:00", "price": 0.15},
        {"label": "HC", "start": "06:00", "end": "07:00", "price": 0.20},
    ])


@pytest.fixture
def heater(mock_hass, state_store, event_bus, tariff_manager):
    """Create a WaterHeaterController with sensible defaults."""
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
        heater_power_w=_HEATER_POWER_W,
    )


def _patch_peak(heater):
    """Patch tariff manager to simulate peak (HP) tariff — not in any period."""
    return patch.object(heater._tariff_manager, "is_in_any_period", return_value=False)


def _patch_offpeak(heater, cheapest=False):
    """Patch tariff manager to simulate off-peak tariff (in a period)."""
    return (
        patch.object(heater._tariff_manager, "is_in_any_period", return_value=True),
        patch.object(heater._tariff_manager, "is_in_cheapest_period", return_value=cheapest),
    )


# ------------------------------------------------------------------
# Solar surplus -> ON (rule 2)
# ------------------------------------------------------------------


class TestSolarSurplus:
    @pytest.mark.asyncio
    async def test_surplus_turns_on(self, heater, state_store, mock_hass):
        """Grid export >= heater_power_w -> heater ON."""
        # Export must be >= 2000W to cover heater draw
        state_store.update_battery(meter_power_w=-2100)

        # Rule 2 fires and returns before any tariff check
        decision = await heater.evaluate()

        assert decision.startswith("solar surplus:")
        assert heater.is_on is True
        mock_hass.services.async_call.assert_called()

    @pytest.mark.asyncio
    async def test_surplus_below_threshold_does_not_turn_on(self, heater, state_store):
        """Export below heater_power_w (e.g. 300W) should NOT trigger solar surplus."""
        state_store.update_battery(meter_power_w=-300)

        with _patch_peak(heater):
            decision = await heater.evaluate()

        assert not decision.startswith("solar surplus:")
        assert heater.is_on is False

    @pytest.mark.asyncio
    async def test_surplus_sets_solar_on_flag(self, heater, state_store):
        """Solar surplus sets _solar_on flag."""
        state_store.update_battery(meter_power_w=-2500)

        # Rule 2 fires and returns before any tariff check
        await heater.evaluate()

        assert heater._solar_on is True


# ------------------------------------------------------------------
# Solar surplus hysteresis exit (rule 3)
# ------------------------------------------------------------------


class TestSolarSurplusEnded:
    @pytest.mark.asyncio
    async def test_surplus_ended_turns_off(self, heater, state_store):
        """Export drops below 50% of heater_power + was solar-ON -> heater turns OFF."""
        # Export is well below the hysteresis threshold (50% of 2000W = 1000W)
        state_store.update_battery(meter_power_w=0, solar_power_w=50.0)
        heater._is_on = True
        heater._solar_on = True
        heater._daily_energy_kwh = 5.0  # enough energy, so off-peak fallback won't fire

        await heater.evaluate()

        # Key assertions: flag cleared and heater turned off
        assert heater.is_on is False
        assert heater._solar_on is False

    @pytest.mark.asyncio
    async def test_hysteresis_keeps_on_in_middle_zone(self, heater, state_store):
        """Export between 50%-100% of heater_power + solar-ON -> stays ON (hysteresis)."""
        # 50% of 2000W = 1000W; set export to 1200W (between 1000 and 2000)
        state_store.update_battery(meter_power_w=-1200)
        heater._is_on = True
        heater._solar_on = True

        with _patch_peak(heater):
            await heater.evaluate()

        # Should stay on due to hysteresis (neither retrigger condition met)
        assert heater.is_on is True


# ------------------------------------------------------------------
# Battery near full -> ON (rule 5)
# ------------------------------------------------------------------


class TestBatteryFull:
    @pytest.mark.asyncio
    async def test_battery_full_with_solar_turns_on(self, heater, state_store):
        """SoC >= 90% + solar producing -> heater ON."""
        state_store.update_battery(
            soc=92.0,
            solar_power_w=1500.0,
            meter_power_w=0,  # no export (battery absorbing)
        )

        # Rule 5 fires and returns before tariff checks
        decision = await heater.evaluate()

        assert "battery full" in decision
        assert heater.is_on is True
        assert heater._battery_on is True

    @pytest.mark.asyncio
    async def test_battery_full_no_solar_does_not_trigger(self, heater, state_store):
        """SoC >= 90% but no solar production -> should NOT trigger."""
        state_store.update_battery(soc=95.0, solar_power_w=50.0, meter_power_w=0)

        # Falls through to rule 7/8, need peak tariff so rule 7 doesn't fire
        with _patch_peak(heater):
            decision = await heater.evaluate()

        assert "battery full" not in decision
        assert heater.is_on is False

    @pytest.mark.asyncio
    async def test_battery_below_threshold_does_not_trigger(self, heater, state_store):
        """SoC < 90% even with solar -> should NOT trigger battery-full rule."""
        state_store.update_battery(soc=88.0, solar_power_w=2000.0, meter_power_w=0)

        with _patch_peak(heater):
            decision = await heater.evaluate()

        assert "battery full" not in decision

    @pytest.mark.asyncio
    async def test_battery_full_hysteresis_stays_on(self, heater, state_store):
        """Battery-ON mode: stays on until SoC drops below 85% (hysteresis)."""
        # SoC at 87% (below 90% trigger but above 85% hysteresis)
        state_store.update_battery(soc=87.0, solar_power_w=1200.0, meter_power_w=0)
        heater._is_on = True
        heater._battery_on = True

        with _patch_peak(heater):
            await heater.evaluate()

        assert heater.is_on is True

    @pytest.mark.asyncio
    async def test_battery_full_exits_below_hysteresis(self, heater, state_store):
        """Battery-ON mode: turns off when SoC drops below 85%."""
        state_store.update_battery(soc=83.0, solar_power_w=1200.0, meter_power_w=0)
        heater._is_on = True
        heater._battery_on = True

        # Rule 6 fires and returns before tariff checks
        decision = await heater.evaluate()

        assert "battery-full mode ended" in decision
        assert heater.is_on is False
        assert heater._battery_on is False


# ------------------------------------------------------------------
# Off-peak fallback (rule 7)
# ------------------------------------------------------------------


class TestOffPeakFallback:
    @pytest.mark.asyncio
    async def test_hsc_tariff_low_energy_turns_on(self, heater, state_store):
        """Cheapest tariff + low daily energy -> heater ON."""
        state_store.update_battery(meter_power_w=100)
        heater._daily_energy_kwh = 1.0

        p_any, p_cheap = _patch_offpeak(heater, cheapest=True)
        with p_any, p_cheap:
            decision = await heater.evaluate()

        assert decision.startswith("off-peak fallback:")
        assert heater.is_on is True

    @pytest.mark.asyncio
    async def test_hc_tariff_after_deadline_turns_on(self, heater, state_store):
        """Off-peak (non-cheapest) tariff after 22:00 + low energy -> heater ON."""
        state_store.update_battery(meter_power_w=100)
        heater._daily_energy_kwh = 1.0

        fake_now = datetime(2026, 2, 23, 22, 30)

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        p_any, p_cheap = _patch_offpeak(heater, cheapest=False)
        with (
            patch("custom_components.beem_ai.water_heater.datetime", FakeDatetime),
            p_any,
            p_cheap,
        ):
            decision = await heater.evaluate()

        assert decision.startswith("off-peak fallback:")

    @pytest.mark.asyncio
    async def test_hc_tariff_before_deadline_no_turn_on(self, heater, state_store):
        """Off-peak (non-cheapest) tariff before 22:00 -> should NOT trigger fallback."""
        state_store.update_battery(meter_power_w=100)
        heater._daily_energy_kwh = 1.0

        fake_now = datetime(2026, 2, 23, 20, 0)

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        p_any, p_cheap = _patch_offpeak(heater, cheapest=False)
        with (
            patch("custom_components.beem_ai.water_heater.datetime", FakeDatetime),
            p_any,
            p_cheap,
        ):
            decision = await heater.evaluate()

        assert decision == "maintaining current state"

    @pytest.mark.asyncio
    async def test_enough_energy_no_fallback(self, heater, state_store):
        """Already heated enough -> off-peak fallback skipped."""
        state_store.update_battery(meter_power_w=100)
        heater._daily_energy_kwh = 5.0

        p_any, p_cheap = _patch_offpeak(heater, cheapest=True)
        with p_any, p_cheap:
            decision = await heater.evaluate()

        assert decision == "maintaining current state"


# ------------------------------------------------------------------
# HP + grid import -> OFF (rule 8)
# ------------------------------------------------------------------


class TestGridImportHP:
    @pytest.mark.asyncio
    async def test_importing_during_hp_turns_off(self, heater, state_store):
        """Grid import during HP tariff -> heater OFF."""
        state_store.update_battery(meter_power_w=800)
        heater._is_on = True

        with _patch_peak(heater):
            decision = await heater.evaluate()

        assert "HP tariff" in decision
        assert heater.is_on is False


# ------------------------------------------------------------------
# System disabled -> OFF (rule 1)
# ------------------------------------------------------------------


class TestSystemDisabled:
    @pytest.mark.asyncio
    async def test_disabled_turns_off(self, heater, state_store):
        """System disabled forces heater OFF and clears mode flags."""
        state_store.enabled = False
        heater._is_on = True
        heater._solar_on = True
        heater._battery_on = True

        decision = await heater.evaluate()

        assert decision == "system disabled"
        assert heater.is_on is False
        assert heater._solar_on is False
        assert heater._battery_on is False


# ------------------------------------------------------------------
# Dry-run mode
# ------------------------------------------------------------------


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_call_service(self, heater, state_store, mock_hass):
        """In dry-run mode, no HA service call is made."""
        heater._dry_run = True
        state_store.update_battery(meter_power_w=-2500)

        # Rule 2 fires (solar surplus) before tariff checks
        decision = await heater.evaluate()

        assert "[DRY RUN]" in decision
        mock_hass.services.async_call.assert_not_called()
        assert heater.is_on is False  # internal state not changed

    @pytest.mark.asyncio
    async def test_dry_run_turn_off_logs_only(self, heater, state_store, mock_hass):
        """In dry-run mode, turn_off logs but doesn't call service."""
        heater._dry_run = True
        heater._is_on = True
        state_store.enabled = False

        decision = await heater.evaluate()

        assert "[DRY RUN]" in decision
        mock_hass.services.async_call.assert_not_called()
        assert heater.is_on is True  # not changed in dry-run


# ------------------------------------------------------------------
# reset_daily()
# ------------------------------------------------------------------


class TestResetDaily:
    def test_resets_counters(self, heater):
        """reset_daily() clears all daily counters and mode flags."""
        heater._daily_energy_kwh = 5.0
        heater._solar_on = True
        heater._battery_on = True
        heater._last_power_reading_time = datetime.now()

        heater.reset_daily()

        assert heater.daily_energy_kwh == 0.0
        assert heater._solar_on is False
        assert heater._battery_on is False
        assert heater._last_power_reading_time is None


# ------------------------------------------------------------------
# reconfigure()
# ------------------------------------------------------------------


class TestReconfigure:
    def test_reconfigure_updates_entities(self, heater):
        """reconfigure() updates switch, power entities, and dry_run."""
        heater.reconfigure({
            "water_heater_switch_entity": "switch.new_heater",
            "water_heater_power_entity": "sensor.new_power",
            "water_heater_power_w": 3000,
            "dry_run": True,
        })

        assert heater._switch_entity == "switch.new_heater"
        assert heater._power_entity == "sensor.new_power"
        assert heater._heater_power_w == 3000.0
        assert heater._dry_run is True


# ------------------------------------------------------------------
# Event publication
# ------------------------------------------------------------------


class TestEventPublication:
    @pytest.mark.asyncio
    async def test_turn_on_publishes_event(self, heater, event_bus, mock_hass):
        """Turning heater on publishes WATER_HEATER_CHANGED with state=on."""
        received = []
        event_bus.subscribe(Event.WATER_HEATER_CHANGED, lambda d: received.append(d))

        await heater._turn_on("test reason")

        assert len(received) == 1
        assert received[0]["state"] == "on"
        assert received[0]["reason"] == "test reason"

    @pytest.mark.asyncio
    async def test_turn_off_publishes_event(self, heater, event_bus, mock_hass):
        """Turning heater off publishes WATER_HEATER_CHANGED with state=off."""
        heater._is_on = True
        received = []
        event_bus.subscribe(Event.WATER_HEATER_CHANGED, lambda d: received.append(d))

        await heater._turn_off("test off")

        assert len(received) == 1
        assert received[0]["state"] == "off"

    @pytest.mark.asyncio
    async def test_no_event_when_already_off(self, heater, event_bus):
        """No event if heater is already off and _turn_off is called."""
        received = []
        event_bus.subscribe(Event.WATER_HEATER_CHANGED, lambda d: received.append(d))

        await heater._turn_off("already off")

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_no_event_when_already_on(self, heater, event_bus, mock_hass):
        """No event if heater is already on and _turn_on is called."""
        heater._is_on = True
        received = []
        event_bus.subscribe(Event.WATER_HEATER_CHANGED, lambda d: received.append(d))

        await heater._turn_on("already on")

        assert len(received) == 0


# ------------------------------------------------------------------
# Energy tracking
# ------------------------------------------------------------------


class TestEnergyTracking:
    def test_energy_accumulates(self, heater, mock_hass):
        """Energy tracking accumulates kWh from power sensor readings."""
        mock_state = MagicMock()
        mock_state.state = "2000"
        mock_hass.states.get = MagicMock(return_value=mock_state)

        heater._estimate_daily_energy()
        assert heater.daily_energy_kwh == 0.0

        heater._last_power_reading_time = datetime(2026, 2, 23, 12, 0)
        fake_now = datetime(2026, 2, 23, 12, 30)

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        with patch("custom_components.beem_ai.water_heater.datetime", FakeDatetime):
            heater._estimate_daily_energy()

        assert heater.daily_energy_kwh == pytest.approx(1.0, abs=0.01)
