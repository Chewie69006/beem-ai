"""Configurable electricity tariff schedule manager for BeemAI.

Supports user-defined tariff periods with start/end times, prices, and labels.
If no periods are configured, only the default price applies 24/7 (label "HP").
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

log = logging.getLogger(__name__)

# Default tariff label for times outside any configured period.
TARIFF_HP = "HP"


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
        if periods:
            self._set_periods(periods)
        log.info(
            "TariffManager initialised: default=%.4f EUR/kWh, %d periods",
            default_price,
            len(self._periods),
        )

    def _set_periods(self, periods: list[dict]) -> None:
        """Parse period dicts into TariffPeriod objects."""
        self._periods = []
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
        """Return the price in EUR/kWh for the given or current tariff."""
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

    def get_daily_reset_hour(self) -> int:
        """Return the hour for daily reset: start of cheapest & longest period, rounded up."""
        if not self._periods:
            return 0  # midnight fallback

        def _duration_minutes(p: TariffPeriod) -> int:
            s = p.start.hour * 60 + p.start.minute
            e = p.end.hour * 60 + p.end.minute
            return (e - s) % 1440  # handles midnight crossing

        best = min(self._periods, key=lambda p: (p.price, -_duration_minutes(p)))
        h, m = best.start.hour, best.start.minute
        if m > 0:
            return (h + 1) % 24
        return h

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _time_in_period(t: time, period: TariffPeriod) -> bool:
        """Check if a time falls within a period, handling midnight crossings."""
        if period.crosses_midnight:
            return t >= period.start or t < period.end
        return period.start <= t < period.end
