"""French 3-tier electricity tariff schedule manager for BeemAI."""

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# Fixed tariff boundaries (not DST-affected)
_HSC_START = time(2, 0)
_HSC_END = time(6, 0)
_HC_EVENING_START = time(23, 0)
_HC_MORNING_END = time(7, 0)
_HP_START = time(7, 0)
_HP_END = time(23, 0)

TARIFF_HP = "HP"
TARIFF_HC = "HC"
TARIFF_HSC = "HSC"


@dataclass
class TariffWindow:
    """A contiguous tariff period."""

    start: datetime
    end: datetime
    tariff: str
    price: float


class TariffManager:
    """Manages the French 3-tier electricity tariff schedule.

    Time windows (fixed, no DST adjustment):
        HSC (Heures Super Creuses): 02:00 - 06:00
        HC  (Heures Creuses):       23:00 - 02:00 and 06:00 - 07:00
        HP  (Heures Pleines):       07:00 - 23:00
    """

    def __init__(self, hp_price: float, hc_price: float, hsc_price: float):
        self._prices = {
            TARIFF_HP: hp_price,
            TARIFF_HC: hc_price,
            TARIFF_HSC: hsc_price,
        }
        log.info(
            "TariffManager initialised: HP=%.4f HC=%.4f HSC=%.4f EUR/kWh",
            hp_price,
            hc_price,
            hsc_price,
        )

    def reconfigure(self, config: dict) -> None:
        """Update tariff prices from ConfigManager."""
        changed = False
        if "tariff_hp_price" in config:
            self._prices[TARIFF_HP] = float(config["tariff_hp_price"])
            changed = True
        if "tariff_hc_price" in config:
            self._prices[TARIFF_HC] = float(config["tariff_hc_price"])
            changed = True
        if "tariff_hsc_price" in config:
            self._prices[TARIFF_HSC] = float(config["tariff_hsc_price"])
            changed = True
        if changed:
            log.info(
                "TariffManager reconfigured: HP=%.4f HC=%.4f HSC=%.4f",
                self._prices[TARIFF_HP],
                self._prices[TARIFF_HC],
                self._prices[TARIFF_HSC],
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current_tariff(self) -> str:
        """Return the current tariff name: HP, HC, or HSC."""
        return self.get_tariff_at(datetime.now())

    def get_price_kwh(self, tariff: Optional[str] = None) -> float:
        """Return the price in EUR/kWh for the given or current tariff."""
        if tariff is None:
            tariff = self.get_current_tariff()
        return self._prices[tariff]

    def get_tariff_at(self, dt: datetime) -> str:
        """Determine which tariff applies at a given datetime."""
        t = dt.time()

        # HSC: 02:00 <= t < 06:00
        if _HSC_START <= t < _HSC_END:
            return TARIFF_HSC

        # HC evening: 23:00 <= t < 00:00
        if t >= _HC_EVENING_START:
            return TARIFF_HC

        # HC early morning: 00:00 <= t < 02:00
        if t < _HSC_START:
            return TARIFF_HC

        # HC post-HSC: 06:00 <= t < 07:00
        if _HSC_END <= t < _HC_MORNING_END:
            return TARIFF_HC

        # Everything else is HP: 07:00 <= t < 23:00
        return TARIFF_HP

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
            tariff = self.get_tariff_at(cursor)
            window_end = self._next_transition(cursor)
            # Clamp to requested range
            window_end = min(window_end, end_dt)
            windows.append(
                TariffWindow(
                    start=cursor,
                    end=window_end,
                    tariff=tariff,
                    price=self._prices[tariff],
                )
            )
            cursor = window_end

        return windows

    def next_hsc_window(self) -> tuple[datetime, datetime]:
        """Return (start, end) of the next HSC window (02:00-06:00)."""
        now = datetime.now()
        today = now.date()

        start = datetime.combine(today, _HSC_START)
        end = datetime.combine(today, _HSC_END)

        # If we're past today's HSC end, use tomorrow's
        if now >= end:
            start += timedelta(days=1)
            end += timedelta(days=1)

        return start, end

    def next_hc_window(self) -> tuple[datetime, datetime]:
        """Return (start, end) of the next HC window (including HSC).

        The full off-peak block runs 23:00 to 07:00.
        """
        now = datetime.now()
        today = now.date()

        # Off-peak block: 23:00 today -> 07:00 tomorrow
        start = datetime.combine(today, _HC_EVENING_START)
        end = datetime.combine(today + timedelta(days=1), _HC_MORNING_END)

        # If we're already inside tonight's off-peak block
        if now >= start:
            # If we haven't passed the end yet, the window is now -> end
            if now < end:
                return start, end
            # Past this window entirely, use tomorrow night's
            start += timedelta(days=1)
            end += timedelta(days=1)
        elif now < datetime.combine(today, _HC_MORNING_END):
            # We're in the early-morning tail of last night's block
            return datetime.combine(
                today - timedelta(days=1), _HC_EVENING_START
            ), datetime.combine(today, _HC_MORNING_END)

        return start, end

    def calculate_savings_vs_hp(self, kwh: float, tariff: str) -> float:
        """Calculate EUR saved by consuming at the given tariff vs HP."""
        hp_cost = kwh * self._prices[TARIFF_HP]
        actual_cost = kwh * self._prices[tariff]
        return round(hp_cost - actual_cost, 4)

    def hours_until_next_hp(self) -> float:
        """Return hours remaining until the next HP period starts (07:00)."""
        now = datetime.now()
        current = self.get_tariff_at(now)
        if current == TARIFF_HP:
            return 0.0

        # Next HP starts at 07:00
        today = now.date()
        next_hp = datetime.combine(today, _HP_START)
        if now >= next_hp:
            next_hp += timedelta(days=1)

        delta = (next_hp - now).total_seconds() / 3600.0
        return round(delta, 2)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_transition(self, dt: datetime) -> datetime:
        """Return the datetime of the next tariff boundary after dt."""
        # Ordered transitions within a day: 02:00, 06:00, 07:00, 23:00
        transitions = [
            time(2, 0),
            time(6, 0),
            time(7, 0),
            time(23, 0),
        ]

        current_time = dt.time()
        today = dt.date()

        for t in transitions:
            if current_time < t:
                return datetime.combine(today, t)

        # All transitions passed today; next is 02:00 tomorrow
        return datetime.combine(today + timedelta(days=1), transitions[0])
