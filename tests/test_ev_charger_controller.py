"""Tests for EvChargerController."""

import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch

from custom_components.beem_ai.ev_charger_controller import (
    ChargerState,
    MAX_CHARGE_AMPS,
    MAX_CONSUMPTION_W,
    MIN_CHARGE_AMPS,
    REGULATE_DELTA_W,
    REGULATE_INTERVAL_S,
    SUSTAIN_SECONDS,
    StartMode,
    WATTS_PER_AMP,
    EvChargerController,
)

# Thresholds are now user-configurable (per-call).  Tests use fixed values
# matching the original module-level defaults so existing assertions hold.
SOC_START_THRESHOLD = 95.0
SOC_STOP_THRESHOLD = 90.0


def _make_controller(user_amps=32):
    """Create a controller with mocked hass."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    amps_state = MagicMock()
    amps_state.state = str(user_amps)
    hass.states.get = MagicMock(return_value=amps_state)

    ctrl = EvChargerController(
        hass=hass,
        toggle_entity_id="switch.ev_charger",
        power_entity_id="number.ev_charger_amps",
    )
    return ctrl, hass


async def _eval(ctrl, soc=96.0, export_w=0.0, solar_power_w=4000.0,
                consumption_w=1000.0, water_heater_heating=True,
                start_soc_threshold=SOC_START_THRESHOLD,
                stop_soc_threshold=SOC_STOP_THRESHOLD):
    """Helper with sensible defaults."""
    await ctrl.evaluate(soc, export_w, solar_power_w, consumption_w,
                        water_heater_heating,
                        start_soc_threshold=start_soc_threshold,
                        stop_soc_threshold=stop_soc_threshold)


async def _start_charging(ctrl, hass, export_w=600, solar_power_w=4000.0,
                           consumption_w=1000.0, t0=1000.0):
    """Helper: get controller into CHARGING state."""
    with patch("time.monotonic", return_value=t0):
        await _eval(ctrl, export_w=export_w, solar_power_w=solar_power_w,
                    consumption_w=consumption_w)
    with patch("time.monotonic", return_value=t0 + SUSTAIN_SECONDS):
        await _eval(ctrl, export_w=export_w, solar_power_w=solar_power_w,
                    consumption_w=consumption_w)
    assert ctrl._state == ChargerState.CHARGING
    hass.services.async_call.reset_mock()
    return t0 + SUSTAIN_SECONDS


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
async def test_no_transition_below_surplus_threshold():
    """Start uses solar-surplus (solar - consumption), not grid export."""
    ctrl, hass = _make_controller()
    # solar 900W, consumption 500W → surplus 400W < EXPORT_MIN_W (500)
    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, solar_power_w=900, consumption_w=500)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, solar_power_w=900, consumption_w=500)
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_starts_at_lower_soc_when_battery_absorbing_all_solar():
    """Regression: user sets start=75%.  At 75% SoC battery absorbs all
    solar → grid export_w = 0.  Start must still trigger from the solar
    surplus (solar − consumption), not grid export.
    """
    ctrl, hass = _make_controller()
    # export_w = 0 (battery absorbing everything), but plenty of solar
    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, soc=76.0, export_w=0.0,
                    solar_power_w=4000, consumption_w=800,
                    start_soc_threshold=75.0)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, soc=76.0, export_w=0.0,
                    solar_power_w=4000, consumption_w=800,
                    start_soc_threshold=75.0)
    assert ctrl._state == ChargerState.CHARGING


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
async def test_always_starts_at_min_amps():
    """Always starts at MIN_CHARGE_AMPS (6A) regardless of surplus."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, export_w=600, solar_power_w=4000, consumption_w=1000)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, export_w=600, solar_power_w=4000, consumption_w=1000)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl._start_mode == StartMode.AUTO
    assert ctrl.current_amps == MIN_CHARGE_AMPS

    calls = hass.services.async_call.call_args_list
    assert calls[0] == call(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": MIN_CHARGE_AMPS},
    )
    assert calls[1] == call(
        "homeassistant", "turn_on", {"entity_id": "switch.ev_charger"},
    )


@pytest.mark.asyncio
async def test_start_amps_clamped_to_min():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, export_w=600, solar_power_w=1500, consumption_w=1000)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, export_w=600, solar_power_w=1500, consumption_w=1000)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.current_amps == MIN_CHARGE_AMPS


@pytest.mark.asyncio
async def test_start_amps_always_min_even_with_high_surplus():
    """Even with huge surplus, always starts at MIN_CHARGE_AMPS."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, export_w=600, solar_power_w=10000, consumption_w=500)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, export_w=600, solar_power_w=10000, consumption_w=500)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.current_amps == MIN_CHARGE_AMPS


@pytest.mark.asyncio
async def test_sustain_timer_resets_when_surplus_drops():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl)  # default surplus = 3000W
    assert ctrl._export_sustained_since is not None

    # Surplus drops to 200W (solar 1000 - consumption 800)
    with patch("time.monotonic", return_value=1015.0):
        await _eval(ctrl, solar_power_w=1000, consumption_w=800)
    assert ctrl._export_sustained_since is None

    with patch("time.monotonic", return_value=1020.0):
        await _eval(ctrl)
    with patch("time.monotonic", return_value=1040.0):
        await _eval(ctrl)
    assert ctrl._state == ChargerState.IDLE

    with patch("time.monotonic", return_value=1020.0 + SUSTAIN_SECONDS):
        await _eval(ctrl)
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
# CHARGING: surplus-based amp regulation (with throttle)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regulate_ramps_up_by_1a():
    """Ramp limit: target is 14A from 6A, but only moves +1A per cycle."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)
    assert ctrl.current_amps == MIN_CHARGE_AMPS

    # surplus → target 14A, but ramp limits to 7A
    ev_draw = MIN_CHARGE_AMPS * WATTS_PER_AMP
    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, solar_power_w=4000, consumption_w=1000 + ev_draw)

    assert ctrl.current_amps == 7
    hass.services.async_call.assert_called_once_with(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": 7},
    )


@pytest.mark.asyncio
async def test_regulate_ramps_to_target_over_cycles():
    """Multiple regulation cycles ramp toward target 1A at a time."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)
    assert ctrl.current_amps == MIN_CHARGE_AMPS  # 6A

    ev_draw = MIN_CHARGE_AMPS * WATTS_PER_AMP
    # Cycle 1: 6→7
    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, solar_power_w=4000, consumption_w=1000 + ev_draw)
    assert ctrl.current_amps == 7

    # Cycle 2: 7→8 (consumption adjusts with new ev_draw)
    ev_draw = 7 * WATTS_PER_AMP
    with patch("time.monotonic", return_value=t + 2 * REGULATE_INTERVAL_S):
        await _eval(ctrl, solar_power_w=4000, consumption_w=1000 + ev_draw)
    assert ctrl.current_amps == 8


@pytest.mark.asyncio
async def test_regulate_ramp_throttled():
    """±1A = 230W < 500W delta → throttled unless interval elapsed."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)
    assert ctrl.current_amps == MIN_CHARGE_AMPS

    ev_draw = MIN_CHARGE_AMPS * WATTS_PER_AMP
    # Only 5s later — 1A delta = 230W < 500W → throttled
    with patch("time.monotonic", return_value=t + 5):
        await _eval(ctrl, solar_power_w=4000, consumption_w=1000 + ev_draw)

    assert ctrl.current_amps == MIN_CHARGE_AMPS  # throttled
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_regulate_decrease_by_1a():
    """Consumption increase → target drops, but ramp limits to -1A."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)
    ctrl._current_amps = 14  # simulate having ramped up
    ctrl._last_regulate_time = t

    # surplus = 4000 - 3000 - 14*230 + 14*230 = 1000 → target 5 → clamped 6
    # target 6 < 14 → ramp to 13
    ev_draw = 14 * WATTS_PER_AMP
    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, solar_power_w=4000, consumption_w=3000 + ev_draw)

    assert ctrl.current_amps == 13


@pytest.mark.asyncio
async def test_regulate_clamped_at_min():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)

    ev_draw = MIN_CHARGE_AMPS * WATTS_PER_AMP
    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, solar_power_w=1000, consumption_w=500 + ev_draw)

    assert ctrl.current_amps == MIN_CHARGE_AMPS
    assert ctrl._state == ChargerState.CHARGING


@pytest.mark.asyncio
async def test_regulate_clamped_at_max():
    """Even with huge surplus, ramp limits to +1A per cycle."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)
    ctrl._current_amps = MAX_CHARGE_AMPS - 1  # 31A
    ctrl._last_regulate_time = t

    # consumption must stay below 7kW to avoid overload path
    # home=200W + EV=31*230=7130W = 7330W > 7000 → use very low home consumption
    # Actually EV draw is part of consumption_w, so keep total < 7000
    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, solar_power_w=10000, consumption_w=6900)

    assert ctrl.current_amps == MAX_CHARGE_AMPS


# ------------------------------------------------------------------
# Overload protection: consumption >= 7kW
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overload_reduces_by_1a():
    """Consumption >= 7kW → reduce by 1A immediately (no throttle)."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)
    ctrl._current_amps = 14
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=t + 2):  # only 2s
        await _eval(ctrl, solar_power_w=4000, consumption_w=7500)

    assert ctrl.current_amps == 13
    assert ctrl._state == ChargerState.CHARGING
    hass.services.async_call.assert_called_once_with(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": 13},
    )


@pytest.mark.asyncio
async def test_overload_stops_if_already_at_minimum():
    """Consumption >= 7kW and already at min amps → stop charging."""
    ctrl, hass = _make_controller(user_amps=32)
    t = await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)
    ctrl._current_amps = MIN_CHARGE_AMPS
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=t + 2):
        await _eval(ctrl, solar_power_w=4000, consumption_w=8000)

    assert ctrl._state == ChargerState.IDLE
    assert ctrl.current_amps == 32  # restored


@pytest.mark.asyncio
async def test_overload_reduces_one_at_a_time():
    """Large excess still only reduces 1A per cycle."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)
    ctrl._current_amps = 14
    hass.services.async_call.reset_mock()

    # Even with excess=2000W, only reduces by 1A
    with patch("time.monotonic", return_value=t + 2):
        await _eval(ctrl, solar_power_w=4000, consumption_w=9000)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.current_amps == 13


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
    """After WH stops, regulation still works — ramps +1A toward target."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass, solar_power_w=4000, consumption_w=1000)
    assert ctrl.current_amps == MIN_CHARGE_AMPS

    ev_draw = MIN_CHARGE_AMPS * WATTS_PER_AMP
    # target = 16A but ramp limits to 7A
    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, soc=92.0, solar_power_w=4000,
                    consumption_w=500 + ev_draw, water_heater_heating=False)

    assert ctrl.current_amps == 7


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
    """Stop fires only when pinned at 6A AND solar < consumption AND SoC low."""
    ctrl, hass = _make_controller(user_amps=32)
    await _start_charging(ctrl, hass)
    assert ctrl.current_amps == MIN_CHARGE_AMPS  # pinned at min after start

    # solar < consumption → battery draining to cover EV
    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1,
                solar_power_w=500, consumption_w=2000)

    assert ctrl._state == ChargerState.IDLE
    assert ctrl.is_charging is False
    calls = hass.services.async_call.call_args_list
    assert calls[0] == call(
        "homeassistant", "turn_off", {"entity_id": "switch.ev_charger"}
    )
    assert calls[1] == call(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": 32},
    )
    assert ctrl.current_amps == 32


@pytest.mark.asyncio
async def test_does_not_stop_when_solar_still_covers_consumption():
    """Below stop SoC, but solar still > consumption → keep charging."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    # SoC dropped below threshold but solar still covers everything
    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 5,
                solar_power_w=3000, consumption_w=1500)
    assert ctrl._state == ChargerState.CHARGING


@pytest.mark.asyncio
async def test_does_not_stop_when_not_at_min_amps():
    """Below stop SoC with solar < consumption, but amps > 6A — keep going.

    Amp regulation will reduce amperage first; stop only fires once we're
    pinned at minimum.
    """
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)
    ctrl._current_amps = 12  # simulate having ramped up

    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1,
                solar_power_w=500, consumption_w=2000)
    assert ctrl._state == ChargerState.CHARGING


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
async def test_soc_exactly_at_start_threshold_triggers():
    """SoC == 95% should now trigger (>= threshold)."""
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, soc=SOC_START_THRESHOLD, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, soc=SOC_START_THRESHOLD, export_w=600)

    assert ctrl._state == ChargerState.CHARGING


@pytest.mark.asyncio
async def test_stops_below_stop_threshold():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    # Pinned at 6A + solar < consumption + SoC below threshold
    await _eval(ctrl, soc=85.0, solar_power_w=500, consumption_w=2000)
    assert ctrl._state == ChargerState.IDLE


# ------------------------------------------------------------------
# Save / restore user amps
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_saves_user_amps_on_start():
    ctrl, hass = _make_controller(user_amps=32)
    assert ctrl._saved_amps is None
    await _start_charging(ctrl, hass)
    assert ctrl._saved_amps == 32


@pytest.mark.asyncio
async def test_restores_user_amps_on_stop():
    ctrl, hass = _make_controller(user_amps=25)
    await _start_charging(ctrl, hass)
    assert ctrl._saved_amps == 25

    hass.services.async_call.reset_mock()
    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1,
                solar_power_w=500, consumption_w=2000)

    assert ctrl._state == ChargerState.IDLE
    assert ctrl.current_amps == 25
    assert ctrl._saved_amps is None

    calls = hass.services.async_call.call_args_list
    assert calls[1] == call(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": 25},
    )


@pytest.mark.asyncio
async def test_restores_amps_on_shutdown_stop():
    ctrl, hass = _make_controller(user_amps=32)
    await _start_charging(ctrl, hass)
    ctrl._current_amps = 8
    hass.services.async_call.reset_mock()

    await ctrl._turn_off()

    assert ctrl.current_amps == 32
    calls = hass.services.async_call.call_args_list
    assert calls[1] == call(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": 32},
    )


@pytest.mark.asyncio
async def test_no_restore_if_entity_unavailable():
    ctrl, hass = _make_controller()
    hass.states.get.return_value = None

    await _start_charging(ctrl, hass)
    assert ctrl._saved_amps is None

    hass.services.async_call.reset_mock()
    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1,
                solar_power_w=500, consumption_w=2000)

    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_off", {"entity_id": "switch.ev_charger"}
    )


# ------------------------------------------------------------------
# Manual mode: start_manual() / stop()
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_start_enters_charging():
    ctrl, hass = _make_controller(user_amps=20)
    await ctrl.start_manual()

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl._start_mode == StartMode.MANUAL
    assert ctrl.current_amps == MIN_CHARGE_AMPS
    assert ctrl._saved_amps == 20
    # Should have set amps and turned on
    calls = hass.services.async_call.call_args_list
    assert calls[0] == call(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": MIN_CHARGE_AMPS},
    )
    assert calls[1] == call(
        "homeassistant", "turn_on", {"entity_id": "switch.ev_charger"},
    )


@pytest.mark.asyncio
async def test_manual_start_noop_if_already_charging():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)
    hass.services.async_call.reset_mock()

    await ctrl.start_manual()  # should be a no-op
    hass.services.async_call.assert_not_called()
    assert ctrl._start_mode == StartMode.AUTO  # unchanged


@pytest.mark.asyncio
async def test_manual_stop():
    ctrl, hass = _make_controller(user_amps=20)
    await ctrl.start_manual()
    hass.services.async_call.reset_mock()

    await ctrl.stop()

    assert ctrl._state == ChargerState.IDLE
    assert ctrl._start_mode is None
    assert ctrl.current_amps == 20  # restored


@pytest.mark.asyncio
async def test_stop_noop_if_idle():
    ctrl, hass = _make_controller()
    await ctrl.stop()

    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_manual_mode_ignores_soc_drop():
    """Manual mode should NOT stop on low SoC."""
    ctrl, hass = _make_controller()
    await ctrl.start_manual()
    hass.services.async_call.reset_mock()

    await _eval(ctrl, soc=50.0)  # way below SOC_STOP_THRESHOLD

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl._start_mode == StartMode.MANUAL


@pytest.mark.asyncio
async def test_manual_mode_overload_clamps_at_min():
    """Manual mode overload: clamp at MIN_CHARGE_AMPS instead of stopping."""
    ctrl, hass = _make_controller()
    await ctrl.start_manual()
    ctrl._current_amps = MIN_CHARGE_AMPS
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=2000.0):
        await _eval(ctrl, solar_power_w=4000, consumption_w=8000)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl._start_mode == StartMode.MANUAL
    assert ctrl.current_amps == MIN_CHARGE_AMPS


@pytest.mark.asyncio
async def test_manual_mode_overload_reduces_by_1a():
    """Manual mode overload with higher amps: reduces by 1A, stays charging."""
    ctrl, hass = _make_controller()
    await ctrl.start_manual()
    ctrl._current_amps = 14
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=2000.0):
        await _eval(ctrl, solar_power_w=4000, consumption_w=9000)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.current_amps == 13
    hass.services.async_call.assert_called_once_with(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": 13},
    )


@pytest.mark.asyncio
async def test_auto_mode_sets_start_mode():
    """Auto-start sets _start_mode to AUTO."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)
    assert ctrl._start_mode == StartMode.AUTO


@pytest.mark.asyncio
async def test_auto_stop_clears_start_mode():
    """SoC drop in AUTO mode (with solar < consumption) clears _start_mode."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1,
                solar_power_w=500, consumption_w=2000)

    assert ctrl._state == ChargerState.IDLE
    assert ctrl._start_mode is None


# ------------------------------------------------------------------
# Water heater prerequisite: None = no WH configured → OK to start
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starts_without_water_heater():
    """When water_heater_heating is None (no WH), EV starts on surplus alone."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, export_w=600, water_heater_heating=None)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, export_w=600, water_heater_heating=None)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl._start_mode == StartMode.AUTO


@pytest.mark.asyncio
async def test_no_start_when_water_heater_off():
    """When water_heater_heating is False (WH exists but off), EV doesn't start."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, export_w=600, water_heater_heating=False)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, export_w=600, water_heater_heating=False)

    assert ctrl._state == ChargerState.IDLE
