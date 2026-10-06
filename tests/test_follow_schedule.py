"""The "Follow Wallbox Schedule" switch (System device).

On (default): EV Auto leaves a Wallbox-scheduled charge alone at 32 A and
hands the charger back to its schedule after BeemAI's own stops.  Off:
Auto pilots every session it finds running, as before.  The value lives
in the config entry options, like the mode selects.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.beem_ai.const import (
    OPT_EV_CHARGER_POWER,
    OPT_EV_CHARGER_TOGGLE,
    OPT_EV_FOLLOW_SCHEDULE,
)
from custom_components.beem_ai.coordinator import BeemAICoordinator
from custom_components.beem_ai.switch import BeemAIEvFollowScheduleSwitch

OPTIONS = {
    OPT_EV_CHARGER_TOGGLE: "switch.wallbox",
    OPT_EV_CHARGER_POWER: "number.amps",
}


@pytest.fixture
def coordinator(mock_hass, state_store):
    entry = MagicMock()
    entry.data = {}
    entry.options = {}
    entry.entry_id = "test-entry"
    c = BeemAICoordinator(mock_hass, entry)
    c.state_store = state_store
    c.async_update_listeners = MagicMock()
    c._setup_ev_charger(OPTIONS)
    c._ev_charger.resume_schedule_if_idle = AsyncMock()
    return c


# ---- Coordinator ------------------------------------------------------


def test_follows_schedule_by_default(coordinator):
    assert coordinator.ev_follow_schedule is True
    assert coordinator._ev_charger.follow_schedule is True


def test_options_update_assigns_the_flag_in_place(coordinator):
    ev = coordinator._ev_charger
    ev._start_mode = "kept"

    coordinator._setup_ev_charger(dict(OPTIONS, **{OPT_EV_FOLLOW_SCHEDULE: False}))

    assert coordinator._ev_charger is ev
    assert ev.follow_schedule is False
    assert coordinator.ev_follow_schedule is False
    assert ev._start_mode == "kept"  # not a reconfigure


def test_new_controller_gets_the_stored_flag(mock_hass, state_store):
    entry = MagicMock()
    entry.data = {}
    entry.options = {}
    entry.entry_id = "test-entry"
    c = BeemAICoordinator(mock_hass, entry)

    c._setup_ev_charger(dict(OPTIONS, **{OPT_EV_FOLLOW_SCHEDULE: False}))

    assert c._ev_charger.follow_schedule is False


@pytest.mark.asyncio
async def test_turning_off_sends_nothing(coordinator):
    await coordinator.async_set_ev_follow_schedule(False)

    assert coordinator._ev_charger.follow_schedule is False
    coordinator._ev_charger.resume_schedule_if_idle.assert_not_called()


@pytest.mark.asyncio
async def test_turning_on_in_auto_hands_the_charger_back(coordinator):
    coordinator._ev_charger.follow_schedule = False
    coordinator.ev_charger_mode = "Auto"

    await coordinator.async_set_ev_follow_schedule(True)

    assert coordinator._ev_charger.follow_schedule is True
    coordinator._ev_charger.resume_schedule_if_idle.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["Disabled", "Manual", "Force Charge"])
async def test_turning_on_outside_auto_sends_nothing(coordinator, mode):
    coordinator.ev_charger_mode = mode

    await coordinator.async_set_ev_follow_schedule(True)

    coordinator._ev_charger.resume_schedule_if_idle.assert_not_called()


@pytest.mark.asyncio
async def test_turning_on_while_disabled_sends_nothing(coordinator):
    """The Enabled switch gates every outbound command."""
    coordinator.state_store.enabled = False
    coordinator.ev_charger_mode = "Auto"

    await coordinator.async_set_ev_follow_schedule(True)

    assert coordinator._ev_charger.follow_schedule is True
    coordinator._ev_charger.resume_schedule_if_idle.assert_not_called()


@pytest.mark.asyncio
async def test_without_ev_charger_is_a_noop(coordinator):
    coordinator._ev_charger = None

    await coordinator.async_set_ev_follow_schedule(False)

    assert coordinator.ev_follow_schedule is True  # options default


# ---- Switch entity ----------------------------------------------------


def _switch(coordinator, options=None):
    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.options = options or {}
    sw = BeemAIEvFollowScheduleSwitch(coordinator, entry)
    sw.hass = MagicMock()
    sw.async_write_ha_state = MagicMock()
    return sw, entry


def test_switch_reads_the_coordinator(coordinator):
    sw, _ = _switch(coordinator)

    assert sw._attr_unique_id == "test-entry_ev_follow_schedule"
    assert sw.available is True
    assert sw.is_on is True

    coordinator._ev_charger.follow_schedule = False
    assert sw.is_on is False


def test_switch_unavailable_without_charger(coordinator):
    sw, _ = _switch(coordinator)
    coordinator._ev_charger = None

    assert sw.available is False


@pytest.mark.asyncio
async def test_switch_off_applies_and_persists(coordinator):
    sw, entry = _switch(coordinator, options={"ev_charger_mode": "Auto"})

    await sw.async_turn_off()

    assert coordinator._ev_charger.follow_schedule is False
    sw.hass.config_entries.async_update_entry.assert_called_once_with(
        entry,
        options={"ev_charger_mode": "Auto", OPT_EV_FOLLOW_SCHEDULE: False},
    )
    sw.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_switch_on_applies_and_persists(coordinator):
    coordinator._ev_charger.follow_schedule = False
    sw, entry = _switch(coordinator)

    await sw.async_turn_on()

    assert coordinator._ev_charger.follow_schedule is True
    sw.hass.config_entries.async_update_entry.assert_called_once_with(
        entry, options={OPT_EV_FOLLOW_SCHEDULE: True},
    )


@pytest.mark.asyncio
async def test_switch_only_created_with_an_ev_charger(coordinator):
    from custom_components.beem_ai import switch as switch_platform

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test-entry"
    hass.data = {"beem_ai": {"test-entry": coordinator}}

    added = MagicMock()
    await switch_platform.async_setup_entry(hass, entry, added)
    names = [type(e).__name__ for e in added.call_args.args[0]]
    assert "BeemAIEvFollowScheduleSwitch" in names

    coordinator._ev_charger = None
    added = MagicMock()
    await switch_platform.async_setup_entry(hass, entry, added)
    names = [type(e).__name__ for e in added.call_args.args[0]]
    assert "BeemAIEvFollowScheduleSwitch" not in names
