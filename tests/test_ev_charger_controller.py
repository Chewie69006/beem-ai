"""Tests for EvChargerController."""

import time

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


async def _start_charging(ctrl, hass, export_w=600):
    """Helper: get controller into CHARGING state."""
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=export_w, water_heater_heating=True)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=export_w, water_heater_heating=True)
    assert ctrl._state == ChargerState.CHARGING
    hass.services.async_call.reset_mock()


# ------------------------------------------------------------------
# State: initial
# ------------------------------------------------------------------


def test_initial_state():
    """Controller starts in IDLE."""
    ctrl, _ = _make_controller()
    assert ctrl._state == ChargerState.IDLE
    assert ctrl.is_charging is False
    assert ctrl.current_amps == MIN_CHARGE_AMPS


# ------------------------------------------------------------------
# IDLE → CHARGING transitions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_transition_when_water_heater_not_heating():
    """SoC and export OK but water heater not heating — stays IDLE."""
    ctrl, hass = _make_controller()
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600, water_heater_heating=False)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=600, water_heater_heating=False)
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_transition_below_soc_threshold():
    """Water heater ON, export OK, but SoC too low — stays IDLE."""
    ctrl, hass = _make_controller()
    await ctrl.evaluate(soc=90.0, export_w=600, water_heater_heating=True)
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_transition_below_export_threshold():
    """Water heater ON, SoC OK, but export too low — stays IDLE."""
    ctrl, hass = _make_controller()
    await ctrl.evaluate(soc=96.0, export_w=400, water_heater_heating=True)
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_transition_before_sustain_period():
    """All conditions met but not sustained long enough — stays IDLE."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600, water_heater_heating=True)
    assert ctrl._state == ChargerState.IDLE

    # Only 10 seconds later — not enough
    with patch("time.monotonic", return_value=1010.0):
        await ctrl.evaluate(soc=96.0, export_w=600, water_heater_heating=True)
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_transition_starts_at_calculated_amps():
    """Starts at best amps calculated from export surplus."""
    ctrl, hass = _make_controller()

    # 600W export → int(600/230) = 2 → clamped to MIN_CHARGE_AMPS (6)
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600, water_heater_heating=True)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=600, water_heater_heating=True)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.current_amps == MIN_CHARGE_AMPS  # int(600/230)=2 → clamped to 6

    calls = hass.services.async_call.call_args_list
    assert len(calls) == 2
    assert calls[0] == call(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": MIN_CHARGE_AMPS},
    )
    assert calls[1] == call(
        "homeassistant", "turn_on", {"entity_id": "switch.ev_charger"},
    )


@pytest.mark.asyncio
async def test_transition_starts_at_high_amps_with_big_surplus():
    """Large export surplus → starts at higher amps directly."""
    ctrl, hass = _make_controller()

    # 2760W export → int(2760/230) = 12A
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=2760, water_heater_heating=True)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=2760, water_heater_heating=True)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.current_amps == 12

    calls = hass.services.async_call.call_args_list
    assert calls[0] == call(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": 12},
    )


@pytest.mark.asyncio
async def test_sustain_timer_resets_when_export_drops():
    """Export drops during sustain period — timer resets."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600, water_heater_heating=True)
    assert ctrl._export_sustained_since is not None

    # Export drops
    with patch("time.monotonic", return_value=1015.0):
        await ctrl.evaluate(soc=96.0, export_w=200, water_heater_heating=True)
    assert ctrl._export_sustained_since is None

    # Restart sustain
    with patch("time.monotonic", return_value=1020.0):
        await ctrl.evaluate(soc=96.0, export_w=600, water_heater_heating=True)

    # Not enough time from new start
    with patch("time.monotonic", return_value=1040.0):
        await ctrl.evaluate(soc=96.0, export_w=600, water_heater_heating=True)
    assert ctrl._state == ChargerState.IDLE

    # Now enough time
    with patch("time.monotonic", return_value=1020.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=600, water_heater_heating=True)
    assert ctrl._state == ChargerState.CHARGING


@pytest.mark.asyncio
async def test_sustain_timer_resets_when_water_heater_stops():
    """Water heater turns off during sustain period — timer resets."""
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=600, water_heater_heating=True)
    assert ctrl._export_sustained_since is not None

    # Water heater stops
    with patch("time.monotonic", return_value=1015.0):
        await ctrl.evaluate(soc=96.0, export_w=600, water_heater_heating=False)
    assert ctrl._export_sustained_since is None


# ------------------------------------------------------------------
# CHARGING: amp regulation
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amps_increase_with_surplus():
    """Exporting surplus → amps ramp up."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    assert ctrl.current_amps == MIN_CHARGE_AMPS  # 6A

    # Exporting 920W surplus → delta = int(920/230) = 4A → target = 10A
    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(soc=96.0, export_w=920, water_heater_heating=True)

    assert ctrl.current_amps == 10
    hass.services.async_call.assert_called_once_with(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": 10},
    )


@pytest.mark.asyncio
async def test_amps_decrease_on_import():
    """Importing from grid → amps decrease."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    # First ramp up to 15A
    ctrl._current_amps = 15

    # Importing 460W → delta = int(-460/230) = -2A → target = 13A
    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(soc=96.0, export_w=-460, water_heater_heating=True)

    assert ctrl.current_amps == 13


@pytest.mark.asyncio
async def test_amps_clamped_at_max():
    """Amps never exceed MAX_CHARGE_AMPS."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    ctrl._current_amps = 30

    # Huge surplus → would want 30 + 10 = 40, but clamped to 32
    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(
            soc=96.0, export_w=2300, water_heater_heating=True
        )

    assert ctrl.current_amps == MAX_CHARGE_AMPS


@pytest.mark.asyncio
async def test_amps_clamped_at_min():
    """Amps never go below MIN_CHARGE_AMPS (stays at min, doesn't stop)."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    assert ctrl.current_amps == MIN_CHARGE_AMPS  # 6A

    # Importing 1000W → delta = -4A → target = 2A → clamped to 6A (no change)
    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(
            soc=96.0, export_w=-1000, water_heater_heating=True
        )

    assert ctrl.current_amps == MIN_CHARGE_AMPS
    assert ctrl._state == ChargerState.CHARGING  # Still charging
    hass.services.async_call.assert_not_called()  # No change → no call


@pytest.mark.asyncio
async def test_no_service_call_when_amps_unchanged():
    """No service call if calculated amps match current."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    # Small surplus: delta = int(100/230) = 0A → no change
    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(soc=96.0, export_w=100, water_heater_heating=True)

    assert ctrl.current_amps == MIN_CHARGE_AMPS
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_ramp_up_then_stabilize():
    """Amps ramp up across cycles then stabilize when surplus is consumed."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    # Cycle 1: big surplus → ramp up
    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(
            soc=96.0, export_w=1380, water_heater_heating=True
        )
    assert ctrl.current_amps == 12  # 6 + int(1380/230) = 6 + 6 = 12

    hass.services.async_call.reset_mock()

    # Cycle 2: now the EV draws more, surplus smaller
    with patch("time.monotonic", return_value=1110.0):
        await ctrl.evaluate(soc=96.0, export_w=230, water_heater_heating=True)
    assert ctrl.current_amps == 13  # 12 + 1

    hass.services.async_call.reset_mock()

    # Cycle 3: balanced — tiny surplus, no change
    with patch("time.monotonic", return_value=1120.0):
        await ctrl.evaluate(soc=96.0, export_w=50, water_heater_heating=True)
    assert ctrl.current_amps == 13  # int(50/230) = 0
    hass.services.async_call.assert_not_called()


# ------------------------------------------------------------------
# CHARGING continues when water heater stops
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continues_charging_when_water_heater_stops():
    """Water heater stops while charging → EV keeps charging."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    # Water heater stops (SoC still fine)
    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(soc=92.0, export_w=500, water_heater_heating=False)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.is_charging is True


@pytest.mark.asyncio
async def test_regulates_amps_when_water_heater_stops():
    """Water heater off → EV still regulates amps normally."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    ctrl._current_amps = 10

    # Water heater stopped but big surplus → increase amps
    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(soc=92.0, export_w=690, water_heater_heating=False)

    assert ctrl.current_amps == 13  # 10 + int(690/230) = 10 + 3


# ------------------------------------------------------------------
# CHARGING → IDLE transitions
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stays_charging_at_exact_threshold():
    """SoC exactly at stop threshold (90%) — stays CHARGING (< required)."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(
            soc=SOC_STOP_THRESHOLD, export_w=0, water_heater_heating=True
        )

    assert ctrl._state == ChargerState.CHARGING
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_stops_when_soc_drops_below_threshold():
    """SoC drops below stop threshold → stops charging."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(
            soc=SOC_STOP_THRESHOLD - 1, export_w=0, water_heater_heating=True
        )

    assert ctrl._state == ChargerState.IDLE
    assert ctrl.is_charging is False
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_off", {"entity_id": "switch.ev_charger"}
    )


@pytest.mark.asyncio
async def test_stays_charging_above_stop_threshold():
    """SoC above stop threshold — stays CHARGING."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(soc=91.0, export_w=0, water_heater_heating=True)

    assert ctrl._state == ChargerState.CHARGING
    hass.services.async_call.assert_not_called()


# ------------------------------------------------------------------
# reconfigure
# ------------------------------------------------------------------


def test_reconfigure_updates_entity_ids():
    """reconfigure() updates toggle and power entity IDs."""
    ctrl, _ = _make_controller()

    ctrl.reconfigure("switch.new_charger", "number.new_amps")

    assert ctrl._toggle_entity_id == "switch.new_charger"
    assert ctrl._power_entity_id == "number.new_amps"


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soc_exactly_at_start_threshold_no_trigger():
    """SoC == 95% (not >) should not trigger charging."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(
            soc=SOC_START_THRESHOLD, export_w=600, water_heater_heating=True
        )
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(
            soc=SOC_START_THRESHOLD, export_w=600, water_heater_heating=True
        )

    assert ctrl._state == ChargerState.IDLE


@pytest.mark.asyncio
async def test_stops_below_stop_threshold():
    """SoC below stop threshold also stops."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    with patch("time.monotonic", return_value=1100.0):
        await ctrl.evaluate(soc=85.0, export_w=0, water_heater_heating=True)

    assert ctrl._state == ChargerState.IDLE


@pytest.mark.asyncio
async def test_start_amps_clamped_to_max():
    """Huge export at start → starting amps clamped to MAX_CHARGE_AMPS."""
    ctrl, hass = _make_controller()

    # 10000W → int(10000/230) = 43 → clamped to 32
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.evaluate(soc=96.0, export_w=10000, water_heater_heating=True)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await ctrl.evaluate(soc=96.0, export_w=10000, water_heater_heating=True)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.current_amps == MAX_CHARGE_AMPS
