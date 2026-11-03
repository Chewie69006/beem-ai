"""Export alert: notify once the grid export holds ≥ threshold for the
duration (500 W / 60 s by default), once per export episode."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.beem_ai.const import (
    DEFAULT_EXPORT_ALERT_S,
    DEFAULT_EXPORT_ALERT_W,
    OPT_EXPORT_ALERT_S,
    OPT_EXPORT_ALERT_W,
)
from custom_components.beem_ai.coordinator import BeemAICoordinator
from custom_components.beem_ai.export_alert import (
    EXPORT_ALERT_GRACE_S,
    EXPORT_ALERT_REARM_S,
    EXPORT_NOTIFY_TAG,
    ExportAlert,
    export_notification,
)
from custom_components.beem_ai.number import (
    BeemAIExportAlertDuration,
    BeemAIExportAlertThreshold,
)
from custom_components.beem_ai.water_heater_controller import (
    WaterHeaterController,
)

from .test_water_heater_controller import SWITCH_ID, FakeHass


def _feed(alert, export_w, t0, t1, step=5, **kw):
    """Samples every ``step`` s over [t0, t1); returns (t, held) of the
    alerts that fired."""
    fired = []
    t = t0
    while t < t1:
        held = alert.update(export_w, kw.get("threshold", 500),
                            kw.get("duration", 60), hold=kw.get("hold", False),
                            now=t)
        if held is not None:
            fired.append((t, held))
        t += step
    return fired


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


def test_fires_once_the_export_has_held_the_duration():
    alert = ExportAlert()
    fired = _feed(alert, 600, 0, 300)
    assert fired == [(60, 60)]
    assert alert.notified


def test_export_under_the_threshold_never_fires():
    alert = ExportAlert()
    assert _feed(alert, 450, 0, 3600) == []
    # Meter noise at a full battery (a few W of "export").
    assert _feed(alert, 8, 0, 3600) == []


def test_short_dip_does_not_restart_the_timer():
    alert = ExportAlert()
    assert _feed(alert, 600, 0, 40) == []
    assert _feed(alert, 100, 40, 40 + EXPORT_ALERT_GRACE_S - 5) == []
    fired = _feed(alert, 600, 50, 120)
    assert fired == [(60, 60)]


def test_dip_longer_than_the_grace_restarts_the_timer():
    alert = ExportAlert()
    assert _feed(alert, 600, 0, 50) == []
    assert _feed(alert, 0, 50, 50 + EXPORT_ALERT_GRACE_S + 5) == []
    fired = _feed(alert, 600, 70, 200)
    assert fired == [(130, 60)]


def test_one_alert_per_episode_until_the_export_stays_low():
    alert = ExportAlert()
    assert len(_feed(alert, 1500, 0, 600)) == 1
    # Clouds: 10 min without export, then the sun is back — same episode.
    assert _feed(alert, 0, 600, 1200) == []
    assert _feed(alert, 1500, 1200, 2400) == []
    # Export ends for the re-arm time: the next one is a new episode.
    end = 2400 + EXPORT_ALERT_REARM_S
    assert _feed(alert, 0, 2400, end + 5) == []
    assert not alert.notified
    fired = _feed(alert, 900, end + 5, end + 200)
    assert fired == [(end + 65, 60)]


def test_threshold_zero_turns_the_alert_off():
    alert = ExportAlert()
    assert _feed(alert, 3000, 0, 600, threshold=0) == []
    assert not alert.notified


def test_hold_restarts_the_timer():
    """While the water heater start is timed, the export is about to go
    to the tank: the alert counts from the end of the hold."""
    alert = ExportAlert()
    assert _feed(alert, 800, 0, 30, hold=True) == []
    assert _feed(alert, 800, 30, 200) == [(90, 60)]


def test_configured_duration_and_threshold_apply():
    alert = ExportAlert()
    assert _feed(alert, 1200, 0, 600, threshold=1000, duration=300) == [
        (300, 300),
    ]


# ---------------------------------------------------------------------------
# Notification text
# ---------------------------------------------------------------------------


def test_notification_french():
    title, message = export_notification(1250, 60, 100.0, "full", "fr")
    assert title == "BeemAI · surplus solaire"
    assert message.splitlines() == [
        "Export de 1,2 kW vers le réseau depuis 1 min, batterie à 100 %.",
        "C'est le moment de lancer le lave-vaisselle ou un peu de chauffage.",
        "Chauffe-eau : déjà chaud.",
    ]


def test_notification_english_without_water_heater():
    title, message = export_notification(640, 45, 98.4, None, "en")
    assert title == "BeemAI · solar surplus"
    assert message.splitlines() == [
        "Exporting 0.6 kW to the grid for 45 s, battery at 98 %.",
        "A good time to start the dishwasher or some heating.",
    ]


# ---------------------------------------------------------------------------
# Coordinator — through the real MQTT callback
# ---------------------------------------------------------------------------


@pytest.fixture
def coordinator(mock_hass, state_store):
    mock_hass.config.language = "fr"
    entry = MagicMock()
    entry.data = {}
    entry.options = {"notify_service": "notify.mobile_app_pixel"}
    entry.entry_id = "test-entry"
    c = BeemAICoordinator(mock_hass, entry)
    c.state_store = state_store
    return c


async def _tick(coordinator, t, **battery):
    """One MQTT update, the tasks it creates awaited (diverters, then
    the notification)."""
    coordinator.state_store.update_battery(**battery)
    pending = []
    coordinator.hass.async_create_task = pending.append
    with patch("time.monotonic", return_value=t):
        coordinator._on_battery_update()
        for coro in pending:
            await coro


async def _export(coordinator, t0, t1, export_w, soc=100.0):
    for t in range(t0, t1, 5):
        await _tick(
            coordinator, t, soc=soc, solar_power_w=export_w + 400,
            battery_power_w=0.0, meter_power_w=-export_w,
        )


def _export_notifications(hass):
    return [
        c.args for c in hass.services.async_call.call_args_list
        if c.args[0] == "notify"
        and c.args[2]["data"]["tag"] == EXPORT_NOTIFY_TAG
    ]


def test_defaults_are_500_w_over_one_minute(coordinator):
    assert coordinator.export_alert_w == DEFAULT_EXPORT_ALERT_W == 500
    assert coordinator.export_alert_s == DEFAULT_EXPORT_ALERT_S == 60


@pytest.mark.asyncio
async def test_sustained_export_notifies_once(coordinator):
    await _export(coordinator, 0, 1800, 1300)

    sent = _export_notifications(coordinator.hass)
    assert len(sent) == 1
    domain, service, data = sent[0]
    assert (domain, service) == ("notify", "mobile_app_pixel")
    assert data["title"] == "BeemAI · surplus solaire"
    assert data["message"].startswith(
        "Export de 1,3 kW vers le réseau depuis 1 min"
    )


@pytest.mark.asyncio
async def test_short_export_does_not_notify(coordinator):
    """A load switching off: the battery takes the surplus back in 30 s."""
    await _export(coordinator, 0, 30, 2000)
    await _export(coordinator, 30, 600, 0)
    assert _export_notifications(coordinator.hass) == []


@pytest.mark.asyncio
async def test_notifies_while_beemai_is_disabled(coordinator):
    """Information only, like the evening notification."""
    coordinator.state_store.enabled = False
    await _export(coordinator, 0, 120, 800)
    assert len(_export_notifications(coordinator.hass)) == 1


@pytest.mark.asyncio
async def test_no_notify_service_sends_nothing(coordinator):
    coordinator.notify_service = ""
    await _export(coordinator, 0, 120, 800)
    coordinator.hass.services.async_call.assert_not_called()
    assert coordinator._export_alert.notified


@pytest.mark.asyncio
async def test_options_update_applies_new_values(coordinator):
    """What the number entities do: write the options, which reload."""
    await coordinator.async_options_updated({
        "notify_service": "notify.mobile_app_pixel",
        OPT_EXPORT_ALERT_W: 1000, OPT_EXPORT_ALERT_S: 120,
    })
    await _export(coordinator, 0, 600, 900)
    assert _export_notifications(coordinator.hass) == []
    await _export(coordinator, 600, 900, 1100)
    assert len(_export_notifications(coordinator.hass)) == 1


def _water_heater(coordinator):
    hass = FakeHass()
    coordinator._water_heater = WaterHeaterController(hass, SWITCH_ID)
    coordinator.water_heater_mode = "Auto"
    coordinator.wh_sustain_s = 30
    return hass


@pytest.mark.asyncio
async def test_waits_while_the_water_heater_start_is_timed(coordinator):
    """Battery full, 800 W of export: the heater starts after its 30 s
    sustain and takes it.  With a 20 s alert, no notification."""
    wh_hass = _water_heater(coordinator)
    coordinator.export_alert_s = 20

    t = 0
    while wh_hass._switch_state == "off":
        assert t < 120
        await _export(coordinator, t, t + 5, 800)
        t += 5
    # The heater draws 2.2 kW: the battery covers the rest.
    for t in range(t, t + 600, 5):
        await _tick(
            coordinator, t, soc=99.0, solar_power_w=1200.0,
            battery_power_w=-1400.0, meter_power_w=0.0,
        )

    assert _export_notifications(coordinator.hass) == []


@pytest.mark.asyncio
async def test_hot_tank_does_not_hold_the_alert(coordinator):
    wh_hass = _water_heater(coordinator)
    coordinator._water_heater._fully_heated = True

    await _export(coordinator, 0, 120, 2500)

    assert wh_hass._switch_state == "off"
    sent = _export_notifications(coordinator.hass)
    assert len(sent) == 1
    assert sent[0][2]["message"].endswith("Chauffe-eau : déjà chaud.")


# ---------------------------------------------------------------------------
# Number entities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("cls,attr,key,value,stored", [
    (BeemAIExportAlertThreshold, "export_alert_w", OPT_EXPORT_ALERT_W,
     750.0, 750.0),
    (BeemAIExportAlertDuration, "export_alert_s", OPT_EXPORT_ALERT_S,
     90.0, 90),
])
async def test_number_writes_coordinator_and_options(
    cls, attr, key, value, stored,
):
    coordinator = MagicMock()
    entry = MagicMock()
    entry.entry_id = "e"
    entry.options = {"other": 1}
    num = cls(coordinator, entry)
    num.hass = MagicMock()
    num.async_write_ha_state = MagicMock()

    await num.async_set_native_value(value)

    assert getattr(coordinator, attr) == stored
    num.hass.config_entries.async_update_entry.assert_called_once_with(
        entry, options={"other": 1, key: stored},
    )
    assert num.native_value == float(stored)
