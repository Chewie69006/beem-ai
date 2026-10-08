"""Coordinator-level overload coordination.

The water-heater controller no longer reacts to consumption on its own.
When the household crosses ``OVERLOAD_THRESHOLD_W`` with positive
import, the coordinator first lets the EV charger trim its amps
(handled inside ``EvChargerController.evaluate``).  If consumption is
still over the threshold ``OVERLOAD_WH_FORCE_STOP_GRACE_S`` later, the
coordinator force-stops the water heater — bypassing its min-duration
floor.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.beem_ai.const import (
    EV_MODE_AUTO,
    EV_MODE_FORCE,
    EV_MODE_MANUAL,
)
from custom_components.beem_ai.coordinator import (
    BeemAICoordinator,
    OVERLOAD_WH_FORCE_STOP_GRACE_S,
)
from custom_components.beem_ai.ev_charger_controller import (
    EvChargerController,
    StartMode,
)
from custom_components.beem_ai.water_heater_controller import (
    COOLDOWN_AFTER_EXTERNAL_OFF_S,
    WaterHeaterController,
)

from .test_water_heater_controller import SWITCH_ID, FakeHass


@pytest.fixture
def coordinator(mock_hass, state_store):
    entry = MagicMock()
    entry.data = {}
    entry.options = {}
    entry.entry_id = "test-entry"
    c = BeemAICoordinator(mock_hass, entry)
    c.state_store = state_store
    return c


@pytest.mark.asyncio
async def test_no_overload_clears_timer(coordinator):
    coordinator._overload_started_at = 1000.0
    await coordinator._handle_overload(consumption_w=3000.0, import_w=0.0)
    assert coordinator._overload_started_at is None


@pytest.mark.asyncio
async def test_overload_below_threshold_no_action(coordinator):
    await coordinator._handle_overload(consumption_w=6900.0, import_w=500.0)
    assert coordinator._overload_started_at is None


@pytest.mark.asyncio
async def test_overload_first_tick_arms_timer_only(coordinator):
    """The first overloaded tick just records the timestamp — the EV
    controller's own evaluate() throttles amps in the same cycle."""
    coordinator._water_heater = MagicMock()
    coordinator._water_heater.is_heating = True
    coordinator._water_heater.force_stop_overload = AsyncMock()

    with patch("time.monotonic", return_value=500.0):
        await coordinator._handle_overload(
            consumption_w=8000.0, import_w=1500.0,
        )

    assert coordinator._overload_started_at == 500.0
    coordinator._water_heater.force_stop_overload.assert_not_called()


@pytest.mark.asyncio
async def test_overload_within_grace_no_force_stop(coordinator):
    coordinator._water_heater = MagicMock()
    coordinator._water_heater.is_heating = True
    coordinator._water_heater.force_stop_overload = AsyncMock()
    coordinator._overload_started_at = 500.0

    with patch(
        "time.monotonic",
        return_value=500.0 + OVERLOAD_WH_FORCE_STOP_GRACE_S - 1,
    ):
        await coordinator._handle_overload(
            consumption_w=8000.0, import_w=1500.0,
        )

    coordinator._water_heater.force_stop_overload.assert_not_called()


@pytest.mark.asyncio
async def test_overload_past_grace_force_stops_wh(coordinator):
    coordinator._water_heater = MagicMock()
    coordinator._water_heater.is_heating = True
    coordinator._water_heater.force_stop_overload = AsyncMock()
    coordinator._overload_started_at = 500.0

    with patch(
        "time.monotonic",
        return_value=500.0 + OVERLOAD_WH_FORCE_STOP_GRACE_S + 1,
    ):
        await coordinator._handle_overload(
            consumption_w=8000.0, import_w=1500.0,
        )

    coordinator._water_heater.force_stop_overload.assert_awaited_once_with(
        8000.0,
    )


@pytest.mark.asyncio
async def test_overload_past_grace_skips_wh_when_not_heating(coordinator):
    coordinator._water_heater = MagicMock()
    coordinator._water_heater.is_heating = False
    coordinator._water_heater.force_stop_overload = AsyncMock()
    coordinator._overload_started_at = 500.0

    with patch(
        "time.monotonic",
        return_value=500.0 + OVERLOAD_WH_FORCE_STOP_GRACE_S + 1,
    ):
        await coordinator._handle_overload(
            consumption_w=8000.0, import_w=1500.0,
        )

    coordinator._water_heater.force_stop_overload.assert_not_called()


@pytest.mark.asyncio
async def test_overload_requires_positive_import(coordinator):
    """High consumption fully covered by solar (no import) is not an
    overload — exporting means the breaker isn't being pushed."""
    await coordinator._handle_overload(
        consumption_w=8000.0, import_w=0.0,
    )
    assert coordinator._overload_started_at is None


# ---------------------------------------------------------------------
# EV charging hands-off at full power (Force Charge, or a Wallbox
# schedule session in Auto) suspends overload handling entirely
# ---------------------------------------------------------------------


def _charging_ev(coordinator, mode, start_mode=None):
    """A real EV controller on a charging Wallbox, plus a heating WH.

    Returns the dict backing the entity states, so a test can flip the
    switch off.
    """
    states = {"switch.wallbox": "on", "number.wallbox_amps": "32"}

    def get_state(entity_id):
        if entity_id not in states:
            return None
        state = MagicMock()
        state.state = states[entity_id]
        return state

    coordinator.hass.states.get.side_effect = get_state
    coordinator.ev_charger_mode = mode
    coordinator._ev_charger = EvChargerController(
        hass=coordinator.hass,
        toggle_entity_id="switch.wallbox",
        power_entity_id="number.wallbox_amps",
    )
    coordinator._ev_charger._start_mode = start_mode
    coordinator._water_heater = MagicMock()
    coordinator._water_heater.is_heating = True
    coordinator._water_heater.force_stop_overload = AsyncMock()
    return states


def _force_charging_ev(coordinator):
    return _charging_ev(coordinator, EV_MODE_FORCE, StartMode.FORCE)


async def _overload_past_grace(coordinator):
    with patch("time.monotonic", return_value=500.0):
        await coordinator._handle_overload(
            consumption_w=9000.0, import_w=2000.0,
        )
    with patch("time.monotonic",
               return_value=500.0 + OVERLOAD_WH_FORCE_STOP_GRACE_S + 10):
        await coordinator._handle_overload(
            consumption_w=9000.0, import_w=2000.0,
        )


@pytest.mark.asyncio
async def test_force_charge_never_stops_water_heater(coordinator):
    """Force accepts going over the limit, so the WH is left alone."""
    _force_charging_ev(coordinator)

    with patch("time.monotonic", return_value=500.0):
        await coordinator._handle_overload(
            consumption_w=9000.0, import_w=2000.0,
        )
    with patch("time.monotonic",
               return_value=500.0 + OVERLOAD_WH_FORCE_STOP_GRACE_S + 10):
        await coordinator._handle_overload(
            consumption_w=9000.0, import_w=2000.0,
        )

    assert coordinator._overload_started_at is None
    coordinator._water_heater.force_stop_overload.assert_not_called()


@pytest.mark.asyncio
async def test_force_charge_resets_armed_timer(coordinator):
    """An overload armed before Force started is dropped, so leaving
    Force gives the grace window a fresh start."""
    _force_charging_ev(coordinator)
    coordinator._overload_started_at = 100.0

    await coordinator._handle_overload(consumption_w=9000.0, import_w=2000.0)

    assert coordinator._overload_started_at is None
    coordinator._water_heater.force_stop_overload.assert_not_called()


@pytest.mark.asyncio
async def test_force_mode_but_ev_not_charging_still_protects(coordinator):
    """Force selected while the charger is off is not a free pass."""
    states = _force_charging_ev(coordinator)
    states["switch.wallbox"] = "off"

    with patch("time.monotonic", return_value=500.0):
        await coordinator._handle_overload(
            consumption_w=9000.0, import_w=2000.0,
        )
    with patch("time.monotonic",
               return_value=500.0 + OVERLOAD_WH_FORCE_STOP_GRACE_S + 10):
        await coordinator._handle_overload(
            consumption_w=9000.0, import_w=2000.0,
        )

    coordinator._water_heater.force_stop_overload.assert_called_once()


@pytest.mark.asyncio
async def test_schedule_session_never_stops_water_heater(coordinator):
    """A charge the Wallbox schedule started runs at 32A like Force —
    the 7 kW rule is suspended for it too."""
    _charging_ev(coordinator, EV_MODE_AUTO, StartMode.SCHEDULE)

    await _overload_past_grace(coordinator)

    assert coordinator._overload_started_at is None
    coordinator._water_heater.force_stop_overload.assert_not_called()


@pytest.mark.asyncio
async def test_auto_piloted_session_still_protects(coordinator):
    """A session BeemAI pilots in Auto keeps the overload rule."""
    _charging_ev(coordinator, EV_MODE_AUTO, StartMode.AUTO)

    await _overload_past_grace(coordinator)

    coordinator._water_heater.force_stop_overload.assert_called_once()


@pytest.mark.asyncio
async def test_schedule_session_under_manual_mode_protects(coordinator):
    """Switching to Manual hands the session back to BeemAI's piloting,
    overload rule included."""
    _charging_ev(coordinator, EV_MODE_MANUAL, StartMode.SCHEDULE)

    await _overload_past_grace(coordinator)

    coordinator._water_heater.force_stop_overload.assert_called_once()


def test_wh_offpeak_only_after_start_delay(coordinator):
    """Off-peak top-up flag: 5 min into the cheapest period, not before."""
    from datetime import datetime

    from custom_components.beem_ai.tariff_manager import TariffManager

    assert coordinator._wh_offpeak() is False  # no tariff configured

    coordinator._tariff = TariffManager(0.25, [
        {"label": "HC", "start": "21:26", "end": "05:26", "price": 0.15},
    ])
    cases = {
        (21, 20): False, (21, 30): False, (21, 31): True,
        (0, 2): True, (5, 25): True, (5, 26): False,
    }
    for (h, m), expected in cases.items():
        with patch(
            "custom_components.beem_ai.coordinator.datetime"
        ) as dt:
            dt.now.return_value = datetime(2026, 10, 1, h, m)
            assert coordinator._wh_offpeak() is expected, (h, m)


@pytest.mark.asyncio
async def test_force_stop_on_slow_plug_keeps_offpeak_window_open(coordinator):
    """7 Oct, 22:07 replayed through the real tick: the overload handler
    force-stops the heater, then ``evaluate()`` runs in the same tick
    while the plug still reads "on".  The top-up must resume after the
    cooldown instead of staying locked until the end of the window."""
    hass = FakeHass()
    wh = WaterHeaterController(hass, SWITCH_ID)
    coordinator._water_heater = wh
    coordinator.water_heater_mode = "Auto"
    battery = coordinator.state_store.battery

    async def tick(t, meter_w):
        battery.meter_power_w = meter_w
        with patch("time.monotonic", return_value=t), patch.object(
            coordinator, "_wh_offpeak", return_value=True
        ):
            await coordinator._evaluate_surplus_diverters(
                soc=9.0, export_w=0.0,
            )

    await tick(1000.0, 700.0)
    assert wh.is_heating

    hass.lag = True
    await tick(1200.0, 7688.0)  # the car starts charging: overload armed
    await tick(1200.0 + OVERLOAD_WH_FORCE_STOP_GRACE_S, 9451.0)
    hass.flush()
    await tick(1220.0, 7570.0)
    assert not wh.is_heating
    assert not wh._offpeak_done

    # The car is done; the cooldown armed by the force-stop has expired.
    await tick(
        1200.0 + OVERLOAD_WH_FORCE_STOP_GRACE_S
        + COOLDOWN_AFTER_EXTERNAL_OFF_S,
        700.0,
    )
    hass.flush()
    assert wh.is_heating
