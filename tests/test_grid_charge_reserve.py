"""Coordinator: after a grid charge, the diverters only get solar surplus.

Replays the rainy-day morning: the battery was charged from the grid
overnight to 70 %, then a weak sun trickles 600 W into it.  Before, the
water heater's SoC rule (SoC ≥ threshold + battery charging) started the
heater on that trickle and drained the grid-bought energy.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.beem_ai.coordinator import BeemAICoordinator
from custom_components.beem_ai.grid_charge_tracker import (
    GRID_CHARGE_SUSTAIN_S,
    RESERVED_MIN_SOC,
)
from custom_components.beem_ai.water_heater_controller import (
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
    c.wh_soc_threshold = 50.0
    c.wh_charge_power_threshold = 500.0
    return c


async def _tick(coordinator, t, **battery):
    """One MQTT update through the real callback, diverters awaited."""
    coordinator.state_store.update_battery(**battery)
    pending = []
    coordinator.hass.async_create_task = pending.append
    with patch("time.monotonic", return_value=t):
        coordinator._on_battery_update()
        for coro in pending:
            await coro


async def _night_grid_charge(coordinator, t0=0.0):
    """Off-peak: 2.5 kW from the grid into the battery, up to 70 %."""
    t = t0
    while t <= t0 + GRID_CHARGE_SUSTAIN_S + 60:
        await _tick(
            coordinator, t,
            soc=70.0, solar_power_w=0.0,
            battery_power_w=2500.0, meter_power_w=2900.0,
        )
        t += 10
    return t


def _water_heater(coordinator):
    hass = FakeHass()
    coordinator._water_heater = WaterHeaterController(hass, SWITCH_ID)
    coordinator.water_heater_mode = "Auto"
    return coordinator._water_heater


async def _rainy_morning(coordinator, t0):
    """1 kW of sun, 400 W of house: 600 W into the battery, no import."""
    for dt in range(0, 600, 10):
        await _tick(
            coordinator, t0 + dt,
            soc=70.0, solar_power_w=1000.0,
            battery_power_w=600.0, meter_power_w=0.0,
        )


@pytest.mark.asyncio
async def test_heater_stays_off_on_the_morning_after_a_grid_charge(
    coordinator,
):
    t = await _night_grid_charge(coordinator)
    assert coordinator.grid_charge.is_reserved()

    wh = _water_heater(coordinator)
    await _rainy_morning(coordinator, t + 3600)

    assert wh.is_heating is False


@pytest.mark.asyncio
async def test_same_morning_without_grid_charge_heats(coordinator):
    """Control: the SoC rule itself still works on a solar-filled battery."""
    wh = _water_heater(coordinator)
    await _rainy_morning(coordinator, 1000.0)

    assert wh.is_heating is True


@pytest.mark.asyncio
async def test_grid_charge_is_persisted(coordinator):
    """Confirmed, then ended: saved both times (off the event loop)."""
    t = await _night_grid_charge(coordinator)
    await _tick(
        coordinator, t,
        soc=70.0, solar_power_w=0.0,
        battery_power_w=0.0, meter_power_w=400.0,
    )

    saves = [
        c for c in coordinator.hass.async_add_executor_job.call_args_list
        if c.args == (coordinator.grid_charge.save,)
    ]
    assert len(saves) == 2


def _ev_mock(coordinator):
    ev = MagicMock()
    ev.evaluate = AsyncMock()
    ev.is_hands_off = MagicMock(return_value=False)
    coordinator._ev_charger = ev
    coordinator.ev_target_soc = 60.0
    return ev


@pytest.mark.asyncio
async def test_ev_target_raised_while_reserved(coordinator):
    ev = _ev_mock(coordinator)
    coordinator.grid_charge._last_charge_at = (
        datetime.now(timezone.utc) - timedelta(hours=3)
    )

    await coordinator._evaluate_surplus_diverters(soc=70.0, export_w=0.0)

    assert ev.evaluate.await_args.kwargs["target_soc"] == RESERVED_MIN_SOC


@pytest.mark.asyncio
async def test_ev_target_untouched_without_grid_charge(coordinator):
    ev = _ev_mock(coordinator)

    await coordinator._evaluate_surplus_diverters(soc=70.0, export_w=0.0)

    assert ev.evaluate.await_args.kwargs["target_soc"] == 60.0


@pytest.mark.asyncio
async def test_ev_target_above_reserve_level_is_kept(coordinator):
    ev = _ev_mock(coordinator)
    coordinator.ev_target_soc = 98.0
    coordinator.grid_charge._last_charge_at = datetime.now(timezone.utc)

    await coordinator._evaluate_surplus_diverters(soc=70.0, export_w=0.0)

    assert ev.evaluate.await_args.kwargs["target_soc"] == 98.0
