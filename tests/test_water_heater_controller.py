"""Tests for WaterHeaterController."""

import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.beem_ai.water_heater_controller import (
    EXPORT_MIN_W,
    HeaterState,
    SOC_START_THRESHOLD,
    SOC_STOP_THRESHOLD,
    SUSTAIN_SECONDS,
    WaterHeaterController,
)


def _make_controller(power_value=2000.0):
    """Create a controller with mocked hass."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    # Mock power sensor state
    state_obj = MagicMock()
    state_obj.state = str(power_value)
    hass.states.get = MagicMock(return_value=state_obj)

    ctrl = WaterHeaterController(
        hass=hass,
        switch_entity_id="switch.water_heater",
        power_sensor_entity_id="sensor.water_heater_power",
    )
    return ctrl, hass


# ------------------------------------------------------------------
# State: initial
# ------------------------------------------------------------------


def test_initial_state():
    """Controller starts in IDLE with zero energy."""
    ctrl, _ = _make_controller()
    assert ctrl._state == HeaterState.IDLE
    assert ctrl.is_heating is False
    assert ctrl.accumulated_kwh == 0.0


# ------------------------------------------------------------------
# IDLE → HEATING transitions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_transition_below_soc_threshold():
    """Export OK but SoC too low — stays IDLE."""
    ctrl, hass = _make_controller()
    await ctrl.evaluate(soc=90.0, export_w=600)
    assert ctrl._state == HeaterState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_transition_below_export_threshold():
    """SoC OK but export too low — stays IDLE."""
    ctrl, hass = _make_controller()
    await ctrl.evaluate(soc=96.0, export_w=400)
    assert ctrl._state == HeaterState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_transition_before_sustain_period():
    """Conditions met but not sustained long enough — stays IDLE."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600)
    assert ctrl._state == HeaterState.IDLE

    # Only 10 seconds later — not enough
    with patch("time.monotonic", return_value=1010.0):
        await ctrl.evaluate(soc=96.0, export_w=600)
    assert ctrl._state == HeaterState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_transition_after_sustain_period():
    """Conditions met and sustained → turns on heater."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600)

    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=600)

    assert ctrl._state == HeaterState.HEATING
    assert ctrl.is_heating is True
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_on", {"entity_id": "switch.water_heater"}
    )


@pytest.mark.asyncio
async def test_sustain_timer_resets_when_export_drops():
    """Export drops during sustain period — timer resets."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600)
    assert ctrl._export_sustained_since is not None

    # Export drops
    with patch("time.monotonic", return_value=1015.0):
        await ctrl.evaluate(soc=96.0, export_w=200)
    assert ctrl._export_sustained_since is None

    # Restart sustain
    with patch("time.monotonic", return_value=1020.0):
        await ctrl.evaluate(soc=96.0, export_w=600)

    # Not enough time from new start
    with patch("time.monotonic", return_value=1040.0):
        await ctrl.evaluate(soc=96.0, export_w=600)
    assert ctrl._state == HeaterState.IDLE

    # Now enough time
    with patch("time.monotonic", return_value=1020.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=600)
    assert ctrl._state == HeaterState.HEATING


# ------------------------------------------------------------------
# HEATING → IDLE transitions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stays_heating_at_exact_threshold():
    """SoC exactly at stop threshold (90%) — stays HEATING (< required)."""
    ctrl, hass = _make_controller()

    # Get into HEATING state
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=600)
    assert ctrl._state == HeaterState.HEATING
    hass.services.async_call.reset_mock()

    # SoC at threshold — stays HEATING
    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(soc=SOC_STOP_THRESHOLD, export_w=0)

    assert ctrl._state == HeaterState.HEATING
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_stops_when_soc_drops_below_threshold():
    """SoC drops below stop threshold → turns off heater."""
    ctrl, hass = _make_controller()

    # Get into HEATING state
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=600)
    assert ctrl._state == HeaterState.HEATING
    hass.services.async_call.reset_mock()

    # SoC drops below threshold
    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(soc=SOC_STOP_THRESHOLD - 1, export_w=0)

    assert ctrl._state == HeaterState.IDLE
    assert ctrl.is_heating is False
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_off", {"entity_id": "switch.water_heater"}
    )


@pytest.mark.asyncio
async def test_stays_heating_above_stop_threshold():
    """SoC above stop threshold — stays HEATING."""
    ctrl, hass = _make_controller()

    # Get into HEATING state
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=600)
    assert ctrl._state == HeaterState.HEATING
    hass.services.async_call.reset_mock()

    # SoC still above threshold
    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(soc=91.0, export_w=0)

    assert ctrl._state == HeaterState.HEATING
    hass.services.async_call.assert_not_called()


# ------------------------------------------------------------------
# Energy accumulation
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_energy_accumulation():
    """Energy accumulates during HEATING based on power sensor."""
    ctrl, hass = _make_controller(power_value=2000.0)

    # Get into HEATING state
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=600)
    assert ctrl._state == HeaterState.HEATING

    # 1 hour later at 2000W → 2 kWh
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS + 3600):
        await ctrl.evaluate(soc=92.0, export_w=0)

    assert abs(ctrl.accumulated_kwh - 2.0) < 0.01


@pytest.mark.asyncio
async def test_energy_accumulation_unavailable_sensor():
    """Unavailable power sensor doesn't crash, just skips accumulation."""
    ctrl, hass = _make_controller()
    hass.states.get.return_value = None  # Sensor unavailable

    # Get into HEATING state
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=600)
    assert ctrl._state == HeaterState.HEATING

    # Evaluate with unavailable sensor — should not crash
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS + 3600):
        await ctrl.evaluate(soc=92.0, export_w=0)

    assert ctrl.accumulated_kwh == 0.0


@pytest.mark.asyncio
async def test_energy_accumulation_non_numeric_sensor():
    """Non-numeric sensor state handled gracefully."""
    ctrl, hass = _make_controller()
    state_obj = MagicMock()
    state_obj.state = "unavailable"
    hass.states.get.return_value = state_obj

    # Get into HEATING state
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=600)
    assert ctrl._state == HeaterState.HEATING

    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS + 3600):
        await ctrl.evaluate(soc=92.0, export_w=0)

    assert ctrl.accumulated_kwh == 0.0


# ------------------------------------------------------------------
# reset_daily
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_daily_clears_energy():
    """reset_daily() zeroes accumulated energy."""
    ctrl, hass = _make_controller(power_value=2000.0)

    # Get into HEATING and accumulate some energy
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS + 3600):
        await ctrl.evaluate(soc=92.0, export_w=0)

    assert ctrl.accumulated_kwh > 0

    ctrl.reset_daily()
    assert ctrl.accumulated_kwh == 0.0


# ------------------------------------------------------------------
# reconfigure
# ------------------------------------------------------------------


def test_reconfigure_updates_entity_ids():
    """reconfigure() updates switch and power entity IDs."""
    ctrl, _ = _make_controller()

    ctrl.reconfigure("switch.new_heater", "sensor.new_power")

    assert ctrl._switch_entity_id == "switch.new_heater"
    assert ctrl._power_sensor_entity_id == "sensor.new_power"


# ------------------------------------------------------------------
# Edge: SoC exactly at start threshold — should NOT trigger
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soc_exactly_at_start_threshold_no_trigger():
    """SoC == 95% (not >) should not trigger heating."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=SOC_START_THRESHOLD, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=SOC_START_THRESHOLD, export_w=600)

    assert ctrl._state == HeaterState.IDLE


# ------------------------------------------------------------------
# Edge: SoC below stop threshold
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stops_below_stop_threshold():
    """SoC below stop threshold also stops."""
    ctrl, hass = _make_controller()

    # Get into HEATING
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=600)
    assert ctrl._state == HeaterState.HEATING
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(soc=85.0, export_w=0)

    assert ctrl._state == HeaterState.IDLE
