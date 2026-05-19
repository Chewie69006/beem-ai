"""Water heater controller — diverts solar surplus to hot water.

Source of truth for "is the heater on?" is the HA switch entity itself —
we never trust an in-memory copy of it.  Each ``evaluate()`` reads the
entity state once at the top of the tick and branches on that.
"""

from __future__ import annotations

import enum
import logging
import time
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Rule 1: hardcoded "final boss" — always active
EXPORT_SOC_THRESHOLD = 95.0  # SoC must be > this AND exporting

DEFAULT_SUSTAIN_SECONDS = 30  # Default sustain window (user-configurable)
SUSTAIN_SECONDS = DEFAULT_SUSTAIN_SECONDS  # Back-compat alias for existing imports
GRACE_SECONDS = 15  # Brief condition dips under this don't reset the sustain timer
HYSTERESIS_PCT = 10.0  # SoC hysteresis to prevent cycling
MAX_CONSUMPTION_W = 7000  # Kill diverters if house exceeds this
DEFAULT_MIN_DURATION_S = 15 * 60  # Default minimum heating duration (user-configurable)


class WhMode(enum.Enum):
    """User-selected controller mode (from the BeemAI select entity)."""

    DISABLED = "Disabled"
    AUTO = "Auto"


def _mode_from_str(mode: str) -> WhMode:
    """Parse a user-facing mode string into the enum; default to AUTO."""
    try:
        return WhMode(mode)
    except ValueError:
        return WhMode.AUTO


class WaterHeaterController:
    """Controls a water heater switch based on solar surplus."""

    def __init__(
        self,
        hass: HomeAssistant,
        switch_entity_id: str,
    ) -> None:
        self._hass = hass
        self._switch_entity_id = switch_entity_id

        # Session bookkeeping — only meaningful while heater is physically on.
        # Cleared on every off→on or on→off transition.
        self._sustained_since: float | None = None
        self._last_ok_at: float | None = None
        self._active_soc_threshold: float | None = None

    # -- Entity reads --

    def _is_switch_on(self) -> bool:
        """Read the switch entity state directly from HA."""
        state = self._hass.states.get(self._switch_entity_id)
        return state is not None and state.state == "on"

    def _seconds_since_turned_on(self) -> float | None:
        """Wall-clock seconds since the switch last transitioned to on.

        Returns ``None`` if the entity has no ``last_changed`` (shouldn't
        happen in real HA, but possible with bare mocks).
        """
        state = self._hass.states.get(self._switch_entity_id)
        if state is None:
            return None
        last_changed = getattr(state, "last_changed", None)
        if not isinstance(last_changed, datetime):
            return None
        return (datetime.now(timezone.utc) - last_changed).total_seconds()

    # -- Public properties --

    @property
    def is_heating(self) -> bool:
        """Return True if the switch entity is on."""
        return self._is_switch_on()

    # -- Core evaluate (called on every MQTT update) --

    async def evaluate(
        self,
        soc: float,
        export_w: float,
        charge_power_w: float,
        consumption_w: float,
        import_w: float,
        soc_threshold: float,
        charge_power_threshold: float,
        sustain_seconds: int = DEFAULT_SUSTAIN_SECONDS,
        min_duration_s: int = DEFAULT_MIN_DURATION_S,
        mode: str = WhMode.AUTO.value,
    ) -> None:
        """Evaluate state machine and act.

        ``mode`` is the user-selected operating mode:
          - ``Disabled``: no-op; if heating, force-off.
          - ``Auto``:     sustained-surplus start, SoC-stop, overload-stop.

        Branches are driven by the live switch-entity state read at the
        top of the tick — never an in-memory copy.
        """
        now = time.monotonic()
        wh_mode = _mode_from_str(mode)
        is_on = self._is_switch_on()

        if wh_mode == WhMode.DISABLED:
            if is_on:
                _LOGGER.info(
                    "Water heater: mode=Disabled and switch is on — turning off"
                )
                await self._turn_off()
            self._clear_session()
            decision = "disabled"
        elif is_on:
            # Heating branch — initialize session state if this is the
            # first tick of a session we didn't start ourselves (external
            # toggle, HA restart, or options reload).
            if self._active_soc_threshold is None:
                self._active_soc_threshold = min(
                    EXPORT_SOC_THRESHOLD, soc_threshold
                )
                _LOGGER.info(
                    "Water heater: switch is on without active session — "
                    "adopting stop threshold SoC < %.1f%%",
                    self._active_soc_threshold - HYSTERESIS_PCT,
                )
            decision = await self._evaluate_heating(
                soc, consumption_w, import_w, now, min_duration_s,
            )
        else:
            # Idle branch — if we had an active session, the switch was
            # turned off externally (or we just stopped ourselves); clear
            # the post-start bookkeeping.  Don't touch the sustain timer —
            # _evaluate_idle owns that.
            if self._active_soc_threshold is not None:
                self._active_soc_threshold = None
            decision = await self._evaluate_idle(
                soc, export_w, charge_power_w,
                soc_threshold, charge_power_threshold, now,
                import_w=import_w,
                sustain_seconds=sustain_seconds,
            )

        _LOGGER.debug(
            "WH eval: mode=%s on=%s soc=%.1f%% socThr=%.1f%% "
            "export=%.0fW chargeP=%.0fW chargeThr=%.0fW import=%.0fW "
            "cons=%.0fW → %s",
            wh_mode.value, is_on,
            soc, soc_threshold,
            export_w, charge_power_w, charge_power_threshold, import_w,
            consumption_w, decision,
        )

    async def _evaluate_idle(
        self,
        soc: float,
        export_w: float,
        charge_power_w: float,
        soc_threshold: float,
        charge_power_threshold: float,
        now: float,
        import_w: float = 0.0,
        sustain_seconds: int = DEFAULT_SUSTAIN_SECONDS,
    ) -> str:
        """IDLE state: check if either rule triggers."""
        # Grace period never exceeds half the sustain window — otherwise a
        # condition that's been false for longer than sustain would never
        # reset the timer.
        grace_s = min(GRACE_SECONDS, max(1, sustain_seconds // 2))
        rule1 = soc >= EXPORT_SOC_THRESHOLD and export_w > 0
        rule2 = (
            soc >= soc_threshold
            and charge_power_w >= charge_power_threshold
            and import_w <= 0
        )

        if rule1 or rule2:
            if rule1 and rule2:
                active_soc = min(EXPORT_SOC_THRESHOLD, soc_threshold)
            elif rule1:
                active_soc = EXPORT_SOC_THRESHOLD
            else:
                active_soc = soc_threshold

            reason = (
                f"export={export_w:.0f}W" if rule1
                else f"charge={charge_power_w:.0f}W"
            )

            self._last_ok_at = now
            if self._sustained_since is None:
                self._sustained_since = now
                _LOGGER.info(
                    "Water heater: surplus detected — SoC=%.1f%%, %s, "
                    "waiting %ds sustained before turning on",
                    soc, reason, sustain_seconds,
                )
                return f"idle: arming sustain ({sustain_seconds}s, {reason})"
            elif now - self._sustained_since >= sustain_seconds:
                _LOGGER.info(
                    "Solar surplus detected: SoC=%.1f%%, %s "
                    "(sustained %.0fs) — turning on water heater",
                    soc, reason, now - self._sustained_since,
                )
                self._active_soc_threshold = active_soc
                self._sustained_since = None
                self._last_ok_at = None
                await self._turn_on()
                return f"start: {reason} (active SoC={active_soc:.1f}%)"
            else:
                sustained = now - self._sustained_since
                return (
                    f"idle: sustaining {sustained:.0f}s/{sustain_seconds}s "
                    f"({reason})"
                )

        if (
            self._sustained_since is not None
            and self._last_ok_at is not None
            and now - self._last_ok_at >= grace_s
        ):
            self._sustained_since = None
            self._last_ok_at = None
            return "idle: sustain reset (grace expired)"
        if self._sustained_since is not None:
            return "idle: in grace (conditions dipped)"
        return (
            f"idle: no trigger (rule1={'y' if soc >= EXPORT_SOC_THRESHOLD else 'n'}/"
            f"export={export_w:.0f}W, "
            f"rule2 SoC>{soc_threshold:.0f} & charge>{charge_power_threshold:.0f}W)"
        )

    async def _evaluate_heating(
        self,
        soc: float,
        consumption_w: float,
        import_w: float,
        now: float,
        min_duration_s: int,
    ) -> str:
        """HEATING state: stop on SoC drop or overload.

        Overload (house ≥ 7 kW AND importing) is a safety override and
        fires regardless of minimum duration.  SoC-drop stop is deferred
        until the heater has been running for at least ``min_duration_s``
        (measured from the switch entity's ``last_changed``).
        """
        if consumption_w >= MAX_CONSUMPTION_W and import_w > 0:
            _LOGGER.info(
                "Water heater: consumption %.0fW >= %dW and importing %.0fW "
                "— turning off to protect grid",
                consumption_w, MAX_CONSUMPTION_W, import_w,
            )
            await self._turn_off()
            self._clear_session()
            return f"stop: overload (cons {consumption_w:.0f}W, import {import_w:.0f}W)"

        assert self._active_soc_threshold is not None  # set in evaluate()
        stop_threshold = self._active_soc_threshold - HYSTERESIS_PCT
        if soc < stop_threshold:
            elapsed = self._seconds_since_turned_on()
            if elapsed is not None and elapsed < min_duration_s:
                remaining = min_duration_s - elapsed
                _LOGGER.debug(
                    "Water heater: SoC=%.1f%% below stop=%.1f%% but min "
                    "duration not met (%.0fs remaining) — keeping on",
                    soc, stop_threshold, remaining,
                )
                return (
                    f"hold: SoC<{stop_threshold:.1f}% but min-duration "
                    f"{remaining:.0f}s remaining"
                )

            _LOGGER.info(
                "Battery SoC dropped to %.1f%% (< %.1f%%) — turning off "
                "water heater",
                soc, stop_threshold,
            )
            await self._turn_off()
            self._clear_session()
            return f"stop: SoC {soc:.1f}% < {stop_threshold:.1f}%"
        return f"heating: SoC {soc:.1f}% (stop at {stop_threshold:.1f}%)"

    # -- Switch control --

    async def _turn_on(self) -> None:
        """Turn on the water heater switch."""
        await self._hass.services.async_call(
            "homeassistant",
            "turn_on",
            {"entity_id": self._switch_entity_id},
        )

    async def _turn_off(self) -> None:
        """Turn off the water heater switch."""
        await self._hass.services.async_call(
            "homeassistant",
            "turn_off",
            {"entity_id": self._switch_entity_id},
        )

    def _clear_session(self) -> None:
        """Reset session bookkeeping (timers + active threshold)."""
        self._sustained_since = None
        self._last_ok_at = None
        self._active_soc_threshold = None

    # -- Mode control --

    async def handle_mode_change(self, mode: str) -> None:
        """React to a user-driven mode change from the select entity."""
        wh_mode = _mode_from_str(mode)
        if wh_mode == WhMode.DISABLED:
            _LOGGER.info("Water heater: mode set to Disabled — stopping")
            if self._is_switch_on():
                await self._turn_off()
            self._clear_session()

    # -- Lifecycle --

    def reconfigure(self, switch_entity_id: str) -> None:
        """Update entity ID from options."""
        self._switch_entity_id = switch_entity_id
        self._clear_session()
        _LOGGER.info(
            "Water heater controller reconfigured: switch=%s",
            switch_entity_id,
        )
