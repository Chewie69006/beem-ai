"""Water heater controller — diverts solar surplus to hot water."""

from __future__ import annotations

import enum
import logging
import time

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Rule 1: hardcoded "final boss" — always active
EXPORT_SOC_THRESHOLD = 95.0  # SoC must be > this AND exporting

SUSTAIN_SECONDS = 30  # Conditions must be sustained for this long
HYSTERESIS_PCT = 10.0  # SoC hysteresis to prevent cycling
MAX_CONSUMPTION_W = 7000  # Kill diverters if house exceeds this


class HeaterState(enum.Enum):
    """Water heater state machine states."""

    IDLE = "idle"
    HEATING = "heating"


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
        self._active_soc_threshold: float = EXPORT_SOC_THRESHOLD
        self._accumulated_kwh: float = 0.0
        self._last_accumulate_time: float | None = None

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
    ) -> None:
        """Evaluate state machine and act.

        Two independent rules can start heating:
          Rule 1 (hardcoded):     SoC > 95% AND exporting to grid
          Rule 2 (configurable):  SoC > soc_threshold AND charge_power >= charge_power_threshold

        Stop: SoC < (active rule's SoC threshold) - HYSTERESIS_PCT
              OR consumption >= 7kW AND importing from grid
        """
        now = time.monotonic()

        if self._state == HeaterState.IDLE:
            await self._evaluate_idle(
                soc, export_w, charge_power_w,
                soc_threshold, charge_power_threshold, now,
            )
        elif self._state == HeaterState.HEATING:
            await self._evaluate_heating(soc, consumption_w, import_w, now)

    async def _evaluate_idle(
        self,
        soc: float,
        export_w: float,
        charge_power_w: float,
        soc_threshold: float,
        charge_power_threshold: float,
        now: float,
    ) -> None:
        """IDLE state: check if either rule triggers."""
        # Rule 1: hardcoded — SoC > 95% AND exporting
        rule1 = soc > EXPORT_SOC_THRESHOLD and export_w > 0
        # Rule 2: configurable — SoC > threshold AND charging above threshold
        rule2 = soc > soc_threshold and charge_power_w >= charge_power_threshold

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

            if self._sustained_since is None:
                self._sustained_since = now
                _LOGGER.info(
                    "Water heater: surplus detected — SoC=%.1f%%, %s, "
                    "waiting %ds sustained before turning on",
                    soc, reason, SUSTAIN_SECONDS,
                )
            elif now - self._sustained_since >= SUSTAIN_SECONDS:
                _LOGGER.info(
                    "Solar surplus detected: SoC=%.1f%%, %s "
                    "(sustained %.0fs) — turning on water heater",
                    soc, reason, now - self._sustained_since,
                )
                self._active_soc_threshold = active_soc
                await self._turn_on()
                self._state = HeaterState.HEATING
                self._last_accumulate_time = now
                self._sustained_since = None
        else:
            self._sustained_since = None

    async def _evaluate_heating(
        self, soc: float, consumption_w: float, import_w: float, now: float
    ) -> None:
        """HEATING state: accumulate energy, check if we should stop."""
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
            return

        stop_threshold = self._active_soc_threshold - HYSTERESIS_PCT
        if soc < stop_threshold:
            _LOGGER.info(
                "Battery SoC dropped to %.1f%% (< %.1f%%) — turning off water "
                "heater (accumulated %.3f kWh)",
                soc, stop_threshold, self._accumulated_kwh,
            )
            await self._turn_off()
            self._state = HeaterState.IDLE
            self._last_accumulate_time = None

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

    # -- Lifecycle --

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
