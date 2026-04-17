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


class ChargerState(enum.Enum):
    """EV charger state machine states."""

    IDLE = "idle"
    CHARGING = "charging"


class StartMode(enum.Enum):
    """Who initiated the current charging session."""

    AUTO = "auto"
    MANUAL = "manual"


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

    # -- Manual control --

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

    # -- Core evaluate (called on every MQTT update, after water heater) --

    async def evaluate(
        self,
        soc: float,
        meter_power_w: float,
        battery_power_w: float,
        solar_power_w: float,
        consumption_w: float,
        water_heater_heating: bool | None,
        start_soc_threshold: float,
        stop_soc_threshold: float,
    ) -> None:
        """Evaluate state machine and act.

        ``meter_power_w`` uses +import/-export, ``battery_power_w`` uses
        +charge/-discharge.  Both are read directly from MQTT telemetry
        (``BatteryState``) so they correctly reflect the true headroom.

        ``solar_power_w`` and ``consumption_w`` are used only for logging
        and the overload safety (``MAX_CONSUMPTION_W``).

        ``start_soc_threshold`` / ``stop_soc_threshold`` are
        user-configurable (see Number entities in ``number.py``, persisted
        in ``ConfigEntry`` options).
        """
        now = time.monotonic()
        headroom_w = -meter_power_w + battery_power_w

        if self._state == ChargerState.IDLE:
            await self._evaluate_idle(
                soc, headroom_w, solar_power_w, consumption_w,
                water_heater_heating, start_soc_threshold, now,
            )
        elif self._state == ChargerState.CHARGING:
            await self._evaluate_charging(
                soc, headroom_w, battery_power_w,
                solar_power_w, consumption_w,
                stop_soc_threshold, now,
            )

    async def _evaluate_idle(
        self,
        soc: float,
        headroom_w: float,
        solar_power_w: float,
        consumption_w: float,
        water_heater_heating: bool | None,
        start_soc_threshold: float,
        now: float,
    ) -> None:
        """IDLE state: check if we should start charging.

        Start requires:
          - Water heater prerequisite met (or no WH configured)
          - SoC ≥ user's start threshold
          - ``headroom_w`` (export + battery-charge) sustained above
            ``START_HEADROOM_W`` for ``SUSTAIN_SECONDS``.  This guarantees
            we can absorb at least 6 A without importing from the grid.

        ``water_heater_heating`` is ``None`` when no water heater is
        configured, which is treated as "prerequisite satisfied".
        """
        wh_ok = water_heater_heating is None or water_heater_heating
        if (
            wh_ok
            and soc >= start_soc_threshold
            and headroom_w >= START_HEADROOM_W
        ):
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
            elif now - self._export_sustained_since >= SUSTAIN_SECONDS:
                _LOGGER.info(
                    "EV charger: headroom sustained %.0fs — SoC=%.1f%%, "
                    "solar=%.0fW, consumption=%.0fW, headroom=%.0fW — "
                    "starting at %dA",
                    now - self._export_sustained_since,
                    soc, solar_power_w, consumption_w, headroom_w,
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
        else:
            # Grace period: brief headroom dips (oscillating grid/consumption)
            # don't reset the sustain timer — only reset if conditions have
            # been false continuously for GRACE_SECONDS.
            if (
                self._export_sustained_since is not None
                and self._last_headroom_ok_at is not None
                and now - self._last_headroom_ok_at >= GRACE_SECONDS
            ):
                self._export_sustained_since = None
                self._last_headroom_ok_at = None

    async def _evaluate_charging(
        self,
        soc: float,
        headroom_w: float,
        battery_power_w: float,
        solar_power_w: float,
        consumption_w: float,
        stop_soc_threshold: float,
        now: float,
    ) -> None:
        """CHARGING state: regulate amps or stop on low SoC / overload.

        SoC-based stop fires (in *both* AUTO and MANUAL modes) when ALL
        of these are true:
          - EV is already clamped at minimum amperage (6 A)
          - Battery is discharging (``battery_power_w < 0``) — i.e. we're
            pulling from storage to keep the EV charging
          - SoC has dropped below the user's stop threshold

        Rationale: the "EV Charger" switch looks like a simple on/off
        toggle — users reasonably expect the SoC safeguard to protect
        the battery whether charging was started automatically by
        solar-surplus detection or manually via the switch.
        """
        if soc < stop_soc_threshold:
            at_min_amps = self._current_amps <= MIN_CHARGE_AMPS
            battery_draining = battery_power_w < 0
            if at_min_amps and battery_draining:
                _LOGGER.info(
                    "EV charger: pinned at %dA, battery=%.0fW (draining), "
                    "SoC=%.1f%% < %.1f%% — stopping EV charging",
                    MIN_CHARGE_AMPS, battery_power_w,
                    soc, stop_soc_threshold,
                )
                await self._turn_off()
                self._state = ChargerState.IDLE
                self._start_mode = None
                return

        # Overload protection: house consuming too much — reduce by 1A
        if consumption_w >= MAX_CONSUMPTION_W:
            target = self._current_amps - 1

            if target < MIN_CHARGE_AMPS:
                if self._start_mode == StartMode.MANUAL:
                    # Manual mode: stay at minimum instead of stopping
                    return

                _LOGGER.info(
                    "EV charger: consumption %.0fW >= %dW and already at "
                    "minimum — stopping EV charging",
                    consumption_w, MAX_CONSUMPTION_W,
                )
                await self._turn_off()
                self._state = ChargerState.IDLE
                self._start_mode = None
                return

            _LOGGER.info(
                "EV charger: overload %.0fW >= %dW — reducing %dA → %dA",
                consumption_w, MAX_CONSUMPTION_W,
                self._current_amps, target,
            )
            self._current_amps = target
            self._last_regulate_time = now
            await self._set_amps(target)
            return

        # Regulate amperage based on real headroom
        await self._regulate_amps(
            headroom_w, solar_power_w, consumption_w, now
        )

    async def _regulate_amps(
        self,
        headroom_w: float,
        solar_power_w: float,
        consumption_w: float,
        now: float,
    ) -> None:
        """Adjust charging amps to track real headroom.

        ``headroom_w`` already reflects the current EV draw (it's derived
        from grid + battery telemetry), so the target is simply::

            target = current_amps + floor(headroom_w / 230)

        Positive headroom → we can grow.  Negative → we must shrink.

        Throttled: only sends an update when delta ≥ 500 W (~2 A) or 30 s
        have elapsed — except for *emergency shrink* (headroom
        ≤ -EMERGENCY_SHRINK_W) which bypasses the throttle so we respond
        fast to a sudden import spike.
        """
        delta_amps = int(headroom_w // WATTS_PER_AMP)
        target_amps = max(
            MIN_CHARGE_AMPS,
            min(MAX_CHARGE_AMPS, self._current_amps + delta_amps),
        )

        # Ramp limit: move at most ±1A per regulation cycle
        if target_amps > self._current_amps:
            new_amps = self._current_amps + 1
        elif target_amps < self._current_amps:
            new_amps = self._current_amps - 1
        else:
            return  # already at target

        elapsed = now - self._last_regulate_time
        emergency_shrink = (
            new_amps < self._current_amps
            and headroom_w <= -EMERGENCY_SHRINK_W
        )
        # A single ±1A step is only 230 W (< REGULATE_DELTA_W), so in
        # practice we wait for the interval unless this is an emergency
        # shrink.
        if elapsed < REGULATE_INTERVAL_S and not emergency_shrink:
            return

        _LOGGER.info(
            "EV charger: adjusting %dA → %dA (target=%dA, "
            "solar=%.0fW, consumption=%.0fW, headroom=%.0fW%s)",
            self._current_amps, new_amps, target_amps,
            solar_power_w, consumption_w, headroom_w,
            ", emergency" if emergency_shrink else "",
        )
        self._current_amps = new_amps
        self._last_regulate_time = now
        await self._set_amps(new_amps)

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
