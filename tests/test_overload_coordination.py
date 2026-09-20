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

from custom_components.beem_ai.const import EV_MODE_FORCE
from custom_components.beem_ai.coordinator import (
    BeemAICoordinator,
    OVERLOAD_WH_FORCE_STOP_GRACE_S,
)


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
# EV charger in Force Charge suspends overload handling entirely
# ---------------------------------------------------------------------


def _force_charging_ev(coordinator):
    coordinator.ev_charger_mode = EV_MODE_FORCE
    coordinator._ev_charger = MagicMock()
    coordinator._ev_charger.is_charging = True
    coordinator._water_heater = MagicMock()
    coordinator._water_heater.is_heating = True
    coordinator._water_heater.force_stop_overload = AsyncMock()


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
    _force_charging_ev(coordinator)
    coordinator._ev_charger.is_charging = False

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
