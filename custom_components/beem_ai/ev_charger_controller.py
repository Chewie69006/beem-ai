"""EV charger controller — second-priority solar surplus diverter.

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
watt of solar we're currently stashing in the battery.  Once the EV is
drawing N amps, the next MQTT cycle reflects that draw in ``meter_power_w``
and/or ``battery_power_w`` — the regulation is self-correcting and no
longer depends on how ``consumption_w`` accounts for the EV.

When a water heater is configured, the EV charger waits for it to be ON
before starting.  Without a water heater, the EV starts on surplus alone.
"""

from __future__ import annotations

import enum
import logging
import time

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Amperage limits
MIN_CHARGE_AMPS = 6
MAX_CHARGE_AMPS = 32

# Watts per amp (single-phase 230 V)
WATTS_PER_AMP = 230

# Minimum sustained headroom before we start at 6 A.  6 A × 230 V = 1380 W:
# below this we'd start importing the moment the EV plugs in.
START_HEADROOM_W = MIN_CHARGE_AMPS * WATTS_PER_AMP
SUSTAIN_SECONDS = 30
GRACE_SECONDS = 15  # Brief headroom dips under this don't reset the sustain timer

# Regulation throttle: one ±1A adjustment per REGULATE_INTERVAL_S,
# unless the emergency-shrink path fires.
REGULATE_INTERVAL_S = 30

# Emergency-shrink threshold: when we're drawing this much more than
# available, bypass the throttle and shrink immediately.
EMERGENCY_SHRINK_W = 500

# Overload protection
MAX_CONSUMPTION_W = 7000  # stop charging if house exceeds this

# SoC tracking bias: when SoC is off-target we add this many amps on top
# of the no-net-export equilibrium.  Combined with the ±1 A ramp limit
# this gives a 1 A / 30 s drift toward target — slow enough to avoid
# overshoot, fast enough to noticeably move SoC over the course of an
# afternoon.
#   SoC > target → +SOC_BIAS_AMPS  (drain the battery toward target)
#   SoC < target → -SOC_BIAS_AMPS  (preserve battery so it can recover)
# A small deadband around target avoids 1-amp flutter from MQTT jitter.
SOC_BIAS_AMPS = 1
SOC_DEADBAND_PCT = 0.5


class ChargerState(enum.Enum):
    """EV charger state machine states."""

    IDLE = "idle"
    CHARGING = "charging"


class EvMode(enum.Enum):
    """User-selected controller mode (from the BeemAI select entity)."""

    DISABLED = "Disabled"
    AUTO = "Auto"
    MANUAL = "Manual"


# Legacy alias kept for any external imports; StartMode values now map
# 1:1 to the user-facing EvMode (except Disabled, which is not a running
# session).  Manual/Auto here == the mode that was active when the
# session started and still governs how overload and low-surplus are
# handled.
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
    ) -> None:
        self._hass = hass
        self._toggle_entity_id = toggle_entity_id
        self._power_entity_id = power_entity_id

        self._state = ChargerState.IDLE
        self._start_mode: StartMode | None = None
        self._export_sustained_since: float | None = None
        self._last_headroom_ok_at: float | None = None
        self._current_amps: int = MIN_CHARGE_AMPS
        self._saved_amps: int | None = None  # user's setting before we took over
        self._last_regulate_time: float = 0.0  # last time we sent an amp update

    # -- Public properties --

    @property
    def is_charging(self) -> bool:
        """Return True if EV charger is currently on."""
        return self._state == ChargerState.CHARGING

    @property
    def current_amps(self) -> int:
        """Return the current charging amperage."""
        return self._current_amps

    # -- Manual / mode control --

    async def start_manual(self) -> None:
        """Start charging manually at minimum amps."""
        if self._state == ChargerState.CHARGING:
            return
        _LOGGER.info("EV charger: manual start requested")
        self._saved_amps = self._read_current_amps()
        self._current_amps = MIN_CHARGE_AMPS
        self._last_regulate_time = time.monotonic()
        self._start_mode = StartMode.MANUAL
        await self._turn_on()
        self._state = ChargerState.CHARGING
        self._export_sustained_since = None
        self._last_headroom_ok_at = None

    async def stop(self) -> None:
        """Stop charging (from any mode)."""
        if self._state == ChargerState.IDLE:
            return
        _LOGGER.info("EV charger: stop requested")
        await self._turn_off()
        self._state = ChargerState.IDLE
        self._start_mode = None

    async def _force_off(self) -> None:
        """Unconditionally turn the physical switch off and reset state.

        Used when we want "off" to mean "off" regardless of what the
        controller thinks — e.g. on Disabled, the controller may have
        just been rebuilt by an options reload and its in-memory state
        is IDLE even though the physical switch is still on.
        """
        state = self._hass.states.get(self._toggle_entity_id)
        physically_on = state is not None and state.state == "on"
        if self._state == ChargerState.CHARGING or physically_on:
            await self._turn_off()
        self._state = ChargerState.IDLE
        self._start_mode = None
        self._export_sustained_since = None
        self._last_headroom_ok_at = None

    async def handle_mode_change(self, mode: str) -> None:
        """React to a user-driven mode change from the select entity.

        - ``Disabled``: stop immediately (unconditionally turn the switch
          off, even if our internal state says IDLE — it may be stale
          after an options-reload recreates the controller while the
          physical switch is still on).
        - ``Manual``:   start immediately at 6A if idle (no sustain wait).
        - ``Auto``:     no immediate action; ``evaluate()`` will take over.
        """
        ev_mode = _mode_from_str(mode)
        if ev_mode == EvMode.DISABLED:
            _LOGGER.info("EV charger: mode set to Disabled — stopping")
            await self._force_off()
        elif ev_mode == EvMode.MANUAL:
            if self._state == ChargerState.IDLE:
                _LOGGER.info("EV charger: mode set to Manual — starting at %dA", MIN_CHARGE_AMPS)
                await self.start_manual()
            # If already charging, keep running; mode will drive behavior in evaluate.
        # Auto: nothing to do here.

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

        ``mode`` is the user-selected operating mode:
          - ``Disabled``: no-op; if charging, stop.
          - ``Auto``:     closed-loop SoC regulation around ``target_soc``.
            Starts when SoC reaches target with sustained surplus; biases
            ±1 A to drift SoC back toward target; stops when pinned at 6 A
            and SoC has dropped to ``target_soc - soc_hysteresis`` OR when
            solar can't even cover the house consumption.
          - ``Manual``:   no auto-start; on overload ≥7 kW → stop.  No SoC
            stop and no SoC bias — user explicitly asked the charger to
            keep running at the headroom equilibrium until they change
            mode or overload trips.

        ``meter_power_w`` uses +import/-export, ``battery_power_w`` uses
        +charge/-discharge.  Both are read directly from MQTT telemetry
        (``BatteryState``) so they correctly reflect the true headroom.

        ``solar_power_w`` is used to detect "no real surplus" (Auto stop)
        and (with ``consumption_w``) the overload safety
        (``MAX_CONSUMPTION_W``).

        ``target_soc`` / ``soc_hysteresis`` are user-configurable (see
        Number entities in ``number.py``, persisted in ``ConfigEntry``
        options).
        """
        now = time.monotonic()
        headroom_w = -meter_power_w + battery_power_w
        ev_mode = _mode_from_str(mode)
        prev_state = self._state
        prev_amps = self._current_amps

        if ev_mode == EvMode.DISABLED:
            # Force-off even if _state == IDLE: the physical switch may
            # still be on (e.g. controller was just rebuilt by an options
            # reload) and we want Disabled to unambiguously mean "off".
            await self._force_off()
            decision = "disabled: force-off"
        elif self._state == ChargerState.IDLE:
            if ev_mode == EvMode.AUTO:
                decision = await self._evaluate_idle(
                    soc, headroom_w, solar_power_w, consumption_w,
                    water_heater_heating, target_soc, now,
                )
            else:
                decision = "idle: Manual mode — waiting for user start"
        else:
            decision = await self._evaluate_charging(
                soc, headroom_w, battery_power_w,
                solar_power_w, consumption_w,
                target_soc, soc_hysteresis, now, ev_mode,
            )

        _LOGGER.debug(
            "EV eval: mode=%s state=%s→%s amps=%d→%d soc=%.1f%% "
            "target=%.1f%% hyst=%.1f%% meter=%+.0fW batt=%+.0fW "
            "headroom=%+.0fW solar=%.0fW cons=%.0fW wh=%s → %s",
            ev_mode.value, prev_state.value, self._state.value,
            prev_amps, self._current_amps,
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
        """IDLE state: check if we should start charging.

        Start requires (Auto mode):
          - Water heater prerequisite met (or no WH configured)
          - SoC ≥ ``target_soc`` — we only divert once the battery has
            reached the level the user wants to hold
          - ``headroom_w`` (export + battery-charge) sustained above
            ``START_HEADROOM_W`` for ``SUSTAIN_SECONDS``.  This guarantees
            we can absorb at least 6 A without importing from the grid.

        ``water_heater_heating`` is ``None`` when no water heater is
        configured (or the prerequisite is disabled), treated as
        "prerequisite satisfied".
        """
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
            self._saved_amps = self._read_current_amps()
            self._current_amps = MIN_CHARGE_AMPS
            self._last_regulate_time = now
            _LOGGER.info(
                "EV charger: saved user amps=%s before taking over",
                self._saved_amps,
            )
            await self._turn_on()
            self._state = ChargerState.CHARGING
            self._start_mode = StartMode.AUTO
            self._export_sustained_since = None
            self._last_headroom_ok_at = None
            return f"start AUTO at {MIN_CHARGE_AMPS}A"

        # Grace period: brief dips don't reset the sustain timer
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
        headroom_w: float,
        battery_power_w: float,
        solar_power_w: float,
        consumption_w: float,
        target_soc: float,
        soc_hysteresis: float,
        now: float,
        ev_mode: EvMode,
    ) -> str:
        """CHARGING state: regulate amps or stop on low SoC / overload.

        Mode-dependent stop behavior:
          - ``Auto``: SoC-floor stop fires when pinned at 6 A and SoC has
            dropped to ``target_soc - soc_hysteresis``.  Also stops when
            solar production can't even cover the house consumption (no
            real surplus left to divert).  Overload (≥ 7 kW) first shrinks
            amps; if already at min, stops.
          - ``Manual``: no SoC stop and no "no surplus" stop (user
            explicitly wants to keep charging at 6 A if surplus collapses).
            Overload (≥ 7 kW) still stops unconditionally — the safety
            override.
        """
        stop_soc = target_soc - soc_hysteresis

        # Overload protection runs first — it's a safety override for both
        # modes and must take precedence over surplus-based stops.
        if consumption_w >= MAX_CONSUMPTION_W:
            if ev_mode == EvMode.MANUAL:
                _LOGGER.info(
                    "EV charger (Manual): consumption %.0fW >= %dW — "
                    "stopping EV charging (safety override)",
                    consumption_w, MAX_CONSUMPTION_W,
                )
                await self._turn_off()
                self._state = ChargerState.IDLE
                self._start_mode = None
                return f"stop: Manual overload (cons {consumption_w:.0f}W)"

            target = self._current_amps - 1
            if target < MIN_CHARGE_AMPS:
                _LOGGER.info(
                    "EV charger: consumption %.0fW >= %dW and already at "
                    "minimum — stopping EV charging",
                    consumption_w, MAX_CONSUMPTION_W,
                )
                await self._turn_off()
                self._state = ChargerState.IDLE
                self._start_mode = None
                return f"stop: overload at min (cons {consumption_w:.0f}W)"

            _LOGGER.info(
                "EV charger: overload %.0fW >= %dW — reducing %dA → %dA",
                consumption_w, MAX_CONSUMPTION_W,
                self._current_amps, target,
            )
            self._current_amps = target
            self._last_regulate_time = now
            await self._set_amps(target)
            return f"overload: cons {consumption_w:.0f}W → {target}A"

        # Auto-only surplus stops (after overload, before regulation):
        if ev_mode == EvMode.AUTO:
            if (
                self._current_amps <= MIN_CHARGE_AMPS
                and soc < stop_soc
            ):
                _LOGGER.info(
                    "EV charger: pinned at %dA, SoC=%.1f%% < %.1f%% "
                    "(target %.1f%% − hysteresis %.1f%%) — stopping",
                    MIN_CHARGE_AMPS, soc, stop_soc,
                    target_soc, soc_hysteresis,
                )
                await self._turn_off()
                self._state = ChargerState.IDLE
                self._start_mode = None
                return f"stop: SoC floor (SoC {soc:.1f}% < {stop_soc:.1f}%)"

            if solar_power_w < consumption_w:
                _LOGGER.info(
                    "EV charger: solar=%.0fW < house consumption=%.0fW — "
                    "no real surplus to divert, stopping EV charging",
                    solar_power_w, consumption_w,
                )
                await self._turn_off()
                self._state = ChargerState.IDLE
                self._start_mode = None
                return (
                    f"stop: no surplus (solar {solar_power_w:.0f}W < "
                    f"cons {consumption_w:.0f}W)"
                )

        # Regulate amperage based on real headroom (+ SoC bias in Auto).
        return await self._regulate_amps(
            soc, headroom_w, target_soc,
            solar_power_w, consumption_w, now, ev_mode,
        )

    async def _regulate_amps(
        self,
        soc: float,
        headroom_w: float,
        target_soc: float,
        solar_power_w: float,
        consumption_w: float,
        now: float,
        ev_mode: EvMode,
    ) -> str:
        """Adjust charging amps to track real headroom (+ SoC bias in Auto).

        ``headroom_w`` already reflects the current EV draw (it's derived
        from grid + battery telemetry).  The base target is::

            base_target = current_amps + floor(headroom_w / 230)

        In ``Auto`` mode we add a small SoC-tracking bias so the closed
        loop drifts SoC toward ``target_soc``:
          - ``soc - target_soc > +SOC_DEADBAND_PCT`` → +SOC_BIAS_AMPS
          - ``soc - target_soc < -SOC_DEADBAND_PCT`` → -SOC_BIAS_AMPS
          - otherwise (deadband) → 0
        Combined with the ±1 A ramp limit this gives ~1 A / 30 s of drift.
        ``Manual`` mode uses no bias (pure headroom equilibrium).

        Throttled: only sends an update when 30 s have elapsed — except
        for *emergency shrink* (headroom ≤ -EMERGENCY_SHRINK_W) which
        bypasses the throttle so we respond fast to a sudden import spike.
        """
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
            min(MAX_CHARGE_AMPS, self._current_amps + delta_amps + soc_bias),
        )

        # Ramp limit: move at most ±1A per regulation cycle
        if target_amps > self._current_amps:
            new_amps = self._current_amps + 1
        elif target_amps < self._current_amps:
            new_amps = self._current_amps - 1
        else:
            return (
                f"hold {self._current_amps}A "
                f"(headroom {headroom_w:.0f}W, bias {soc_bias:+d})"
            )

        elapsed = now - self._last_regulate_time
        emergency_shrink = (
            new_amps < self._current_amps
            and headroom_w <= -EMERGENCY_SHRINK_W
        )
        if elapsed < REGULATE_INTERVAL_S and not emergency_shrink:
            return (
                f"throttled {self._current_amps}A "
                f"(elapsed {elapsed:.0f}s/{REGULATE_INTERVAL_S}s, "
                f"would-be {new_amps}A)"
            )

        _LOGGER.info(
            "EV charger: adjusting %dA → %dA (target=%dA, bias=%+d, "
            "solar=%.0fW, consumption=%.0fW, headroom=%.0fW%s)",
            self._current_amps, new_amps, target_amps, soc_bias,
            solar_power_w, consumption_w, headroom_w,
            ", emergency" if emergency_shrink else "",
        )
        prev = self._current_amps
        self._current_amps = new_amps
        self._last_regulate_time = now
        await self._set_amps(new_amps)
        return (
            f"adjust {prev}A→{new_amps}A "
            f"(headroom {headroom_w:.0f}W, bias {soc_bias:+d}"
            f"{', emergency' if emergency_shrink else ''})"
        )

    # -- Switch control --

    async def _turn_on(self) -> None:
        """Set charging amperage to minimum and start charging."""
        await self._set_amps(self._current_amps)
        await self._hass.services.async_call(
            "homeassistant",
            "turn_on",
            {"entity_id": self._toggle_entity_id},
        )

    async def _turn_off(self) -> None:
        """Stop EV charging and restore the user's original amperage."""
        self._start_mode = None
        await self._hass.services.async_call(
            "homeassistant",
            "turn_off",
            {"entity_id": self._toggle_entity_id},
        )
        if self._saved_amps is not None:
            _LOGGER.info(
                "EV charger: restoring user amps %dA → %dA",
                self._current_amps, self._saved_amps,
            )
            await self._set_amps(self._saved_amps)
            self._current_amps = self._saved_amps
            self._saved_amps = None

    def _read_current_amps(self) -> int | None:
        """Read the current amperage from the HA number entity."""
        state = self._hass.states.get(self._power_entity_id)
        if state is None:
            return None
        try:
            return int(float(state.state))
        except (ValueError, TypeError):
            return None

    async def _set_amps(self, amps: int) -> None:
        """Set the wallbox charging amperage."""
        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": self._power_entity_id, "value": amps},
        )

    # -- Lifecycle --

    def resync_state(self) -> None:
        """Sync internal state to the actual HA toggle state.

        Called once at integration startup so that if HA restarts while the
        EV is physically charging, we resume in the CHARGING state and the
        SoC/overload stop checks can still fire.  Start mode is set to
        MANUAL conservatively (we can't know who started the session, and
        MANUAL keeps the charger at minimum on overload instead of stopping
        outright — user still has the switch to stop manually).
        """
        state = self._hass.states.get(self._toggle_entity_id)
        if state is None or state.state != "on":
            return
        self._state = ChargerState.CHARGING
        self._start_mode = StartMode.MANUAL
        current = self._read_current_amps()
        if current is not None:
            self._current_amps = max(MIN_CHARGE_AMPS, min(MAX_CHARGE_AMPS, current))
        self._last_regulate_time = time.monotonic()
        _LOGGER.info(
            "EV charger: resynced to CHARGING (toggle is on) — "
            "current=%dA, mode=MANUAL",
            self._current_amps,
        )

    def reconfigure(self, toggle_entity_id: str, power_entity_id: str) -> None:
        """Update entity IDs from options."""
        self._toggle_entity_id = toggle_entity_id
        self._power_entity_id = power_entity_id
        _LOGGER.info(
            "EV charger controller reconfigured: toggle=%s, power=%s",
            toggle_entity_id, power_entity_id,
        )
