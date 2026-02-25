"""Configurable electricity tariff schedule manager for BeemAI.

Supports user-defined tariff periods with start/end times, prices, and labels.
Falls back to the French 3-tier schedule (HC/HSC/HP) if no periods are configured.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# Legacy tariff names (kept for backwards compatibility and water_heater.py)
TARIFF_HP = "HP"
TARIFF_HC = "HC"
TARIFF_HSC = "HSC"

# Default French 3-tier periods (used when no custom periods are configured)
_DEFAULT_PERIODS = [
    {"label": "HC", "start": "23:00", "end": "02:00", "price": 0.21},
    {"label": "HSC", "start": "02:00", "end": "06:00", "price": 0.16},
    {"label": "HC", "start": "06:00", "end": "07:00", "price": 0.21},
]
_DEFAULT_HP_PRICE = 0.27


@dataclass
class TariffWindow:
    """A contiguous tariff period."""

    start: datetime
    end: datetime
    tariff: str
    price: float


@dataclass
class TariffPeriod:
    """A configured tariff time slot."""

    label: str
    start: time
    end: time
    price: float

    @property
    def crosses_midnight(self) -> bool:
        """Return True if this period spans midnight (e.g. 23:00-02:00)."""
        return self.end <= self.start


class TariffManager:
    """Manages configurable electricity tariff schedules.

    Accepts a default price and a list of tariff periods. Any time not
    covered by a period uses the default price with label "HP".
    """

    def __init__(self, default_price: float, periods: list[dict] | None = None):
        self._default_price = default_price
        self._periods: list[TariffPeriod] = []
        self._set_periods(periods)
        log.info(
            "TariffManager initialised: default=%.4f EUR/kWh, %d periods",
            default_price,
            len(self._periods),
        )

    def _set_periods(self, periods: list[dict] | None) -> None:
        """Parse period dicts into TariffPeriod objects."""
        self._periods = []
        if not periods:
            # Fall back to hardcoded French 3-tier
            periods = _DEFAULT_PERIODS
            self._default_price = max(self._default_price, _DEFAULT_HP_PRICE)

        for p in periods:
            try:
                parts_s = p["start"].split(":")
                parts_e = p["end"].split(":")
                self._periods.append(TariffPeriod(
                    label=p.get("label", "OFF"),
                    start=time(int(parts_s[0]), int(parts_s[1])),
                    end=time(int(parts_e[0]), int(parts_e[1])),
                    price=float(p["price"]),
                ))
            except (KeyError, ValueError, IndexError) as exc:
                log.warning("Skipping invalid tariff period %s: %s", p, exc)

    def reconfigure(self, config: dict) -> None:
        """Update tariff configuration."""
        changed = False

        if "tariff_default_price" in config:
            self._default_price = float(config["tariff_default_price"])
            changed = True

        if "tariff_periods_json" in config:
            raw = config["tariff_periods_json"]
            if isinstance(raw, str) and raw:
                try:
                    periods = json.loads(raw)
                    self._set_periods(periods)
                    changed = True
                except (json.JSONDecodeError, TypeError):
                    log.warning("Invalid tariff_periods_json, keeping current periods")
            elif isinstance(raw, list):
                self._set_periods(raw)
                changed = True

        if changed:
            log.info(
                "TariffManager reconfigured: default=%.4f, %d periods",
                self._default_price,
                len(self._periods),
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def default_price(self) -> float:
        """Return the default (peak) price."""
        return self._default_price

    @property
    def periods(self) -> list[TariffPeriod]:
        """Return configured periods."""
        return list(self._periods)

    def get_current_tariff(self) -> str:
        """Return the current tariff label."""
        return self.get_tariff_at(datetime.now())

    def get_tariff_at(self, dt: datetime) -> str:
        """Determine which tariff label applies at a given datetime."""
        t = dt.time()
        for period in self._periods:
            if self._time_in_period(t, period):
                return period.label
        return TARIFF_HP

    def get_price_at(self, dt: datetime) -> float:
        """Return the price at a given datetime."""
        t = dt.time()
        for period in self._periods:
            if self._time_in_period(t, period):
                return period.price
        return self._default_price

    def get_price_kwh(self, tariff: Optional[str] = None) -> float:
        """Return the price in EUR/kWh for the given or current tariff.

        For backwards compatibility: looks up by label. If multiple periods
        share the same label, returns the first match's price.
        """
        if tariff is None:
            return self.get_price_at(datetime.now())
        for period in self._periods:
            if period.label == tariff:
                return period.price
        return self._default_price

    def get_cheapest_tariff(self) -> tuple[str, float]:
        """Return (label, price) of the cheapest period. Falls back to default."""
        if not self._periods:
            return TARIFF_HP, self._default_price
        cheapest = min(self._periods, key=lambda p: p.price)
        return cheapest.label, cheapest.price

    def is_in_cheapest_period(self, dt: datetime | None = None) -> bool:
        """Return True if the given time is in a period with the minimum price."""
        if not self._periods:
            return False
        if dt is None:
            dt = datetime.now()
        min_price = min(p.price for p in self._periods)
        t = dt.time()
        for period in self._periods:
            if period.price == min_price and self._time_in_period(t, period):
                return True
        return False

    def is_in_any_period(self, dt: datetime | None = None) -> bool:
        """Return True if the given time falls within any configured period."""
        if dt is None:
            dt = datetime.now()
        t = dt.time()
        return any(self._time_in_period(t, p) for p in self._periods)

    def get_windows_today(self) -> list[TariffWindow]:
        """Return all tariff windows for today (00:00 to 24:00)."""
        today = datetime.now().date()
        start = datetime.combine(today, time(0, 0))
        end = start + timedelta(days=1)
        return self.get_windows_range(start, end)

    def get_windows_range(
        self, start_dt: datetime, end_dt: datetime
    ) -> list[TariffWindow]:
        """Return a list of TariffWindow covering the given range."""
        if start_dt >= end_dt:
            return []

        windows: list[TariffWindow] = []
        cursor = start_dt

        while cursor < end_dt:
            label = self.get_tariff_at(cursor)
            price = self.get_price_at(cursor)
            window_end = self._next_transition(cursor)
            window_end = min(window_end, end_dt)
            windows.append(
                TariffWindow(
                    start=cursor,
                    end=window_end,
                    tariff=label,
                    price=price,
                )
            )
            cursor = window_end

        return windows

    def next_cheapest_window(self) -> tuple[datetime, datetime] | None:
        """Return (start, end) of the next cheapest-price window, or None."""
        if not self._periods:
            return None
        min_price = min(p.price for p in self._periods)
        cheapest_periods = [p for p in self._periods if p.price == min_price]
        if not cheapest_periods:
            return None

        now = datetime.now()
        today = now.date()

        # Check each cheapest period for the next occurrence
        best_start = None
        best_end = None

        for period in cheapest_periods:
            start_dt = datetime.combine(today, period.start)
            if period.crosses_midnight:
                end_dt = datetime.combine(today + timedelta(days=1), period.end)
            else:
                end_dt = datetime.combine(today, period.end)

            # If we're past this window, try tomorrow
            if now >= end_dt:
                start_dt += timedelta(days=1)
                end_dt += timedelta(days=1)

            if best_start is None or start_dt < best_start:
                best_start = start_dt
                best_end = end_dt

        if best_start is not None:
            return best_start, best_end
        return None

    def next_off_peak_window(self) -> tuple[datetime, datetime] | None:
        """Return (start, end) of the next off-peak (any period) window.

        The full off-peak block is the contiguous span of all periods
        that are not default/HP.
        """
        if not self._periods:
            return None

        now = datetime.now()
        today = now.date()

        # Find earliest period start that covers or follows now
        # For simplicity, return the span from first period start to last period end
        # within a single night cycle

        # Sort periods by start time, accounting for midnight crossings
        sorted_periods = sorted(self._periods, key=lambda p: p.start)

        # Find contiguous blocks
        # For the typical case (23:00-07:00 off-peak block), we want the full span
        for period in sorted_periods:
            start_dt = datetime.combine(today, period.start)
            if period.crosses_midnight:
                end_dt = datetime.combine(today + timedelta(days=1), period.end)
            else:
                end_dt = datetime.combine(today, period.end)

            if now < end_dt:
                # Found a period that hasn't ended yet
                # Extend to cover consecutive periods
                block_end = end_dt
                for other in sorted_periods:
                    other_start = datetime.combine(today, other.start)
                    if other.crosses_midnight:
                        other_end = datetime.combine(today + timedelta(days=1), other.end)
                    else:
                        other_end = datetime.combine(today, other.end)
                    # Check if this period starts where the block ends
                    if other_start == block_end:
                        block_end = other_end
                return start_dt, block_end

        # All today's periods are past, try tomorrow
        first = sorted_periods[0]
        start_dt = datetime.combine(today + timedelta(days=1), first.start)
        if first.crosses_midnight:
            end_dt = datetime.combine(today + timedelta(days=2), first.end)
        else:
            end_dt = datetime.combine(today + timedelta(days=1), first.end)
        return start_dt, end_dt

    def calculate_savings_vs_hp(self, kwh: float, tariff: str) -> float:
        """Calculate EUR saved by consuming at the given tariff vs default."""
        hp_cost = kwh * self._default_price
        actual_cost = kwh * self.get_price_kwh(tariff)
        return round(hp_cost - actual_cost, 4)

    def hours_until_next_hp(self) -> float:
        """Return hours remaining until the next default-price (HP) period."""
        now = datetime.now()
        current_label = self.get_tariff_at(now)
        if current_label == TARIFF_HP and not self.is_in_any_period(now):
            return 0.0

        # Find next transition to HP
        cursor = now
        max_search = timedelta(hours=48)
        while cursor - now < max_search:
            next_t = self._next_transition(cursor)
            label = self.get_tariff_at(next_t)
            if label == TARIFF_HP and not self.is_in_any_period(next_t):
                delta = (next_t - now).total_seconds() / 3600.0
                return round(delta, 2)
            cursor = next_t

        return 0.0

    # ------------------------------------------------------------------
    # Backwards-compatible aliases
    # ------------------------------------------------------------------

    def next_hsc_window(self) -> tuple[datetime, datetime]:
        """Return (start, end) of the next cheapest window.

        Backwards-compatible alias for next_cheapest_window().
        """
        result = self.next_cheapest_window()
        if result is None:
            # Fallback: return tomorrow 02:00-06:00
            today = datetime.now().date()
            return (
                datetime.combine(today + timedelta(days=1), time(2, 0)),
                datetime.combine(today + timedelta(days=1), time(6, 0)),
            )
        return result

    def next_hc_window(self) -> tuple[datetime, datetime]:
        """Return (start, end) of the next off-peak window.

        Backwards-compatible alias for next_off_peak_window().
        """
        result = self.next_off_peak_window()
        if result is None:
            today = datetime.now().date()
            return (
                datetime.combine(today, time(23, 0)),
                datetime.combine(today + timedelta(days=1), time(7, 0)),
            )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _time_in_period(t: time, period: TariffPeriod) -> bool:
        """Check if a time falls within a period, handling midnight crossings."""
        if period.crosses_midnight:
            return t >= period.start or t < period.end
        return period.start <= t < period.end

    def _get_transition_times(self) -> list[time]:
        """Return all boundary times from configured periods, sorted."""
        times_set: set[tuple[int, int]] = set()
        for period in self._periods:
            times_set.add((period.start.hour, period.start.minute))
            times_set.add((period.end.hour, period.end.minute))
        return sorted(time(h, m) for h, m in times_set)

    def _next_transition(self, dt: datetime) -> datetime:
        """Return the datetime of the next tariff boundary after dt."""
        transitions = self._get_transition_times()
        if not transitions:
            # No periods: entire day is HP
            return datetime.combine(dt.date() + timedelta(days=1), time(0, 0))

        current_time = dt.time()
        today = dt.date()

        for t in transitions:
            if current_time < t:
                return datetime.combine(today, t)

        # All transitions passed today; next is the first one tomorrow
        return datetime.combine(today + timedelta(days=1), transitions[0])
