"""Grid charge tracker — remembers when the grid last charged the battery.

On a day the battery was charged from the grid (off-peak, because the
solar forecast falls short), what it holds was bought for the house.
The EV must not spend it: an Auto charge started at the EV's target SoC
runs on the battery, and the house then imports at the peak rate in the
evening.  While :py:meth:`is_reserved` is True the coordinator raises
the EV's Auto target to ``RESERVED_MIN_SOC`` (battery nearly full).
The water heater is not held back: its rules already need solar surplus
(no import), and that surplus goes to the tank whatever filled the
battery overnight.

Detection is measurement-based, so it works whatever started the charge
(the Beem app, its schedule, BeemAI's own switches).  The grid power
going into the battery is the part of the charge the import covers::

    grid_charge_w = clamp(import_w, 0, battery_power_w)

It must stay at or above ``GRID_CHARGE_MIN_W`` for
``GRID_CHARGE_SUSTAIN_S`` in a row: the Beem brings the meter back to
zero within seconds, so a load spike while the battery charges from the
panels never lasts that long, while a grid charge runs for hours.

The battery stays reserved for ``GRID_CHARGE_HOLD`` after the last grid
charge — a charge ending at 06:00 covers the whole solar day and lapses
at 02:00, before the next night's decision.  The timestamp is persisted
so a restart in the morning doesn't hand the battery back to the
diverters.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

GRID_CHARGE_MIN_W = 200
GRID_CHARGE_SUSTAIN_S = 120
GRID_CHARGE_HOLD = timedelta(hours=20)
# While reserved, the diverters may only draw on the battery above this
# SoC — the same level as the water heater's export rule.
RESERVED_MIN_SOC = 95.0


class GridChargeTracker:
    """Detects grid charging of the battery and holds the reserve flag."""

    def __init__(self, file_path: str | Path | None = None) -> None:
        self._file_path = Path(file_path) if file_path else None
        # Monotonic start of the current run of grid-charging ticks.
        self._since: float | None = None
        # True once the current run has lasted GRID_CHARGE_SUSTAIN_S.
        self._charging: bool = False
        # Wall clock (UTC) of the last tick of confirmed grid charging.
        self._last_charge_at: datetime | None = None

    @property
    def charging(self) -> bool:
        """True while a sustained grid charge is in progress."""
        return self._charging

    @property
    def last_charge_at(self) -> datetime | None:
        """When the grid last charged the battery (UTC), if ever."""
        return self._last_charge_at

    @property
    def reserved_until(self) -> datetime | None:
        """When the reserve lapses (UTC), if a grid charge was seen."""
        if self._last_charge_at is None:
            return None
        return self._last_charge_at + GRID_CHARGE_HOLD

    def is_reserved(self, now: datetime | None = None) -> bool:
        """True while the battery holds energy bought from the grid."""
        if self._charging:
            return True
        until = self.reserved_until
        if until is None:
            return False
        now = now or datetime.now(timezone.utc)
        return now < until

    def update(
        self,
        battery_power_w: float,
        import_w: float,
        now: datetime | None = None,
    ) -> bool:
        """Feed one live sample.  Returns True when the state worth
        persisting changed (a grid charge was confirmed or ended)."""
        mono = time.monotonic()
        grid_charge_w = max(0.0, min(import_w, battery_power_w))

        if grid_charge_w < GRID_CHARGE_MIN_W:
            self._since = None
            if not self._charging:
                return False
            self._charging = False
            _LOGGER.info(
                "Grid charging ended — battery reserved for the house "
                "until %s",
                self.reserved_until,
            )
            return True

        if self._since is None:
            self._since = mono
        if mono - self._since < GRID_CHARGE_SUSTAIN_S:
            return False

        self._last_charge_at = now or datetime.now(timezone.utc)
        if self._charging:
            return False
        self._charging = True
        _LOGGER.info(
            "Grid charging detected (%.0f W from the grid into the "
            "battery) — EV Auto target raised to the reserve level "
            "until %s",
            grid_charge_w, self.reserved_until,
        )
        return True

    # -- Persistence --

    def save(self) -> None:
        """Write the last grid charge time to disk (blocking)."""
        if self._file_path is None:
            return
        data = {
            "last_charge_at": (
                self._last_charge_at.isoformat()
                if self._last_charge_at else None
            ),
        }
        try:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._file_path, "w") as f:
                json.dump(data, f)
        except OSError:
            _LOGGER.exception("Failed to save %s", self._file_path)

    def load(self) -> None:
        """Read the last grid charge time back from disk (blocking)."""
        if self._file_path is None or not self._file_path.exists():
            return
        try:
            with open(self._file_path) as f:
                raw = json.load(f).get("last_charge_at")
            if raw:
                last = datetime.fromisoformat(raw)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                self._last_charge_at = last
        except (OSError, ValueError, TypeError, AttributeError):
            _LOGGER.exception("Failed to load %s", self._file_path)
            return
        _LOGGER.info(
            "Last grid charge: %s — battery reserved until %s",
            self._last_charge_at, self.reserved_until,
        )
