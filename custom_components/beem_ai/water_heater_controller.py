"""Water heater controller — diverts solar surplus to hot water."""

from __future__ import annotations

import enum
import logging
import time

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


class HeaterState(enum.Enum):
    """Water heater state machine states."""

    IDLE = "idle"
    HEATING = "heating"


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
        power_sensor_entity_id: str,
    ) -> None:
        self._hass = hass
        self._switch_entity_id = switch_entity_id
        self._power_sensor_entity_id = power_sensor_entity_id

        self._state = HeaterState.IDLE
        self._sustained_since: float | None = None
        self._last_ok_at: float | None = None
        self._active_soc_threshold: float = EXPORT_SOC_THRESHOLD
        self._accumulated_kwh: float = 0.0
        self._last_accumulate_time: float | None = None
        self._heating_started_at: float | None = None

    # -- Public properties --

    @property
    def is_heating(self) -> bool:
        """Return True if heater is currently on."""
        return self._state == HeaterState.HEATING

    @property
    def accumulated_kwh(self) -> float:
        """Return total energy consumed by heater today."""
        return round(self._accumulated_kwh, 3)

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

        Two independent rules can start heating in Auto:
          Rule 1 (hardcoded):     SoC > 95% AND exporting to grid
          Rule 2 (configurable):  SoC > soc_threshold AND charge_power >= charge_power_threshold

        Stop: SoC < (active rule's SoC threshold) - HYSTERESIS_PCT — but
        only once the heater has been on for ``min_duration_s``.  Overload
        (consumption >= 7kW AND importing) is a safety override and fires
        regardless of minimum duration.

        ``sustain_seconds`` replaces the old hardcoded 30s sustain window
        and is user-configurable (WH Sustain Duration number entity).
        """
        now = time.monotonic()
        wh_mode = _mode_from_str(mode)

        if wh_mode == WhMode.DISABLED:
            # Force-off even if _state == IDLE: the physical switch may
            # still be on (e.g. controller was just rebuilt by an options
            # reload, or user toggled the switch directly).
            await self._force_off()
            return

        if self._state == HeaterState.IDLE:
            await self._evaluate_idle(
                soc, export_w, charge_power_w,
                soc_threshold, charge_power_threshold, now,
                import_w=import_w,
                sustain_seconds=sustain_seconds,
            )
        elif self._state == HeaterState.HEATING:
            await self._evaluate_heating(
                soc, consumption_w, import_w, now, min_duration_s,
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
    ) -> None:
        """IDLE state: check if either rule triggers."""
        # Grace period never exceeds half the sustain window — otherwise a
        # condition that's been false for longer than sustain would never
        # reset the timer.
        grace_s = min(GRACE_SECONDS, max(1, sustain_seconds // 2))
        # Rule 1: hardcoded — SoC >= 95% AND exporting
        rule1 = soc >= EXPORT_SOC_THRESHOLD and export_w > 0
        # Rule 2: configurable — SoC >= threshold AND charging from solar (not grid)
        rule2 = (
            soc >= soc_threshold
            and charge_power_w >= charge_power_threshold
            and import_w <= 0
        )

        if rule1 or rule2:
            # Pick the active SoC threshold (lowest wins — more permissive stop)
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
            elif now - self._sustained_since >= sustain_seconds:
                _LOGGER.info(
                    "Solar surplus detected: SoC=%.1f%%, %s "
                    "(sustained %.0fs) — turning on water heater",
                    soc, reason, now - self._sustained_since,
                )
                self._active_soc_threshold = active_soc
                await self._turn_on()
                self._state = HeaterState.HEATING
                self._last_accumulate_time = now
                self._heating_started_at = now
                self._sustained_since = None
                self._last_ok_at = None
        else:
            # Grace period: brief dips (e.g. oscillating grid) don't reset
            # the sustain timer — only reset if conditions have been false
            # continuously for grace_s seconds.
            if (
                self._sustained_since is not None
                and self._last_ok_at is not None
                and now - self._last_ok_at >= grace_s
            ):
                self._sustained_since = None
                self._last_ok_at = None

    async def _evaluate_heating(
        self,
        soc: float,
        consumption_w: float,
        import_w: float,
        now: float,
        min_duration_s: int,
    ) -> None:
        """HEATING state: accumulate energy, check if we should stop.

        Overload (house ≥ 7 kW AND importing) is a safety override and
        fires regardless of minimum duration.  SoC-drop stop is deferred
        until the heater has been running for at least ``min_duration_s``.
        """
        self._accumulate_energy(now)

        # Overload protection: house consuming too much AND importing from grid
        if consumption_w >= MAX_CONSUMPTION_W and import_w > 0:
            _LOGGER.info(
                "Water heater: consumption %.0fW >= %dW and importing %.0fW "
                "— turning off to protect grid (accumulated %.3f kWh)",
                consumption_w, MAX_CONSUMPTION_W, import_w,
                self._accumulated_kwh,
            )
            await self._turn_off()
            self._state = HeaterState.IDLE
            self._last_accumulate_time = None
            self._heating_started_at = None
            return

        stop_threshold = self._active_soc_threshold - HYSTERESIS_PCT
        if soc < stop_threshold:
            # Respect minimum heating duration — user asked that once
            # we've started heating, we run for at least ``min_duration_s``
            # before honouring the SoC-drop stop.
            if (
                self._heating_started_at is not None
                and now - self._heating_started_at < min_duration_s
            ):
                remaining = min_duration_s - (now - self._heating_started_at)
                _LOGGER.debug(
                    "Water heater: SoC=%.1f%% below stop=%.1f%% but min "
                    "duration not met (%.0fs remaining) — keeping on",
                    soc, stop_threshold, remaining,
                )
                return

            _LOGGER.info(
                "Battery SoC dropped to %.1f%% (< %.1f%%) — turning off water "
                "heater (accumulated %.3f kWh)",
                soc, stop_threshold, self._accumulated_kwh,
            )
            await self._turn_off()
            self._state = HeaterState.IDLE
            self._last_accumulate_time = None
            self._heating_started_at = None

    def _accumulate_energy(self, now: float) -> None:
        """Read power sensor and accumulate energy (kWh)."""
        if self._last_accumulate_time is None:
            self._last_accumulate_time = now
            return

        power_w = self._read_power_sensor()
        if power_w is None or power_w < 0:
            self._last_accumulate_time = now
            return

        elapsed_h = (now - self._last_accumulate_time) / 3600.0
        self._accumulated_kwh += power_w * elapsed_h / 1000.0
        self._last_accumulate_time = now

    def _read_power_sensor(self) -> float | None:
        """Read the power consumption sensor from HA state."""
        state = self._hass.states.get(self._power_sensor_entity_id)
        if state is None:
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

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

    async def _force_off(self) -> None:
        """Unconditionally turn the switch off and reset internal state.

        Used for mode=Disabled: even if the controller thinks IDLE, the
        physical switch may still be on (e.g. user toggled it directly,
        or options reload recreated the controller).
        """
        state = self._hass.states.get(self._switch_entity_id)
        physically_on = state is not None and state.state == "on"
        if self._state == HeaterState.HEATING or physically_on:
            await self._turn_off()
        self._state = HeaterState.IDLE
        self._sustained_since = None
        self._last_ok_at = None
        self._last_accumulate_time = None
        self._heating_started_at = None

    # -- Mode control --

    async def handle_mode_change(self, mode: str) -> None:
        """React to a user-driven mode change from the select entity.

        - ``Disabled``: force the switch off (even if state is stale
          IDLE — same pattern as EvChargerController).
        - ``Auto``:     no immediate action; ``evaluate()`` will take over.
        """
        wh_mode = _mode_from_str(mode)
        if wh_mode == WhMode.DISABLED:
            _LOGGER.info("Water heater: mode set to Disabled — stopping")
            await self._force_off()

    # -- Lifecycle --

    def resync_state(self, soc_threshold: float) -> None:
        """Sync internal state to the actual HA switch state.

        Called once at integration startup so that if HA restarts while the
        heater is physically ON, we resume in the HEATING state and the
        SoC/overload stop checks can still fire.  Without this, the
        controller would think it's IDLE and the switch would stay on
        indefinitely regardless of SoC.

        We don't know which rule started the session, so use the lower of
        the two SoC thresholds — more permissive stop, safer for battery.
        """
        state = self._hass.states.get(self._switch_entity_id)
        if state is None or state.state != "on":
            return
        self._state = HeaterState.HEATING
        self._active_soc_threshold = min(EXPORT_SOC_THRESHOLD, soc_threshold)
        now = time.monotonic()
        self._last_accumulate_time = now
        self._heating_started_at = now
        _LOGGER.info(
            "Water heater: resynced to HEATING (switch is on) — "
            "stop at SoC < %.1f%%",
            self._active_soc_threshold - HYSTERESIS_PCT,
        )

    def reset_daily(self) -> None:
        """Reset daily accumulated energy."""
        if self._accumulated_kwh > 0:
            _LOGGER.info(
                "Daily reset: water heater consumed %.3f kWh today",
                self._accumulated_kwh,
            )
        self._accumulated_kwh = 0.0

    def reconfigure(self, switch_entity_id: str, power_sensor_entity_id: str) -> None:
        """Update entity IDs from options."""
        self._switch_entity_id = switch_entity_id
        self._power_sensor_entity_id = power_sensor_entity_id
        _LOGGER.info(
            "Water heater controller reconfigured: switch=%s, power=%s",
            switch_entity_id, power_sensor_entity_id,
        )
