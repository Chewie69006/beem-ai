"""The Enabled switch is a master off.

Turning ``switch.beemai_system_enabled`` off stops everything BeemAI
drives and then issues no further commands at all — that's what makes it
usable as an override: stop, then start the charge yourself from the
Wallbox app with nothing clamping amps or enforcing the 7 kW ceiling.

Everything else is *not* a master off.  An options change or an
integration reload must leave a running session alone and let the next
MQTT tick judge it against the current thresholds.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.beem_ai.const import (
    OPT_EV_CHARGER_POWER,
    OPT_EV_CHARGER_TOGGLE,
    OPT_EV_TARGET_SOC,
    OPT_WATER_HEATER_SWITCH,
    OPT_WH_SOC_THRESHOLD,
)
from custom_components.beem_ai.coordinator import BeemAICoordinator
from custom_components.beem_ai.ev_charger_controller import EvChargerController
from custom_components.beem_ai.water_heater_controller import (
    WaterHeaterController,
)


def _controller_mock(**extra):
    m = MagicMock()
    for name in ("evaluate", "stop", "release_control", "handle_mode_change"):
        setattr(m, name, AsyncMock())
    for name, value in extra.items():
        setattr(m, name, value)
    return m


@pytest.fixture
def coordinator(mock_hass, state_store):
    entry = MagicMock()
    entry.data = {}
    entry.options = {}
    entry.entry_id = "test-entry"
    c = BeemAICoordinator(mock_hass, entry)
    c.state_store = state_store
    c._ev_charger = _controller_mock(
        is_charging=True, entity_ids=("switch.wallbox", "number.amps", None),
    )
    c._water_heater = _controller_mock(
        is_heating=True, switch_entity_id="switch.water_heater",
    )
    c._water_heater.force_stop_overload = AsyncMock()
    return c


# ---- Diverter gating ----------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_skips_both_diverters(coordinator):
    coordinator.state_store.enabled = False

    await coordinator._evaluate_surplus_diverters(soc=90.0, export_w=2000.0)

    coordinator._water_heater.evaluate.assert_not_called()
    coordinator._ev_charger.evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_skips_overload_handling(coordinator):
    """The 7 kW rule is the one the user hits hardest — it must not run."""
    coordinator.state_store.enabled = False

    with patch.object(
        coordinator, "_handle_overload", new=AsyncMock()
    ) as handle:
        await coordinator._evaluate_surplus_diverters(
            soc=90.0, export_w=0.0,
        )

    handle.assert_not_called()


@pytest.mark.asyncio
async def test_enabled_evaluates_diverters(coordinator):
    coordinator.state_store.enabled = True

    await coordinator._evaluate_surplus_diverters(soc=90.0, export_w=2000.0)

    coordinator._water_heater.evaluate.assert_called_once()
    coordinator._ev_charger.evaluate.assert_called_once()


# ---- Master off ---------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_stops_everything(coordinator):
    coordinator._overload_started_at = 1234.0

    await coordinator.async_set_enabled(False)

    assert coordinator.state_store.enabled is False
    assert coordinator._overload_started_at is None
    coordinator._ev_charger.stop.assert_awaited_once()
    coordinator._water_heater.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_disable_stops_the_charge_whoever_started_it(coordinator):
    """No exception for a session BeemAI started itself, nor for one it
    adopted — the switch means stop."""
    coordinator._ev_charger.stop = AsyncMock()

    await coordinator.async_set_enabled(False)

    coordinator._ev_charger.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_enable_does_not_command_anything(coordinator):
    coordinator.state_store.enabled = False

    await coordinator.async_set_enabled(True)

    assert coordinator.state_store.enabled is True
    coordinator._ev_charger.stop.assert_not_called()
    coordinator._water_heater.stop.assert_not_called()


@pytest.mark.asyncio
async def test_set_enabled_is_idempotent(coordinator):
    """Already disabled — don't re-issue stops on every switch write."""
    coordinator.state_store.enabled = False

    await coordinator.async_set_enabled(False)

    coordinator._ev_charger.stop.assert_not_called()


# ---- Mode changes are inert while disabled -----------------------------


@pytest.mark.asyncio
async def test_ev_mode_change_inert_while_disabled(coordinator):
    coordinator.state_store.enabled = False

    await coordinator.async_set_ev_charger_mode("Manual")

    assert coordinator.ev_charger_mode == "Manual"
    coordinator._ev_charger.handle_mode_change.assert_not_called()


@pytest.mark.asyncio
async def test_wh_mode_change_inert_while_disabled(coordinator):
    coordinator.state_store.enabled = False

    await coordinator.async_set_water_heater_mode("Manual")

    assert coordinator.water_heater_mode == "Manual"
    coordinator._water_heater.handle_mode_change.assert_not_called()


@pytest.mark.asyncio
async def test_ev_mode_change_applies_when_enabled(coordinator):
    await coordinator.async_set_ev_charger_mode("Manual")

    coordinator._ev_charger.handle_mode_change.assert_awaited_once_with(
        "Manual"
    )


# ---- An options change must not disturb a running session --------------


OPTIONS = {
    OPT_EV_CHARGER_TOGGLE: "switch.wallbox",
    OPT_EV_CHARGER_POWER: "number.amps",
    OPT_WATER_HEATER_SWITCH: "switch.water_heater",
}


@pytest.mark.asyncio
async def test_threshold_change_keeps_the_same_controllers(coordinator):
    """Editing a threshold used to recreate the EV controller, dropping
    _saved_amps and _start_mode mid-charge."""
    ev, wh = coordinator._ev_charger, coordinator._water_heater

    coordinator._setup_ev_charger(dict(OPTIONS, **{OPT_EV_TARGET_SOC: 80.0}))
    coordinator._setup_water_heater(dict(OPTIONS, **{OPT_WH_SOC_THRESHOLD: 80.0}))

    assert coordinator._ev_charger is ev
    assert coordinator._water_heater is wh
    ev.reconfigure.assert_not_called()
    wh.reconfigure.assert_not_called()


@pytest.mark.asyncio
async def test_entity_change_reconfigures_in_place(coordinator):
    ev, wh = coordinator._ev_charger, coordinator._water_heater

    coordinator._setup_ev_charger(
        dict(OPTIONS, **{OPT_EV_CHARGER_POWER: "number.other_amps"})
    )
    coordinator._setup_water_heater(
        dict(OPTIONS, **{OPT_WATER_HEATER_SWITCH: "switch.other"})
    )

    assert coordinator._ev_charger is ev
    assert coordinator._water_heater is wh
    ev.reconfigure.assert_called_once_with(
        "switch.wallbox", "number.other_amps", None,
    )
    wh.reconfigure.assert_called_once_with("switch.other")


@pytest.mark.asyncio
async def test_clearing_the_entities_drops_the_controller(coordinator):
    coordinator._setup_ev_charger({})
    coordinator._setup_water_heater({})

    assert coordinator._ev_charger is None
    assert coordinator._water_heater is None


@pytest.mark.asyncio
async def test_options_update_never_stops_a_session(coordinator):
    await coordinator.async_options_updated(dict(OPTIONS, **{OPT_EV_TARGET_SOC: 80.0}))

    coordinator._ev_charger.stop.assert_not_called()
    coordinator._water_heater.stop.assert_not_called()
    coordinator._ev_charger.release_control.assert_not_called()
    coordinator._water_heater.release_control.assert_not_called()


@pytest.mark.asyncio
async def test_new_threshold_reaches_the_next_tick(coordinator):
    """The tick is where a changed value is judged — evaluate() gets the
    new number, rather than the session being cut on the spot."""
    await coordinator.async_options_updated(dict(OPTIONS, **{OPT_EV_TARGET_SOC: 80.0}))

    await coordinator._evaluate_surplus_diverters(soc=90.0, export_w=0.0)

    kwargs = coordinator._ev_charger.evaluate.call_args.kwargs
    assert kwargs["target_soc"] == 80.0


# ---- Shutdown -----------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_leaves_the_charger_strictly_alone(coordinator):
    """A reload must not cut the charge — nor bump it back to 32 A during
    the restart window, which is how you trip the breaker unattended."""
    await coordinator.async_shutdown()

    coordinator._ev_charger.stop.assert_not_called()
    coordinator._ev_charger.release_control.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_releases_the_water_heater(coordinator):
    """HA may not come back — don't leave an immersion heater we started
    running unsupervised."""
    await coordinator.async_shutdown()

    coordinator._water_heater.release_control.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_while_disabled_touches_nothing(coordinator):
    coordinator.state_store.enabled = False

    await coordinator.async_shutdown()

    coordinator._water_heater.release_control.assert_not_called()
    coordinator._ev_charger.stop.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_leaves_the_enabled_flag_alone(coordinator):
    """Unloading is not a user disable — the switch keeps its state so
    the next start restores what the user chose."""
    await coordinator.async_shutdown()

    assert coordinator.state_store.enabled is True


# ---- EV controller: stop restores the user's amperage ------------------


def _ev(mock_hass, switch_on: bool, amps: int):
    def get_state(entity_id):
        state = MagicMock()
        if entity_id == "switch.wallbox":
            state.state = "on" if switch_on else "off"
        else:
            state.state = str(amps)
        return state

    mock_hass.states.get.side_effect = get_state
    return EvChargerController(
        hass=mock_hass,
        toggle_entity_id="switch.wallbox",
        power_entity_id="number.wallbox_amps",
    )


@pytest.mark.asyncio
async def test_ev_stop_turns_off_and_restores_amps(mock_hass):
    ev = _ev(mock_hass, switch_on=True, amps=6)
    ev._saved_amps = 32

    await ev.stop()

    calls = [
        (c.args[0], c.args[1], c.args[2])
        for c in mock_hass.services.async_call.call_args_list
    ]
    assert (
        "homeassistant", "turn_off", {"entity_id": "switch.wallbox"},
    ) in calls
    assert (
        "number", "set_value",
        {"entity_id": "number.wallbox_amps", "value": 32},
    ) in calls
    assert ev._saved_amps is None
    assert ev._start_mode is None


@pytest.mark.asyncio
async def test_ev_entity_ids_round_trip(mock_hass):
    ev = _ev(mock_hass, switch_on=False, amps=16)

    assert ev.entity_ids == ("switch.wallbox", "number.wallbox_amps", None)

    ev.reconfigure("switch.a", "number.b", "sensor.c")

    assert ev.entity_ids == ("switch.a", "number.b", "sensor.c")


# ---- Water heater release on unload ------------------------------------


def _wh(mock_hass, switch_on: bool):
    state = MagicMock()
    state.state = "on" if switch_on else "off"
    mock_hass.states.get.return_value = state
    return WaterHeaterController(
        hass=mock_hass, switch_entity_id="switch.water_heater",
    )


@pytest.mark.asyncio
async def test_wh_release_stops_a_session_beemai_started(mock_hass):
    wh = _wh(mock_hass, switch_on=True)
    wh._commanded_on = True

    await wh.release_control()

    mock_hass.services.async_call.assert_awaited_once_with(
        "homeassistant",
        "turn_off",
        {"entity_id": "switch.water_heater"},
    )


@pytest.mark.asyncio
async def test_wh_release_leaves_an_external_session_alone(mock_hass):
    wh = _wh(mock_hass, switch_on=True)
    wh._commanded_on = False

    await wh.release_control()

    mock_hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_wh_stop_does_not_care_who_started_it(mock_hass):
    """The master off has no such exemption."""
    wh = _wh(mock_hass, switch_on=True)
    wh._commanded_on = False

    await wh.stop()

    mock_hass.services.async_call.assert_awaited_once_with(
        "homeassistant",
        "turn_off",
        {"entity_id": "switch.water_heater"},
    )


@pytest.mark.asyncio
async def test_wh_commanded_on_cleared_by_external_off(mock_hass):
    wh = _wh(mock_hass, switch_on=True)
    await wh._turn_on()
    assert wh._commanded_on is True

    # Switch goes off behind our back (plug timer, manual flip).
    mock_hass.states.get.return_value.state = "off"
    await wh.evaluate(
        soc=90.0,
        export_w=0.0,
        charge_power_w=0.0,
        consumption_w=1000.0,
        import_w=0.0,
        soc_threshold=95.0,
        charge_power_threshold=500.0,
    )

    assert wh._commanded_on is False


# ---- Enabled switch restores across reloads ----------------------------


@pytest.mark.asyncio
async def test_switch_restores_disabled_state(coordinator):
    """A config entry reload must not silently put BeemAI back in charge."""
    from custom_components.beem_ai.switch import BeemAIEnabledSwitch

    entry = MagicMock()
    entry.entry_id = "test-entry"
    switch = BeemAIEnabledSwitch(coordinator, entry)
    last = MagicMock()
    last.state = "off"
    switch.async_get_last_state = AsyncMock(return_value=last)

    await switch.async_added_to_hass()

    assert coordinator.state_store.enabled is False
    assert switch.is_on is False


@pytest.mark.asyncio
async def test_switch_without_previous_state_stays_enabled(coordinator):
    from custom_components.beem_ai.switch import BeemAIEnabledSwitch

    entry = MagicMock()
    entry.entry_id = "test-entry"
    switch = BeemAIEnabledSwitch(coordinator, entry)
    switch.async_get_last_state = AsyncMock(return_value=None)

    await switch.async_added_to_hass()

    assert coordinator.state_store.enabled is True
