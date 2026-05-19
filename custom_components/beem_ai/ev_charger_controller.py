"""EV charger controller — second-priority solar surplus diverter.

Source of truth for "is the EV charging?" and "what's the current
amperage?" is the HA toggle + amperage entities themselves — we never
trust an in-memory copy.  Each ``evaluate()`` reads them once at the
top of the tick and branches on that.

Starts at 6A when real surplus is sustained, then adjusts ±1A per MQTT
cycle toward the target.  Surplus is computed directly from the grid
meter and battery power signals (not from ``consumption_w``), so we
avoid the phantom-surplus feedback loop that happens when the
consumption sensor does not include EV draw.

Headroom model
--------------
Sign conventions:
  - ``meter_power_w``  : + import  / - export
  - ``battery_power_w``: + charging / - discharging

The available headroom we can divert to the EV is::

    headroom_w = -meter_power_w + battery_power_w

That is, every watt of export we're currently throwing away plus every
watt of solar we're currently stashing in the battery.

When a water heater is configured, the EV charger waits for it to be ON
before starting.  Without a water heater, the EV starts on surplus alone.
"""

from __future__ import annotations

import enum
import logging
import time

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

# Amperage limits
MIN_CHARGE_AMPS = 6
MAX_CHARGE_AMPS = 32

# Watts per amp (single-phase 230 V)
WATTS_PER_AMP = 230

# Minimum sustained headroom before we start at 6 A.
START_HEADROOM_W = MIN_CHARGE_AMPS * WATTS_PER_AMP
SUSTAIN_SECONDS = 30
GRACE_SECONDS = 15

REGULATE_INTERVAL_S = 30

# Wallbox's cloud API frequently raises HomeAssistantError client-side
# even when the action succeeded on the device. Hold session state for
# this long after issuing turn_on so the entity has time to catch up
# instead of resetting the state machine on the next tick.
PENDING_START_GRACE_S = 120
# Cadence for nudging the Wallbox integration to repoll during the
# pending window.
PENDING_REFRESH_INTERVAL_S = 20

EMERGENCY_SHRINK_W = 500

MAX_CONSUMPTION_W = 7000

# Wallbox `sensor.*_status_description` values that mean "the car is
# physically not drawing current right now" — typically because the EV's
# BMS hit its own target SoC, the schedule is paused, or the car is
# simply idle. When the configured status entity reports one of these
# for STATUS_NO_DEMAND_SUSTAIN_S, we treat the session as complete and
# turn the switch off so it doesn't sit in "resume" forever.
NO_DEMAND_STATUSES = frozenset({
    "waiting for car demand",
    "connected: waiting for car demand",
    "ready",
    "paused",
    "scheduled",
    "locked, car connected",
})
STATUS_NO_DEMAND_SUSTAIN_S = 60

SOC_BIAS_AMPS = 1
SOC_DEADBAND_PCT = 0.5


class EvMode(enum.Enum):
    """User-selected controller mode (from the BeemAI select entity)."""

    DISABLED = "Disabled"
    AUTO = "Auto"
    MANUAL = "Manual"


class StartMode(enum.Enum):
    """Who / which mode initiated the current charging session."""

    AUTO = "auto"
    MANUAL = "manual"


def _mode_from_str(mode: str) -> EvMode:
    """Parse a user-facing mode string into the enum; default to AUTO."""
    try:
        return EvMode(mode)
    except ValueError:
        return EvMode.AUTO


class EvChargerController:
    """Controls an EV charger based on real grid + battery headroom."""

    def __init__(
        self,
        hass: HomeAssistant,
        toggle_entity_id: str,
        power_entity_id: str,
        status_entity_id: str | None = None,
    ) -> None:
        self._hass = hass
        self._toggle_entity_id = toggle_entity_id
        self._power_entity_id = power_entity_id
        self._status_entity_id = status_entity_id or None

        # Session bookkeeping — only meaningful while charger is physically
        # on.  Cleared on every on→off transition (and re-initialized on
        # off→on transitions we didn't drive ourselves).
        self._start_mode: StartMode | None = None
        self._saved_amps: int | None = None
        self._export_sustained_since: float | None = None
        self._last_headroom_ok_at: float | None = None
        self._last_regulate_time: float = 0.0
        # Set when we issue turn_on; cleared when the entity confirms
        # it's on OR when PENDING_START_GRACE_S elapses without
        # confirmation. Lets us survive Wallbox's noisy cloud API.
        self._pending_start_since: float | None = None
        self._last_entity_refresh_at: float = 0.0
        # First time we observed the status entity reporting "no demand"
        # in the current session. Cleared whenever the car resumes
        # drawing or the session ends.
        self._no_demand_since: float | None = None

    # -- Entity reads --

    def _is_switch_on(self) -> bool:
        """Read the toggle entity state directly from HA."""
        state = self._hass.states.get(self._toggle_entity_id)
        return state is not None and state.state == "on"

    def _read_amps(self) -> int | None:
        """Read the current amperage from the HA number entity."""
        state = self._hass.states.get(self._power_entity_id)
        if state is None:
            return None
        try:
            return int(float(state.state))
        except (ValueError, TypeError):
            return None

    def _read_amps_clamped(self) -> int:
        """Read amps, clamped to [MIN, MAX]; falls back to MIN if unreadable."""
        amps = self._read_amps()
        if amps is None:
            return MIN_CHARGE_AMPS
        return max(MIN_CHARGE_AMPS, min(MAX_CHARGE_AMPS, amps))

    def _read_status(self) -> str | None:
        """Read the charger status entity (lowercased), if configured."""
        if not self._status_entity_id:
            return None
        state = self._hass.states.get(self._status_entity_id)
        if state is None or state.state in (None, "", "unknown", "unavailable"):
            return None
        return str(state.state).strip().lower()

    # -- Public properties --

    @property
    def is_charging(self) -> bool:
        """Return True if the toggle entity is on."""
        return self._is_switch_on()

    @property
    def current_amps(self) -> int:
        """Return the current charging amperage (read from the HA entity)."""
        return self._read_amps_clamped()

    # -- Manual / mode control --

    async def start_manual(self) -> None:
        """Start charging manually at minimum amps."""
        if self._is_switch_on():
            return
        _LOGGER.info("EV charger: manual start requested")
        self._saved_amps = self._read_amps()
        self._last_regulate_time = time.monotonic()
        self._start_mode = StartMode.MANUAL
        self._export_sustained_since = None
        self._last_headroom_ok_at = None
        await self._set_amps(MIN_CHARGE_AMPS)
        await self._turn_on()

    async def stop(self) -> None:
        """Stop charging (from any mode)."""
        if not self._is_switch_on() and self._start_mode is None:
            return
        _LOGGER.info("EV charger: stop requested")
        await self._turn_off_and_restore()
        self._clear_session()

    async def handle_mode_change(self, mode: str) -> None:
        """React to a user-driven mode change from the select entity.

        - ``Disabled``: stop immediately.
        - ``Manual``:   start immediately at 6A if idle (no sustain wait).
        - ``Auto``:     no immediate action; ``evaluate()`` will take over.
        """
        ev_mode = _mode_from_str(mode)
        if ev_mode == EvMode.DISABLED:
            _LOGGER.info("EV charger: mode set to Disabled — stopping")
            if self._is_switch_on():
                await self._turn_off_and_restore()
            self._clear_session()
        elif ev_mode == EvMode.MANUAL:
            if not self._is_switch_on():
                _LOGGER.info(
                    "EV charger: mode set to Manual — starting at %dA",
                    MIN_CHARGE_AMPS,
                )
                await self.start_manual()

    # -- Core evaluate (called on every MQTT update, after water heater) --

    async def evaluate(
        self,
        soc: float,
        meter_power_w: float,
        battery_power_w: float,
        solar_power_w: float,
        consumption_w: float,
        water_heater_heating: bool | None,
        target_soc: float,
        soc_hysteresis: float,
        mode: str = EvMode.AUTO.value,
    ) -> None:
        """Evaluate state machine and act.

        Branches are driven by the live toggle-entity state read at the
        top of the tick.  See module docstring for the headroom model.
        """
        now = time.monotonic()
        headroom_w = -meter_power_w + battery_power_w
        ev_mode = _mode_from_str(mode)
        is_on = self._is_switch_on()
        amps = self._read_amps_clamped()

        if ev_mode == EvMode.DISABLED:
            if is_on:
                _LOGGER.info("EV charger: mode=Disabled and switch is on — stopping")
                await self._turn_off_and_restore()
            self._clear_session()
            decision = "disabled"
        elif is_on:
            # Charging branch — initialize session bookkeeping if this is
            # the first tick of a session we didn't start ourselves
            # (external toggle, HA restart, options reload).  Conservative
            # defaults: assume MANUAL (keeps charger at min on overload
            # rather than stopping outright) and no saved amps.
            if self._pending_start_since is not None:
                _LOGGER.info(
                    "EV charger: pending start confirmed after %.0fs — "
                    "entity now reports on",
                    now - self._pending_start_since,
                )
                self._pending_start_since = None
            if self._start_mode is None:
                self._start_mode = StartMode.MANUAL
                self._last_regulate_time = now
                _LOGGER.info(
                    "EV charger: switch is on without active session — "
                    "adopting MANUAL mode at %dA",
                    amps,
                )
            decision = await self._evaluate_charging(
                soc, amps, headroom_w, battery_power_w,
                solar_power_w, consumption_w,
                target_soc, soc_hysteresis, now, ev_mode,
            )
        elif self._pending_start_since is not None:
            # We issued turn_on but the entity hasn't flipped yet.
            # Wallbox's cloud often acks the action after the HTTP call
            # has already errored client-side, so wait out the grace
            # window before resetting state. Nudge HA to repoll
            # periodically so we don't sit on a stale cache.
            pending_elapsed = now - self._pending_start_since
            if pending_elapsed >= PENDING_START_GRACE_S:
                _LOGGER.warning(
                    "EV charger: entity still off %.0fs after turn_on — "
                    "giving up on this attempt",
                    pending_elapsed,
                )
                self._pending_start_since = None
                self._start_mode = None
                self._saved_amps = None
                if ev_mode == EvMode.AUTO:
                    decision = await self._evaluate_idle(
                        soc, headroom_w, solar_power_w, consumption_w,
                        water_heater_heating, target_soc, now,
                    )
                else:
                    decision = "idle: Manual mode — waiting for user start"
            else:
                if now - self._last_entity_refresh_at >= PENDING_REFRESH_INTERVAL_S:
                    self._last_entity_refresh_at = now
                    await self._refresh_toggle_entity()
                decision = (
                    f"pending start: waiting for entity "
                    f"({pending_elapsed:.0f}s/{PENDING_START_GRACE_S}s)"
                )
        else:
            # Idle branch — if we had an active session, the switch was
            # turned off externally (or we just stopped ourselves and
            # came back round to evaluate); either way, clear the
            # post-start bookkeeping.  Don't touch the sustain timer —
            # _evaluate_idle owns that.
            if self._start_mode is not None:
                self._start_mode = None
                self._saved_amps = None
            if ev_mode == EvMode.AUTO:
                decision = await self._evaluate_idle(
                    soc, headroom_w, solar_power_w, consumption_w,
                    water_heater_heating, target_soc, now,
                )
            else:
                decision = "idle: Manual mode — waiting for user start"

        _LOGGER.debug(
            "EV eval: mode=%s on=%s amps=%d soc=%.1f%% target=%.1f%% "
            "hyst=%.1f%% meter=%+.0fW batt=%+.0fW headroom=%+.0fW "
            "solar=%.0fW cons=%.0fW wh=%s → %s",
            ev_mode.value, is_on, amps,
            soc, target_soc, soc_hysteresis,
            meter_power_w, battery_power_w, headroom_w,
            solar_power_w, consumption_w, water_heater_heating,
            decision,
        )

    async def _evaluate_idle(
        self,
        soc: float,
        headroom_w: float,
        solar_power_w: float,
        consumption_w: float,
        water_heater_heating: bool | None,
        target_soc: float,
        now: float,
    ) -> str:
        """IDLE state: check if we should start charging (Auto mode)."""
        wh_ok = water_heater_heating is None or water_heater_heating
        conditions_met = (
            wh_ok
            and soc >= target_soc
            and headroom_w >= START_HEADROOM_W
        )

        if conditions_met:
            self._last_headroom_ok_at = now
            if self._export_sustained_since is None:
                self._export_sustained_since = now
                _LOGGER.info(
                    "EV charger: headroom detected — SoC=%.1f%%, "
                    "solar=%.0fW, consumption=%.0fW, headroom=%.0fW, "
                    "wh=%s, waiting %ds sustained",
                    soc, solar_power_w, consumption_w, headroom_w,
                    water_heater_heating, SUSTAIN_SECONDS,
                )
                return f"idle: arming sustain timer ({SUSTAIN_SECONDS}s)"

            sustained = now - self._export_sustained_since
            if sustained < SUSTAIN_SECONDS:
                return f"idle: sustaining {sustained:.0f}s/{SUSTAIN_SECONDS}s"

            _LOGGER.info(
                "EV charger: headroom sustained %.0fs — SoC=%.1f%%, "
                "solar=%.0fW, consumption=%.0fW, headroom=%.0fW — "
                "starting at %dA",
                sustained, soc, solar_power_w, consumption_w, headroom_w,
                MIN_CHARGE_AMPS,
            )
            self._saved_amps = self._read_amps()
            self._last_regulate_time = now
            self._start_mode = StartMode.AUTO
            self._export_sustained_since = None
            self._last_headroom_ok_at = None
            _LOGGER.info(
                "EV charger: saved user amps=%s before taking over",
                self._saved_amps,
            )
            await self._set_amps(MIN_CHARGE_AMPS)
            await self._turn_on()
            return f"start AUTO at {MIN_CHARGE_AMPS}A"

        if (
            self._export_sustained_since is not None
            and self._last_headroom_ok_at is not None
            and now - self._last_headroom_ok_at >= GRACE_SECONDS
        ):
            self._export_sustained_since = None
            self._last_headroom_ok_at = None

        if not wh_ok:
            return "idle: water heater prerequisite not met"
        if soc < target_soc:
            return f"idle: SoC {soc:.1f}% < target {target_soc:.1f}%"
        return f"idle: headroom {headroom_w:.0f}W < {START_HEADROOM_W}W"

    async def _evaluate_charging(
        self,
        soc: float,
        amps: int,
        headroom_w: float,
        battery_power_w: float,
        solar_power_w: float,
        consumption_w: float,
        target_soc: float,
        soc_hysteresis: float,
        now: float,
        ev_mode: EvMode,
    ) -> str:
        """CHARGING state: regulate amps or stop on low SoC / overload."""
        stop_soc = target_soc - soc_hysteresis

        # Car-not-drawing stop (Wallbox status entity).  Once the car's
        # BMS hits its own SoC target, the Wallbox stays in "resume"
        # state but reports e.g. "Waiting for car demand".  Holding the
        # switch on does nothing useful, blocks an Auto re-arm, and
        # leaves the user confused.  Sustain to ignore brief
        # session-handshake transitions.
        status = self._read_status()
        if status is not None and status in NO_DEMAND_STATUSES:
            if self._no_demand_since is None:
                self._no_demand_since = now
                _LOGGER.info(
                    "EV charger: status=%r reports no car demand — "
                    "waiting %ds sustained before stopping",
                    status, STATUS_NO_DEMAND_SUSTAIN_S,
                )
                return f"charging: no-demand armed ({status!r})"
            elapsed = now - self._no_demand_since
            if elapsed >= STATUS_NO_DEMAND_SUSTAIN_S:
                _LOGGER.info(
                    "EV charger: status=%r sustained %.0fs — "
                    "stopping (car not drawing)",
                    status, elapsed,
                )
                await self._turn_off_and_restore()
                self._clear_session()
                return f"stop: no car demand ({status!r})"
            return (
                f"charging: no-demand sustaining "
                f"{elapsed:.0f}s/{STATUS_NO_DEMAND_SUSTAIN_S}s"
            )
        # Car resumed drawing (or status unknown) — drop the arm.
        if self._no_demand_since is not None:
            self._no_demand_since = None

        # Overload protection — safety override for both modes.
        if consumption_w >= MAX_CONSUMPTION_W:
            if ev_mode == EvMode.MANUAL:
                _LOGGER.info(
                    "EV charger (Manual): consumption %.0fW >= %dW — "
                    "stopping EV charging (safety override)",
                    consumption_w, MAX_CONSUMPTION_W,
                )
                await self._turn_off_and_restore()
                self._clear_session()
                return f"stop: Manual overload (cons {consumption_w:.0f}W)"

            target = amps - 1
            if target < MIN_CHARGE_AMPS:
                _LOGGER.info(
                    "EV charger: consumption %.0fW >= %dW and already at "
                    "minimum — stopping EV charging",
                    consumption_w, MAX_CONSUMPTION_W,
                )
                await self._turn_off_and_restore()
                self._clear_session()
                return f"stop: overload at min (cons {consumption_w:.0f}W)"

            _LOGGER.info(
                "EV charger: overload %.0fW >= %dW — reducing %dA → %dA",
                consumption_w, MAX_CONSUMPTION_W, amps, target,
            )
            self._last_regulate_time = now
            await self._set_amps(target)
            return f"overload: cons {consumption_w:.0f}W → {target}A"

        # Auto-only surplus stops (after overload, before regulation):
        if ev_mode == EvMode.AUTO:
            if amps <= MIN_CHARGE_AMPS and soc < stop_soc:
                _LOGGER.info(
                    "EV charger: pinned at %dA, SoC=%.1f%% < %.1f%% "
                    "(target %.1f%% − hysteresis %.1f%%) — stopping",
                    MIN_CHARGE_AMPS, soc, stop_soc,
                    target_soc, soc_hysteresis,
                )
                await self._turn_off_and_restore()
                self._clear_session()
                return f"stop: SoC floor (SoC {soc:.1f}% < {stop_soc:.1f}%)"

        return await self._regulate_amps(
            soc, amps, headroom_w, target_soc,
            solar_power_w, consumption_w, now, ev_mode,
        )

    async def _regulate_amps(
        self,
        soc: float,
        amps: int,
        headroom_w: float,
        target_soc: float,
        solar_power_w: float,
        consumption_w: float,
        now: float,
        ev_mode: EvMode,
    ) -> str:
        """Adjust charging amps to track real headroom (+ SoC bias in Auto)."""
        delta_amps = int(headroom_w // WATTS_PER_AMP)

        if ev_mode == EvMode.AUTO:
            soc_diff = soc - target_soc
            if soc_diff > SOC_DEADBAND_PCT:
                soc_bias = SOC_BIAS_AMPS
            elif soc_diff < -SOC_DEADBAND_PCT:
                soc_bias = -SOC_BIAS_AMPS
            else:
                soc_bias = 0
        else:
            soc_bias = 0

        target_amps = max(
            MIN_CHARGE_AMPS,
            min(MAX_CHARGE_AMPS, amps + delta_amps + soc_bias),
        )

        if target_amps > amps:
            new_amps = amps + 1
        elif target_amps < amps:
            new_amps = amps - 1
        else:
            return (
                f"hold {amps}A "
                f"(headroom {headroom_w:.0f}W, bias {soc_bias:+d})"
            )

        elapsed = now - self._last_regulate_time
        emergency_shrink = (
            new_amps < amps
            and headroom_w <= -EMERGENCY_SHRINK_W
        )
        if elapsed < REGULATE_INTERVAL_S and not emergency_shrink:
            return (
                f"throttled {amps}A "
                f"(elapsed {elapsed:.0f}s/{REGULATE_INTERVAL_S}s, "
                f"would-be {new_amps}A)"
            )

        _LOGGER.info(
            "EV charger: adjusting %dA → %dA (target=%dA, bias=%+d, "
            "solar=%.0fW, consumption=%.0fW, headroom=%.0fW%s)",
            amps, new_amps, target_amps, soc_bias,
            solar_power_w, consumption_w, headroom_w,
            ", emergency" if emergency_shrink else "",
        )
        self._last_regulate_time = now
        await self._set_amps(new_amps)
        return (
            f"adjust {amps}A→{new_amps}A "
            f"(headroom {headroom_w:.0f}W, bias {soc_bias:+d}"
            f"{', emergency' if emergency_shrink else ''})"
        )

    # -- Switch control --

    async def _turn_on(self) -> None:
        """Turn on the EV charger toggle.

        Marks pending-start whether or not the service call raises:
        Wallbox's cloud frequently errors client-side after the action
        has actually been accepted server-side. The state machine then
        waits up to PENDING_START_GRACE_S for the entity to reflect on.
        """
        now = time.monotonic()
        self._pending_start_since = now
        self._last_entity_refresh_at = now
        try:
            await self._hass.services.async_call(
                "homeassistant",
                "turn_on",
                {"entity_id": self._toggle_entity_id},
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "EV charger: turn_on failed (%s) — Wallbox cloud may "
                "still have accepted the request; holding for entity "
                "to confirm (grace %ds)",
                err, PENDING_START_GRACE_S,
            )
        await self._refresh_toggle_entity()

    async def _turn_off_and_restore(self) -> None:
        """Stop EV charging and restore the user's original amperage."""
        try:
            await self._hass.services.async_call(
                "homeassistant",
                "turn_off",
                {"entity_id": self._toggle_entity_id},
            )
        except HomeAssistantError as err:
            _LOGGER.warning("EV charger: turn_off failed: %s", err)
        await self._refresh_toggle_entity()
        if self._saved_amps is not None:
            _LOGGER.info(
                "EV charger: restoring user amps %dA → %dA",
                self._read_amps_clamped(), self._saved_amps,
            )
            await self._set_amps(self._saved_amps)

    async def _set_amps(self, amps: int) -> None:
        """Set the wallbox charging amperage."""
        try:
            await self._hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": self._power_entity_id, "value": amps},
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "EV charger: set_amps(%d) failed: %s", amps, err,
            )

    async def _refresh_toggle_entity(self) -> None:
        """Best-effort: ask HA to repoll the toggle entity."""
        try:
            await self._hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": self._toggle_entity_id},
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "EV charger: update_entity failed: %s", err,
            )

    def _clear_session(self) -> None:
        """Reset all session bookkeeping."""
        self._start_mode = None
        self._saved_amps = None
        self._export_sustained_since = None
        self._last_headroom_ok_at = None
        self._pending_start_since = None
        self._no_demand_since = None

    # -- Lifecycle --

    def reconfigure(
        self,
        toggle_entity_id: str,
        power_entity_id: str,
        status_entity_id: str | None = None,
    ) -> None:
        """Update entity IDs from options."""
        self._toggle_entity_id = toggle_entity_id
        self._power_entity_id = power_entity_id
        self._status_entity_id = status_entity_id or None
        self._clear_session()
        _LOGGER.info(
            "EV charger controller reconfigured: toggle=%s, power=%s, status=%s",
            toggle_entity_id, power_entity_id, status_entity_id,
        )
