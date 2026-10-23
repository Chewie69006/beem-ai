"""Energy history — the house consumption, hour by hour, split by use.

The charge-target suggestion (ROADMAP §10) needs to know what the house
draws, and which part of it the battery has to carry::

    base = consumption − water heater − EV − heating − cooling

The water heater and the EV run on solar surplus or off-peak straight
from the grid; heating and cooling follow the outdoor temperature and are
modelled on their own.

Everything is read from Home Assistant's long-term statistics (hourly,
kept forever): no log of our own to maintain, and last winter is there
from the first run.  Power sensors (state_class ``measurement``) give an
hourly mean, i.e. Wh per hour; energy counters (``total_increasing``,
daily resets included) give an hourly change.  The recorder converts the
units (W, kWh, °C).

The heat-pump counters lag: the Daikin cloud reports by 2-hour blocks,
so an hour's heating can land in the next one.  Hence the base load is
taken over whole night windows (01:00–05:00), never hour by hour.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

_LOGGER = logging.getLogger(__name__)

HOURLY_DAYS = 28
HVAC_HISTORY_DAYS = 365
# Night window (local hours) for the always-on base load.
NIGHT_START_HOUR = 1
NIGHT_END_HOUR = 5
BASE_LOAD_NIGHTS = 14
BASE_LOAD_MIN_NIGHTS = 3
# Units the recorder converts to, by unit class.
UNITS = {"energy": "kWh", "power": "W", "temperature": "°C"}

# (start, end, statistic_ids, period, types) → {statistic_id: [row, …]}
Fetch = Callable[
    [datetime, datetime, set[str], str, set[str]],
    Awaitable[dict[str, list[dict]]],
]


async def recorder_fetch(
    hass,
    start: datetime,
    end: datetime,
    statistic_ids: set[str],
    period: str,
    types: set[str],
) -> dict[str, list[dict]]:
    """Read long-term statistics, in the recorder's own executor."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import (
        statistics_during_period,
    )

    return await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass, start, end, statistic_ids, period, UNITS, types,
    )


@dataclass(frozen=True)
class HistorySources:
    """The statistic IDs (entity IDs) the history is built from."""

    consumption: str | None = None
    solar: str | None = None
    water_heater: str | None = None
    ev: str | None = None
    heating: tuple[str, ...] = ()
    cooling: tuple[str, ...] = ()
    outdoor_temp: str | None = None

    def all_ids(self) -> set[str]:
        ids = {
            self.consumption, self.solar, self.water_heater, self.ev,
            self.outdoor_temp, *self.heating, *self.cooling,
        }
        ids.discard(None)
        ids.discard("")
        return ids

    def hvac_ids(self) -> set[str]:
        ids = {*self.heating, *self.cooling, self.outdoor_temp}
        ids.discard(None)
        ids.discard("")
        return ids


@dataclass
class HourRecord:
    """One hour of the house, in kWh (temperature in °C)."""

    start: datetime  # local time
    consumption_kwh: float | None
    solar_kwh: float | None = None
    water_heater_kwh: float = 0.0
    ev_kwh: float = 0.0
    heating_kwh: float = 0.0
    cooling_kwh: float = 0.0
    outdoor_temp_c: float | None = None

    @property
    def base_kwh(self) -> float | None:
        """Consumption the battery has to carry — may be < 0 on a single
        hour when a lagging heat-pump counter catches up."""
        if self.consumption_kwh is None:
            return None
        return (
            self.consumption_kwh - self.water_heater_kwh - self.ev_kwh
            - self.heating_kwh - self.cooling_kwh
        )


@dataclass
class DayRecord:
    """A local day summed from its hours."""

    day: date
    hours: int = 0
    consumption_kwh: float = 0.0
    solar_kwh: float = 0.0
    water_heater_kwh: float = 0.0
    ev_kwh: float = 0.0
    heating_kwh: float = 0.0
    cooling_kwh: float = 0.0
    _temps: list[float] = field(default_factory=list, repr=False)

    @property
    def base_kwh(self) -> float:
        return max(
            0.0,
            self.consumption_kwh - self.water_heater_kwh - self.ev_kwh
            - self.heating_kwh - self.cooling_kwh,
        )

    @property
    def outdoor_temp_c(self) -> float | None:
        if not self._temps:
            return None
        return sum(self._temps) / len(self._temps)

    def as_dict(self) -> dict:
        temp = self.outdoor_temp_c
        return {
            "date": self.day.isoformat(),
            "hours": self.hours,
            "consumption_kwh": round(self.consumption_kwh, 1),
            "base_kwh": round(self.base_kwh, 1),
            "heating_kwh": round(self.heating_kwh, 1),
            "cooling_kwh": round(self.cooling_kwh, 1),
            "water_heater_kwh": round(self.water_heater_kwh, 1),
            "ev_kwh": round(self.ev_kwh, 1),
            "solar_kwh": round(self.solar_kwh, 1),
            "outdoor_temp_c": round(temp, 1) if temp is not None else None,
        }


@dataclass
class HvacDay:
    """One day of heating / cooling against the outdoor temperature."""

    day: date
    heating_kwh: float | None
    cooling_kwh: float | None
    outdoor_temp_c: float | None


def _by_start(rows: list[dict] | None) -> dict[float, dict]:
    return {_ts(r["start"]): r for r in rows or ()}


def _ts(start) -> float:
    """Row start as a timestamp (a datetime before HA 2023.3)."""
    return start.timestamp() if isinstance(start, datetime) else float(start)


def _change(rows: dict[float, dict], ts: float) -> float | None:
    row = rows.get(ts)
    return None if row is None else row.get("change")


def _mean(rows: dict[float, dict], ts: float) -> float | None:
    row = rows.get(ts)
    return None if row is None else row.get("mean")


def _kwh_from_mean_w(rows: dict[float, dict], ts: float) -> float | None:
    mean = _mean(rows, ts)
    return None if mean is None else mean / 1000.0


def _sum(values) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


class EnergyHistory:
    """Hourly consumption breakdown and daily heating history."""

    def __init__(self, fetch: Fetch) -> None:
        self._fetch = fetch
        self.hours: list[HourRecord] = []
        self.hvac_days: list[HvacDay] = []
        # Configured statistic IDs the recorder has nothing for (no
        # state_class, wrong entity, recorder excluded…).
        self.missing: list[str] = []
        self.updated: datetime | None = None

    async def async_refresh(
        self, sources: HistorySources, now: datetime
    ) -> None:
        """Re-read the statistics.  ``now`` is aware, in local time."""
        tz = now.tzinfo
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        ids = sources.all_ids()
        hourly: dict[str, list[dict]] = {}
        if ids:
            hourly = await self._fetch(
                midnight - timedelta(days=HOURLY_DAYS), now, ids, "hour",
                {"mean", "change"},
            )
        daily: dict[str, list[dict]] = {}
        hvac_ids = sources.hvac_ids()
        if hvac_ids:
            daily = await self._fetch(
                midnight - timedelta(days=HVAC_HISTORY_DAYS), now, hvac_ids,
                "day", {"mean", "change"},
            )

        self.hours = self._build_hours(hourly, sources, tz)
        self.hvac_days = self._build_hvac_days(daily, sources, tz)
        self.missing = sorted(
            i for i in ids if not hourly.get(i) and not daily.get(i)
        )
        self.updated = now

    # -- Building --

    @staticmethod
    def _build_hours(
        rows: dict[str, list[dict]], sources: HistorySources, tz,
    ) -> list[HourRecord]:
        consumption = _by_start(rows.get(sources.consumption))
        solar = _by_start(rows.get(sources.solar))
        wh = _by_start(rows.get(sources.water_heater))
        ev = _by_start(rows.get(sources.ev))
        heating = [_by_start(rows.get(i)) for i in sources.heating]
        cooling = [_by_start(rows.get(i)) for i in sources.cooling]
        temp = _by_start(rows.get(sources.outdoor_temp))

        hours = []
        for ts in sorted(consumption):
            hours.append(HourRecord(
                start=datetime.fromtimestamp(ts, tz),
                consumption_kwh=_kwh_from_mean_w(consumption, ts),
                solar_kwh=_kwh_from_mean_w(solar, ts),
                water_heater_kwh=_kwh_from_mean_w(wh, ts) or 0.0,
                ev_kwh=_kwh_from_mean_w(ev, ts) or 0.0,
                heating_kwh=_sum(_change(h, ts) for h in heating) or 0.0,
                cooling_kwh=_sum(_change(c, ts) for c in cooling) or 0.0,
                outdoor_temp_c=_mean(temp, ts),
            ))
        return hours

    @staticmethod
    def _build_hvac_days(
        rows: dict[str, list[dict]], sources: HistorySources, tz,
    ) -> list[HvacDay]:
        heating = [_by_start(rows.get(i)) for i in sources.heating]
        cooling = [_by_start(rows.get(i)) for i in sources.cooling]
        temp = _by_start(rows.get(sources.outdoor_temp))
        starts = set(temp)
        for table in (*heating, *cooling):
            starts.update(table)

        days = []
        for ts in sorted(starts):
            days.append(HvacDay(
                day=datetime.fromtimestamp(ts, tz).date(),
                heating_kwh=_sum(_change(h, ts) for h in heating),
                cooling_kwh=_sum(_change(c, ts) for c in cooling),
                outdoor_temp_c=_mean(temp, ts),
            ))
        return days

    # -- Views --

    def complete_days(self) -> list[DayRecord]:
        """Local days before today, summed from their hours."""
        if not self.updated:
            return []
        today = self.updated.date()
        days: dict[date, DayRecord] = {}
        for h in self.hours:
            d = h.start.date()
            if d >= today or h.consumption_kwh is None:
                continue
            rec = days.setdefault(d, DayRecord(day=d))
            rec.hours += 1
            rec.consumption_kwh += h.consumption_kwh
            rec.solar_kwh += h.solar_kwh or 0.0
            rec.water_heater_kwh += h.water_heater_kwh
            rec.ev_kwh += h.ev_kwh
            rec.heating_kwh += h.heating_kwh
            rec.cooling_kwh += h.cooling_kwh
            if h.outdoor_temp_c is not None:
                rec._temps.append(h.outdoor_temp_c)
        return [days[d] for d in sorted(days)]

    def night_base_loads_w(self) -> list[float]:
        """Average base load (W) of each complete night window, oldest
        first.  A night with a missing hour is left out."""
        nights: dict[date, list[float]] = {}
        for h in self.hours:
            if not NIGHT_START_HOUR <= h.start.hour < NIGHT_END_HOUR:
                continue
            base = h.base_kwh
            if base is None:
                continue
            nights.setdefault(h.start.date(), []).append(base)
        window = NIGHT_END_HOUR - NIGHT_START_HOUR
        return [
            max(0.0, sum(v) / len(v) * 1000.0)
            for _, v in sorted(nights.items())
            if len(v) >= window
        ]

    def base_load_w(self) -> float | None:
        """Learned always-on load: median of the last nights' windows."""
        nights = self.night_base_loads_w()[-BASE_LOAD_NIGHTS:]
        if len(nights) < BASE_LOAD_MIN_NIGHTS:
            return None
        return statistics.median(nights)

    def hvac_history(self) -> tuple[int, date | None]:
        """Days of heating / cooling data in the last year, and the first."""
        days = [
            d.day for d in self.hvac_days
            if d.heating_kwh is not None or d.cooling_kwh is not None
        ]
        return len(days), (days[0] if days else None)
