"""EV charger controller — second-priority solar surplus diverter.

Starts at 6A when solar surplus is sustained, then adjusts ±1A per MQTT
cycle toward the target.  If the house starts importing from the grid,
the amperage is reduced.  Goal: never draw from the grid for EV charging.

When a water heater is configured, the EV charger waits for it to be ON
before starting.  Without a water heater, the EV starts on surplus alone.
"""

from __future__ import annotations

import enum
import logging
import time

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# SoC thresholds are supplied per-call via evaluate() (user-configurable
# Number entities, persisted in ConfigEntry options).  Defaults are defined
# in coordinator.py to keep a single source of truth.
EXPORT_MIN_W = 500
SUSTAIN_SECONDS = 30

# Amperage limits
MIN_CHARGE_AMPS = 6
MAX_CHARGE_AMPS = 32

# Watts per amp (single-phase 230 V)
WATTS_PER_AMP = 230

# Regulation throttle: skip update unless delta >= this OR time elapsed
REGULATE_DELTA_W = 500  # ~2A worth of change
REGULATE_INTERVAL_S = 30  # force update at least every 30s

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
    """Controls an EV charger based on solar surplus (after water heater)."""

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
        export_w: float,
        solar_power_w: float,
        consumption_w: float,
        water_heater_heating: bool | None,
        start_soc_threshold: float,
        stop_soc_threshold: float,
    ) -> None:
        """Evaluate state machine and act.

        start_soc_threshold / stop_soc_threshold are user-configurable
        (see Number entities in number.py, persisted in ConfigEntry options).
        """
        now = time.monotonic()

        if self._state == ChargerState.IDLE:
            await self._evaluate_idle(
                soc, export_w, solar_power_w, consumption_w,
                water_heater_heating, start_soc_threshold, now,
            )
        elif self._state == ChargerState.CHARGING:
            await self._evaluate_charging(
                soc, solar_power_w, consumption_w, stop_soc_threshold, now
            )

    async def _evaluate_idle(
        self,
        soc: float,
        export_w: float,
        solar_power_w: float,
        consumption_w: float,
        water_heater_heating: bool | None,
        start_soc_threshold: float,
        now: float,
    ) -> None:
        """IDLE state: check if we should start charging.

        Start condition uses *available solar surplus* (solar − consumption)
        instead of grid export, because at mid-range SoC the battery
        absorbs every watt of surplus and export_w stays at 0 — which
        would never trigger a start with a user-lowered SoC threshold.

        Once SoC is above the user's start threshold, a small slice of
        solar is diverted from the battery to the EV (starting at 6 A).

        water_heater_heating is None when no water heater is configured,
        which is treated as "prerequisite satisfied".
        """
        wh_ok = water_heater_heating is None or water_heater_heating
        solar_surplus_w = solar_power_w - consumption_w
        if (
            wh_ok
            and soc >= start_soc_threshold
            and solar_surplus_w >= EXPORT_MIN_W
        ):
            if self._export_sustained_since is None:
                self._export_sustained_since = now
                _LOGGER.info(
                    "EV charger: surplus detected — SoC=%.1f%%, "
                    "solar=%.0fW, consumption=%.0fW, surplus=%.0fW, "
                    "wh=%s, waiting %ds sustained",
                    soc, solar_power_w, consumption_w, solar_surplus_w,
                    water_heater_heating, SUSTAIN_SECONDS,
                )
            elif now - self._export_sustained_since >= SUSTAIN_SECONDS:
                _LOGGER.info(
                    "EV charger: surplus sustained %.0fs — SoC=%.1f%%, "
                    "solar=%.0fW, consumption=%.0fW — starting at %dA",
                    now - self._export_sustained_since,
                    soc, solar_power_w, consumption_w, MIN_CHARGE_AMPS,
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
        else:
            self._export_sustained_since = None

    async def _evaluate_charging(
        self,
        soc: float,
        solar_power_w: float,
        consumption_w: float,
        stop_soc_threshold: float,
        now: float,
    ) -> None:
        """CHARGING state: regulate amps or stop on low SoC / overload.

        SoC-based stop (AUTO only) fires only when ALL of these are true:
        - EV is already clamped at minimum amperage (6 A) — i.e. we
          can't reduce draw any further.
        - Solar is insufficient to sustain even 6 A on its own, so the
          battery is being drained to keep the EV charging.
        - Battery SoC has dropped below the user's stop threshold.

        This mirrors the user-described semantic: stop once we're
        pinned at minimum and solar can no longer cover it.
        """
        if self._start_mode == StartMode.AUTO and soc < stop_soc_threshold:
            at_min_amps = self._current_amps <= MIN_CHARGE_AMPS
            # consumption_w already includes EV draw, so solar < consumption
            # means the battery (or grid) is covering the shortfall.
            battery_draining = solar_power_w < consumption_w
            if at_min_amps and battery_draining:
                _LOGGER.info(
                    "EV charger: pinned at %dA, solar=%.0fW < consumption="
                    "%.0fW (battery draining), SoC=%.1f%% < %.1f%% — "
                    "stopping EV charging",
                    MIN_CHARGE_AMPS, solar_power_w, consumption_w,
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
                "EV charger: overload %.0fW >= %dW — reducing "
                "%dA → %dA",
                consumption_w, MAX_CONSUMPTION_W,
                self._current_amps, target,
            )
            self._current_amps = target
            self._last_regulate_time = now
            await self._set_amps(target)
            return

        # Regulate amperage based on solar surplus
        await self._regulate_amps(solar_power_w, consumption_w, now)

    async def _regulate_amps(
        self, solar_power_w: float, consumption_w: float, now: float
    ) -> None:
        """Set charging amps to absorb solar surplus without grid draw.

        Formula: target = floor(surplus / 230) + 1
        The +1A buffer slightly over-draws so we use a tiny bit of battery
        rather than exporting.

        Throttled: only sends an update when delta >= 500W (~2A) or 30s
        have elapsed since the last update.
        """
        target_amps = self._compute_target_amps(
            solar_power_w, consumption_w, ev_drawing=True
        )

        # Ramp limit: move at most ±1A per regulation cycle
        if target_amps > self._current_amps:
            new_amps = self._current_amps + 1
        elif target_amps < self._current_amps:
            new_amps = self._current_amps - 1
        else:
            return  # already at target

        delta_w = abs(new_amps - self._current_amps) * WATTS_PER_AMP
        elapsed = now - self._last_regulate_time

        if delta_w < REGULATE_DELTA_W and elapsed < REGULATE_INTERVAL_S:
            return

        _LOGGER.info(
            "EV charger: adjusting %dA → %dA (target=%dA, "
            "solar=%.0fW, consumption=%.0fW, surplus=%.0fW)",
            self._current_amps, new_amps, target_amps,
            solar_power_w, consumption_w,
            solar_power_w - consumption_w + self._current_amps * WATTS_PER_AMP,
        )
        self._current_amps = new_amps
        self._last_regulate_time = now
        await self._set_amps(new_amps)

    def _compute_target_amps(
        self, solar_power_w: float, consumption_w: float, *, ev_drawing: bool
    ) -> int:
        """Compute target amps from available solar surplus.

        When ev_drawing=True, consumption_w includes EV power, so we add it
        back to get the true home-only consumption.
        """
        ev_power_w = self._current_amps * WATTS_PER_AMP if ev_drawing else 0
        surplus_w = solar_power_w - consumption_w + ev_power_w
        target = int(surplus_w / WATTS_PER_AMP) + 1
        return max(MIN_CHARGE_AMPS, min(MAX_CHARGE_AMPS, target))

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

    def reconfigure(self, toggle_entity_id: str, power_entity_id: str) -> None:
        """Update entity IDs from options."""
        self._toggle_entity_id = toggle_entity_id
        self._power_entity_id = power_entity_id
        _LOGGER.info(
            "EV charger controller reconfigured: toggle=%s, power=%s",
            toggle_entity_id, power_entity_id,
        )
