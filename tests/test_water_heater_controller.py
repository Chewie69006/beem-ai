"""Tests for WaterHeaterController."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.beem_ai.water_heater_controller import (
    EXPORT_SOC_THRESHOLD,
    HYSTERESIS_PCT,
    HeaterState,
    SUSTAIN_SECONDS,
    WaterHeaterController,
)

# Default configurable thresholds (rule 2)
SOC_THRESHOLD = 80.0
CHARGE_POWER_THRESHOLD = 500.0


def _make_controller(power_value=2000.0):
    """Create a controller with mocked hass."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    state_obj = MagicMock()
    state_obj.state = str(power_value)
    hass.states.get = MagicMock(return_value=state_obj)

    ctrl = WaterHeaterController(
        hass=hass,
        switch_entity_id="switch.water_heater",
        power_sensor_entity_id="sensor.water_heater_power",
    )
    return ctrl, hass


async def _evaluate(ctrl, soc, export_w=0.0, charge_power_w=0.0,
                    soc_threshold=SOC_THRESHOLD,
                    charge_power_threshold=CHARGE_POWER_THRESHOLD):
    """Helper to call evaluate with default thresholds."""
    await ctrl.evaluate(soc, export_w, charge_power_w, soc_threshold,
                        charge_power_threshold)


async def _heat_via_export(ctrl, hass, t0=1000.0):
    """Drive into HEATING via rule 1 (export) and reset mock."""
    with patch("time.monotonic", return_value=t0):
        await _evaluate(ctrl, soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=t0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=96.0, export_w=600)
    assert ctrl._state == HeaterState.HEATING
    hass.services.async_call.reset_mock()
    return t0 + SUSTAIN_SECONDS


async def _heat_via_charge(ctrl, hass, t0=1000.0):
    """Drive into HEATING via rule 2 (charge power) and reset mock."""
    with patch("time.monotonic", return_value=t0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)
    with patch("time.monotonic", return_value=t0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)
    assert ctrl._state == HeaterState.HEATING
    hass.services.async_call.reset_mock()
    return t0 + SUSTAIN_SECONDS


# ==================================================================
# Initial state
# ==================================================================


def test_initial_state():
    ctrl, _ = _make_controller()
    assert ctrl._state == HeaterState.IDLE
    assert ctrl.is_heating is False
    assert ctrl.accumulated_kwh == 0.0


# ==================================================================
# Rule 1: hardcoded — SoC > 95% AND exporting
# ==================================================================


@pytest.mark.asyncio
async def test_rule1_triggers_when_exporting_above_95():
    """Rule 1: SoC > 95% + exporting → heats after sustain."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=96.0, export_w=600)

    assert ctrl._state == HeaterState.HEATING
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_on", {"entity_id": "switch.water_heater"}
    )


@pytest.mark.asyncio
async def test_rule1_any_positive_export():
    """Rule 1: even 1W export triggers it."""
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=96.0, export_w=1.0)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=96.0, export_w=1.0)

    assert ctrl._state == HeaterState.HEATING


@pytest.mark.asyncio
async def test_rule1_soc_too_low():
    """Rule 1: SoC <= 95% — doesn't trigger (even if exporting)."""
    ctrl, hass = _make_controller()
    await _evaluate(ctrl, soc=95.0, export_w=600)
    assert ctrl._state == HeaterState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_rule1_not_exporting():
    """Rule 1: not exporting — doesn't trigger (even with SoC > 95%)."""
    ctrl, hass = _make_controller()
    # No export, no charge power above threshold either
    await _evaluate(ctrl, soc=96.0, export_w=0, charge_power_w=0)
    assert ctrl._state == HeaterState.IDLE


@pytest.mark.asyncio
async def test_rule1_sustain_resets_when_export_stops():
    """Rule 1: export stops mid-sustain → timer resets."""
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=96.0, export_w=600)
    assert ctrl._sustained_since is not None

    with patch("time.monotonic", return_value=1015.0):
        await _evaluate(ctrl, soc=96.0, export_w=0, charge_power_w=0)
    assert ctrl._sustained_since is None


@pytest.mark.asyncio
async def test_rule1_before_sustain():
    """Rule 1: conditions met but not sustained — stays IDLE."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=1010.0):
        await _evaluate(ctrl, soc=96.0, export_w=600)

    assert ctrl._state == HeaterState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_rule1_stop_hysteresis():
    """Rule 1: stops at SoC < 90% (95% - 5% hysteresis)."""
    ctrl, hass = _make_controller()
    t = await _heat_via_export(ctrl, hass)

    # At 90% — stays heating
    with patch("time.monotonic", return_value=t + 60):
        await _evaluate(ctrl, soc=EXPORT_SOC_THRESHOLD - HYSTERESIS_PCT)
    assert ctrl._state == HeaterState.HEATING

    # Below 90% — stops
    with patch("time.monotonic", return_value=t + 120):
        await _evaluate(ctrl, soc=EXPORT_SOC_THRESHOLD - HYSTERESIS_PCT - 1)
    assert ctrl._state == HeaterState.IDLE
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_off", {"entity_id": "switch.water_heater"}
    )


# ==================================================================
# Rule 2: configurable — SoC > threshold AND charge power >= threshold
# ==================================================================


@pytest.mark.asyncio
async def test_rule2_triggers_on_charge_power():
    """Rule 2: SoC > 80% + charge power >= 500W → heats after sustain."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)

    assert ctrl._state == HeaterState.HEATING
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_on", {"entity_id": "switch.water_heater"}
    )


@pytest.mark.asyncio
async def test_rule2_soc_too_low():
    """Rule 2: SoC <= configurable threshold — doesn't trigger."""
    ctrl, hass = _make_controller()
    await _evaluate(ctrl, soc=80.0, export_w=0, charge_power_w=600)
    assert ctrl._state == HeaterState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_rule2_charge_power_too_low():
    """Rule 2: charge power below threshold — doesn't trigger."""
    ctrl, hass = _make_controller()
    await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=400)
    assert ctrl._state == HeaterState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_rule2_sustain_resets_when_power_drops():
    """Rule 2: charge power drops mid-sustain → timer resets."""
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)
    assert ctrl._sustained_since is not None

    with patch("time.monotonic", return_value=1015.0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=200)
    assert ctrl._sustained_since is None


@pytest.mark.asyncio
async def test_rule2_stop_hysteresis():
    """Rule 2: stops at SoC < 75% (80% - 5% hysteresis)."""
    ctrl, hass = _make_controller()
    t = await _heat_via_charge(ctrl, hass)

    stop = SOC_THRESHOLD - HYSTERESIS_PCT  # 75%

    # At 75% — stays heating
    with patch("time.monotonic", return_value=t + 60):
        await _evaluate(ctrl, soc=stop)
    assert ctrl._state == HeaterState.HEATING

    # Below 75% — stops
    with patch("time.monotonic", return_value=t + 120):
        await _evaluate(ctrl, soc=stop - 1)
    assert ctrl._state == HeaterState.IDLE
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_off", {"entity_id": "switch.water_heater"}
    )


# ==================================================================
# Both rules: rule 2 triggers at lower SoC than rule 1
# ==================================================================


@pytest.mark.asyncio
async def test_rule2_fires_below_95_when_charging():
    """Rule 2 can fire at SoC=81% (below rule 1's 95%) when charging hard."""
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)

    assert ctrl._state == HeaterState.HEATING
    assert ctrl._active_soc_threshold == SOC_THRESHOLD  # 80%


@pytest.mark.asyncio
async def test_both_rules_active_uses_lower_threshold():
    """When both rules match, active SoC threshold is the lower one."""
    ctrl, _ = _make_controller()

    # SoC > 95%, exporting AND charging above threshold → both rules match
    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=96.0, export_w=600, charge_power_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=96.0, export_w=600, charge_power_w=600)

    assert ctrl._state == HeaterState.HEATING
    # SOC_THRESHOLD (80) < EXPORT_SOC_THRESHOLD (95), so min = 80
    assert ctrl._active_soc_threshold == SOC_THRESHOLD


# ==================================================================
# Energy accumulation
# ==================================================================


@pytest.mark.asyncio
async def test_energy_accumulation():
    ctrl, hass = _make_controller(power_value=2000.0)
    t = await _heat_via_export(ctrl, hass)

    with patch("time.monotonic", return_value=t + 3600):
        await _evaluate(ctrl, soc=92.0)

    assert abs(ctrl.accumulated_kwh - 2.0) < 0.01


@pytest.mark.asyncio
async def test_energy_accumulation_unavailable_sensor():
    ctrl, hass = _make_controller()
    hass.states.get.return_value = None

    t = await _heat_via_export(ctrl, hass)

    with patch("time.monotonic", return_value=t + 3600):
        await _evaluate(ctrl, soc=92.0)

    assert ctrl.accumulated_kwh == 0.0


@pytest.mark.asyncio
async def test_energy_accumulation_non_numeric_sensor():
    ctrl, hass = _make_controller()
    state_obj = MagicMock()
    state_obj.state = "unavailable"
    hass.states.get.return_value = state_obj

    t = await _heat_via_export(ctrl, hass)

    with patch("time.monotonic", return_value=t + 3600):
        await _evaluate(ctrl, soc=92.0)

    assert ctrl.accumulated_kwh == 0.0


# ==================================================================
# reset_daily / reconfigure
# ==================================================================


@pytest.mark.asyncio
async def test_reset_daily_clears_energy():
    ctrl, hass = _make_controller(power_value=2000.0)
    t = await _heat_via_export(ctrl, hass)

    with patch("time.monotonic", return_value=t + 3600):
        await _evaluate(ctrl, soc=92.0)

    assert ctrl.accumulated_kwh > 0
    ctrl.reset_daily()
    assert ctrl.accumulated_kwh == 0.0


def test_reconfigure_updates_entity_ids():
    ctrl, _ = _make_controller()
    ctrl.reconfigure("switch.new_heater", "sensor.new_power")
    assert ctrl._switch_entity_id == "switch.new_heater"
    assert ctrl._power_sensor_entity_id == "sensor.new_power"
