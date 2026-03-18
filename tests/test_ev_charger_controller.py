"""Tests for EvChargerController."""

import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch

from custom_components.beem_ai.ev_charger_controller import (
    EXPORT_MIN_W,
    ChargerState,
    MAX_CHARGE_AMPS,
    MIN_CHARGE_AMPS,
    SOC_START_THRESHOLD,
    SOC_STOP_THRESHOLD,
    SUSTAIN_SECONDS,
    WATTS_PER_AMP,
    EvChargerController,
)


def _make_controller():
    """Create a controller with mocked hass."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    ctrl = EvChargerController(
        hass=hass,
        toggle_entity_id="switch.ev_charger",
        power_entity_id="number.ev_charger_amps",
    )
    return ctrl, hass


async def _eval(ctrl, soc=96.0, export_w=0.0, solar_power_w=4000.0,
                consumption_w=1000.0, water_heater_heating=True):
    """Helper with sensible defaults."""
    await ctrl.evaluate(soc, export_w, solar_power_w, consumption_w,
                        water_heater_heating)


async def _start_charging(ctrl, hass, export_w=600, solar_power_w=4000.0,
                           consumption_w=1000.0):
    """Helper: get controller into CHARGING state."""
    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, export_w=export_w, solar_power_w=solar_power_w,
                    consumption_w=consumption_w)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, export_w=export_w, solar_power_w=solar_power_w,
                    consumption_w=consumption_w)
    assert ctrl._state == ChargerState.CHARGING
    hass.services.async_call.reset_mock()


# ------------------------------------------------------------------
# Initial state
# ------------------------------------------------------------------


def test_initial_state():
    ctrl, _ = _make_controller()
    assert ctrl._state == ChargerState.IDLE
    assert ctrl.is_charging is False
    assert ctrl.current_amps == MIN_CHARGE_AMPS


# ------------------------------------------------------------------
# IDLE → CHARGING transitions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_transition_when_water_heater_not_heating():
    ctrl, hass = _make_controller()
    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, export_w=600, water_heater_heating=False)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, export_w=600, water_heater_heating=False)
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_transition_below_soc_threshold():
    ctrl, hass = _make_controller()
    await _eval(ctrl, soc=90.0, export_w=600)
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_transition_below_export_threshold():
    ctrl, hass = _make_controller()
    await _eval(ctrl, export_w=400)
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_transition_before_sustain_period():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, export_w=600)
    assert ctrl._state == ChargerState.IDLE

    with patch("time.monotonic", return_value=1010.0):
        await _eval(ctrl, export_w=600)
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_start_amps_from_surplus():
    """Start amps computed from solar surplus: floor(3000/230)+1 = 14A."""
    ctrl, hass = _make_controller()

    # solar=4000, consumption=1000 → surplus=3000 → floor(3000/230)+1 = 14A
    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, export_w=600, solar_power_w=4000, consumption_w=1000)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, export_w=600, solar_power_w=4000, consumption_w=1000)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.current_amps == 14  # floor(3000/230) + 1

    calls = hass.services.async_call.call_args_list
    assert calls[0] == call(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": 14},
    )
    assert calls[1] == call(
        "homeassistant", "turn_on", {"entity_id": "switch.ev_charger"},
    )


@pytest.mark.asyncio
async def test_start_amps_clamped_to_min():
    """Small surplus → clamped to MIN_CHARGE_AMPS."""
    ctrl, hass = _make_controller()

    # solar=1500, consumption=1000 → surplus=500 → floor(500/230)+1 = 3 → clamped to 6
    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, export_w=600, solar_power_w=1500, consumption_w=1000)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, export_w=600, solar_power_w=1500, consumption_w=1000)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.current_amps == MIN_CHARGE_AMPS


@pytest.mark.asyncio
async def test_start_amps_clamped_to_max():
    """Huge surplus → clamped to MAX_CHARGE_AMPS."""
    ctrl, hass = _make_controller()

    # solar=10000, consumption=500 → surplus=9500 → floor(9500/230)+1 = 42 → clamped to 32
    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, export_w=600, solar_power_w=10000, consumption_w=500)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, export_w=600, solar_power_w=10000, consumption_w=500)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.current_amps == MAX_CHARGE_AMPS


@pytest.mark.asyncio
async def test_sustain_timer_resets_when_export_drops():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, export_w=600)
    assert ctrl._export_sustained_since is not None

    with patch("time.monotonic", return_value=1015.0):
        await _eval(ctrl, export_w=200)
    assert ctrl._export_sustained_since is None

    with patch("time.monotonic", return_value=1020.0):
        await _eval(ctrl, export_w=600)
    with patch("time.monotonic", return_value=1040.0):
        await _eval(ctrl, export_w=600)
    assert ctrl._state == ChargerState.IDLE

    with patch("time.monotonic", return_value=1020.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, export_w=600)
    assert ctrl._state == ChargerState.CHARGING


@pytest.mark.asyncio
async def test_sustain_timer_resets_when_water_heater_stops():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, export_w=600)
    assert ctrl._export_sustained_since is not None

    with patch("time.monotonic", return_value=1015.0):
        await _eval(ctrl, export_w=600, water_heater_heating=False)
    assert ctrl._export_sustained_since is None


# ------------------------------------------------------------------
# CHARGING: surplus-based amp regulation
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regulate_amps_users_example():
    """User's example: solar=4000, home=1000 → 14A.

    While charging at 14A, consumption_w includes EV: 1000 + 14*230 = 4220.
    surplus = 4000 - 4220 + 14*230 = 3000 → floor(3000/230)+1 = 14A (no change).
    """
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)

    assert ctrl.current_amps == 14  # floor(3000/230)+1

    # Simulate next cycle: consumption_w now includes EV draw
    ev_draw = ctrl.current_amps * WATTS_PER_AMP  # 14 * 230 = 3220
    consumption_with_ev = 1000 + ev_draw  # 4220

    await _eval(ctrl, solar_power_w=4000, consumption_w=consumption_with_ev)

    # Should stay at 14A: surplus = 4000 - 4220 + 3220 = 3000 → 14A
    assert ctrl.current_amps == 14
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_regulate_amps_increase_when_solar_increases():
    """Solar increases → amps increase."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)
    assert ctrl.current_amps == 14

    # Solar jumps to 5000W, consumption_w includes EV at 14A
    ev_draw = 14 * WATTS_PER_AMP  # 3220
    await _eval(ctrl, solar_power_w=5000, consumption_w=1000 + ev_draw)

    # surplus = 5000 - 4220 + 3220 = 4000 → floor(4000/230)+1 = 18A
    assert ctrl.current_amps == 18
    hass.services.async_call.assert_called_once_with(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": 18},
    )


@pytest.mark.asyncio
async def test_regulate_amps_decrease_when_consumption_increases():
    """Home consumption increases → amps decrease."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)
    assert ctrl.current_amps == 14

    # Home consumption jumps to 2000W, consumption_w includes EV at 14A
    ev_draw = 14 * WATTS_PER_AMP
    await _eval(ctrl, solar_power_w=4000, consumption_w=2000 + ev_draw)

    # surplus = 4000 - 5220 + 3220 = 2000 → floor(2000/230)+1 = 9A
    assert ctrl.current_amps == 9


@pytest.mark.asyncio
async def test_regulate_amps_clamped_at_min():
    """Very low surplus → clamped to MIN_CHARGE_AMPS."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)

    # Solar drops drastically, consumption_w includes EV at 14A
    ev_draw = 14 * WATTS_PER_AMP
    await _eval(ctrl, solar_power_w=1000, consumption_w=500 + ev_draw)

    # surplus = 1000 - 3720 + 3220 = 500 → floor(500/230)+1 = 3 → clamped to 6
    assert ctrl.current_amps == MIN_CHARGE_AMPS
    assert ctrl._state == ChargerState.CHARGING


@pytest.mark.asyncio
async def test_regulate_amps_clamped_at_max():
    """Huge surplus → clamped to MAX_CHARGE_AMPS."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)

    ev_draw = 14 * WATTS_PER_AMP
    await _eval(ctrl, solar_power_w=10000, consumption_w=500 + ev_draw)

    # surplus = 10000 - 3720 + 3220 = 9500 → 42 → clamped to 32
    assert ctrl.current_amps == MAX_CHARGE_AMPS


@pytest.mark.asyncio
async def test_no_service_call_when_amps_unchanged():
    """No service call if target amps match current."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)

    # Same conditions → same amps
    ev_draw = 14 * WATTS_PER_AMP
    await _eval(ctrl, solar_power_w=4000, consumption_w=1000 + ev_draw)

    assert ctrl.current_amps == 14
    hass.services.async_call.assert_not_called()


# ------------------------------------------------------------------
# CHARGING continues when water heater stops
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continues_charging_when_water_heater_stops():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await _eval(ctrl, soc=92.0, export_w=500, water_heater_heating=False)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.is_charging is True


@pytest.mark.asyncio
async def test_regulates_amps_when_water_heater_stops():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)

    ev_draw = 14 * WATTS_PER_AMP
    # Water heater stopped → home consumption dropped, more surplus for EV
    await _eval(ctrl, soc=92.0, solar_power_w=4000,
                consumption_w=500 + ev_draw, water_heater_heating=False)

    # surplus = 4000 - 3720 + 3220 = 3500 → floor(3500/230)+1 = 16A
    assert ctrl.current_amps == 16


# ------------------------------------------------------------------
# CHARGING → IDLE transitions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stays_charging_at_exact_threshold():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await _eval(ctrl, soc=SOC_STOP_THRESHOLD)
    assert ctrl._state == ChargerState.CHARGING


@pytest.mark.asyncio
async def test_stops_when_soc_drops_below_threshold():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1)

    assert ctrl._state == ChargerState.IDLE
    assert ctrl.is_charging is False
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_off", {"entity_id": "switch.ev_charger"}
    )


@pytest.mark.asyncio
async def test_stays_charging_above_stop_threshold():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await _eval(ctrl, soc=91.0)
    assert ctrl._state == ChargerState.CHARGING


# ------------------------------------------------------------------
# reconfigure / edge cases
# ------------------------------------------------------------------


def test_reconfigure_updates_entity_ids():
    ctrl, _ = _make_controller()
    ctrl.reconfigure("switch.new_charger", "number.new_amps")
    assert ctrl._toggle_entity_id == "switch.new_charger"
    assert ctrl._power_entity_id == "number.new_amps"


@pytest.mark.asyncio
async def test_soc_exactly_at_start_threshold_no_trigger():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, soc=SOC_START_THRESHOLD, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, soc=SOC_START_THRESHOLD, export_w=600)

    assert ctrl._state == ChargerState.IDLE


@pytest.mark.asyncio
async def test_stops_below_stop_threshold():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await _eval(ctrl, soc=85.0)
    assert ctrl._state == ChargerState.IDLE
