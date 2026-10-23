"""Charge planner — suggests tonight's battery target (ROADMAP §10, phase 2).

Information only.  The suggestion is the SoC the battery should hold when
the off-peak window ends so that, with tomorrow's forecast sun, it carries
the house until the next off-peak window without going under its floor.

Hour by hour over that peak window (05:26 → 21:26 here)::

    E(h+1) = min(capacity, E(h) − (house(h) − solar(h)))

with discharge / charge losses, and the target is the smallest start
energy that keeps ``E(h) ≥ min_soc`` throughout.  Working per hour, not on
daily totals, catches the cold morning before sunrise and the evening
peak, which a sunny midday cannot carry over once the battery is full.

The house, from the energy history (recorder statistics)::

    house(h) = base(h) + heating_day × heating_shape(h)
               + cooling_day × cooling_shape(h)

- ``base(h)``: mean base load of that clock hour over the last 14 days
  (water heater and EV left out: they run on surplus or off-peak);
- ``heating_day``: a linear fit of daily heating against heating degrees
  (``max(0, 17 − T_mean)``) over the last year, fed tomorrow's mean
  temperature — or the last 7 days' average without a fit;
- ``heating_shape(h)``: when in the day the heating ran, last 14 days.
  With tomorrow at the recent temperatures, the model reproduces the
  recent consumption.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

ONE_WAY_EFFICIENCY = 0.95
HEATING_BASE_TEMP_C = 17.0
PROFILE_DAYS = 14
MIN_PROFILE_DAYS = 3
RECENT_DAYS = 7
HEATING_FIT_MIN_DAYS = 10
HEATING_FIT_MIN_SPREAD = 2.0  # °C of standard deviation in the degrees
ROUND_TO_PCT = 5
WINDOW_SEARCH = timedelta(hours=48)


# ---------------------------------------------------------------------------
# Peak window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PeakWindow:
    """The hours the battery must carry: off-peak end → next off-peak."""

    start: datetime
    end: datetime

    def steps(self) -> list[tuple[datetime, float]]:
        """(clock-hour start, fraction of that hour inside the window)."""
        out = []
        hour = self.start.replace(minute=0, second=0, microsecond=0)
        while hour < self.end:
            nxt = hour + timedelta(hours=1)
            inside = min(nxt, self.end) - max(hour, self.start)
            frac = inside.total_seconds() / 3600.0
            if frac > 0:
                out.append((hour, frac))
            hour = nxt
        return out


def peak_window(
    is_offpeak: Callable[[datetime], bool], now: datetime,
) -> PeakWindow | None:
    """The next peak window that follows an off-peak window.

    Before tonight's off-peak (or during it), that's tomorrow's day.
    Minute resolution; ``None`` without any off-peak in the next 48 h.
    """
    step = timedelta(minutes=1)
    t = now.replace(second=0, microsecond=0)
    limit = t + WINDOW_SEARCH

    def advance(until_offpeak: bool) -> datetime | None:
        nonlocal t
        while is_offpeak(t) != until_offpeak:
            t += step
            if t > limit:
                return None
        return t

    if advance(True) is None:          # reach (or stay in) off-peak
        return None
    start = advance(False)             # off-peak ends
    if start is None:
        return None
    end = advance(True)                # next off-peak starts
    if end is None:
        return None
    return PeakWindow(start, end)


# ---------------------------------------------------------------------------
# Solar forecast
# ---------------------------------------------------------------------------


@dataclass
class SolarHour:
    """Forecast energy for one clock hour (kWh)."""

    p10: float = 0.0
    p50: float = 0.0
    p90: float = 0.0


def _parse_time(value, tz) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def solar_by_hour(forecast_attrs: list[dict], tz) -> dict[datetime, SolarHour]:
    """Hourly forecast from Solcast-style attributes.

    ``detailedHourly`` (kW over each hour) is preferred; the half-hourly
    ``detailedForecast`` is summed into hours otherwise.  Several entities
    (Forecast Today, Forecast Tomorrow) merge by period.
    """
    hours: dict[datetime, SolarHour] = {}
    for attrs in forecast_attrs:
        rows = attrs.get("detailedHourly")
        period_h = 1.0
        if not rows:
            rows = attrs.get("detailedForecast")
            period_h = 0.5
        for row in rows or ():
            start = _parse_time(row.get("period_start"), tz)
            if start is None:
                continue
            hour = start.replace(minute=0, second=0, microsecond=0)
            slot = hours.setdefault(hour, SolarHour())
            slot.p10 += float(row.get("pv_estimate10") or 0.0) * period_h
            slot.p50 += float(row.get("pv_estimate") or 0.0) * period_h
            slot.p90 += float(row.get("pv_estimate90") or 0.0) * period_h
    return hours


# ---------------------------------------------------------------------------
# Temperature
# ---------------------------------------------------------------------------


def mean_temperature(forecast: list[dict], day: date, tz) -> float | None:
    """Mean of the hourly forecast temperatures falling on ``day``."""
    temps = []
    for row in forecast or ():
        when = _parse_time(row.get("datetime"), tz)
        temp = row.get("temperature")
        if when is None or temp is None or when.date() != day:
            continue
        temps.append(float(temp))
    if len(temps) < 6:
        return None
    return sum(temps) / len(temps)


# ---------------------------------------------------------------------------
# Consumption model
# ---------------------------------------------------------------------------


def heating_degrees(temp_c: float) -> float:
    return max(0.0, HEATING_BASE_TEMP_C - temp_c)


def fit_heating(hvac_days) -> tuple[float, float] | None:
    """Least squares ``heating = a + b × degrees`` over heating days.

    Needs ``HEATING_FIT_MIN_DAYS`` days with heating degrees and some
    spread in temperature; ``b`` is never negative.
    """
    pts = [
        (heating_degrees(d.outdoor_temp_c), d.heating_kwh)
        for d in hvac_days
        if d.heating_kwh is not None and d.outdoor_temp_c is not None
        and heating_degrees(d.outdoor_temp_c) > 0
    ]
    if len(pts) < HEATING_FIT_MIN_DAYS:
        return None
    xs = [p[0] for p in pts]
    if statistics.pstdev(xs) < HEATING_FIT_MIN_SPREAD:
        return None
    ys = [p[1] for p in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in pts) / sxx
    b = max(0.0, b)
    return my - b * mx, b


def _shape(totals: list[float]) -> list[float]:
    total = sum(totals)
    if total <= 0:
        return [0.0] * 24
    return [max(0.0, v) / total for v in totals]


@dataclass
class ConsumptionModel:
    """Tomorrow's house consumption, by clock hour."""

    base: list[float]               # kWh per clock hour
    heating_shape: list[float]      # share of a day's heating per hour
    cooling_shape: list[float]
    recent_heating_kwh: float       # daily average, last RECENT_DAYS
    recent_cooling_kwh: float
    recent_temp_c: float | None
    heating_fit: tuple[float, float] | None
    days: int

    def heating_day(self, temp_c: float | None) -> tuple[float, str]:
        """(kWh of heating for the day, how it was estimated)."""
        if self.heating_fit is not None and temp_c is not None:
            a, b = self.heating_fit
            return max(0.0, a + b * heating_degrees(temp_c)), "model"
        return self.recent_heating_kwh, "recent days"

    def hour_kwh(self, hour: int, heating_day: float) -> tuple[float, float]:
        """(house kWh, of which heating) for a clock hour.

        Only the total is clamped: an hour's base can be slightly negative
        where a lagging heat-pump counter catches up, and its heating share
        carries that energy back.
        """
        heating = heating_day * self.heating_shape[hour]
        cooling = self.recent_cooling_kwh * self.cooling_shape[hour]
        return max(0.0, self.base[hour] + heating + cooling), heating


def build_consumption_model(history) -> ConsumptionModel | None:
    """From an ``EnergyHistory``: needs ``MIN_PROFILE_DAYS`` complete days."""
    days = history.complete_days()
    if len(days) < MIN_PROFILE_DAYS:
        return None
    profile_days = {d.day for d in days[-PROFILE_DAYS:]}
    base = [[] for _ in range(24)]
    heating = [0.0] * 24
    cooling = [0.0] * 24
    for h in history.hours:
        if h.start.date() not in profile_days or h.base_kwh is None:
            continue
        base[h.start.hour].append(h.base_kwh)
        heating[h.start.hour] += h.heating_kwh
        cooling[h.start.hour] += h.cooling_kwh

    recent = days[-RECENT_DAYS:]
    temps = [d.outdoor_temp_c for d in recent[-2:] if d.outdoor_temp_c is not None]
    return ConsumptionModel(
        base=[statistics.fmean(v) if v else 0.0 for v in base],
        heating_shape=_shape(heating),
        cooling_shape=_shape(cooling),
        recent_heating_kwh=statistics.fmean(d.heating_kwh for d in recent),
        recent_cooling_kwh=statistics.fmean(d.cooling_kwh for d in recent),
        recent_temp_c=statistics.fmean(temps) if temps else None,
        heating_fit=fit_heating(history.hvac_days),
        days=len(profile_days),
    )


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------


def _holds(start_kwh, nets, capacity_kwh, floor_kwh) -> bool:
    e = start_kwh
    for net in nets:
        if net > 0:
            e -= net / ONE_WAY_EFFICIENCY
        else:
            e = min(capacity_kwh, e - net * ONE_WAY_EFFICIENCY)
        if e < floor_kwh - 1e-9:
            return False
    return True


def required_soc(nets: list[float], capacity_kwh: float, min_soc: float) -> int:
    """Smallest whole SoC (%) that keeps the battery above ``min_soc`` over
    the net loads (kWh, + = house over sun).  100 if even full fails."""
    floor = capacity_kwh * min_soc / 100.0
    lo = max(0, math.ceil(min_soc))
    for soc in range(lo, 101):
        if _holds(capacity_kwh * soc / 100.0, nets, capacity_kwh, floor):
            return soc
    return 100


def round_up(soc: float, min_soc: float) -> int:
    """Up to the next ROUND_TO_PCT, never under the floor, at most 100."""
    if soc <= min_soc:
        return int(math.ceil(min_soc))
    return min(100, int(math.ceil(soc / ROUND_TO_PCT) * ROUND_TO_PCT))


@dataclass
class Suggestion:
    """Tonight's suggested target and what it was computed from."""

    target_soc: int                 # with the P50 sun
    cautious_soc: int | None        # with the P10 sun
    window: PeakWindow
    solar_kwh: float
    solar_p10_kwh: float
    solar_p90_kwh: float
    consumption_kwh: float
    heating_kwh: float
    outdoor_temp_c: float | None
    temp_source: str
    heating_method: str
    min_soc: float
    computed_at: datetime
    hours: list[dict] = field(default_factory=list)

    @property
    def needs_grid_charge(self) -> bool:
        return self.target_soc > math.ceil(self.min_soc)

    def as_attributes(self) -> dict:
        temp = self.outdoor_temp_c
        return {
            "window_start": self.window.start.isoformat(),
            "window_end": self.window.end.isoformat(),
            "cautious_soc": self.cautious_soc,
            "solar_kwh": round(self.solar_kwh, 1),
            "solar_p10_kwh": round(self.solar_p10_kwh, 1),
            "solar_p90_kwh": round(self.solar_p90_kwh, 1),
            "consumption_kwh": round(self.consumption_kwh, 1),
            "heating_kwh": round(self.heating_kwh, 1),
            "outdoor_temp_c": round(temp, 1) if temp is not None else None,
            "temperature_source": self.temp_source,
            "heating_estimate": self.heating_method,
            "min_soc": self.min_soc,
            "computed_at": self.computed_at.isoformat(),
            "hours": self.hours,
        }


def plan(
    window: PeakWindow,
    solar: dict[datetime, SolarHour],
    model: ConsumptionModel,
    forecast_temp_c: float | None,
    capacity_kwh: float,
    min_soc: float,
    now: datetime,
) -> Suggestion:
    """Suggest the SoC to hold at ``window.start``."""
    if forecast_temp_c is not None:
        temp, temp_source = forecast_temp_c, "forecast"
    elif model.recent_temp_c is not None:
        temp, temp_source = model.recent_temp_c, "last days"
    else:
        temp, temp_source = None, "none"
    heating_day, method = model.heating_day(temp)

    nets_p50, nets_p10, hours = [], [], []
    cons_total = heat_total = 0.0
    sun = SolarHour()
    for hour, frac in window.steps():
        house, heating = model.hour_kwh(hour.hour, heating_day)
        sol = solar.get(hour, SolarHour())
        house, heating = house * frac, heating * frac
        p10, p50, p90 = sol.p10 * frac, sol.p50 * frac, sol.p90 * frac
        nets_p50.append(house - p50)
        nets_p10.append(house - p10)
        cons_total += house
        heat_total += heating
        sun.p10 += p10
        sun.p50 += p50
        sun.p90 += p90
        hours.append({
            "hour": hour.strftime("%H:%M"),
            "consumption_kwh": round(house, 2),
            "solar_kwh": round(p50, 2),
        })

    target = round_up(required_soc(nets_p50, capacity_kwh, min_soc), min_soc)
    cautious = round_up(required_soc(nets_p10, capacity_kwh, min_soc), min_soc)
    return Suggestion(
        target_soc=target,
        cautious_soc=cautious,
        window=window,
        solar_kwh=sun.p50,
        solar_p10_kwh=sun.p10,
        solar_p90_kwh=sun.p90,
        consumption_kwh=cons_total,
        heating_kwh=heat_total,
        outdoor_temp_c=temp,
        temp_source=temp_source,
        heating_method=method,
        min_soc=min_soc,
        computed_at=now,
        hours=hours,
    )


# ---------------------------------------------------------------------------
# Notification text
# ---------------------------------------------------------------------------


def _num(value: float, lang: str) -> str:
    text = f"{value:.1f}"
    return text.replace(".", ",") if lang == "fr" else text


def notification(s: Suggestion, current_soc: float, lang: str) -> tuple[str, str]:
    """(title, message) for the evening notification, French or English."""
    start = s.window.start.strftime("%H:%M")
    end = s.window.end.strftime("%H:%M")
    temp = (
        f"{s.outdoor_temp_c:.0f} °C" if s.outdoor_temp_c is not None else "?"
    )
    if lang == "fr":
        title = "BeemAI · charge de cette nuit"
        if s.needs_grid_charge:
            head = f"Charge conseillée : {s.target_soc} % à {start}"
            if s.cautious_soc and s.cautious_soc > s.target_soc:
                head += f" (prudent : {s.cautious_soc} %)"
            head += "."
        else:
            head = "Pas de recharge réseau nécessaire cette nuit."
        lines = [
            head,
            f"Soleil : {_num(s.solar_kwh, lang)} kWh "
            f"({_num(s.solar_p10_kwh, lang)} – {_num(s.solar_p90_kwh, lang)}).",
            f"Conso {start}–{end} : {_num(s.consumption_kwh, lang)} kWh, "
            f"dont chauffage {_num(s.heating_kwh, lang)} kWh ({temp}).",
            f"Batterie maintenant : {current_soc:.0f} %.",
        ]
    else:
        title = "BeemAI · tonight's charge"
        if s.needs_grid_charge:
            head = f"Suggested charge: {s.target_soc} % by {start}"
            if s.cautious_soc and s.cautious_soc > s.target_soc:
                head += f" (cautious: {s.cautious_soc} %)"
            head += "."
        else:
            head = "No grid charge needed tonight."
        lines = [
            head,
            f"Sun: {_num(s.solar_kwh, lang)} kWh "
            f"({_num(s.solar_p10_kwh, lang)}–{_num(s.solar_p90_kwh, lang)}).",
            f"Use {start}–{end}: {_num(s.consumption_kwh, lang)} kWh, "
            f"of which heating {_num(s.heating_kwh, lang)} kWh ({temp}).",
            f"Battery now: {current_soc:.0f} %.",
        ]
    return title, "\n".join(lines)


_REASONS_FR = {
    "no tariff": "pas de tarifs configurés",
    "no off-peak period configured": "aucune période d'heures creuses configurée",
    "not enough consumption history": "pas assez d'historique de consommation",
    "error": "erreur de calcul (voir le journal)",
    "not computed yet": "pas encore calculée",
}


def no_suggestion_notification(reason: str | None, lang: str) -> tuple[str, str]:
    """(title, message) when there is no suggestion to send."""
    reason = reason or "?"
    if lang == "fr":
        if reason.startswith("no solar forecast for "):
            text = "pas de prévision solaire pour le " + reason.rsplit(" ", 1)[-1]
        else:
            text = _REASONS_FR.get(reason, reason)
        return "BeemAI · charge de cette nuit", f"Pas de suggestion ce soir : {text}."
    return "BeemAI · tonight's charge", f"No suggestion tonight: {reason}."
