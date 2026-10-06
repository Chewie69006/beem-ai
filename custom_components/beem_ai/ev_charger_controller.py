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

Wallbox schedule
----------------
In Auto, a charge the Wallbox starts by itself while it was waiting on
its own schedule (status ``Scheduled``, or right after we handed it back
to its schedule) is the schedule's, not ours: it is raised to
``FULL_POWER_AMPS`` once and then left alone — no amperage regulation,
no SoC / no-demand stop, no 7 kW trim — exactly like Force Charge.

A pause sent through the API is a manual stop, after which the Wallbox
skips its schedule until told to resume it.  When a "Resume schedule"
button entity is configured, every stop the Auto rules make is followed
by a press on it, so a solar session ending in the afternoon doesn't
cancel the night's scheduled charge.
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
# Skip the regulate interval when exporting more than this — fast ramp-up
FAST_RAMP_EXPORT_W = 300

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
# Aim for ~6900W when trimming amps under overload — leaves margin
# before re-tripping the 7kW threshold on the next tick.
OVERLOAD_TARGET_W = 6900

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

SOC_BIAS_AMPS = 3
SOC_DEADBAND_PCT = 0.5

# Amperage for a session meant to charge flat out: a Force Charge start,
# or a charge the Wallbox's own schedule started.  Set once, at the
# start — the user can still lower it from the Wallbox app afterwards.
FULL_POWER_AMPS = MAX_CHARGE_AMPS

# Wallbox status while the car is plugged in and the charger is waiting
# for its schedule (HA Wallbox ``ChargerStatus.SCHEDULED``, lowercased).
# The switch reads off in that state.
SCHEDULED_STATUS = "scheduled"


class EvMode(enum.Enum):
    """User-selected controller mode (from the BeemAI select entity)."""

    DISABLED = "Disabled"
    AUTO = "Auto"
    MANUAL = "Manual"
    # Force: start now at FULL_POWER_AMPS, then never touch the amperage
    # again — the user can still change it from the Wallbox app.  Must
    # match const.EV_MODE_FORCE.
    FORCE = "Force Charge"


class StartMode(enum.Enum):
    """Who / which mode initiated the current charging session."""

    AUTO = "auto"
    MANUAL = "manual"
    FORCE = "force"
    # Started by the Wallbox's own schedule, in Auto — see module docstring.
    SCHEDULE = "schedule"


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
        resume_schedule_entity_id: str | None = None,
    ) -> None:
        self._hass = hass
        self._toggle_entity_id = toggle_entity_id
        self._power_entity_id = power_entity_id
        self._status_entity_id = status_entity_id or None
        # Optional Wallbox "Resume schedule" button.  Not part of
        # entity_ids: changing it must not reset a live session, so the
        # coordinator just assigns it on every options update.
        self.resume_schedule_entity_id = resume_schedule_entity_id or None

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
        # Whether the next charge the Wallbox starts on its own is its
        # schedule's (see _adopt_session).  _idle_scheduled is refreshed
        # on every idle tick: the status reads "Scheduled", or we pressed
        # "Resume schedule" (_handed_back, kept until a session consumes
        # it or we start one ourselves).  Requiring an idle tick means a
        # switch still reading on right after our own pause is never
        # mistaken for the schedule starting.
        self._idle_scheduled: bool = False
        self._handed_back: bool = False

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
    def entity_ids(self) -> tuple[str, str, str | None]:
        """The entities this controller drives — used to decide whether
        an options update actually re-points it at something new."""
        return (
            self._toggle_entity_id,
            self._power_entity_id,
            self._status_entity_id,
        )

    @property
    def is_charging(self) -> bool:
        """Return True if the toggle entity is on."""
        return self._is_switch_on()

    @property
    def current_amps(self) -> int:
        """Return the current charging amperage (read from the HA entity)."""
        return self._read_amps_clamped()

    def is_hands_off(self, mode: str) -> bool:
        """True while the running session is one BeemAI must not regulate:
        Force Charge, or a charge the Wallbox schedule started in Auto.

        Both run at full power and accept going over the 7 kW household
        limit, so the coordinator suspends its overload handling too.
        """
        if not self._is_switch_on():
            return False
        ev_mode = _mode_from_str(mode)
        return ev_mode == EvMode.FORCE or (
            ev_mode == EvMode.AUTO
            and self._start_mode == StartMode.SCHEDULE
        )

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

    async def start_force(self) -> None:
        """Start charging now at full power.

        The amperage is raised to ``FULL_POWER_AMPS`` once, before the
        start, and never touched again — the user can still lower it
        from the Wallbox app.  ``_saved_amps`` is deliberately left as
        ``None`` so the eventual stop doesn't write an amperage back.
        """
        if self._is_switch_on():
            return
        _LOGGER.info(
            "EV charger: force start requested at %dA", FULL_POWER_AMPS,
        )
        self._saved_amps = None
        self._last_regulate_time = time.monotonic()
        self._start_mode = StartMode.FORCE
        self._export_sustained_since = None
        self._last_headroom_ok_at = None
        await self._set_full_power(self._read_amps())
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

        - ``Disabled``:     stop immediately.
        - ``Manual``:       start immediately at 6A if idle (no sustain wait).
        - ``Force Charge``: raise the amperage to 32A, start immediately if
          idle, and stop regulating it.
        - ``Auto``:         hand an idle charger back to its Wallbox
          schedule; ``evaluate()`` takes over from there.
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
        elif ev_mode == EvMode.FORCE:
            # Drop any saved amperage from a previous Auto/Manual session
            # so stopping later never overwrites what the user sets in
            # the Wallbox app while Force is active.
            self._saved_amps = None
            if not self._is_switch_on():
                _LOGGER.info(
                    "EV charger: mode set to Force Charge — starting at %dA",
                    FULL_POWER_AMPS,
                )
                await self.start_force()
            else:
                self._start_mode = StartMode.FORCE
                await self._set_full_power(self._read_amps())
        elif ev_mode == EvMode.AUTO:
            # Whatever paused the charger before (Disabled, a Manual or
            # Force session) also took it off its schedule.  Not while it
            # is charging: resuming the schedule outside its window would
            # stop the running session.
            if not self._is_switch_on():
                await self._resume_schedule()

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

        # Remember whether the idle charger is waiting on its schedule:
        # if it then turns on by itself, that's the schedule starting.
        # Not refreshed while one of our own starts is pending.
        if not is_on and self._pending_start_since is None:
            self._idle_scheduled = (
                self._handed_back
                or self._read_status() == SCHEDULED_STATUS
            )

        if ev_mode == EvMode.DISABLED:
            if is_on:
                _LOGGER.info("EV charger: mode=Disabled and switch is on — stopping")
                await self._turn_off_and_restore()
            self._clear_session()
            decision = "disabled"
        elif is_on:
            # Charging branch — adopt the session first if we didn't
            # start it ourselves (Wallbox schedule, external toggle, HA
            # restart, options reload).
            if self._pending_start_since is not None:
                _LOGGER.info(
                    "EV charger: pending start confirmed after %.0fs — "
                    "entity now reports on",
                    now - self._pending_start_since,
                )
                self._pending_start_since = None
            if self._start_mode is None:
                await self._adopt_session(ev_mode, amps, now)
                amps = self._read_amps_clamped()
            decision = await self._evaluate_charging(
                soc, amps, headroom_w, battery_power_w,
                solar_power_w, consumption_w,
                target_soc, soc_hysteresis, now, ev_mode,
                meter_power_w,
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
                    decision = (
                        f"idle: {ev_mode.value} mode — waiting for user start"
                    )
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
                decision = (
                    f"idle: {ev_mode.value} mode — waiting for user start"
                )

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

    async def _adopt_session(
        self, ev_mode: EvMode, amps: int, now: float,
    ) -> None:
        """Take on a charge that is running without us having started it.

        - Force Charge: the user's session — amperage left as it is.
        - Auto, with the Wallbox waiting on its schedule (idle in
          ``Scheduled``, or just handed back to it): the schedule started
          this charge.  Raise it to full power and stay out of its way.
        - Anything else (HA restart, a resume from the Wallbox app):
          conservatively MANUAL — overload trims to the floor rather
          than stopping outright — and the mode's own rules apply.
        """
        from_schedule = self._idle_scheduled
        self._idle_scheduled = False
        if ev_mode == EvMode.FORCE:
            self._start_mode = StartMode.FORCE
        elif ev_mode == EvMode.AUTO and from_schedule:
            self._start_mode = StartMode.SCHEDULE
            self._handed_back = False
        else:
            self._start_mode = StartMode.MANUAL
        self._last_regulate_time = now
        _LOGGER.info(
            "EV charger: switch is on without active session — "
            "adopting %s mode at %dA",
            self._start_mode.value.upper(), amps,
        )
        if self._start_mode == StartMode.SCHEDULE:
            await self._set_full_power(amps)

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
        meter_power_w: float = 0.0,
    ) -> str:
        """CHARGING state: regulate amps or stop on low SoC / overload."""
        stop_soc = target_soc - soc_hysteresis

        # Force mode: the user owns this session end to end.  We never
        # change the amperage, we don't second-guess the car's status,
        # and we don't enforce the 7 kW household limit — going over it
        # is an accepted consequence of the mode.  Only an explicit stop
        # (Disabled, another mode, or the Wallbox app) ends the session.
        if ev_mode == EvMode.FORCE:
            if consumption_w >= MAX_CONSUMPTION_W:
                _LOGGER.debug(
                    "EV charger (Force): consumption %.0fW >= %dW — "
                    "not intervening (user-controlled session)",
                    consumption_w, MAX_CONSUMPTION_W,
                )
            return f"force: holding {amps}A (user-controlled)"

        # Wallbox schedule: same hands-off contract as Force.  The
        # schedule ends the session itself; pausing it here would also
        # take the Wallbox off its schedule.
        if (
            ev_mode == EvMode.AUTO
            and self._start_mode == StartMode.SCHEDULE
        ):
            return f"schedule: holding {amps}A (Wallbox schedule)"

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
                await self._stop_session(ev_mode)
                return f"stop: no car demand ({status!r})"
            return (
                f"charging: no-demand sustaining "
                f"{elapsed:.0f}s/{STATUS_NO_DEMAND_SUSTAIN_S}s"
            )
        # Car resumed drawing (or status unknown) — drop the arm.
        if self._no_demand_since is not None:
            self._no_demand_since = None

        # Overload protection — safety override for both modes.
        # We compute the amps reduction needed to drop the household
        # under OVERLOAD_TARGET_W in a single step (rather than -1A per
        # tick) so a sudden 8 kW spike doesn't sit at 7.5 kW for several
        # ticks while we trim slowly.
        if consumption_w >= MAX_CONSUMPTION_W:
            if ev_mode == EvMode.MANUAL:
                _LOGGER.info(
                    "EV charger (Manual): consumption %.0fW >= %dW — "
                    "stopping EV charging (safety override)",
                    consumption_w, MAX_CONSUMPTION_W,
                )
                await self._stop_session(ev_mode)
                return f"stop: Manual overload (cons {consumption_w:.0f}W)"

            excess_w = consumption_w - OVERLOAD_TARGET_W
            amps_to_drop = max(
                1, int((excess_w + WATTS_PER_AMP - 1) // WATTS_PER_AMP)
            )
            target = amps - amps_to_drop
            if target < MIN_CHARGE_AMPS:
                _LOGGER.info(
                    "EV charger: consumption %.0fW >= %dW and reducing "
                    "%dA would fall below minimum %dA — stopping EV charging",
                    consumption_w, MAX_CONSUMPTION_W,
                    amps_to_drop, MIN_CHARGE_AMPS,
                )
                await self._stop_session(ev_mode)
                return f"stop: overload at min (cons {consumption_w:.0f}W)"

            _LOGGER.info(
                "EV charger: overload %.0fW >= %dW — reducing %dA → %dA "
                "(drop %dA to target <%.0fW)",
                consumption_w, MAX_CONSUMPTION_W, amps, target,
                amps_to_drop, OVERLOAD_TARGET_W,
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
                await self._stop_session(ev_mode)
                return f"stop: SoC floor (SoC {soc:.1f}% < {stop_soc:.1f}%)"

        return await self._regulate_amps(
            soc, amps, headroom_w, target_soc,
            solar_power_w, consumption_w, now, ev_mode, meter_power_w,
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
        meter_power_w: float = 0.0,
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

        # Cap the upward target so the bias never pushes projected
        # household consumption above MAX_CONSUMPTION_W (7 kW).
        amps_budget_under_max = int(
            (MAX_CONSUMPTION_W - consumption_w) // WATTS_PER_AMP
        )
        max_amps_under_7kw = max(MIN_CHARGE_AMPS, amps + amps_budget_under_max)
        target_amps = max(
            MIN_CHARGE_AMPS,
            min(
                MAX_CHARGE_AMPS,
                max_amps_under_7kw,
                amps + delta_amps + soc_bias,
            ),
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
        fast_ramp = (
            new_amps > amps
            and meter_power_w <= -FAST_RAMP_EXPORT_W
        )
        if elapsed < REGULATE_INTERVAL_S and not emergency_shrink and not fast_ramp:
            return (
                f"throttled {amps}A "
                f"(elapsed {elapsed:.0f}s/{REGULATE_INTERVAL_S}s, "
                f"would-be {new_amps}A)"
            )

        tag = ", emergency" if emergency_shrink else (
            ", fast-ramp" if fast_ramp else ""
        )
        _LOGGER.info(
            "EV charger: adjusting %dA → %dA (target=%dA, bias=%+d, "
            "solar=%.0fW, consumption=%.0fW, headroom=%.0fW%s)",
            amps, new_amps, target_amps, soc_bias,
            solar_power_w, consumption_w, headroom_w, tag,
        )
        self._last_regulate_time = now
        await self._set_amps(new_amps)
        return (
            f"adjust {amps}A→{new_amps}A "
            f"(headroom {headroom_w:.0f}W, bias {soc_bias:+d}{tag})"
        )

    # -- Switch control --

    async def _stop_session(self, ev_mode: EvMode) -> None:
        """End a session on one of our own rules (SoC floor, no car
        demand, overload).  In Auto, the charger then goes back to its
        Wallbox schedule — our pause would otherwise cancel it."""
        await self._turn_off_and_restore()
        self._clear_session()
        if ev_mode == EvMode.AUTO:
            await self._resume_schedule()

    async def _resume_schedule(self) -> None:
        """Press the Wallbox "Resume schedule" button, if configured.

        If the schedule window is already open the Wallbox starts
        charging straight away; ``_handed_back`` makes us adopt that as
        the schedule's session — once an idle tick has seen our pause
        land — instead of piloting it, which would only stop it again.
        """
        if not self.resume_schedule_entity_id:
            return
        _LOGGER.info(
            "EV charger: handing the charger back to its schedule (%s)",
            self.resume_schedule_entity_id,
        )
        self._handed_back = True
        try:
            await self._hass.services.async_call(
                "button",
                "press",
                {"entity_id": self.resume_schedule_entity_id},
            )
        except HomeAssistantError as err:
            _LOGGER.warning("EV charger: resume schedule failed: %s", err)

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
        # Our own start — whatever the Wallbox was waiting for before.
        self._idle_scheduled = False
        self._handed_back = False
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

    async def _set_full_power(self, amps: int | None) -> None:
        """Raise the charger to FULL_POWER_AMPS unless it's already there."""
        if amps == FULL_POWER_AMPS:
            return
        _LOGGER.info(
            "EV charger: setting full power %sA → %dA", amps, FULL_POWER_AMPS,
        )
        await self._set_amps(FULL_POWER_AMPS)

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
        self._idle_scheduled = False
        self._handed_back = False
        _LOGGER.info(
            "EV charger controller reconfigured: toggle=%s, power=%s, status=%s",
            toggle_entity_id, power_entity_id, status_entity_id,
        )
