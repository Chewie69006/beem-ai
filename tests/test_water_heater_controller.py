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
    EXPORT_SOC_THRESHOLD,
    HYSTERESIS_PCT,
    MAX_CONSUMPTION_W,
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
# Overload protection: consumption >= 7kW AND importing
# ==================================================================


@pytest.mark.asyncio
async def test_overload_stops_when_importing():
    ctrl, hass = _make_controller()
    t = await _heat_via_export(ctrl, hass)

    with patch("time.monotonic", return_value=t + 60):
        await _evaluate(ctrl, soc=92.0, consumption_w=8000, import_w=1000)

    assert ctrl.is_heating is False
    hass.services.async_call.assert_called_once_with(
        "homeassistant", "turn_off", {"entity_id": SWITCH_ID}
    )


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
async def test_overload_overrides_min_duration():
    """Overload (≥7kW + importing) fires even during min duration."""
    ctrl, hass = _make_controller()
    await _heat_via_charge(ctrl, hass)
    # Switch turned on now — min duration not elapsed

    await _evaluate(
        ctrl, soc=81.0,
        consumption_w=MAX_CONSUMPTION_W + 100,
        import_w=500,
        min_duration_s=30 * 60,
    )

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
# reconfigure
# ==================================================================


def test_reconfigure_updates_switch_id():
    ctrl, _ = _make_controller()
    ctrl.reconfigure("switch.new_heater")
    assert ctrl._switch_entity_id == "switch.new_heater"
