"""The Enabled switch is a real stand-down, not just a paused loop.

With ``switch.beemai_system_enabled`` off, BeemAI must send no commands
to the water heater or the EV charger — no amperage clamp, no 7 kW
overload trim, no start/stop rules — so the charger can be driven from
the Wallbox app without the integration fighting back.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.beem_ai.coordinator import BeemAICoordinator
from custom_components.beem_ai.ev_charger_controller import EvChargerController
from custom_components.beem_ai.water_heater_controller import (
    WaterHeaterController,
)


@pytest.fixture
def coordinator(mock_hass, state_store):
    entry = MagicMock()
    entry.data = {}
    entry.options = {}
    entry.entry_id = "test-entry"
    c = BeemAICoordinator(mock_hass, entry)
    c.state_store = state_store
    c._ev_charger = MagicMock()
    c._ev_charger.evaluate = AsyncMock()
    c._ev_charger.release_control = AsyncMock()
    c._ev_charger.handle_mode_change = AsyncMock()
    c._water_heater = MagicMock()
    c._water_heater.evaluate = AsyncMock()
    c._water_heater.release_control = AsyncMock()
    c._water_heater.handle_mode_change = AsyncMock()
    c._water_heater.force_stop_overload = AsyncMock()
    c._water_heater.is_heating = True
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


# ---- Stand-down on disable ---------------------------------------------


@pytest.mark.asyncio
async def test_disable_releases_device_control(coordinator):
    coordinator._overload_started_at = 1234.0

    await coordinator.async_set_enabled(False)

    assert coordinator.state_store.enabled is False
    assert coordinator._overload_started_at is None
    coordinator._ev_charger.release_control.assert_awaited_once()
    coordinator._water_heater.release_control.assert_awaited_once()


@pytest.mark.asyncio
async def test_enable_does_not_release(coordinator):
    coordinator.state_store.enabled = False

    await coordinator.async_set_enabled(True)

    assert coordinator.state_store.enabled is True
    coordinator._ev_charger.release_control.assert_not_called()


@pytest.mark.asyncio
async def test_set_enabled_is_idempotent(coordinator):
    """Already disabled — don't re-release on every switch write."""
    coordinator.state_store.enabled = False
    coordinator._ev_charger.release_control.reset_mock()

    await coordinator.async_set_enabled(False)

    coordinator._ev_charger.release_control.assert_not_called()


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


# ---- EV controller release semantics -----------------------------------


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
async def test_ev_release_never_stops_the_charger(mock_hass):
    """Disabling BeemAI mid-session must not cut the car off."""
    ev = _ev(mock_hass, switch_on=True, amps=6)
    ev._saved_amps = 32

    await ev.release_control()

    calls = [c.args[:2] for c in mock_hass.services.async_call.call_args_list]
    assert ("homeassistant", "turn_off") not in calls


@pytest.mark.asyncio
async def test_ev_release_restores_user_amps(mock_hass):
    ev = _ev(mock_hass, switch_on=True, amps=6)
    ev._saved_amps = 32

    await ev.release_control()

    mock_hass.services.async_call.assert_awaited_once_with(
        "number",
        "set_value",
        {"entity_id": "number.wallbox_amps", "value": 32},
    )
    assert ev._saved_amps is None
    assert ev._start_mode is None


@pytest.mark.asyncio
async def test_ev_release_without_saved_amps_touches_nothing(mock_hass):
    ev = _ev(mock_hass, switch_on=True, amps=10)

    await ev.release_control()

    mock_hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_ev_release_skips_redundant_set(mock_hass):
    ev = _ev(mock_hass, switch_on=True, amps=16)
    ev._saved_amps = 16

    await ev.release_control()

    mock_hass.services.async_call.assert_not_called()


# ---- Water heater release semantics ------------------------------------


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
async def test_wh_release_leaves_a_user_session_alone(mock_hass):
    wh = _wh(mock_hass, switch_on=True)
    wh._commanded_on = False

    await wh.release_control()

    mock_hass.services.async_call.assert_not_called()


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


# ---- Shutdown ----------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_while_disabled_touches_nothing(coordinator):
    """Unloading a disabled BeemAI has nothing left to hand back."""
    coordinator.state_store.enabled = False
    coordinator._ev_charger.is_charging = True
    coordinator._ev_charger.stop = AsyncMock()
    coordinator._water_heater._turn_off = AsyncMock()

    await coordinator.async_shutdown()

    coordinator._ev_charger.release_control.assert_not_called()
    coordinator._water_heater.release_control.assert_not_called()
    coordinator._ev_charger.stop.assert_not_called()
    coordinator._water_heater._turn_off.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_releases_rather_than_stops(coordinator):
    """A reload must not cut an in-flight charge — release, don't stop."""
    coordinator._ev_charger.is_charging = True
    coordinator._ev_charger.stop = AsyncMock()
    coordinator._water_heater._turn_off = AsyncMock()

    await coordinator.async_shutdown()

    coordinator._ev_charger.release_control.assert_awaited_once()
    coordinator._water_heater.release_control.assert_awaited_once()
    coordinator._ev_charger.stop.assert_not_called()
    coordinator._water_heater._turn_off.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_leaves_the_enabled_flag_alone(coordinator):
    """Releasing on unload is not a user disable — the switch keeps its
    state so the next start restores what the user chose."""
    await coordinator.async_shutdown()

    assert coordinator.state_store.enabled is True


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
