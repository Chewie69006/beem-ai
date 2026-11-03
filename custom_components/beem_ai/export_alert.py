"""Export alert — tells the user the house is exporting to the grid.

Information only: when the battery can't take the surplus (full, or at
its charge limit) the panels export, and the user would rather start the
dishwasher or some heating.  The coordinator notifies the same service
as the evening suggestion once the export holds at or above a threshold
(W) for a duration (s) — two System number entities, 500 W / 60 s by
default.  Threshold 0 = no alert.

- Dips under the threshold shorter than ``EXPORT_ALERT_GRACE_S`` don't
  restart the timer: the meter swings tick to tick under passing loads.
- One alert per export episode: the next one needs the export to have
  stayed under the threshold for ``EXPORT_ALERT_REARM_S`` first, so
  clouds don't send a burst of them.
- ``hold``: the coordinator sets it while BeemAI is timing a water
  heater start — that surplus is about to go to the tank.  The timer
  restarts from zero once the heater is on (or the start is called off).

Pure: no Home Assistant imports.
"""

from __future__ import annotations

import logging
import time

_LOGGER = logging.getLogger(__name__)

EXPORT_ALERT_GRACE_S = 15
EXPORT_ALERT_REARM_S = 30 * 60
# Replaced in place on the phone by the next one; not the evening
# suggestion's tag, which it must not replace.
EXPORT_NOTIFY_TAG = "beem_ai_export"


class ExportAlert:
    """Detects a sustained grid export, once per episode."""

    def __init__(self) -> None:
        # Monotonic start of the current run at or above the threshold.
        self._since: float | None = None
        # First tick of the current dip under the threshold.
        self._dip_since: float | None = None
        # An alert went out for the current episode.
        self._notified: bool = False

    @property
    def notified(self) -> bool:
        """True from an alert until the export has stayed low for
        ``EXPORT_ALERT_REARM_S``."""
        return self._notified

    def update(
        self,
        export_w: float,
        threshold_w: float,
        duration_s: float,
        hold: bool = False,
        now: float | None = None,
    ) -> float | None:
        """Feed one live sample.  Returns how long the export has held
        (s) when the alert is due, else None."""
        now = time.monotonic() if now is None else now
        if threshold_w <= 0:
            self._since = self._dip_since = None
            self._notified = False
            return None

        if export_w >= threshold_w:
            self._dip_since = None
            if self._notified:
                return None
            if hold:
                self._since = None
                return None
            if self._since is None:
                self._since = now
            held = now - self._since
            if held < duration_s:
                return None
            self._notified = True
            self._since = None
            return held

        if self._dip_since is None:
            self._dip_since = now
        dip = now - self._dip_since
        if self._notified:
            if dip >= EXPORT_ALERT_REARM_S:
                self._notified = False
                _LOGGER.info(
                    "Export alert re-armed — export under %.0f W for %.0f min",
                    threshold_w, dip / 60,
                )
            return None
        # The grace never exceeds half the duration, or a short duration
        # would ride through a dip as long as itself.
        grace_s = min(EXPORT_ALERT_GRACE_S, max(1.0, duration_s / 2))
        if self._since is not None and dip >= grace_s:
            self._since = None
        return None


# ---------------------------------------------------------------------------
# Notification text
# ---------------------------------------------------------------------------


def _kw(watts: float, lang: str) -> str:
    text = f"{watts / 1000:.1f}"
    return text.replace(".", ",") if lang == "fr" else text


def _duration(seconds: float) -> str:
    if seconds >= 60:
        return f"{round(seconds / 60)} min"
    return f"{seconds:.0f} s"


def export_notification(
    export_w: float,
    held_s: float,
    soc: float,
    water_heater: str | None,
    lang: str,
) -> tuple[str, str]:
    """(title, message) for the export alert, French or English.

    ``water_heater``: "heating", "full" (fully heated today), "off", or
    None when no water heater is configured.
    """
    if lang == "fr":
        wh = {
            "heating": "Chauffe-eau : en chauffe.",
            "full": "Chauffe-eau : déjà chaud.",
            "off": "Chauffe-eau : éteint.",
        }
        lines = [
            f"Export de {_kw(export_w, lang)} kW vers le réseau depuis "
            f"{_duration(held_s)}, batterie à {soc:.0f} %.",
            "C'est le moment de lancer le lave-vaisselle ou un peu de "
            "chauffage.",
        ]
        title = "BeemAI · surplus solaire"
    else:
        wh = {
            "heating": "Water heater: heating.",
            "full": "Water heater: already hot.",
            "off": "Water heater: off.",
        }
        lines = [
            f"Exporting {_kw(export_w, lang)} kW to the grid for "
            f"{_duration(held_s)}, battery at {soc:.0f} %.",
            "A good time to start the dishwasher or some heating.",
        ]
        title = "BeemAI · solar surplus"
    if water_heater in wh:
        lines.append(wh[water_heater])
    return title, "\n".join(lines)
