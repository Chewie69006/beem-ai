"""Tests for EvChargerController.

Surplus detection and amp regulation are driven by two telemetry signals:

    meter_power_w   (+import / -export)
    battery_power_w (+charge / -discharge)

Headroom = -meter_power_w + battery_power_w.  That is, every watt we're
currently exporting plus every watt we're currently stashing in the
battery can be diverted to the EV on the next cycle.

Tests parameterise the scenarios with explicit meter/battery values
rather than the old ``export_w`` / EV-in-consumption model — this
matches the real-world telemetry and avoids the phantom-surplus feedback
loop that bit us previously.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch

from custom_components.beem_ai.ev_charger_controller import (
    ChargerState,
    EMERGENCY_SHRINK_W,
    MAX_CHARGE_AMPS,
    MAX_CONSUMPTION_W,
    MIN_CHARGE_AMPS,
    REGULATE_INTERVAL_S,
    START_HEADROOM_W,
    SUSTAIN_SECONDS,
    StartMode,
    WATTS_PER_AMP,
    EvChargerController,
)

# Thresholds are user-configurable (per-call).  Tests use fixed values
# matching the original module-level defaults so existing assertions hold.
SOC_START_THRESHOLD = 95.0
SOC_STOP_THRESHOLD = 90.0

# Default plenty-of-headroom values for "starting" scenarios (2000 W
# export → well above START_HEADROOM_W = 1380 W).
DEFAULT_START_METER_W = -2000.0
DEFAULT_START_BATTERY_W = 0.0


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


async def _eval(
    ctrl,
    soc=96.0,
    meter_power_w=DEFAULT_START_METER_W,
    battery_power_w=DEFAULT_START_BATTERY_W,
    solar_power_w=4000.0,
    consumption_w=1000.0,
    water_heater_heating=True,
    start_soc_threshold=SOC_START_THRESHOLD,
    stop_soc_threshold=SOC_STOP_THRESHOLD,
    mode="Auto",
):
    """Helper with sensible defaults."""
    await ctrl.evaluate(
        soc,
        meter_power_w=meter_power_w,
        battery_power_w=battery_power_w,
        solar_power_w=solar_power_w,
        consumption_w=consumption_w,
        water_heater_heating=water_heater_heating,
        start_soc_threshold=start_soc_threshold,
        stop_soc_threshold=stop_soc_threshold,
        mode=mode,
    )


async def _start_charging(
    ctrl, hass,
    meter_power_w=DEFAULT_START_METER_W,
    battery_power_w=DEFAULT_START_BATTERY_W,
    t0=1000.0,
):
    """Helper: get controller into CHARGING state."""
    with patch("time.monotonic", return_value=t0):
        await _eval(ctrl, meter_power_w=meter_power_w,
                    battery_power_w=battery_power_w)
    with patch("time.monotonic", return_value=t0 + SUSTAIN_SECONDS):
        await _eval(ctrl, meter_power_w=meter_power_w,
                    battery_power_w=battery_power_w)
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
        await _eval(ctrl, water_heater_heating=False)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, water_heater_heating=False)
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_transition_below_soc_threshold():
    ctrl, hass = _make_controller()
    await _eval(ctrl, soc=90.0)
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_transition_below_headroom_threshold():
    """Start requires headroom_w ≥ START_HEADROOM_W (1380 W)."""
    ctrl, hass = _make_controller()
    # export 1000 W, battery flat → headroom 1000 W < 1380 W
    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, meter_power_w=-1000, battery_power_w=0)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, meter_power_w=-1000, battery_power_w=0)
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_starts_at_lower_soc_when_battery_absorbing_all_solar():
    """Regression: user sets start=75%.  Battery absorbs all solar →
    meter_power_w ≈ 0, but battery_power_w > 0 → headroom still adequate.
    """
    ctrl, hass = _make_controller()
    # No grid export, battery soaking 2000 W → headroom 2000 W
    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, soc=76.0,
                    meter_power_w=0.0, battery_power_w=2000.0,
                    start_soc_threshold=75.0)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, soc=76.0,
                    meter_power_w=0.0, battery_power_w=2000.0,
                    start_soc_threshold=75.0)
    assert ctrl._state == ChargerState.CHARGING


@pytest.mark.asyncio
async def test_no_transition_before_sustain_period():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl)
    assert ctrl._state == ChargerState.IDLE

    with patch("time.monotonic", return_value=1010.0):
        await _eval(ctrl)
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_always_starts_at_min_amps():
    """Always starts at MIN_CHARGE_AMPS (6A) regardless of headroom."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, meter_power_w=-3000)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, meter_power_w=-3000)

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
async def test_start_amps_always_min_even_with_high_headroom():
    """Even with huge headroom, always starts at MIN_CHARGE_AMPS."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, meter_power_w=-9000)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, meter_power_w=-9000)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.current_amps == MIN_CHARGE_AMPS


@pytest.mark.asyncio
async def test_sustain_timer_resets_when_headroom_drops():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl)  # default headroom 2000 W
    assert ctrl._export_sustained_since is not None

    # Headroom drops to 200 W (< START_HEADROOM_W)
    with patch("time.monotonic", return_value=1015.0):
        await _eval(ctrl, meter_power_w=-200, battery_power_w=0)
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
async def test_oscillating_headroom_does_not_reset_within_grace():
    """Brief headroom dips (< GRACE_SECONDS) must NOT reset the sustain timer."""
    ctrl, _ = _make_controller()

    # t=1000: headroom OK → sustain starts
    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl)
    assert ctrl._export_sustained_since == 1000.0

    # t=1005: brief dip (5s < grace=15s) → timer must NOT reset
    with patch("time.monotonic", return_value=1005.0):
        await _eval(ctrl, meter_power_w=-200, battery_power_w=0)
    assert ctrl._export_sustained_since == 1000.0

    # t=1010: headroom back
    with patch("time.monotonic", return_value=1010.0):
        await _eval(ctrl)
    assert ctrl._export_sustained_since == 1000.0

    # t=1030: >= 30s since sustain start + currently OK → starts charging
    with patch("time.monotonic", return_value=1030.0):
        await _eval(ctrl)
    assert ctrl._state == ChargerState.CHARGING


@pytest.mark.asyncio
async def test_sustain_timer_resets_when_water_heater_stops():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl)
    assert ctrl._export_sustained_since is not None

    with patch("time.monotonic", return_value=1015.0):
        await _eval(ctrl, water_heater_heating=False)
    assert ctrl._export_sustained_since is None


# ------------------------------------------------------------------
# CHARGING: headroom-based amp regulation
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regulate_ramps_up_by_1a():
    """Positive headroom → grow current amps by 1A per cycle."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    assert ctrl.current_amps == MIN_CHARGE_AMPS

    # Export 2000 W → headroom = 2000 W → delta = 8 → target clamped by ramp
    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=-2000, battery_power_w=0)

    assert ctrl.current_amps == 7
    hass.services.async_call.assert_called_once_with(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": 7},
    )


@pytest.mark.asyncio
async def test_regulate_ramps_to_target_over_cycles():
    """Multiple regulation cycles ramp toward target 1A at a time."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    assert ctrl.current_amps == MIN_CHARGE_AMPS  # 6A

    # Cycle 1: headroom = 2000 W → +1A
    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=-2000, battery_power_w=0)
    assert ctrl.current_amps == 7

    # Cycle 2: EV now drew the extra 230W, still 1770W left → +1A
    with patch("time.monotonic", return_value=t + 2 * REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=-1770, battery_power_w=0)
    assert ctrl.current_amps == 8


@pytest.mark.asyncio
async def test_regulate_ramp_throttled():
    """±1A change is throttled unless REGULATE_INTERVAL_S has elapsed."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    assert ctrl.current_amps == MIN_CHARGE_AMPS

    # Only 5s later — throttled
    with patch("time.monotonic", return_value=t + 5):
        await _eval(ctrl, meter_power_w=-2000, battery_power_w=0)

    assert ctrl.current_amps == MIN_CHARGE_AMPS  # throttled
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_regulate_decrease_by_1a():
    """Negative headroom → shrink by 1A."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    ctrl._current_amps = 14
    ctrl._last_regulate_time = t

    # Importing 300 W → headroom = -300 W → delta = -2 → ramp -1 to 13
    # (below emergency-shrink threshold, so throttle still respected;
    # we advance time past the interval)
    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=+300, battery_power_w=0)

    assert ctrl.current_amps == 13


@pytest.mark.asyncio
async def test_regulate_emergency_shrink_bypasses_throttle():
    """Heavy import (headroom ≤ -500W) bypasses the 30s throttle."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    ctrl._current_amps = 14
    ctrl._last_regulate_time = t
    hass.services.async_call.reset_mock()

    # Only 2s elapsed, but importing 600 W (battery discharging 100 W)
    # → headroom = -600 -100... actually headroom = -meter + battery
    #   = -(+600) + 0 = -600 ≤ -EMERGENCY_SHRINK_W → emergency shrink
    assert -600 <= -EMERGENCY_SHRINK_W
    with patch("time.monotonic", return_value=t + 2):
        await _eval(ctrl, meter_power_w=+600, battery_power_w=0)

    assert ctrl.current_amps == 13
    hass.services.async_call.assert_called_once_with(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": 13},
    )


@pytest.mark.asyncio
async def test_regulate_clamped_at_min():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)

    # Headroom near zero → no change, stays at 6 A
    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=-100, battery_power_w=0)

    assert ctrl.current_amps == MIN_CHARGE_AMPS
    assert ctrl._state == ChargerState.CHARGING


@pytest.mark.asyncio
async def test_regulate_clamped_at_max():
    """Huge headroom → ramp to MAX_CHARGE_AMPS, clamped."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    ctrl._current_amps = MAX_CHARGE_AMPS - 1  # 31A
    ctrl._last_regulate_time = t

    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=-5000, battery_power_w=0,
                    consumption_w=500)

    assert ctrl.current_amps == MAX_CHARGE_AMPS


# ------------------------------------------------------------------
# Overload protection: consumption >= 7kW
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overload_reduces_by_1a():
    """Consumption >= 7kW → reduce by 1A immediately (no throttle)."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    ctrl._current_amps = 14
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=t + 2):  # only 2s
        await _eval(ctrl, consumption_w=7500)

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
    t = await _start_charging(ctrl, hass)
    ctrl._current_amps = MIN_CHARGE_AMPS
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=t + 2):
        await _eval(ctrl, consumption_w=8000)

    assert ctrl._state == ChargerState.IDLE
    assert ctrl.current_amps == 32  # restored


@pytest.mark.asyncio
async def test_overload_reduces_one_at_a_time():
    """Large excess still only reduces 1A per cycle."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    ctrl._current_amps = 14
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=t + 2):
        await _eval(ctrl, consumption_w=9000)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.current_amps == 13


# ------------------------------------------------------------------
# CHARGING continues when water heater stops
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continues_charging_when_water_heater_stops():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await _eval(ctrl, soc=92.0, water_heater_heating=False)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.is_charging is True


@pytest.mark.asyncio
async def test_regulates_amps_when_water_heater_stops():
    """After WH stops, regulation still works — ramps +1A toward target."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    assert ctrl.current_amps == MIN_CHARGE_AMPS

    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, soc=92.0,
                    meter_power_w=-2000, battery_power_w=0,
                    water_heater_heating=False)

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
    """Stop fires only when pinned at 6A AND battery discharging AND low SoC."""
    ctrl, hass = _make_controller(user_amps=32)
    await _start_charging(ctrl, hass)
    assert ctrl.current_amps == MIN_CHARGE_AMPS  # pinned at min after start

    # battery_power_w < 0 → draining to cover EV
    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1,
                meter_power_w=0, battery_power_w=-1500)

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
async def test_does_not_stop_when_battery_still_charging():
    """Below stop SoC, but battery still charging (+) → keep charging."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    # SoC dropped below threshold but battery is still charging
    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 5,
                meter_power_w=-500, battery_power_w=+500)
    assert ctrl._state == ChargerState.CHARGING


@pytest.mark.asyncio
async def test_does_not_stop_when_not_at_min_amps():
    """Below stop SoC with battery draining, but amps > 6A — keep going.

    Amp regulation will reduce amperage first; stop only fires once we're
    pinned at minimum.
    """
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)
    ctrl._current_amps = 12  # simulate having ramped up

    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1,
                meter_power_w=0, battery_power_w=-1500)
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
    """SoC == 95% should trigger (>= threshold)."""
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, soc=SOC_START_THRESHOLD)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, soc=SOC_START_THRESHOLD)

    assert ctrl._state == ChargerState.CHARGING


@pytest.mark.asyncio
async def test_stops_below_stop_threshold():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    # Pinned at 6A + battery draining + SoC below threshold
    await _eval(ctrl, soc=85.0, meter_power_w=0, battery_power_w=-1500)
    assert ctrl._state == ChargerState.IDLE


# ------------------------------------------------------------------
# Phantom-surplus regression tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_phantom_ramp_from_misreported_consumption():
    """Regression: overnight bug where solar=711W, consumption=507W,
    meter≈0 (slight import) made the old formula report ~7000W surplus
    because it added EV draw back to consumption.  The headroom model
    ignores consumption entirely for regulation, so this must not ramp.
    """
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    ctrl._current_amps = 30
    ctrl._last_regulate_time = t

    # Real signal: slight import, battery idle → headroom near zero or
    # negative → must not grow.
    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=+100, battery_power_w=0,
                    solar_power_w=711, consumption_w=507)

    assert ctrl.current_amps <= 30  # never grew


@pytest.mark.asyncio
async def test_phantom_ramp_shrinks_on_import():
    """If the true signal shows we're importing, the controller must
    shrink — not grow — regardless of what solar/consumption report.
    """
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    ctrl._current_amps = 30
    ctrl._last_regulate_time = t

    # Strong import → emergency shrink
    with patch("time.monotonic", return_value=t + 2):
        await _eval(ctrl, meter_power_w=+2000, battery_power_w=0,
                    solar_power_w=700, consumption_w=500)

    assert ctrl.current_amps == 29  # shrank, bypassed throttle


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
                meter_power_w=0, battery_power_w=-1500)

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
                meter_power_w=0, battery_power_w=-1500)

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
async def test_manual_mode_honours_soc_drop():
    """Manual mode *does* stop on low SoC when pinned at 6 A and battery
    discharging — the "EV Charger" switch looks like a simple on/off to
    users, so we must keep the safeguard active regardless of who
    started charging.
    """
    ctrl, hass = _make_controller(user_amps=20)
    await ctrl.start_manual()
    hass.services.async_call.reset_mock()

    await _eval(ctrl, soc=50.0, meter_power_w=0, battery_power_w=-1500)

    assert ctrl._state == ChargerState.IDLE
    assert ctrl._start_mode is None
    assert ctrl.current_amps == 20  # restored


@pytest.mark.asyncio
async def test_manual_mode_overload_stops():
    """Manual mode: overload ≥ 7 kW is a hard stop (safety override)."""
    ctrl, hass = _make_controller()
    await ctrl.start_manual()
    ctrl._current_amps = MIN_CHARGE_AMPS
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=2000.0):
        await _eval(ctrl, consumption_w=8000, mode="Manual")

    assert ctrl._state == ChargerState.IDLE


@pytest.mark.asyncio
async def test_manual_mode_overload_reduces_by_1a():
    """Manual mode overload with higher amps: reduces by 1A, stays charging."""
    ctrl, hass = _make_controller()
    await ctrl.start_manual()
    ctrl._current_amps = 14
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=2000.0):
        await _eval(ctrl, consumption_w=9000)

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
    """SoC drop in AUTO mode (with battery draining) clears _start_mode."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1,
                meter_power_w=0, battery_power_w=-1500)

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
        await _eval(ctrl, water_heater_heating=None)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, water_heater_heating=None)

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl._start_mode == StartMode.AUTO


@pytest.mark.asyncio
async def test_no_start_when_water_heater_off():
    """When water_heater_heating is False (WH exists but off), EV doesn't start."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, water_heater_heating=False)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, water_heater_heating=False)

    assert ctrl._state == ChargerState.IDLE


# ==================================================================
# Startup resync
# ==================================================================


def _set_ev_states(hass, toggle_state: str, amps: int = 10) -> None:
    """Mock hass.states.get to return toggle + amps entities correctly."""
    def _get(entity_id):
        m = MagicMock()
        if entity_id == "switch.ev_charger":
            m.state = toggle_state
        elif entity_id == "number.ev_charger_amps":
            m.state = str(amps)
        else:
            m.state = "unknown"
        return m
    hass.states.get = MagicMock(side_effect=_get)


@pytest.mark.asyncio
async def test_resync_toggle_on_sets_charging():
    """Toggle physically ON at startup → controller resyncs to CHARGING (MANUAL)."""
    ctrl, hass = _make_controller()
    _set_ev_states(hass, "on", amps=16)

    ctrl.resync_state()

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl._start_mode == StartMode.MANUAL
    assert ctrl._current_amps == 16


@pytest.mark.asyncio
async def test_resync_toggle_off_stays_idle():
    """Toggle OFF at startup → controller stays IDLE."""
    ctrl, hass = _make_controller()
    _set_ev_states(hass, "off")

    ctrl.resync_state()

    assert ctrl._state == ChargerState.IDLE


@pytest.mark.asyncio
async def test_resync_then_soc_drop_stops_charging():
    """After resync to CHARGING, SoC drop + battery draining + min amps → stop."""
    ctrl, hass = _make_controller()
    _set_ev_states(hass, "on", amps=MIN_CHARGE_AMPS)
    ctrl.resync_state()

    # SoC below stop threshold, battery draining, at min amps → stops
    with patch("time.monotonic", return_value=2000.0):
        await _eval(
            ctrl,
            soc=SOC_STOP_THRESHOLD - 1,
            battery_power_w=-500,  # draining
            meter_power_w=0,
        )

    assert ctrl._state == ChargerState.IDLE


# ==================================================================
# Mode control: Disabled / Auto / Manual
# ==================================================================


@pytest.mark.asyncio
async def test_handle_mode_change_disabled_stops_when_charging():
    """Mode → Disabled while CHARGING must stop the charger."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await ctrl.handle_mode_change("Disabled")

    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": "switch.ev_charger"},
    )


@pytest.mark.asyncio
async def test_handle_mode_change_disabled_noop_when_idle():
    """Mode → Disabled while IDLE is a no-op (no switch toggled)."""
    ctrl, hass = _make_controller()
    await ctrl.handle_mode_change("Disabled")
    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_mode_change_manual_starts_at_min_when_idle():
    """Mode → Manual while IDLE starts immediately at 6A (no sustain wait)."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await ctrl.handle_mode_change("Manual")

    assert ctrl._state == ChargerState.CHARGING
    assert ctrl._start_mode == StartMode.MANUAL
    assert ctrl.current_amps == MIN_CHARGE_AMPS
    calls = hass.services.async_call.call_args_list
    # Sets amps to MIN then turns on
    assert calls[0] == call(
        "number", "set_value",
        {"entity_id": "number.ev_charger_amps", "value": MIN_CHARGE_AMPS},
    )
    assert calls[1] == call(
        "homeassistant", "turn_on", {"entity_id": "switch.ev_charger"},
    )


@pytest.mark.asyncio
async def test_handle_mode_change_manual_noop_when_already_charging():
    """Mode → Manual while already CHARGING leaves session alone."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await ctrl.handle_mode_change("Manual")

    # Still charging, no extra turn_on
    assert ctrl._state == ChargerState.CHARGING
    for c in hass.services.async_call.call_args_list:
        assert c != call(
            "homeassistant", "turn_on", {"entity_id": "switch.ev_charger"},
        )


@pytest.mark.asyncio
async def test_evaluate_disabled_mode_stops_charging():
    """evaluate(mode=Disabled) while CHARGING must stop."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)

    with patch("time.monotonic", return_value=t + 10):
        await _eval(ctrl, mode="Disabled")

    assert ctrl._state == ChargerState.IDLE


@pytest.mark.asyncio
async def test_evaluate_disabled_mode_idle_noop():
    """evaluate(mode=Disabled) while IDLE is a no-op — doesn't auto-start."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, mode="Disabled")
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, mode="Disabled")

    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_manual_mode_ignores_soc_stop():
    """Manual mode: SoC below stop + pinned at 6A + battery draining → KEEPS charging.

    (Auto would stop in this scenario — see test_resync_then_soc_drop_stops_charging.)
    """
    ctrl, hass = _make_controller()

    # Start in Manual
    with patch("time.monotonic", return_value=1000.0):
        await ctrl.handle_mode_change("Manual")
    assert ctrl._state == ChargerState.CHARGING
    assert ctrl.current_amps == MIN_CHARGE_AMPS
    hass.services.async_call.reset_mock()

    # SoC below stop, battery draining, at min amps — Auto would stop here.
    with patch("time.monotonic", return_value=2000.0):
        await _eval(
            ctrl,
            soc=SOC_STOP_THRESHOLD - 1,
            battery_power_w=-500,
            meter_power_w=0,
            mode="Manual",
        )

    assert ctrl._state == ChargerState.CHARGING
    # No turn_off was sent
    for c in hass.services.async_call.call_args_list:
        assert c != call(
            "homeassistant", "turn_off", {"entity_id": "switch.ev_charger"},
        )


@pytest.mark.asyncio
async def test_manual_mode_overload_hard_stops():
    """Manual mode: consumption ≥ 7kW → hard stop (safety override)."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await ctrl.handle_mode_change("Manual")
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=2000.0):
        await _eval(
            ctrl,
            consumption_w=MAX_CONSUMPTION_W + 100,
            mode="Manual",
        )

    assert ctrl._state == ChargerState.IDLE
    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": "switch.ev_charger"},
    )
