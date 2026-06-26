"""Tests for EvChargerController.

The controller reads the live toggle + amps entity state on every
evaluate.  Tests use a stateful ``FakeHass`` whose
``hass.services.async_call`` actually flips the simulated switch state
and updates the amps value, matching real HA semantics.
"""

from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch

from homeassistant.exceptions import HomeAssistantError

from custom_components.beem_ai.ev_charger_controller import (
    EMERGENCY_SHRINK_W,
    MAX_CHARGE_AMPS,
    MAX_CONSUMPTION_W,
    MIN_CHARGE_AMPS,
    PENDING_START_GRACE_S,
    REGULATE_INTERVAL_S,
    START_HEADROOM_W,
    STATUS_NO_DEMAND_SUSTAIN_S,
    SUSTAIN_SECONDS,
    StartMode,
    WATTS_PER_AMP,
    EvChargerController,
)

TARGET_SOC = 95.0
SOC_HYSTERESIS = 5.0
SOC_START_THRESHOLD = TARGET_SOC
SOC_STOP_THRESHOLD = TARGET_SOC - SOC_HYSTERESIS

DEFAULT_START_METER_W = -2000.0
DEFAULT_START_BATTERY_W = 0.0

SWITCH_ID = "switch.ev_charger"
AMPS_ID = "number.ev_charger_amps"
STATUS_ID = "sensor.ev_charger_status"


class FakeHass:
    """Stateful HA stub for the EV charger controller."""

    def __init__(self, user_amps: int = 32) -> None:
        self._switch_state = "off"
        self._switch_last_changed = datetime.now(timezone.utc)
        self._amps: int | None = user_amps
        self._status: str | None = None
        self.services = MagicMock()
        self.services.async_call = AsyncMock(side_effect=self._service_call)
        self.states = MagicMock()
        self.states.get = MagicMock(side_effect=self._states_get)

    async def _service_call(self, domain, service, data):
        entity_id = data.get("entity_id")
        if (
            domain == "homeassistant"
            and service in ("turn_on", "turn_off")
            and entity_id == SWITCH_ID
        ):
            new = "on" if service == "turn_on" else "off"
            if new != self._switch_state:
                self._switch_state = new
                self._switch_last_changed = datetime.now(timezone.utc)
        elif domain == "number" and service == "set_value" and entity_id == AMPS_ID:
            self._amps = int(data["value"])
        # homeassistant.update_entity and other services are no-ops here.

    def _states_get(self, entity_id):
        if entity_id == SWITCH_ID:
            obj = MagicMock()
            obj.state = self._switch_state
            obj.last_changed = self._switch_last_changed
            return obj
        if entity_id == AMPS_ID:
            if self._amps is None:
                return None
            obj = MagicMock()
            obj.state = str(self._amps)
            return obj
        if entity_id == STATUS_ID:
            if self._status is None:
                return None
            obj = MagicMock()
            obj.state = self._status
            return obj
        return None

    def set_switch(self, state: str) -> None:
        self._switch_state = state
        self._switch_last_changed = datetime.now(timezone.utc)

    def set_amps(self, amps: int | None) -> None:
        self._amps = amps

    def set_status(self, status: str | None) -> None:
        self._status = status


def _make_controller(user_amps: int = 32, with_status: bool = False):
    hass = FakeHass(user_amps=user_amps)
    ctrl = EvChargerController(
        hass=hass,
        toggle_entity_id=SWITCH_ID,
        power_entity_id=AMPS_ID,
        status_entity_id=STATUS_ID if with_status else None,
    )
    return ctrl, hass


async def _eval(
    ctrl,
    soc=TARGET_SOC,
    meter_power_w=DEFAULT_START_METER_W,
    battery_power_w=DEFAULT_START_BATTERY_W,
    solar_power_w=4000.0,
    consumption_w=1000.0,
    water_heater_heating=True,
    target_soc=TARGET_SOC,
    soc_hysteresis=SOC_HYSTERESIS,
    mode="Auto",
):
    await ctrl.evaluate(
        soc,
        meter_power_w=meter_power_w,
        battery_power_w=battery_power_w,
        solar_power_w=solar_power_w,
        consumption_w=consumption_w,
        water_heater_heating=water_heater_heating,
        target_soc=target_soc,
        soc_hysteresis=soc_hysteresis,
        mode=mode,
    )


async def _start_charging(
    ctrl, hass,
    meter_power_w=DEFAULT_START_METER_W,
    battery_power_w=DEFAULT_START_BATTERY_W,
    t0=1000.0,
):
    """Drive controller into CHARGING via Auto-start sustain."""
    with patch("time.monotonic", return_value=t0):
        await _eval(ctrl, meter_power_w=meter_power_w,
                    battery_power_w=battery_power_w)
    with patch("time.monotonic", return_value=t0 + SUSTAIN_SECONDS):
        await _eval(ctrl, meter_power_w=meter_power_w,
                    battery_power_w=battery_power_w)
    assert ctrl.is_charging is True
    hass.services.async_call.reset_mock()
    return t0 + SUSTAIN_SECONDS


# ------------------------------------------------------------------
# Initial state
# ------------------------------------------------------------------


def test_initial_state():
    ctrl, _ = _make_controller()
    assert ctrl.is_charging is False
    # 32 A from FakeHass → clamped to MAX
    assert ctrl.current_amps == MAX_CHARGE_AMPS


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
    assert ctrl.is_charging is False
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_transition_below_soc_threshold():
    ctrl, hass = _make_controller()
    await _eval(ctrl, soc=90.0)
    assert ctrl.is_charging is False
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_transition_below_headroom_threshold():
    """Start requires headroom_w ≥ START_HEADROOM_W (1380 W)."""
    ctrl, hass = _make_controller()
    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, meter_power_w=-1000, battery_power_w=0)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, meter_power_w=-1000, battery_power_w=0)
    assert ctrl.is_charging is False
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_starts_at_lower_soc_when_battery_absorbing_all_solar():
    ctrl, hass = _make_controller()
    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, soc=76.0,
                    meter_power_w=0.0, battery_power_w=2000.0,
                    target_soc=75.0)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, soc=76.0,
                    meter_power_w=0.0, battery_power_w=2000.0,
                    target_soc=75.0)
    assert ctrl.is_charging is True


@pytest.mark.asyncio
async def test_no_transition_before_sustain_period():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl)
    assert ctrl.is_charging is False

    with patch("time.monotonic", return_value=1010.0):
        await _eval(ctrl)
    assert ctrl.is_charging is False
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_always_starts_at_min_amps():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, meter_power_w=-3000)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, meter_power_w=-3000)

    assert ctrl.is_charging is True
    assert ctrl._start_mode == StartMode.AUTO
    assert ctrl.current_amps == MIN_CHARGE_AMPS

    calls = hass.services.async_call.call_args_list
    assert calls[0] == call(
        "number", "set_value",
        {"entity_id": AMPS_ID, "value": MIN_CHARGE_AMPS},
    )
    assert calls[1] == call(
        "homeassistant", "turn_on", {"entity_id": SWITCH_ID},
    )


@pytest.mark.asyncio
async def test_start_amps_always_min_even_with_high_headroom():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, meter_power_w=-9000)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, meter_power_w=-9000)

    assert ctrl.is_charging is True
    assert ctrl.current_amps == MIN_CHARGE_AMPS


@pytest.mark.asyncio
async def test_sustain_timer_resets_when_headroom_drops():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl)  # default headroom 2000 W
    assert ctrl._export_sustained_since is not None

    with patch("time.monotonic", return_value=1015.0):
        await _eval(ctrl, meter_power_w=-200, battery_power_w=0)
    assert ctrl._export_sustained_since is None


@pytest.mark.asyncio
async def test_oscillating_headroom_does_not_reset_within_grace():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl)
    assert ctrl._export_sustained_since == 1000.0

    with patch("time.monotonic", return_value=1005.0):
        await _eval(ctrl, meter_power_w=-200, battery_power_w=0)
    assert ctrl._export_sustained_since == 1000.0

    with patch("time.monotonic", return_value=1010.0):
        await _eval(ctrl)
    assert ctrl._export_sustained_since == 1000.0

    with patch("time.monotonic", return_value=1030.0):
        await _eval(ctrl)
    assert ctrl.is_charging is True


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
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    assert ctrl.current_amps == MIN_CHARGE_AMPS

    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=-2000, battery_power_w=0)

    assert ctrl.current_amps == 7
    hass.services.async_call.assert_called_once_with(
        "number", "set_value",
        {"entity_id": AMPS_ID, "value": 7},
    )


@pytest.mark.asyncio
async def test_regulate_ramps_to_target_over_cycles():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    assert ctrl.current_amps == MIN_CHARGE_AMPS

    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=-2000, battery_power_w=0)
    assert ctrl.current_amps == 7

    with patch("time.monotonic", return_value=t + 2 * REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=-1770, battery_power_w=0)
    assert ctrl.current_amps == 8


@pytest.mark.asyncio
async def test_regulate_ramp_throttled():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    assert ctrl.current_amps == MIN_CHARGE_AMPS

    with patch("time.monotonic", return_value=t + 5):
        await _eval(ctrl, meter_power_w=-200, battery_power_w=0)

    assert ctrl.current_amps == MIN_CHARGE_AMPS  # throttled (below fast-ramp)
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_regulate_decrease_by_1a():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    hass.set_amps(14)
    ctrl._last_regulate_time = t

    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=+300, battery_power_w=0)

    assert ctrl.current_amps == 13


@pytest.mark.asyncio
async def test_regulate_emergency_shrink_bypasses_throttle():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    hass.set_amps(14)
    ctrl._last_regulate_time = t
    hass.services.async_call.reset_mock()

    assert -600 <= -EMERGENCY_SHRINK_W
    with patch("time.monotonic", return_value=t + 2):
        await _eval(ctrl, meter_power_w=+600, battery_power_w=0)

    assert ctrl.current_amps == 13
    hass.services.async_call.assert_called_once_with(
        "number", "set_value",
        {"entity_id": AMPS_ID, "value": 13},
    )


@pytest.mark.asyncio
async def test_regulate_clamped_at_min():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)

    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=-100, battery_power_w=0)

    assert ctrl.current_amps == MIN_CHARGE_AMPS
    assert ctrl.is_charging is True


@pytest.mark.asyncio
async def test_regulate_clamped_at_max():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    hass.set_amps(MAX_CHARGE_AMPS - 1)
    ctrl._last_regulate_time = t

    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=-5000, battery_power_w=0,
                    consumption_w=500)

    assert ctrl.current_amps == MAX_CHARGE_AMPS


# ------------------------------------------------------------------
# Overload protection: consumption >= 7kW
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overload_reduces_to_target_in_one_step():
    """Mild overload (600W over target) → reduce by ceil(600/230)=3A
    in a single tick, not -1A."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    hass.set_amps(14)
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=t + 2):
        await _eval(ctrl, consumption_w=7500)

    # excess = 7500 - 6900 = 600W → 3A drop → 14 - 3 = 11A
    assert ctrl.current_amps == 11
    assert ctrl.is_charging is True
    hass.services.async_call.assert_called_once_with(
        "number", "set_value",
        {"entity_id": AMPS_ID, "value": 11},
    )


@pytest.mark.asyncio
async def test_overload_stops_if_already_at_minimum():
    ctrl, hass = _make_controller(user_amps=32)
    t = await _start_charging(ctrl, hass)
    # After auto-start, amps are at MIN already.  Saved_amps == 32.
    assert ctrl.current_amps == MIN_CHARGE_AMPS
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=t + 2):
        await _eval(ctrl, consumption_w=8000)

    assert ctrl.is_charging is False
    assert ctrl.current_amps == 32  # restored


@pytest.mark.asyncio
async def test_overload_stops_when_required_reduction_below_min():
    """Severe overload (9000W, excess 2500W ≈ 11A) from 14A would put
    us at 3A — below MIN_CHARGE_AMPS — so we stop instead."""
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    hass.set_amps(14)
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=t + 2):
        await _eval(ctrl, consumption_w=9000)

    assert ctrl.is_charging is False


# ------------------------------------------------------------------
# CHARGING continues when water heater stops
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continues_charging_when_water_heater_stops():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await _eval(ctrl, soc=92.0, water_heater_heating=False)

    assert ctrl.is_charging is True


@pytest.mark.asyncio
async def test_regulates_amps_when_water_heater_stops():
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
    assert ctrl.is_charging is True


@pytest.mark.asyncio
async def test_stops_when_soc_drops_below_threshold():
    """Stop fires only when pinned at 6A AND battery discharging AND low SoC."""
    ctrl, hass = _make_controller(user_amps=32)
    await _start_charging(ctrl, hass)
    assert ctrl.current_amps == MIN_CHARGE_AMPS

    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1,
                meter_power_w=0, battery_power_w=-1500)

    assert ctrl.is_charging is False
    calls = hass.services.async_call.call_args_list
    assert calls[0] == call(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID}
    )
    # calls[1] is the post-turn_off update_entity refresh.
    assert calls[2] == call(
        "number", "set_value",
        {"entity_id": AMPS_ID, "value": 32},
    )
    assert ctrl.current_amps == 32


@pytest.mark.asyncio
async def test_does_not_stop_when_not_at_min_amps():
    """Below stop SoC with battery draining, but amps > 6A — keep going."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)
    hass.set_amps(12)

    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1,
                meter_power_w=0, battery_power_w=-1500)
    assert ctrl.is_charging is True


@pytest.mark.asyncio
async def test_stays_charging_above_stop_threshold():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await _eval(ctrl, soc=91.0)
    assert ctrl.is_charging is True


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
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, soc=SOC_START_THRESHOLD)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, soc=SOC_START_THRESHOLD)

    assert ctrl.is_charging is True


@pytest.mark.asyncio
async def test_stops_below_stop_threshold():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await _eval(ctrl, soc=85.0, meter_power_w=0, battery_power_w=-1500)
    assert ctrl.is_charging is False


# ------------------------------------------------------------------
# Phantom-surplus regression tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_phantom_ramp_from_misreported_consumption():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    hass.set_amps(30)
    ctrl._last_regulate_time = t

    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, meter_power_w=+100, battery_power_w=0,
                    solar_power_w=711, consumption_w=507)

    assert ctrl.current_amps <= 30


@pytest.mark.asyncio
async def test_phantom_ramp_shrinks_on_import():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    hass.set_amps(30)
    ctrl._last_regulate_time = t

    with patch("time.monotonic", return_value=t + 2):
        await _eval(ctrl, meter_power_w=+2000, battery_power_w=0,
                    solar_power_w=700, consumption_w=500)

    assert ctrl.current_amps == 29


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

    assert ctrl.is_charging is False
    assert ctrl.current_amps == 25
    assert ctrl._saved_amps is None

    calls = hass.services.async_call.call_args_list
    # calls[0]=turn_off, calls[1]=update_entity refresh, calls[2]=restore amps.
    assert calls[2] == call(
        "number", "set_value",
        {"entity_id": AMPS_ID, "value": 25},
    )


@pytest.mark.asyncio
async def test_no_restore_if_entity_unavailable():
    """Amps entity returns None → we never captured _saved_amps → no
    restore is attempted on stop, just a turn_off."""
    ctrl, hass = _make_controller()
    hass.set_amps(None)

    await _start_charging(ctrl, hass)
    assert ctrl._saved_amps is None

    hass.services.async_call.reset_mock()
    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1,
                meter_power_w=0, battery_power_w=-1500)

    assert ctrl.is_charging is False
    # turn_off + post-turn_off update_entity refresh, no amps restore.
    calls = hass.services.async_call.call_args_list
    assert calls == [
        call("homeassistant", "turn_off", {"entity_id": SWITCH_ID}),
        call("homeassistant", "update_entity", {"entity_id": SWITCH_ID}),
    ]


# ------------------------------------------------------------------
# Manual mode: start_manual() / stop()
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_start_enters_charging():
    ctrl, hass = _make_controller(user_amps=20)
    await ctrl.start_manual()

    assert ctrl.is_charging is True
    assert ctrl._start_mode == StartMode.MANUAL
    assert ctrl.current_amps == MIN_CHARGE_AMPS
    assert ctrl._saved_amps == 20
    calls = hass.services.async_call.call_args_list
    assert calls[0] == call(
        "number", "set_value",
        {"entity_id": AMPS_ID, "value": MIN_CHARGE_AMPS},
    )
    assert calls[1] == call(
        "homeassistant", "turn_on", {"entity_id": SWITCH_ID},
    )


@pytest.mark.asyncio
async def test_manual_start_noop_if_already_charging():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)
    hass.services.async_call.reset_mock()

    await ctrl.start_manual()
    hass.services.async_call.assert_not_called()
    assert ctrl._start_mode == StartMode.AUTO


@pytest.mark.asyncio
async def test_manual_stop():
    ctrl, hass = _make_controller(user_amps=20)
    await ctrl.start_manual()
    hass.services.async_call.reset_mock()

    await ctrl.stop()

    assert ctrl.is_charging is False
    assert ctrl._start_mode is None
    assert ctrl.current_amps == 20


@pytest.mark.asyncio
async def test_stop_noop_if_idle():
    ctrl, hass = _make_controller()
    await ctrl.stop()

    assert ctrl.is_charging is False
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_manual_mode_overload_stops():
    """Manual mode: overload ≥ 7 kW is a hard stop (safety override)."""
    ctrl, hass = _make_controller()
    await ctrl.start_manual()
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=2000.0):
        await _eval(ctrl, consumption_w=8000, mode="Manual")

    assert ctrl.is_charging is False


@pytest.mark.asyncio
async def test_auto_mode_overload_throttles_when_reduction_feasible():
    """In Auto mode, overload trims amps to bring consumption below
    the 6900W target — only stopping when that would fall under 6A.
    Here cons=7500 from amps=14 → drop 3A → 11A, still charging."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)
    hass.set_amps(14)
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=2000.0):
        await _eval(ctrl, consumption_w=7500)

    assert ctrl.is_charging is True
    assert ctrl.current_amps == 11
    hass.services.async_call.assert_called_once_with(
        "number", "set_value",
        {"entity_id": AMPS_ID, "value": 11},
    )


@pytest.mark.asyncio
async def test_auto_mode_sets_start_mode():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)
    assert ctrl._start_mode == StartMode.AUTO


@pytest.mark.asyncio
async def test_auto_stop_clears_start_mode():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1,
                meter_power_w=0, battery_power_w=-1500)

    assert ctrl.is_charging is False
    assert ctrl._start_mode is None


# ------------------------------------------------------------------
# Water heater prerequisite: None = no WH configured → OK to start
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starts_without_water_heater():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, water_heater_heating=None)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, water_heater_heating=None)

    assert ctrl.is_charging is True
    assert ctrl._start_mode == StartMode.AUTO


@pytest.mark.asyncio
async def test_no_start_when_water_heater_off():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, water_heater_heating=False)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, water_heater_heating=False)

    assert ctrl.is_charging is False


# ==================================================================
# External toggles / session-adoption
# ==================================================================


@pytest.mark.asyncio
async def test_externally_turned_on_adopts_manual_session():
    """Switch turned on externally → next evaluate adopts MANUAL session.

    Replaces the old resync_state test — the new design picks up the
    physical state automatically on the next evaluate.
    """
    ctrl, hass = _make_controller(user_amps=16)
    hass.set_switch("on")

    # First evaluate after external turn-on: adopts MANUAL session
    await _eval(ctrl, soc=92.0, meter_power_w=0, battery_power_w=0)
    assert ctrl.is_charging is True
    assert ctrl._start_mode == StartMode.MANUAL
    assert ctrl.current_amps == 16  # whatever the wallbox was at


@pytest.mark.asyncio
async def test_externally_turned_on_then_soc_drop_stops_auto():
    """Switch on externally + then evaluate in Auto with SoC drop → stops.

    The adopted session defaults to MANUAL (so SoC-floor doesn't fire in
    Auto without re-checking).  But if the user explicitly passes Auto
    mode and conditions are met, the SoC stop still uses pinned-at-min
    semantics.  This test verifies the externally-adopted session behaves
    correctly under MANUAL semantics: SoC drop alone doesn't stop it.
    """
    ctrl, hass = _make_controller(user_amps=10)
    hass.set_switch("on")

    # Default mode is Auto; the controller adopts MANUAL bookkeeping but
    # the *mode* (passed to evaluate) is Auto, so Auto-stop logic still
    # applies.  Pinned at MIN_CHARGE_AMPS (we'll force amps to 6).
    hass.set_amps(MIN_CHARGE_AMPS)

    await _eval(ctrl, soc=SOC_STOP_THRESHOLD - 1,
                meter_power_w=0, battery_power_w=-1500)

    assert ctrl.is_charging is False


@pytest.mark.asyncio
async def test_externally_turned_off_clears_session():
    """Switch turned off externally → controller clears session bookkeeping."""
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)
    assert ctrl._start_mode is not None

    hass.set_switch("off")
    await _eval(ctrl, soc=92.0)

    assert ctrl.is_charging is False
    assert ctrl._start_mode is None
    assert ctrl._saved_amps is None


# ==================================================================
# Mode control: Disabled / Auto / Manual
# ==================================================================


@pytest.mark.asyncio
async def test_handle_mode_change_disabled_stops_when_charging():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await ctrl.handle_mode_change("Disabled")

    assert ctrl.is_charging is False
    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID},
    )


@pytest.mark.asyncio
async def test_handle_mode_change_disabled_noop_when_idle():
    ctrl, hass = _make_controller()
    await ctrl.handle_mode_change("Disabled")
    assert ctrl.is_charging is False
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_handle_mode_change_manual_starts_at_min_when_idle():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await ctrl.handle_mode_change("Manual")

    assert ctrl.is_charging is True
    assert ctrl._start_mode == StartMode.MANUAL
    assert ctrl.current_amps == MIN_CHARGE_AMPS
    calls = hass.services.async_call.call_args_list
    assert calls[0] == call(
        "number", "set_value",
        {"entity_id": AMPS_ID, "value": MIN_CHARGE_AMPS},
    )
    assert calls[1] == call(
        "homeassistant", "turn_on", {"entity_id": SWITCH_ID},
    )


@pytest.mark.asyncio
async def test_handle_mode_change_manual_noop_when_already_charging():
    ctrl, hass = _make_controller()
    await _start_charging(ctrl, hass)

    await ctrl.handle_mode_change("Manual")

    assert ctrl.is_charging is True
    for c in hass.services.async_call.call_args_list:
        assert c != call(
            "homeassistant", "turn_on", {"entity_id": SWITCH_ID},
        )


@pytest.mark.asyncio
async def test_evaluate_disabled_mode_stops_charging():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)

    with patch("time.monotonic", return_value=t + 10):
        await _eval(ctrl, mode="Disabled")

    assert ctrl.is_charging is False


@pytest.mark.asyncio
async def test_evaluate_disabled_mode_idle_noop():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, mode="Disabled")
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl, mode="Disabled")

    assert ctrl.is_charging is False
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_manual_mode_ignores_soc_stop():
    """Manual mode: SoC below stop + pinned at 6A → KEEPS charging."""
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await ctrl.handle_mode_change("Manual")
    assert ctrl.is_charging is True
    assert ctrl.current_amps == MIN_CHARGE_AMPS
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=2000.0):
        await _eval(
            ctrl,
            soc=SOC_STOP_THRESHOLD - 1,
            battery_power_w=-500,
            meter_power_w=0,
            mode="Manual",
        )

    assert ctrl.is_charging is True
    for c in hass.services.async_call.call_args_list:
        assert c != call(
            "homeassistant", "turn_off", {"entity_id": SWITCH_ID},
        )


@pytest.mark.asyncio
async def test_disabled_force_off_even_when_session_stale():
    """Disabled must turn off the switch even if controller has no
    in-memory session (e.g. fresh after options reload)."""
    ctrl, hass = _make_controller()
    hass.set_switch("on")
    assert ctrl._start_mode is None

    await ctrl.handle_mode_change("Disabled")

    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID},
    )
    assert ctrl.is_charging is False


@pytest.mark.asyncio
async def test_evaluate_disabled_force_off_when_session_stale():
    """evaluate(mode=Disabled) turns off even with no in-memory session."""
    ctrl, hass = _make_controller()
    hass.set_switch("on")

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl, mode="Disabled")

    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID},
    )


# ==================================================================
# Closed-loop SoC bias (Auto)
# ==================================================================


@pytest.mark.asyncio
async def test_auto_soc_bias_above_target_grows():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    assert ctrl.current_amps == MIN_CHARGE_AMPS

    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, soc=TARGET_SOC + 2.0,
                    meter_power_w=0, battery_power_w=0)
    assert ctrl.current_amps == 7


@pytest.mark.asyncio
async def test_auto_soc_bias_below_target_shrinks():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    hass.set_amps(12)
    ctrl._last_regulate_time = t

    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, soc=TARGET_SOC - 2.0,
                    meter_power_w=0, battery_power_w=0)
    assert ctrl.current_amps == 11


@pytest.mark.asyncio
async def test_auto_soc_bias_within_deadband_holds():
    ctrl, hass = _make_controller()
    t = await _start_charging(ctrl, hass)
    hass.set_amps(10)
    ctrl._last_regulate_time = t

    with patch("time.monotonic", return_value=t + REGULATE_INTERVAL_S):
        await _eval(ctrl, soc=TARGET_SOC,
                    meter_power_w=0, battery_power_w=0)
    assert ctrl.current_amps == 10


@pytest.mark.asyncio
async def test_manual_mode_no_soc_bias():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await ctrl.handle_mode_change("Manual")
    hass.set_amps(10)
    ctrl._last_regulate_time = 1000.0
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=1000.0 + REGULATE_INTERVAL_S):
        await _eval(ctrl, soc=TARGET_SOC + 5.0,
                    meter_power_w=0, battery_power_w=0,
                    mode="Manual")
    assert ctrl.current_amps == 10


@pytest.mark.asyncio
async def test_manual_mode_overload_hard_stops():
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

    assert ctrl.is_charging is False
    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID},
    )


# ------------------------------------------------------------------
# Pending-start grace (Wallbox cloud flakiness)
# ------------------------------------------------------------------


def _flaky_hass_factory():
    """FakeHass where homeassistant.turn_on raises HomeAssistantError but
    the switch entity stays off (simulates the Wallbox-cloud failure
    mode where the HTTP call errors client-side but the entity is
    *not* updated)."""

    hass = FakeHass(user_amps=32)
    original = hass._service_call

    async def flaky(domain, service, data):
        if (
            domain == "homeassistant"
            and service == "turn_on"
            and data.get("entity_id") == SWITCH_ID
        ):
            raise HomeAssistantError("Error communicating with Wallbox API")
        await original(domain, service, data)

    hass.services.async_call = AsyncMock(side_effect=flaky)
    return hass


@pytest.mark.asyncio
async def test_turn_on_error_does_not_reset_session_within_grace():
    """When turn_on raises and the entity stays off, the controller
    must hold session state and not loop through sustain-arm again."""
    ctrl, _ = _make_controller()
    ctrl._hass = _flaky_hass_factory()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl)

    assert ctrl._pending_start_since == 1000.0 + SUSTAIN_SECONDS
    assert ctrl._start_mode == StartMode.AUTO
    assert ctrl.is_charging is False

    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS + 5):
        await _eval(ctrl)

    assert ctrl._start_mode == StartMode.AUTO
    assert ctrl._pending_start_since == 1000.0 + SUSTAIN_SECONDS
    assert ctrl._export_sustained_since is None


@pytest.mark.asyncio
async def test_pending_start_clears_when_entity_eventually_confirms():
    """When the Wallbox entity finally reports `on` after a flaky
    turn_on, the controller should confirm and proceed to charging."""
    ctrl, _ = _make_controller()
    hass = _flaky_hass_factory()
    ctrl._hass = hass

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl)
    assert ctrl._pending_start_since is not None

    hass.set_switch("on")
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS + 20):
        await _eval(ctrl)

    assert ctrl._pending_start_since is None
    assert ctrl.is_charging is True
    assert ctrl._start_mode == StartMode.AUTO


@pytest.mark.asyncio
async def test_pending_start_gives_up_after_grace_window():
    """After PENDING_START_GRACE_S without entity confirmation, the
    controller resets state and re-evaluates from idle."""
    ctrl, _ = _make_controller()
    ctrl._hass = _flaky_hass_factory()

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl)
    pending_t0 = ctrl._pending_start_since

    with patch("time.monotonic",
               return_value=pending_t0 + PENDING_START_GRACE_S + 1):
        await _eval(ctrl, meter_power_w=0, battery_power_w=0)

    assert ctrl._pending_start_since is None
    assert ctrl._start_mode is None


@pytest.mark.asyncio
async def test_update_entity_called_during_pending():
    """While pending, controller should periodically nudge HA to repoll."""
    ctrl, _ = _make_controller()
    hass = _flaky_hass_factory()
    ctrl._hass = hass

    with patch("time.monotonic", return_value=1000.0):
        await _eval(ctrl)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _eval(ctrl)
    initial_refreshes = sum(
        1 for c in hass.services.async_call.call_args_list
        if c.args[:2] == ("homeassistant", "update_entity")
    )

    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS + 5):
        await _eval(ctrl)
    mid_refreshes = sum(
        1 for c in hass.services.async_call.call_args_list
        if c.args[:2] == ("homeassistant", "update_entity")
    )
    assert mid_refreshes == initial_refreshes

    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS + 25):
        await _eval(ctrl)
    late_refreshes = sum(
        1 for c in hass.services.async_call.call_args_list
        if c.args[:2] == ("homeassistant", "update_entity")
    )
    assert late_refreshes == initial_refreshes + 1


@pytest.mark.asyncio
async def test_set_amps_swallows_homeassistant_error():
    """set_value failures must not propagate — controller continues."""
    ctrl, _ = _make_controller()
    hass = FakeHass(user_amps=32)

    async def raise_on_set(domain, service, data):
        if domain == "number" and service == "set_value":
            raise HomeAssistantError("amps service failed")

    hass.services.async_call = AsyncMock(side_effect=raise_on_set)
    ctrl._hass = hass

    await ctrl._set_amps(10)


# ==================================================================
# Wallbox status entity — stop when car not drawing
# ==================================================================


@pytest.mark.asyncio
async def test_status_no_demand_sustained_stops_charging():
    """status='waiting for car demand' for the full sustain → stop."""
    ctrl, hass = _make_controller(with_status=True)
    t = await _start_charging(ctrl, hass)
    hass.set_status("Waiting for car demand")

    with patch("time.monotonic", return_value=t + 1):
        await _eval(ctrl)
    assert ctrl.is_charging is True  # armed, not yet stopped

    with patch("time.monotonic",
               return_value=t + 1 + STATUS_NO_DEMAND_SUSTAIN_S):
        await _eval(ctrl)

    assert ctrl.is_charging is False
    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID},
    )


@pytest.mark.asyncio
async def test_status_no_demand_resets_arm_when_car_resumes():
    """If status flips back before sustain elapses, don't stop."""
    ctrl, hass = _make_controller(with_status=True)
    t = await _start_charging(ctrl, hass)
    hass.set_status("Waiting for car demand")

    with patch("time.monotonic", return_value=t + 1):
        await _eval(ctrl)
    assert ctrl._no_demand_since is not None

    hass.set_status("Charging")
    with patch("time.monotonic",
               return_value=t + 1 + STATUS_NO_DEMAND_SUSTAIN_S + 10):
        await _eval(ctrl)

    assert ctrl.is_charging is True
    assert ctrl._no_demand_since is None


@pytest.mark.asyncio
async def test_status_other_state_does_not_stop():
    """Active states like 'Charging' must never trigger the stop."""
    ctrl, hass = _make_controller(with_status=True)
    t = await _start_charging(ctrl, hass)
    hass.set_status("Charging")

    with patch("time.monotonic",
               return_value=t + STATUS_NO_DEMAND_SUSTAIN_S + 30):
        await _eval(ctrl)

    assert ctrl.is_charging is True
    assert ctrl._no_demand_since is None


@pytest.mark.asyncio
async def test_status_unknown_does_not_stop():
    """Unknown / unavailable status must not trigger the stop."""
    ctrl, hass = _make_controller(with_status=True)
    t = await _start_charging(ctrl, hass)
    hass.set_status("unknown")

    with patch("time.monotonic",
               return_value=t + STATUS_NO_DEMAND_SUSTAIN_S + 30):
        await _eval(ctrl)

    assert ctrl.is_charging is True


@pytest.mark.asyncio
async def test_status_match_is_case_insensitive():
    """Wallbox capitalization shouldn't matter."""
    ctrl, hass = _make_controller(with_status=True)
    t = await _start_charging(ctrl, hass)
    hass.set_status("WAITING FOR CAR DEMAND")

    with patch("time.monotonic", return_value=t + 1):
        await _eval(ctrl)
    with patch("time.monotonic",
               return_value=t + 1 + STATUS_NO_DEMAND_SUSTAIN_S):
        await _eval(ctrl)

    assert ctrl.is_charging is False


@pytest.mark.asyncio
async def test_no_status_entity_skips_check():
    """Without a status entity configured, the branch is a no-op."""
    ctrl, hass = _make_controller(with_status=False)
    t = await _start_charging(ctrl, hass)
    # Even if some other entity has 'waiting for car demand', controller
    # doesn't read it.
    with patch("time.monotonic",
               return_value=t + STATUS_NO_DEMAND_SUSTAIN_S + 30):
        await _eval(ctrl)

    assert ctrl.is_charging is True
