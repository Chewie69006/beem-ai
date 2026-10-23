"""Coordinator wiring of the energy history (recorder statistics)."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from custom_components.beem_ai.const import (
    OPT_BASE_LOAD_OVERRIDE_W,
    OPT_COOLING_ENERGY_ENTITIES,
    OPT_EV_CHARGING_POWER_SENSOR,
    OPT_HEATING_ENERGY_ENTITIES,
    OPT_OUTDOOR_TEMP_ENTITY,
    OPT_WH_POWER_ENTITY,
)
from custom_components.beem_ai.coordinator import (
    HISTORY_FIRST_REFRESH_DELAY_S,
    HISTORY_REFRESH_INTERVAL,
    BeemAICoordinator,
)
from custom_components.beem_ai.energy_history import EnergyHistory

from .test_energy_history import FakeRecorder, _nights

OPTIONS = {
    OPT_WH_POWER_ENTITY: "sensor.wh_power",
    OPT_EV_CHARGING_POWER_SENSOR: "sensor.wallbox_power",
    OPT_HEATING_ENERGY_ENTITIES: [
        "sensor.salon_heating", "sensor.bureau_heating",
    ],
    OPT_COOLING_ENERGY_ENTITIES: ["sensor.salon_cooling"],
    OPT_OUTDOOR_TEMP_ENTITY: "sensor.outdoor",
}

OWN_SENSORS = {
    "test-entry_consumption": "sensor.consumption",
    "test-entry_solar_power": "sensor.solar",
}


@pytest.fixture
def coordinator(mock_hass, state_store):
    entry = MagicMock()
    entry.data = {}
    entry.options = dict(OPTIONS)
    entry.entry_id = "test-entry"
    c = BeemAICoordinator(mock_hass, entry)
    c.state_store = state_store
    return c


@pytest.fixture(autouse=True)
def registry():
    reg = MagicMock()
    reg.async_get_entity_id.side_effect = (
        lambda domain, platform, unique_id: OWN_SENSORS.get(unique_id)
    )
    with patch(
        "custom_components.beem_ai.coordinator.er.async_get",
        return_value=reg,
    ):
        yield reg


def test_sources_combine_own_sensors_and_options(coordinator):
    sources = coordinator._history_sources()

    assert sources.consumption == "sensor.consumption"
    assert sources.solar == "sensor.solar"
    assert sources.water_heater == "sensor.wh_power"
    assert sources.ev == "sensor.wallbox_power"
    assert sources.heating == ("sensor.salon_heating", "sensor.bureau_heating")
    assert sources.cooling == ("sensor.salon_cooling",)
    assert sources.outdoor_temp == "sensor.outdoor"


@pytest.mark.asyncio
async def test_refresh_reads_the_recorder_and_learns_the_base_load(
    coordinator,
):
    rec = FakeRecorder()
    _nights(rec, base_w=300)
    coordinator._energy_history = EnergyHistory(rec)
    coordinator.async_update_listeners = MagicMock()

    await coordinator._refresh_history()

    assert rec.calls[0][3] == "hour"
    assert coordinator.base_load_w == pytest.approx(300)
    assert coordinator.energy_history.updated.tzinfo is not None
    assert str(coordinator.energy_history.updated.tzinfo) == "Europe/Paris"
    coordinator.async_update_listeners.assert_called_once()


@pytest.mark.asyncio
async def test_override_wins_over_the_learned_base_load(coordinator):
    rec = FakeRecorder()
    _nights(rec, base_w=300)
    coordinator._energy_history = EnergyHistory(rec)
    await coordinator._refresh_history()

    coordinator.base_load_override_w = 450

    assert coordinator.base_load_w == 450.0


@pytest.mark.asyncio
async def test_refresh_failure_is_logged_not_raised(coordinator, caplog):
    async def broken(*args):
        raise RuntimeError("recorder not running")

    coordinator._energy_history = EnergyHistory(broken)
    coordinator.async_update_listeners = MagicMock()

    with caplog.at_level(logging.WARNING):
        await coordinator._refresh_history()

    assert "Energy history refresh failed" in caplog.text
    coordinator.async_update_listeners.assert_not_called()
    assert coordinator.base_load_w is None


def test_refresh_is_scheduled_hourly_and_once_at_start(coordinator):
    with patch(
        "custom_components.beem_ai.coordinator.async_track_time_interval"
    ) as interval, patch(
        "custom_components.beem_ai.coordinator.async_call_later"
    ) as later:
        coordinator._schedule_tasks()

    assert any(
        c.args[1] == coordinator._refresh_history
        and c.args[2] == HISTORY_REFRESH_INTERVAL
        for c in interval.call_args_list
    )
    later.assert_called_once_with(
        coordinator.hass, HISTORY_FIRST_REFRESH_DELAY_S,
        coordinator._refresh_history,
    )


@pytest.mark.asyncio
async def test_options_update_rereads_inputs_and_refreshes(coordinator):
    pending = MagicMock()
    coordinator._history_first_unsub = pending
    with patch(
        "custom_components.beem_ai.coordinator.async_call_later"
    ) as later:
        await coordinator.async_options_updated({
            **OPTIONS,
            OPT_HEATING_ENERGY_ENTITIES: ["sensor.salon_heating"],
            OPT_BASE_LOAD_OVERRIDE_W: 200,
        })

    assert coordinator.heating_energy_entities == ["sensor.salon_heating"]
    assert coordinator.base_load_override_w == 200
    pending.assert_called_once()  # the previous one-shot is cancelled
    later.assert_called_once()
    assert later.call_args.args[2] == coordinator._refresh_history


@pytest.mark.asyncio
async def test_shutdown_cancels_the_history_timers(coordinator):
    coordinator._history_unsub = MagicMock()
    coordinator._history_first_unsub = MagicMock()

    await coordinator.async_shutdown()

    coordinator._history_unsub.assert_called_once()
    coordinator._history_first_unsub.assert_called_once()


def test_no_options_means_no_history_inputs(mock_hass, state_store):
    entry = MagicMock()
    entry.data = {}
    entry.options = {}
    entry.entry_id = "test-entry"
    c = BeemAICoordinator(mock_hass, entry)

    sources = c._history_sources()

    assert sources.heating == ()
    assert sources.ev is None
    assert c.base_load_override_w == 0
    # Own sensors are still read.
    assert sources.consumption == "sensor.consumption"
