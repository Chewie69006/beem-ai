"""Coordinator: tonight's charge suggestion and the evening notification."""

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from custom_components.beem_ai.coordinator import (
    SUGGESTION_NOTIFY_TAG,
    BeemAICoordinator,
)
from custom_components.beem_ai.energy_history import EnergyHistory
from custom_components.beem_ai.tariff_manager import TariffManager

from .test_energy_history import SOURCES, FakeRecorder, _house_hour

TZ = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 23, 20, 0, tzinfo=TZ)
SOLCAST = "sensor.solcast_pv_forecast_forecast_tomorrow"
WEATHER = "weather.forecast_home"


def _solcast_attrs(kw=1.0):
    return {"detailedHourly": [
        {"period_start": datetime(2026, 10, 24, h, tzinfo=TZ),
         "pv_estimate": kw, "pv_estimate10": kw / 2, "pv_estimate90": kw * 1.5}
        for h in range(9, 17)
    ]}


def _weather_forecast(temp=6.0):
    start = datetime(2026, 10, 24, tzinfo=TZ)
    return {WEATHER: {"forecast": [
        {"datetime": (start + timedelta(hours=h)).isoformat(),
         "temperature": temp}
        for h in range(24)
    ]}}


OWN_SENSORS = {
    "test-entry_consumption": "sensor.consumption",
    "test-entry_solar_power": "sensor.solar",
}


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


@pytest.fixture
def services():
    calls = []

    async def call(domain, service, data, **kwargs):
        calls.append((domain, service, data, kwargs))
        if (domain, service) == ("weather", "get_forecasts"):
            return _weather_forecast()
        return None

    return calls, call


@pytest_asyncio.fixture
async def coordinator(mock_hass, state_store, services):
    calls, call = services
    mock_hass.services.async_call = AsyncMock(side_effect=call)
    mock_hass.config.language = "fr"
    solcast = MagicMock()
    solcast.attributes = _solcast_attrs()
    mock_hass.states.get.side_effect = (
        lambda eid: solcast if eid == SOLCAST else None
    )

    entry = MagicMock()
    entry.data = {}
    entry.options = {
        "solar_forecast_entities": [SOLCAST],
        "weather_entity": WEATHER,
        "notify_service": "mobile_app_pixel",
        # Same statistic IDs as the recorded history.
        "wh_power_entity": SOURCES.water_heater,
        "ev_charging_power_sensor": SOURCES.ev,
        "heating_energy_entities": list(SOURCES.heating),
        "cooling_energy_entities": list(SOURCES.cooling),
        "outdoor_temperature_entity": SOURCES.outdoor_temp,
    }
    entry.entry_id = "test-entry"
    c = BeemAICoordinator(mock_hass, entry)
    c.state_store = state_store
    state_store.battery.soc = 32.0
    state_store.update_control(min_soc=10)
    c._tariff = TariffManager(0.25, [
        {"label": "HC", "start": "21:26", "end": "05:26", "price": 0.20},
    ])

    rec = FakeRecorder()
    for n in range(5):
        d = date(2026, 10, 18 + n)
        for hour in range(24):
            _house_hour(rec, d, hour, cons_w=600, temp=8.0)
    c._energy_history = EnergyHistory(rec)
    await c._energy_history.async_refresh(SOURCES, NOW)
    with patch(
        "custom_components.beem_ai.coordinator.dt_util.now", return_value=NOW,
    ):
        yield c


@pytest.mark.asyncio
async def test_suggestion_from_all_inputs(coordinator, services):
    calls, _ = services
    await coordinator._update_suggestion()

    s = coordinator.suggestion
    assert coordinator.suggestion_reason is None
    assert s.window.start == datetime(2026, 10, 24, 5, 26, tzinfo=TZ)
    assert s.solar_kwh == pytest.approx(8.0)
    assert s.temp_source == "forecast"
    assert s.outdoor_temp_c == pytest.approx(6.0)
    assert s.consumption_kwh == pytest.approx(9.6)  # 16 h × 0.6 kWh
    assert 10 <= s.target_soc <= s.cautious_soc <= 100
    assert ("weather", "get_forecasts",
            {"entity_id": WEATHER, "type": "hourly"},
            {"blocking": True, "return_response": True}) in calls


@pytest.mark.asyncio
async def test_weather_failure_falls_back_to_recent_days(coordinator):
    coordinator.hass.services.async_call = AsyncMock(
        side_effect=RuntimeError("no weather")
    )
    await coordinator._update_suggestion()
    assert coordinator.suggestion.temp_source == "last days"
    assert coordinator.suggestion.outdoor_temp_c == pytest.approx(8.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("setup,reason", [
    (lambda c: setattr(c, "_tariff", None), "no tariff"),
    (lambda c: setattr(c, "_tariff", TariffManager(0.25, [])),
     "no off-peak period configured"),
    (lambda c: setattr(c, "_energy_history", EnergyHistory(FakeRecorder())),
     "not enough consumption history"),
    (lambda c: setattr(c, "solar_forecast_entities", []),
     "no solar forecast for 2026-10-24"),
])
async def test_no_suggestion_says_why(coordinator, setup, reason):
    setup(coordinator)
    await coordinator._update_suggestion()
    assert coordinator.suggestion is None
    assert coordinator.suggestion_reason == reason


@pytest.mark.asyncio
async def test_evening_notification_in_french(coordinator, services):
    calls, _ = services
    coordinator.async_update_listeners = MagicMock()

    await coordinator._send_suggestion()

    [(domain, service, data, kwargs)] = [c for c in calls if c[0] == "notify"]
    assert (domain, service) == ("notify", "mobile_app_pixel")
    assert data["title"] == "BeemAI · charge de cette nuit"
    assert "05:26" in data["message"]
    assert "Batterie maintenant : 32 %." in data["message"]
    assert data["data"] == {"tag": SUGGESTION_NOTIFY_TAG}
    coordinator.async_update_listeners.assert_called()


@pytest.mark.asyncio
async def test_notification_rereads_the_history_first(coordinator):
    """A restart just before the time: the history isn't read yet."""
    recorded = coordinator._energy_history._fetch
    coordinator._energy_history = EnergyHistory(recorded)

    await coordinator._send_suggestion()

    assert coordinator.energy_history.updated == NOW
    assert coordinator.suggestion is not None


@pytest.mark.asyncio
async def test_notification_survives_a_recorder_failure(coordinator, services):
    """The read fails: the previous history still gives a suggestion."""
    calls, _ = services

    async def broken(*args):
        raise RuntimeError("database busy")

    coordinator._energy_history._fetch = broken
    await coordinator._send_suggestion()

    assert coordinator.suggestion is not None
    assert [c for c in calls if c[0] == "notify"]


@pytest.mark.asyncio
async def test_notification_explains_a_missing_suggestion(coordinator, services):
    calls, _ = services
    coordinator.solar_forecast_entities = []
    coordinator.hass.config.language = "en"
    coordinator.notify_service = "notify.mobile_app_pixel"

    await coordinator._send_suggestion()

    [(_, service, data, _)] = [c for c in calls if c[0] == "notify"]
    assert service == "mobile_app_pixel"
    assert data["message"] == (
        "No suggestion tonight: no solar forecast for 2026-10-24."
    )


@pytest.mark.asyncio
async def test_no_notify_service_still_updates_the_sensor(coordinator, services):
    calls, _ = services
    coordinator.notify_service = ""

    await coordinator._send_suggestion()

    assert not [c for c in calls if c[0] == "notify"]
    assert coordinator.suggestion is not None


@pytest.mark.parametrize("value,expected", [
    ("19:45:00", (19, 45, 0)),
    ("21:05", (21, 5, 0)),
    ("bogus", (20, 0, 0)),
])
def test_notification_time_is_armed(mock_hass, value, expected):
    entry = MagicMock()
    entry.data = {}
    entry.options = {"suggestion_time": value}
    entry.entry_id = "test-entry"
    c = BeemAICoordinator(mock_hass, entry)
    previous = MagicMock()
    c._suggestion_unsub = previous

    with patch(
        "custom_components.beem_ai.coordinator.async_track_time_change"
    ) as track:
        c._schedule_suggestion()

    previous.assert_called_once()
    hour, minute, second = expected
    track.assert_called_once_with(
        mock_hass, c._send_suggestion, hour=hour, minute=minute, second=second,
    )
