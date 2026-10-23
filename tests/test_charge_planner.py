"""Tests for the charge planner — tonight's suggested battery target."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.beem_ai.charge_planner import (
    ConsumptionModel,
    PeakWindow,
    SolarHour,
    build_consumption_model,
    fit_heating,
    mean_temperature,
    no_suggestion_notification,
    notification,
    peak_window,
    plan,
    required_soc,
    round_up,
    solar_by_hour,
)
from custom_components.beem_ai.energy_history import EnergyHistory, HvacDay
from custom_components.beem_ai.tariff_manager import TariffManager

from .test_energy_history import SOURCES, FakeRecorder, _house_hour

TZ = ZoneInfo("Europe/Paris")
HC = TariffManager(0.25, [
    {"label": "HC", "start": "21:26", "end": "05:26", "price": 0.20},
])


def _at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


# -- Peak window -------------------------------------------------------------


@pytest.mark.parametrize("now", [_at(23, 20), _at(23, 23), _at(24, 3)])
def test_peak_window_is_tomorrows_day(now):
    window = peak_window(HC.is_in_cheapest_period, now)
    assert window == PeakWindow(_at(24, 5, 26), _at(24, 21, 26))


def test_peak_window_in_the_morning_is_the_next_one():
    """At 10:00, tonight's charge is for tomorrow's day."""
    window = peak_window(HC.is_in_cheapest_period, _at(23, 10))
    assert window.start == _at(24, 5, 26)


def test_no_offpeak_no_window():
    flat = TariffManager(0.25, [])
    assert peak_window(flat.is_in_cheapest_period, _at(23, 20)) is None


def test_window_steps_cover_partial_hours():
    steps = PeakWindow(_at(24, 5, 26), _at(24, 21, 26)).steps()
    assert steps[0] == (_at(24, 5), pytest.approx(34 / 60))
    assert steps[-1] == (_at(24, 21), pytest.approx(26 / 60))
    assert sum(f for _, f in steps) == pytest.approx(16.0)


# -- Solar forecast ----------------------------------------------------------


def test_solar_from_detailed_hourly_and_half_hourly():
    tomorrow = {"detailedHourly": [
        {"period_start": "2026-10-24T12:00:00+02:00",
         "pv_estimate": 2.0, "pv_estimate10": 1.0, "pv_estimate90": 3.0},
        {"period_start": _at(24, 13),
         "pv_estimate": 1.5, "pv_estimate10": 0.5, "pv_estimate90": 2.0},
    ]}
    today = {"detailedForecast": [
        {"period_start": "2026-10-23T12:00:00+02:00", "pv_estimate": 1.0,
         "pv_estimate10": 0.4, "pv_estimate90": 1.2},
        {"period_start": "2026-10-23T12:30:00+02:00", "pv_estimate": 3.0,
         "pv_estimate10": 1.0, "pv_estimate90": 4.0},
    ]}

    hours = solar_by_hour([today, tomorrow], TZ)

    assert hours[_at(24, 12)].p50 == pytest.approx(2.0)
    assert hours[_at(24, 13)].p10 == pytest.approx(0.5)
    # Half-hourly kW over 30 min → kWh summed into the hour.
    assert hours[_at(23, 12)].p50 == pytest.approx(2.0)
    assert hours[_at(23, 12)].p90 == pytest.approx(2.6)


def test_solar_utc_periods_land_on_local_hours():
    hours = solar_by_hour([{"detailedHourly": [
        {"period_start": "2026-10-24T10:00:00+00:00", "pv_estimate": 1.0},
    ]}], TZ)
    assert list(hours) == [_at(24, 12)]


# -- Temperature -------------------------------------------------------------


def test_mean_temperature_of_the_window_day():
    forecast = [
        {"datetime": (_at(24, 0) + timedelta(hours=h)).isoformat(),
         "temperature": float(h)}
        for h in range(30)  # spills into the 25th
    ]
    assert mean_temperature(forecast, date(2026, 10, 24), TZ) == pytest.approx(11.5)
    assert mean_temperature(forecast[:5], date(2026, 10, 24), TZ) is None


# -- Heating model -----------------------------------------------------------


def _hvac(temp, kwh, n=0):
    return HvacDay(date(2026, 1, 1) + timedelta(days=n), kwh, None, temp)


def test_heating_fit_recovers_the_slope():
    days = [_hvac(t, 1.0 + 0.5 * (17 - t), i) for i, t in enumerate(range(-2, 14))]
    a, b = fit_heating(days)
    assert b == pytest.approx(0.5)
    assert a == pytest.approx(1.0)


def test_heating_fit_needs_days_and_spread():
    assert fit_heating([_hvac(5, 6, i) for i in range(5)]) is None
    assert fit_heating([_hvac(5, 6, i) for i in range(20)]) is None
    # Summer days (no heating degrees) don't count.
    assert fit_heating([_hvac(25, 0, i) for i in range(30)]) is None


def test_heating_fit_never_slopes_down():
    days = [_hvac(t, 10 - 0.5 * (17 - t), i) for i, t in enumerate(range(-2, 14))]
    assert fit_heating(days)[1] == 0.0


# -- Consumption model from the history -------------------------------------


async def _history(days=5, cons_w=1000, heat_kwh_at_19=2.0):
    rec = FakeRecorder()
    for n in range(days):
        d = date(2026, 10, 18 + n)
        for hour in range(24):
            heat = heat_kwh_at_19 if hour == 19 else 0.0
            _house_hour(rec, d, hour, cons_w=cons_w + heat * 1000,
                        heat=(heat, 0.0), temp=8.0)
    history = EnergyHistory(rec)
    await history.async_refresh(SOURCES, _at(23, 12))
    return history


@pytest.mark.asyncio
async def test_consumption_model_from_history():
    model = build_consumption_model(await _history())

    assert model.days == 5
    assert model.base == pytest.approx([1.0] * 24)
    assert model.heating_shape[19] == pytest.approx(1.0)
    assert model.recent_heating_kwh == pytest.approx(2.0)
    assert model.recent_temp_c == pytest.approx(8.0)
    house, heating = model.hour_kwh(19, model.recent_heating_kwh)
    assert (house, heating) == (pytest.approx(3.0), pytest.approx(2.0))


@pytest.mark.asyncio
async def test_consumption_model_needs_a_few_days():
    assert build_consumption_model(await _history(days=2)) is None


# -- Target ------------------------------------------------------------------


def test_required_soc_covers_the_deficit_with_losses():
    # 10 kWh battery, floor 10 % = 1 kWh, 5 kWh to carry.
    assert required_soc([2.5, 2.5], 10.0, 10) == 63  # 1 + 5/0.95 = 6.26 kWh


def test_required_soc_hour_by_hour_beats_daily_totals():
    """Morning deficit, a midday surplus that fills the battery, evening
    deficit: the day is net positive, yet the morning still needs 32 %."""
    nets = [2.0, -10.0, 4.0]
    assert required_soc(nets, 10.0, 10) == 32  # 1 + 2/0.95 = 3.1 kWh


def test_required_soc_full_battery_not_enough():
    assert required_soc([20.0], 10.0, 10) == 100


@pytest.mark.parametrize("soc,expected", [(31, 35), (35, 35), (8, 10), (98, 100)])
def test_round_up(soc, expected):
    assert round_up(soc, 10) == expected


def _flat_model(base_kwh=0.5, heating_fit=(0.0, 0.5)):
    return ConsumptionModel(
        base=[base_kwh] * 24,
        heating_shape=[1 / 24] * 24,
        cooling_shape=[0.0] * 24,
        recent_heating_kwh=3.0,
        recent_cooling_kwh=0.0,
        recent_temp_c=12.0,
        heating_fit=heating_fit,
        days=14,
    )


WINDOW = PeakWindow(_at(24, 6), _at(24, 22))  # 16 whole hours


def test_plan_without_sun_carries_the_whole_window():
    s = plan(WINDOW, {}, _flat_model(heating_fit=None), None, 10.0, 10, _at(23, 20))

    # 16 h × (0.5 base + 3 kWh/24 heating) = 10 kWh — more than the battery.
    assert s.consumption_kwh == pytest.approx(10.0)
    assert s.heating_kwh == pytest.approx(2.0)
    assert s.heating_method == "recent days"
    assert s.temp_source == "last days"
    assert s.target_soc == 100
    assert s.needs_grid_charge


def test_plan_uses_the_forecast_temperature_and_the_sun():
    sun = {_at(24, h): SolarHour(p10=0.5, p50=1.5, p90=2.0) for h in range(10, 16)}
    s = plan(WINDOW, sun, _flat_model(), 5.0, 13.4, 10, _at(23, 20))

    # Heating from the fit: 0.5 × (17 − 5) = 6 kWh a day, 4 in the window.
    assert s.heating_method == "model"
    assert s.temp_source == "forecast"
    assert s.heating_kwh == pytest.approx(4.0)
    assert s.solar_kwh == pytest.approx(9.0)
    assert s.solar_p10_kwh == pytest.approx(3.0)
    assert s.cautious_soc >= s.target_soc
    assert s.target_soc % 5 == 0
    assert len(s.hours) == 16


def test_plan_sunny_day_needs_no_grid_charge():
    sun = {_at(24, h): SolarHour(p10=3, p50=4, p90=5) for h in range(6, 22)}
    s = plan(WINDOW, sun, _flat_model(), 15.0, 13.4, 10, _at(23, 20))
    assert s.target_soc == 10
    assert not s.needs_grid_charge


# -- Notification ------------------------------------------------------------


def test_notification_french():
    sun = {_at(24, h): SolarHour(p10=0.5, p50=1.0, p90=1.5) for h in range(10, 16)}
    s = plan(PeakWindow(_at(24, 5, 26), _at(24, 21, 26)), sun, _flat_model(),
             6.0, 13.4, 10, _at(23, 20))

    title, msg = notification(s, 32.4, "fr")

    assert "charge de cette nuit" in title
    assert f"Charge conseillée : {s.target_soc} % à 05:26" in msg
    assert "Soleil : 6,0 kWh (3,0 – 9,0)." in msg
    assert "Conso 05:26–21:26" in msg
    assert "(6 °C)" in msg
    assert "Batterie maintenant : 32 %." in msg


def test_notification_no_charge_english():
    sun = {_at(24, h): SolarHour(p10=3, p50=4, p90=5) for h in range(6, 22)}
    s = plan(WINDOW, sun, _flat_model(), 15.0, 13.4, 10, _at(23, 20))

    title, msg = notification(s, 80, "en")

    assert title == "BeemAI · tonight's charge"
    assert msg.startswith("No grid charge needed tonight.")
    assert "Sun: 64.0 kWh" in msg


def test_no_suggestion_notification_translates_the_reason():
    assert no_suggestion_notification("no solar forecast for 2026-10-24", "fr")[1] == (
        "Pas de suggestion ce soir : pas de prévision solaire pour le 2026-10-24."
    )
    assert no_suggestion_notification("not enough consumption history", "fr")[1] == (
        "Pas de suggestion ce soir : pas assez d'historique de consommation."
    )
    assert no_suggestion_notification("error", "en") == (
        "BeemAI · tonight's charge", "No suggestion tonight: error.",
    )
