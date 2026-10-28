"""Coordinator: after a grid charge, the reserve holds back the EV only.

Replays 28/10: the battery charged from the grid overnight (9 → 50 %),
then at 10:40 the sun put 2.5 kW into it at SoC 55 %.  The water
heater's SoC rule (SoC ≥ threshold + solar charge, no import) must start
it — a solar surplus goes to the tank whatever filled the battery.
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
    c.wh_charge_power_threshold = 1500.0
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
    """Off-peak: 5 kW from the grid into the battery, at SoC 50 %."""
    t = t0
    while t <= t0 + GRID_CHARGE_SUSTAIN_S + 60:
        await _tick(
            coordinator, t,
            soc=50.0, solar_power_w=0.0,
            battery_power_w=5000.0, meter_power_w=5400.0,
        )
        t += 10
    return t


def _water_heater(coordinator):
    hass = FakeHass()
    coordinator._water_heater = WaterHeaterController(hass, SWITCH_ID)
    coordinator.water_heater_mode = "Auto"
    return coordinator._water_heater


async def _sunny_morning(coordinator, t0):
    """3 kW of sun, 500 W of house: 2.5 kW into the battery, no import."""
    for dt in range(0, 600, 10):
        await _tick(
            coordinator, t0 + dt,
            soc=55.0, solar_power_w=3000.0,
            battery_power_w=2500.0, meter_power_w=0.0,
        )


@pytest.mark.asyncio
async def test_solar_charge_heats_on_the_morning_after_a_grid_charge(
    coordinator,
):
    wh = _water_heater(coordinator)
    t = await _night_grid_charge(coordinator)
    assert coordinator.grid_charge.is_reserved()
    # The grid charge itself is no surplus: SoC and charge meet the rule,
    # the import doesn't.
    assert wh.is_heating is False

    await _sunny_morning(coordinator, t + 3600)

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
