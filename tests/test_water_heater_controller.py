"""Tests for WaterHeaterController.

The controller reads the live switch-entity state on every evaluate, so
tests use a stateful ``FakeHass`` whose ``hass.services.async_call`` for
``homeassistant.turn_on``/``turn_off`` actually flips the simulated
switch state (and bumps ``last_changed``).  This mirrors real HA closely
enough that the controller's branching logic exercises the same code
paths as in production.
"""

from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.beem_ai.water_heater_controller import (
    COOLDOWN_AFTER_EXTERNAL_OFF_S,
    EXPORT_SOC_THRESHOLD,
    HYSTERESIS_PCT,
    SUSTAIN_SECONDS,
    WaterHeaterController,
)

SOC_THRESHOLD = 80.0
CHARGE_POWER_THRESHOLD = 500.0
SWITCH_ID = "switch.water_heater"


class FakeHass:
    """Stateful HA stub for the water heater controller."""

    def __init__(self) -> None:
        self._switch_state = "off"
        self._switch_last_changed = datetime.now(timezone.utc)
        self.services = MagicMock()
        self.services.async_call = AsyncMock(side_effect=self._service_call)
        self.states = MagicMock()
        self.states.get = MagicMock(side_effect=self._states_get)

    async def _service_call(self, domain, service, data):
        if domain == "homeassistant":
            new = "on" if service == "turn_on" else "off"
            if new != self._switch_state:
                self._switch_state = new
                self._switch_last_changed = datetime.now(timezone.utc)

    def _states_get(self, entity_id):
        if entity_id != SWITCH_ID:
            return None
        obj = MagicMock()
        obj.state = self._switch_state
        obj.last_changed = self._switch_last_changed
        return obj

    def set_switch(self, state: str, seconds_ago: float = 0.0) -> None:
        """Force the switch state and last_changed (testing helper)."""
        self._switch_state = state
        self._switch_last_changed = (
            datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        )

    def remove_switch(self) -> None:
        """Make hass.states.get return None for the switch."""
        self.states.get = MagicMock(return_value=None)


def _make_controller():
    hass = FakeHass()
    ctrl = WaterHeaterController(hass=hass, switch_entity_id=SWITCH_ID)
    return ctrl, hass


async def _evaluate(
    ctrl, soc, export_w=0.0, charge_power_w=0.0,
    consumption_w=500.0, import_w=0.0,
    soc_threshold=SOC_THRESHOLD,
    charge_power_threshold=CHARGE_POWER_THRESHOLD,
    sustain_seconds=SUSTAIN_SECONDS,
    min_duration_s=0,
    mode="Auto",
):
    """Helper to call evaluate with sane defaults.

    ``min_duration_s`` defaults to 0 so legacy SoC-stop tests fire
    immediately.  Pass a non-zero value to exercise minimum-duration.
    """
    await ctrl.evaluate(
        soc, export_w, charge_power_w, consumption_w,
        import_w, soc_threshold, charge_power_threshold,
        sustain_seconds=sustain_seconds,
        min_duration_s=min_duration_s,
        mode=mode,
    )


async def _heat_via_export(ctrl, hass, t0=1000.0):
    """Drive into HEATING via rule 1 (export) and reset the services mock."""
    with patch("time.monotonic", return_value=t0):
        await _evaluate(ctrl, soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=t0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=96.0, export_w=600)
    assert ctrl.is_heating is True
    hass.services.async_call.reset_mock()
    return t0 + SUSTAIN_SECONDS


async def _heat_via_charge(ctrl, hass, t0=1000.0):
    """Drive into HEATING via rule 2 (charge power) and reset the services mock."""
    with patch("time.monotonic", return_value=t0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)
    with patch("time.monotonic", return_value=t0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)
    assert ctrl.is_heating is True
    hass.services.async_call.reset_mock()
    return t0 + SUSTAIN_SECONDS


# ==================================================================
# Initial state
# ==================================================================


def test_initial_state():
    ctrl, _ = _make_controller()
    assert ctrl.is_heating is False


# ==================================================================
# Rule 1: hardcoded — SoC >= 95% AND exporting
# ==================================================================


@pytest.mark.asyncio
async def test_rule1_triggers_when_exporting_above_95():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=96.0, export_w=600)

    assert ctrl.is_heating is True
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_on", {"entity_id": SWITCH_ID}
    )


@pytest.mark.asyncio
async def test_rule1_any_positive_export():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=96.0, export_w=1.0)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=96.0, export_w=1.0)

    assert ctrl.is_heating is True


@pytest.mark.asyncio
async def test_rule1_soc_at_threshold_triggers():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=EXPORT_SOC_THRESHOLD, export_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=EXPORT_SOC_THRESHOLD, export_w=600)

    assert ctrl.is_heating is True


@pytest.mark.asyncio
async def test_rule1_soc_too_low():
    ctrl, hass = _make_controller()
    await _evaluate(ctrl, soc=94.9, export_w=600)
    assert ctrl.is_heating is False
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_rule1_not_exporting():
    ctrl, _ = _make_controller()
    await _evaluate(ctrl, soc=96.0, export_w=0, charge_power_w=0)
    assert ctrl.is_heating is False


@pytest.mark.asyncio
async def test_rule1_sustain_resets_when_export_stops():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=96.0, export_w=600)
    assert ctrl._sustained_since is not None

    with patch("time.monotonic", return_value=1015.0):
        await _evaluate(ctrl, soc=96.0, export_w=0, charge_power_w=0)
    assert ctrl._sustained_since is None


@pytest.mark.asyncio
async def test_rule1_before_sustain():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=96.0, export_w=600)
    with patch("time.monotonic", return_value=1010.0):
        await _evaluate(ctrl, soc=96.0, export_w=600)

    assert ctrl.is_heating is False
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_rule1_stop_hysteresis():
    ctrl, hass = _make_controller()
    t = await _heat_via_export(ctrl, hass)

    with patch("time.monotonic", return_value=t + 60):
        await _evaluate(ctrl, soc=EXPORT_SOC_THRESHOLD - HYSTERESIS_PCT)
    assert ctrl.is_heating is True

    with patch("time.monotonic", return_value=t + 120):
        await _evaluate(ctrl, soc=EXPORT_SOC_THRESHOLD - HYSTERESIS_PCT - 1)
    assert ctrl.is_heating is False
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID}
    )


# ==================================================================
# Rule 2: configurable — SoC > threshold AND charge power >= threshold
# ==================================================================


@pytest.mark.asyncio
async def test_rule2_triggers_on_charge_power():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)

    assert ctrl.is_heating is True
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_on", {"entity_id": SWITCH_ID}
    )


@pytest.mark.asyncio
async def test_rule2_soc_too_low():
    ctrl, hass = _make_controller()
    await _evaluate(ctrl, soc=79.9, export_w=0, charge_power_w=600)
    assert ctrl.is_heating is False
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_rule2_charge_power_too_low():
    ctrl, hass = _make_controller()
    await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=400)
    assert ctrl.is_heating is False
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_rule2_does_not_fire_when_grid_charging():
    """Importing from grid must NOT count as solar surplus."""
    ctrl, hass = _make_controller()
    await _evaluate(
        ctrl, soc=81.0, export_w=0, charge_power_w=600, import_w=600,
    )
    assert ctrl.is_heating is False
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_rule2_sustain_resets_when_power_drops():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)
    assert ctrl._sustained_since is not None

    with patch("time.monotonic", return_value=1015.0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=200)
    assert ctrl._sustained_since is None


@pytest.mark.asyncio
async def test_oscillating_conditions_do_not_reset_sustain_within_grace():
    """Brief condition dips (< GRACE_SECONDS) must NOT reset the sustain timer."""
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)
    assert ctrl._sustained_since == 1000.0

    with patch("time.monotonic", return_value=1005.0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=200)
    assert ctrl._sustained_since == 1000.0

    with patch("time.monotonic", return_value=1010.0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)
    assert ctrl._sustained_since == 1000.0

    with patch("time.monotonic", return_value=1030.0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)
    assert ctrl.is_heating is True


@pytest.mark.asyncio
async def test_rule2_stop_hysteresis():
    ctrl, hass = _make_controller()
    t = await _heat_via_charge(ctrl, hass)

    stop = SOC_THRESHOLD - HYSTERESIS_PCT  # 75% w/ default SOC_THRESHOLD=80

    with patch("time.monotonic", return_value=t + 60):
        await _evaluate(ctrl, soc=stop)
    assert ctrl.is_heating is True

    with patch("time.monotonic", return_value=t + 120):
        await _evaluate(ctrl, soc=stop - 1)
    assert ctrl.is_heating is False
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID}
    )


# ==================================================================
# Both rules: rule 2 triggers at lower SoC than rule 1
# ==================================================================


@pytest.mark.asyncio
async def test_rule2_fires_below_95_when_charging():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=81.0, export_w=0, charge_power_w=600)

    assert ctrl.is_heating is True
    assert ctrl._active_soc_threshold == SOC_THRESHOLD


@pytest.mark.asyncio
async def test_both_rules_active_uses_lower_threshold():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=96.0, export_w=600, charge_power_w=600)
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=96.0, export_w=600, charge_power_w=600)

    assert ctrl.is_heating is True
    assert ctrl._active_soc_threshold == SOC_THRESHOLD


# ==================================================================
# Overload protection — now coordinated at the BeemAICoordinator level
# (see tests/test_overload_coordination.py).  The WH controller itself
# no longer reacts to consumption alone; the coordinator calls
# ``force_stop_overload`` after a grace window if the EV throttle was
# insufficient.
# ==================================================================


@pytest.mark.asyncio
async def test_high_consumption_alone_does_not_stop_wh():
    """High consumption while heating no longer triggers an automatic
    stop inside the WH controller — that decision is now the
    coordinator's, after a 15s grace where the EV gets to throttle."""
    ctrl, hass = _make_controller()
    t = await _heat_via_export(ctrl, hass)

    with patch("time.monotonic", return_value=t + 60):
        await _evaluate(ctrl, soc=92.0, consumption_w=8000, import_w=1000)

    assert ctrl.is_heating is True
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_overload_if_not_importing():
    ctrl, hass = _make_controller()
    t = await _heat_via_export(ctrl, hass)

    with patch("time.monotonic", return_value=t + 60):
        await _evaluate(ctrl, soc=92.0, consumption_w=8000, import_w=0)

    assert ctrl.is_heating is True
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_no_overload_below_threshold():
    ctrl, hass = _make_controller()
    t = await _heat_via_export(ctrl, hass)

    with patch("time.monotonic", return_value=t + 60):
        await _evaluate(ctrl, soc=92.0, consumption_w=6000, import_w=500)

    assert ctrl.is_heating is True
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_force_stop_overload_bypasses_min_duration():
    """The coordinator's emergency hook stops the heater even when the
    min-duration floor has not been reached."""
    ctrl, hass = _make_controller()
    await _heat_via_charge(ctrl, hass)
    # Switch turned on just now — min_duration would normally block stop.

    await ctrl.force_stop_overload(consumption_w=8200.0)

    assert ctrl.is_heating is False
    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID},
    )


@pytest.mark.asyncio
async def test_force_stop_overload_noop_when_not_heating():
    ctrl, hass = _make_controller()
    await ctrl.force_stop_overload(consumption_w=8000.0)
    assert ctrl.is_heating is False
    hass.services.async_call.assert_not_called()


# ==================================================================
# External switch toggles / session-adoption
# ==================================================================


@pytest.mark.asyncio
async def test_externally_turned_on_adopts_session():
    """Switch turned on externally → next evaluate adopts a session and
    will honour subsequent SoC-stop logic."""
    ctrl, hass = _make_controller()
    hass.set_switch("on", seconds_ago=0)

    # First evaluate adopts the session (uses min(95, 80) = 80 → stop at 70)
    await _evaluate(ctrl, soc=90.0)
    assert ctrl.is_heating is True
    assert ctrl._active_soc_threshold == SOC_THRESHOLD

    # SoC drop below 70 → stops (default min_duration_s=0)
    await _evaluate(ctrl, soc=69.0)
    assert ctrl.is_heating is False
    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID},
    )


@pytest.mark.asyncio
async def test_externally_turned_off_clears_session():
    """Switch turned off externally → controller clears session bookkeeping."""
    ctrl, hass = _make_controller()
    await _heat_via_export(ctrl, hass)
    assert ctrl._active_soc_threshold is not None

    # External turn-off
    hass.set_switch("off")
    await _evaluate(ctrl, soc=85.0)

    assert ctrl.is_heating is False
    assert ctrl._active_soc_threshold is None


# ==================================================================
# Minimum heating duration + configurable sustain
# ==================================================================


@pytest.mark.asyncio
async def test_min_duration_defers_soc_stop():
    """SoC drop during min duration → heater stays on."""
    ctrl, hass = _make_controller()
    await _heat_via_charge(ctrl, hass)
    # Switch turned on ~now → seconds_since_turned_on is small

    await _evaluate(ctrl, soc=65.0, min_duration_s=30 * 60)

    assert ctrl.is_heating is True
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_min_duration_elapsed_allows_soc_stop():
    """SoC drop after min duration elapsed → heater stops."""
    ctrl, hass = _make_controller()
    await _heat_via_charge(ctrl, hass)
    # Backdate last_changed to simulate 31 min ago
    hass.set_switch("on", seconds_ago=31 * 60)

    await _evaluate(ctrl, soc=65.0, min_duration_s=30 * 60)

    assert ctrl.is_heating is False
    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID},
    )


@pytest.mark.asyncio
async def test_custom_sustain_seconds_used_for_turn_on():
    ctrl, _ = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=96.0, export_w=600, sustain_seconds=60)

    with patch("time.monotonic", return_value=1030.0):
        await _evaluate(ctrl, soc=96.0, export_w=600, sustain_seconds=60)
    assert ctrl.is_heating is False

    with patch("time.monotonic", return_value=1060.0):
        await _evaluate(ctrl, soc=96.0, export_w=600, sustain_seconds=60)
    assert ctrl.is_heating is True


# ==================================================================
# Mode control: Disabled / Auto
# ==================================================================


@pytest.mark.asyncio
async def test_handle_mode_change_disabled_stops_when_heating():
    ctrl, hass = _make_controller()
    await _heat_via_export(ctrl, hass)

    await ctrl.handle_mode_change("Disabled")

    assert ctrl.is_heating is False
    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID},
    )


@pytest.mark.asyncio
async def test_handle_mode_change_disabled_force_off_when_external_on():
    """Switch is on but controller has no session yet → Disabled still
    turns it off."""
    ctrl, hass = _make_controller()
    hass.set_switch("on")

    await ctrl.handle_mode_change("Disabled")

    assert ctrl.is_heating is False
    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID},
    )


@pytest.mark.asyncio
async def test_evaluate_disabled_mode_does_not_start():
    ctrl, hass = _make_controller()

    with patch("time.monotonic", return_value=1000.0):
        await _evaluate(ctrl, soc=96.0, export_w=600, mode="Disabled")
    with patch("time.monotonic", return_value=1000.0 + SUSTAIN_SECONDS):
        await _evaluate(ctrl, soc=96.0, export_w=600, mode="Disabled")

    assert ctrl.is_heating is False
    for c in hass.services.async_call.call_args_list:
        assert c.args[:2] != ("homeassistant", "turn_on")


@pytest.mark.asyncio
async def test_evaluate_disabled_mode_stops_heating():
    ctrl, hass = _make_controller()
    await _heat_via_export(ctrl, hass)

    await _evaluate(ctrl, soc=96.0, export_w=600, mode="Disabled")

    assert ctrl.is_heating is False
    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID},
    )


# ==================================================================
# External transition detection + post-external-OFF cooldown
# ==================================================================


@pytest.mark.asyncio
async def test_external_off_arms_cooldown_and_blocks_restart():
    """When the plug auto-off timer (or anything else) flips the
    switch off behind our back, the controller must not immediately
    re-arm and rapidly re-fire on a fading surplus."""
    ctrl, hass = _make_controller()

    # Drive into heating so _expected_state == "on".
    t0 = await _heat_via_export(ctrl, hass)

    # External off (simulates the smart plug's 4h auto-off timer).
    hass.set_switch("off")
    hass.services.async_call.reset_mock()

    # Even with strong surplus, we must not start during cooldown.
    with patch("time.monotonic", return_value=t0 + 60):
        await _evaluate(ctrl, soc=96.0, export_w=2000)
    with patch("time.monotonic", return_value=t0 + 60 + SUSTAIN_SECONDS + 5):
        await _evaluate(ctrl, soc=96.0, export_w=2000)

    assert ctrl.is_heating is False
    for c in hass.services.async_call.call_args_list:
        assert c.args[:2] != ("homeassistant", "turn_on")


@pytest.mark.asyncio
async def test_cooldown_expires_then_normal_start_works():
    ctrl, hass = _make_controller()
    t0 = await _heat_via_export(ctrl, hass)
    hass.set_switch("off")
    hass.services.async_call.reset_mock()

    # Tick during cooldown — no start.
    with patch("time.monotonic", return_value=t0 + 60):
        await _evaluate(ctrl, soc=96.0, export_w=2000)
    assert ctrl.is_heating is False

    # Jump past cooldown and re-arm.
    past_cooldown = t0 + 60 + COOLDOWN_AFTER_EXTERNAL_OFF_S + 5
    with patch("time.monotonic", return_value=past_cooldown):
        await _evaluate(ctrl, soc=96.0, export_w=2000)
    with patch(
        "time.monotonic",
        return_value=past_cooldown + SUSTAIN_SECONDS + 1,
    ):
        await _evaluate(ctrl, soc=96.0, export_w=2000)

    assert ctrl.is_heating is True


@pytest.mark.asyncio
async def test_disabled_then_auto_clears_cooldown():
    """User flipping the mode to Disabled is an explicit reset — the
    cooldown should not survive into the next Auto session."""
    ctrl, hass = _make_controller()
    t0 = await _heat_via_export(ctrl, hass)
    hass.set_switch("off")
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=t0 + 60):
        await _evaluate(ctrl, soc=96.0, export_w=2000, mode="Disabled")
    assert ctrl._cooldown_until_monotonic is None

    with patch("time.monotonic", return_value=t0 + 90):
        await _evaluate(ctrl, soc=96.0, export_w=2000)
    with patch("time.monotonic", return_value=t0 + 90 + SUSTAIN_SECONDS + 1):
        await _evaluate(ctrl, soc=96.0, export_w=2000)
    assert ctrl.is_heating is True


# ==================================================================
# Symmetric surplus-loss stop (after min_duration)
# ==================================================================


@pytest.mark.asyncio
async def test_surplus_loss_does_not_stop_during_min_duration():
    """No surplus + within min_duration → keep heating."""
    ctrl, hass = _make_controller()
    t0 = await _heat_via_charge(ctrl, hass)
    # last_changed is "now" → min_duration not elapsed.

    with patch("time.monotonic", return_value=t0 + 5):
        await _evaluate(
            ctrl, soc=85.0, export_w=0, charge_power_w=0,
            min_duration_s=15 * 60,
        )

    assert ctrl.is_heating is True
    hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_surplus_loss_stops_after_min_duration_and_sustain():
    """After min_duration elapsed, sustained loss of both rules → stop."""
    ctrl, hass = _make_controller()
    t0 = await _heat_via_charge(ctrl, hass)
    # Backdate physical switch turn-on past min_duration.
    hass.set_switch("on", seconds_ago=16 * 60)
    hass.services.async_call.reset_mock()

    # First tick with no surplus — arm.
    with patch("time.monotonic", return_value=t0 + 5):
        await _evaluate(
            ctrl, soc=85.0, export_w=0, charge_power_w=0,
            min_duration_s=15 * 60,
        )
    assert ctrl.is_heating is True
    assert ctrl._stop_armed_since is not None

    # After sustain seconds → stop.
    with patch("time.monotonic", return_value=t0 + 5 + SUSTAIN_SECONDS + 1):
        await _evaluate(
            ctrl, soc=85.0, export_w=0, charge_power_w=0,
            min_duration_s=15 * 60,
        )
    assert ctrl.is_heating is False
    hass.services.async_call.assert_any_call(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID},
    )


@pytest.mark.asyncio
async def test_surplus_returning_clears_stop_arm():
    """Brief loss then surplus comes back → no stop, arm cleared."""
    ctrl, hass = _make_controller()
    t0 = await _heat_via_charge(ctrl, hass)
    hass.set_switch("on", seconds_ago=16 * 60)
    hass.services.async_call.reset_mock()

    with patch("time.monotonic", return_value=t0 + 5):
        await _evaluate(
            ctrl, soc=85.0, export_w=0, charge_power_w=0,
            min_duration_s=15 * 60,
        )
    assert ctrl._stop_armed_since is not None

    with patch("time.monotonic", return_value=t0 + 15):
        await _evaluate(
            ctrl, soc=85.0, export_w=0,
            charge_power_w=600,  # rule2 re-fires
            min_duration_s=15 * 60,
        )
    assert ctrl.is_heating is True
    assert ctrl._stop_armed_since is None
    hass.services.async_call.assert_not_called()


# ==================================================================
# reconfigure
# ==================================================================


def test_reconfigure_updates_switch_id():
    ctrl, _ = _make_controller()
    ctrl.reconfigure("switch.new_heater")
    assert ctrl._switch_entity_id == "switch.new_heater"
