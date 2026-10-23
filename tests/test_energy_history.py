"""Tests for EnergyHistory — the consumption breakdown from the recorder."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.beem_ai.energy_history import (
    BASE_LOAD_MIN_NIGHTS,
    HOURLY_DAYS,
    HVAC_HISTORY_DAYS,
    EnergyHistory,
    HistorySources,
)

TZ = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 23, 12, 30, tzinfo=TZ)

SOURCES = HistorySources(
    consumption="sensor.consumption",
    solar="sensor.solar",
    water_heater="sensor.wh_power",
    ev="sensor.wallbox_power",
    heating=("sensor.salon_heating", "sensor.bureau_heating"),
    cooling=("sensor.salon_cooling",),
    outdoor_temp="sensor.outdoor",
)


def _ts(day: date, hour: int) -> float:
    return datetime(day.year, day.month, day.day, hour, tzinfo=TZ).timestamp()


class FakeRecorder:
    """Serves hourly / daily rows and records the queries."""

    def __init__(self):
        self.hourly: dict[str, list[dict]] = {}
        self.daily: dict[str, list[dict]] = {}
        self.calls = []

    async def __call__(self, start, end, ids, period, types):
        self.calls.append((start, end, set(ids), period, set(types)))
        table = self.hourly if period == "hour" else self.daily
        return {i: rows for i, rows in table.items() if i in ids}

    def hour(self, stat, day, hour, **values):
        self.hourly.setdefault(stat, []).append(
            {"start": _ts(day, hour), **values}
        )

    def day(self, stat, day, **values):
        self.daily.setdefault(stat, []).append(
            {"start": _ts(day, 0), **values}
        )


def _house_hour(rec, day, hour, cons_w, wh_w=0.0, ev_w=0.0,
                heat=(0.0, 0.0), cool=0.0, temp=10.0, solar_w=0.0):
    rec.hour("sensor.consumption", day, hour, mean=cons_w)
    rec.hour("sensor.solar", day, hour, mean=solar_w)
    rec.hour("sensor.wh_power", day, hour, mean=wh_w)
    rec.hour("sensor.wallbox_power", day, hour, mean=ev_w)
    rec.hour("sensor.salon_heating", day, hour, change=heat[0])
    rec.hour("sensor.bureau_heating", day, hour, change=heat[1])
    rec.hour("sensor.salon_cooling", day, hour, change=cool)
    rec.hour("sensor.outdoor", day, hour, mean=temp)


async def _refreshed(rec, sources=SOURCES, now=NOW):
    history = EnergyHistory(rec)
    await history.async_refresh(sources, now)
    return history


@pytest.mark.asyncio
async def test_hour_breakdown_subtracts_every_known_load():
    rec = FakeRecorder()
    d = date(2026, 10, 22)
    _house_hour(rec, d, 19, cons_w=4000, wh_w=500, ev_w=1000,
                heat=(0.8, 0.2), cool=0.0, temp=8.5, solar_w=100)

    history = await _refreshed(rec)

    [h] = history.hours
    assert h.start == datetime(2026, 10, 22, 19, tzinfo=TZ)
    assert h.consumption_kwh == pytest.approx(4.0)
    assert h.water_heater_kwh == pytest.approx(0.5)
    assert h.ev_kwh == pytest.approx(1.0)
    assert h.heating_kwh == pytest.approx(1.0)
    assert h.base_kwh == pytest.approx(1.5)
    assert h.solar_kwh == pytest.approx(0.1)
    assert h.outdoor_temp_c == 8.5


@pytest.mark.asyncio
async def test_queries_cover_the_windows():
    rec = FakeRecorder()
    await _refreshed(rec)

    hourly, daily = rec.calls
    midnight = NOW.replace(hour=0, minute=0)
    assert hourly[0] == midnight - timedelta(days=HOURLY_DAYS)
    assert hourly[1] == NOW
    assert hourly[2] == SOURCES.all_ids()
    assert hourly[3:] == ("hour", {"mean", "change"})
    assert daily[0] == midnight - timedelta(days=HVAC_HISTORY_DAYS)
    assert daily[2] == {
        "sensor.salon_heating", "sensor.bureau_heating",
        "sensor.salon_cooling", "sensor.outdoor",
    }
    assert daily[3] == "day"


@pytest.mark.asyncio
async def test_nothing_configured_queries_nothing():
    rec = FakeRecorder()
    history = await _refreshed(rec, sources=HistorySources())
    assert rec.calls == []
    assert history.hours == []
    assert history.base_load_w() is None


@pytest.mark.asyncio
async def test_complete_days_leave_today_out():
    rec = FakeRecorder()
    for hour in range(24):
        _house_hour(rec, date(2026, 10, 22), hour, cons_w=1000,
                    heat=(0.25, 0.0), temp=hour)
    _house_hour(rec, NOW.date(), 1, cons_w=1000)

    history = await _refreshed(rec)

    [day] = history.complete_days()
    assert day.day == date(2026, 10, 22)
    assert day.hours == 24
    assert day.consumption_kwh == pytest.approx(24.0)
    assert day.heating_kwh == pytest.approx(6.0)
    assert day.base_kwh == pytest.approx(18.0)
    assert day.outdoor_temp_c == pytest.approx(11.5)
    assert day.as_dict()["base_kwh"] == 18.0
    assert day.as_dict()["hours"] == 24


def _nights(rec, base_w, days=5, lagging_heat=False):
    """Night windows 01:00-05:00 at ``base_w`` plus 400 W of heating.

    With ``lagging_heat``, the heat pump reports the window's heating in
    a lump at 02:00 and 04:00 (2-hour blocks), as the Daikin cloud does.
    """
    for n in range(days):
        d = date(2026, 10, 18 + n)
        for hour in range(1, 5):
            heat = 0.4
            if lagging_heat:
                heat = 0.8 if hour in (2, 4) else 0.0
            _house_hour(rec, d, hour, cons_w=base_w + 400, heat=(heat, 0.0))


@pytest.mark.asyncio
async def test_base_load_is_the_median_night():
    rec = FakeRecorder()
    _nights(rec, base_w=300)
    # One night with the oven on until 02:00 doesn't move the median.
    rec.hourly["sensor.consumption"][0]["mean"] = 4000

    history = await _refreshed(rec)

    assert history.base_load_w() == pytest.approx(300)


@pytest.mark.asyncio
async def test_base_load_ignores_the_heat_pump_reporting_lag():
    """Hourly, the lumped heating would read 1100 W then −100 W; over the
    window it averages out."""
    rec = FakeRecorder()
    _nights(rec, base_w=300, lagging_heat=True)

    history = await _refreshed(rec)

    assert history.night_base_loads_w() == pytest.approx([300] * 5)


@pytest.mark.asyncio
async def test_base_load_needs_enough_complete_nights():
    rec = FakeRecorder()
    _nights(rec, base_w=300, days=BASE_LOAD_MIN_NIGHTS - 1)
    # A night with an hour missing doesn't count.
    for hour in (1, 2, 3):
        _house_hour(rec, date(2026, 10, 21), hour, cons_w=700)

    history = await _refreshed(rec)

    assert len(history.night_base_loads_w()) == BASE_LOAD_MIN_NIGHTS - 1
    assert history.base_load_w() is None


@pytest.mark.asyncio
async def test_missing_statistics_are_listed():
    rec = FakeRecorder()
    rec.hour("sensor.consumption", date(2026, 10, 22), 1, mean=500)

    history = await _refreshed(rec)

    assert "sensor.consumption" not in history.missing
    assert "sensor.wh_power" in history.missing
    assert "sensor.outdoor" in history.missing


@pytest.mark.asyncio
async def test_hvac_history_from_daily_statistics():
    rec = FakeRecorder()
    rec.day("sensor.salon_heating", date(2025, 12, 1), change=12.0)
    rec.day("sensor.bureau_heating", date(2025, 12, 1), change=3.0)
    rec.day("sensor.outdoor", date(2025, 12, 1), mean=2.5)
    rec.day("sensor.outdoor", date(2025, 11, 30), mean=4.0)

    history = await _refreshed(rec)

    assert [d.day for d in history.hvac_days] == [
        date(2025, 11, 30), date(2025, 12, 1),
    ]
    dec1 = history.hvac_days[1]
    assert dec1.heating_kwh == pytest.approx(15.0)
    assert dec1.cooling_kwh is None
    assert dec1.outdoor_temp_c == 2.5
    # Temperature alone isn't a heating / cooling day.
    assert history.hvac_history() == (1, date(2025, 12, 1))


@pytest.mark.asyncio
async def test_datetime_row_starts_are_accepted():
    """Before HA 2023.3, rows carried a datetime ``start``."""
    rec = FakeRecorder()
    rec.hourly["sensor.consumption"] = [{
        "start": datetime(2026, 10, 22, 3, tzinfo=TZ), "mean": 800,
    }]

    history = await _refreshed(rec)

    assert history.hours[0].start.hour == 3
    assert history.hours[0].consumption_kwh == pytest.approx(0.8)
