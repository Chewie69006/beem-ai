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
MAX_CONSUMPTION_W = 7000  # Threshold used by the coordinator for overload coordination
DEFAULT_MIN_DURATION_S = 15 * 60  # Default minimum heating duration (user-configurable)
COOLDOWN_AFTER_EXTERNAL_OFF_S = 15 * 60  # Block restart after an unexpected OFF

# Fully-heated detection: power must stay below this for FULLY_HEATED_SUSTAIN_S
FULLY_HEATED_POWER_W = 50
FULLY_HEATED_SUSTAIN_S = 60


class WhMode(enum.Enum):
    """User-selected controller mode (from the BeemAI select entity)."""

    DISABLED = "Disabled"
    AUTO = "Auto"
    MANUAL = "Manual"


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
        # Arm for the surplus-loss stop (heating branch).  Set when the
        # start rules go False after min_duration; cleared as soon as
        # they go True again or when the switch turns off.
        self._stop_armed_since: float | None = None
        # Tracks what state we last commanded (or observed at startup).
        # An observed state differing from this is treated as an
        # external transition (auto-off timer on the plug, manual flip,
        # integration glitch).
        self._expected_state: str | None = None
        # Set when we observe an external ON→OFF; idle branch refuses
        # to start while this is in the future.
        self._cooldown_until_monotonic: float | None = None

        # Fully-heated tracking
        self._energy_today_wh: float = 0.0
        self._last_power_sample_time: float | None = None
        self._fully_heated: bool = False
        self._low_power_since: float | None = None

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

    def _read_power_w(self, power_entity_id: str | None) -> float | None:
        """Read instantaneous power from the WH power entity."""
        if not power_entity_id:
            return None
        state = self._hass.states.get(power_entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    # -- Public properties --

    @property
    def is_heating(self) -> bool:
        """Return True if the switch entity is on."""
        return self._is_switch_on()

    @property
    def fully_heated(self) -> bool:
        """Return True if the WH reached full temperature today."""
        return self._fully_heated

    @property
    def energy_today_wh(self) -> float:
        """Accumulated WH energy consumption today (Wh)."""
        return self._energy_today_wh

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
        power_entity_id: str | None = None,
        fully_heated_threshold_wh: float = 0,
    ) -> None:
        """Evaluate state machine and act.

        ``mode`` is the user-selected operating mode:
          - ``Disabled``: no-op; if heating, force-off.
          - ``Auto``:     sustained-surplus start, SoC-stop, overload-stop.
          - ``Manual``:   start on mode change; keeps running until user stops.

        Branches are driven by the live switch-entity state read at the
        top of the tick — never an in-memory copy.
        """
        now = time.monotonic()
        wh_mode = _mode_from_str(mode)
        is_on = self._is_switch_on()
        observed = "on" if is_on else "off"

        # Accumulate energy from power entity
        wh_power = self._read_power_w(power_entity_id)
        if wh_power is not None and is_on and wh_power > 0:
            if self._last_power_sample_time is not None:
                dt_h = (now - self._last_power_sample_time) / 3600.0
                if 0 < dt_h < 1:
                    self._energy_today_wh += wh_power * dt_h
            self._last_power_sample_time = now
        elif wh_power is not None and is_on:
            self._last_power_sample_time = now
        else:
            self._last_power_sample_time = None

        # Fully-heated detection: ON + low power + accumulated > threshold
        # Skip in Manual mode — it's an explicit user override.
        if (
            wh_mode != WhMode.MANUAL
            and is_on
            and wh_power is not None
            and fully_heated_threshold_wh > 0
            and self._energy_today_wh >= fully_heated_threshold_wh
            and wh_power < FULLY_HEATED_POWER_W
        ):
            if self._low_power_since is None:
                self._low_power_since = now
            elif now - self._low_power_since >= FULLY_HEATED_SUSTAIN_S:
                if not self._fully_heated:
                    _LOGGER.info(
                        "Water heater: fully heated — consumed %.0f Wh today "
                        "(threshold %.0f Wh), power %.1f W — "
                        "disabling for rest of day",
                        self._energy_today_wh, fully_heated_threshold_wh,
                        wh_power,
                    )
                    self._fully_heated = True
                    await self._turn_off()
                    self._clear_session()
                    return
        else:
            self._low_power_since = None

        # Detect transitions we didn't command (plug auto-off timer,
        # manual toggle, integration glitch).  On unexpected OFF, arm
        # the cooldown so we don't immediately re-fire on a residual
        # surplus reading.
        if self._expected_state is not None and observed != self._expected_state:
            if observed == "off":
                _LOGGER.warning(
                    "Water heater: switch turned off externally "
                    "(plug auto-off, manual, or integration glitch) — "
                    "applying %ds cooldown before any restart",
                    COOLDOWN_AFTER_EXTERNAL_OFF_S,
                )
                self._cooldown_until_monotonic = (
                    now + COOLDOWN_AFTER_EXTERNAL_OFF_S
                )
                self._clear_session()
            else:
                _LOGGER.warning(
                    "Water heater: switch turned on externally — "
                    "controller will adopt the running session",
                )
        self._expected_state = observed

        # Start rules — computed once here so both idle (fire-time
        # validation) and heating (symmetric stop) see the same view.
        rule1 = soc >= EXPORT_SOC_THRESHOLD and export_w > 0
        rule2 = (
            soc >= soc_threshold
            and charge_power_w >= charge_power_threshold
            and import_w <= 0
        )

        if wh_mode == WhMode.DISABLED:
            if is_on:
                _LOGGER.info(
                    "Water heater: mode=Disabled and switch is on — turning off"
                )
                await self._turn_off()
            self._clear_session()
            # Disabled is an explicit user action — clear cooldown so
            # flipping back to Auto re-arms cleanly.
            self._cooldown_until_monotonic = None
            decision = "disabled"
        elif self._fully_heated:
            # Fully heated today — refuse to start/keep running
            if is_on:
                _LOGGER.info(
                    "Water heater: fully heated today — turning off"
                )
                await self._turn_off()
                self._clear_session()
            decision = "blocked: fully heated today"
        elif wh_mode == WhMode.MANUAL:
            if is_on:
                decision = "heating: manual mode"
            else:
                decision = "idle: Manual mode — waiting for user start"
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
                soc, consumption_w, import_w, now,
                min_duration_s=min_duration_s,
                start_conditions_met=(rule1 or rule2),
                sustain_seconds=sustain_seconds,
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
                rule1=rule1, rule2=rule2,
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
        rule1: bool | None = None,
        rule2: bool | None = None,
    ) -> str:
        """IDLE state: check if either rule triggers."""
        # Grace period never exceeds half the sustain window — otherwise a
        # condition that's been false for longer than sustain would never
        # reset the timer.
        grace_s = min(GRACE_SECONDS, max(1, sustain_seconds // 2))
        if rule1 is None:
            rule1 = soc >= EXPORT_SOC_THRESHOLD and export_w > 0
        if rule2 is None:
            rule2 = (
                soc >= soc_threshold
                and charge_power_w >= charge_power_threshold
                and import_w <= 0
            )

        # Cooldown after an external OFF — refuse to start until it expires.
        if (
            self._cooldown_until_monotonic is not None
            and now < self._cooldown_until_monotonic
        ):
            self._sustained_since = None
            self._last_ok_at = None
            remaining = self._cooldown_until_monotonic - now
            return f"idle: cooldown ({remaining:.0f}s remaining)"
        if (
            self._cooldown_until_monotonic is not None
            and now >= self._cooldown_until_monotonic
        ):
            _LOGGER.info("Water heater: cooldown expired — restart allowed")
            self._cooldown_until_monotonic = None

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
        start_conditions_met: bool,
        sustain_seconds: int,
    ) -> str:
        """HEATING state: stop on SoC drop or sustained surplus loss.

        Overload (house ≥ 7 kW with import) is now coordinated at a
        higher level (see ``BeemCoordinator._handle_overload``) which
        throttles the EV charger first and only calls
        :py:meth:`force_stop_overload` after a grace window.  The
        heating branch itself only handles surplus-driven stops:

        - **SoC drop** — when SoC falls below the active stop threshold
          (active start threshold − HYSTERESIS_PCT), provided
          ``min_duration_s`` has elapsed.
        - **Surplus lost** — once ``min_duration_s`` has elapsed, if
          neither start rule is true for ``sustain_seconds``, we stop.
          This is symmetric to the start path and is what prevents the
          heater from draining the battery once solar fades.

        ``min_duration_s`` acts as a floor on both stops: the heater
        always runs at least that long after any commanded turn-on,
        unless the coordinator force-stops it.
        """
        assert self._active_soc_threshold is not None  # set in evaluate()
        stop_threshold = self._active_soc_threshold - HYSTERESIS_PCT
        elapsed = self._seconds_since_turned_on()
        min_duration_met = elapsed is not None and elapsed >= min_duration_s

        if soc < stop_threshold:
            if not min_duration_met:
                remaining = (
                    min_duration_s - elapsed
                    if elapsed is not None
                    else min_duration_s
                )
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

        # Surplus-loss stop, only enforced once the minimum-duration
        # floor has been crossed.  Tracks an arm timer so a brief dip
        # doesn't immediately cut a session.
        if not min_duration_met:
            self._stop_armed_since = None
            remaining = (
                min_duration_s - elapsed
                if elapsed is not None
                else min_duration_s
            )
            return (
                f"heating: SoC {soc:.1f}% (stop at {stop_threshold:.1f}%, "
                f"min-duration {remaining:.0f}s remaining)"
            )

        if not start_conditions_met:
            if self._stop_armed_since is None:
                self._stop_armed_since = now
                _LOGGER.info(
                    "Water heater: surplus lost while heating — "
                    "waiting %ds sustained before stopping",
                    sustain_seconds,
                )
                return f"heating: stop armed ({sustain_seconds}s)"
            sustained = now - self._stop_armed_since
            if sustained >= sustain_seconds:
                _LOGGER.info(
                    "Water heater: surplus lost sustained %.0fs — "
                    "turning off (SoC=%.1f%%)",
                    sustained, soc,
                )
                await self._turn_off()
                self._clear_session()
                return f"stop: surplus lost (sustained {sustained:.0f}s)"
            return (
                f"heating: stop sustaining {sustained:.0f}s/{sustain_seconds}s"
            )

        # Conditions came back — disarm the stop timer.
        if self._stop_armed_since is not None:
            _LOGGER.info("Water heater: surplus returned — stop arm cleared")
            self._stop_armed_since = None
        return f"heating: SoC {soc:.1f}% (stop at {stop_threshold:.1f}%)"

    # -- Switch control --

    async def _turn_on(self) -> None:
        """Turn on the water heater switch."""
        await self._hass.services.async_call(
            "homeassistant",
            "turn_on",
            {"entity_id": self._switch_entity_id},
        )
        self._expected_state = "on"

    async def _turn_off(self) -> None:
        """Turn off the water heater switch."""
        await self._hass.services.async_call(
            "homeassistant",
            "turn_off",
            {"entity_id": self._switch_entity_id},
        )
        self._expected_state = "off"

    async def force_stop_overload(self, consumption_w: float) -> None:
        """Force-stop bypassing min-duration — used by the coordinator
        when overload protection demands the heater go off.
        """
        if not self._is_switch_on():
            return
        _LOGGER.warning(
            "Water heater: force-stop on sustained overload (cons=%.0fW)",
            consumption_w,
        )
        await self._turn_off()
        self._clear_session()

    def _clear_session(self) -> None:
        """Reset session bookkeeping (timers + active threshold)."""
        self._sustained_since = None
        self._last_ok_at = None
        self._active_soc_threshold = None
        self._stop_armed_since = None
        self._low_power_since = None

    # -- Manual mode control --

    async def start_manual(self) -> None:
        """Start heating manually."""
        if self._is_switch_on():
            return
        _LOGGER.info("Water heater: manual start requested")
        await self._turn_on()

    async def stop(self) -> None:
        """Stop heating (from any mode)."""
        if not self._is_switch_on():
            return
        _LOGGER.info("Water heater: stop requested")
        await self._turn_off()
        self._clear_session()

    # -- Mode control --

    async def handle_mode_change(self, mode: str) -> None:
        """React to a user-driven mode change from the select entity."""
        wh_mode = _mode_from_str(mode)
        if wh_mode == WhMode.DISABLED:
            _LOGGER.info("Water heater: mode set to Disabled — stopping")
            if self._is_switch_on():
                await self._turn_off()
            self._clear_session()
        elif wh_mode == WhMode.MANUAL:
            # Manual overrides fully-heated lockout
            if self._fully_heated:
                _LOGGER.info(
                    "Water heater: Manual mode clears fully-heated lockout"
                )
                self._fully_heated = False
            if not self._is_switch_on():
                _LOGGER.info("Water heater: mode set to Manual — starting")
                self._cooldown_until_monotonic = None
                await self.start_manual()

    # -- Daily reset --

    def reset_daily(self) -> None:
        """Reset daily accumulators (called by coordinator at daily reset)."""
        self._energy_today_wh = 0.0
        self._fully_heated = False
        self._low_power_since = None
        self._last_power_sample_time = None
        _LOGGER.info("Water heater: daily reset — energy and fully-heated cleared")

    # -- Lifecycle --

    def reconfigure(self, switch_entity_id: str) -> None:
        """Update entity ID from options."""
        self._switch_entity_id = switch_entity_id
        self._clear_session()
        _LOGGER.info(
            "Water heater controller reconfigured: switch=%s",
            switch_entity_id,
        )
