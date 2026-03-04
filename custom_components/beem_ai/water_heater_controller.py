"""Water heater controller — diverts solar surplus to hot water."""

from __future__ import annotations

import enum
import logging
import time

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Thresholds
SOC_START_THRESHOLD = 95.0  # Start heating above this SoC
SOC_STOP_THRESHOLD = 90.0   # Stop heating at or below this SoC
EXPORT_MIN_W = 500           # Minimum export power (W)
SUSTAIN_SECONDS = 30         # Export must be sustained for this long


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
        self._export_sustained_since: float | None = None
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

    async def evaluate(self, soc: float, export_w: float) -> None:
        """Evaluate state machine and act."""
        now = time.monotonic()

        if self._state == HeaterState.IDLE:
            await self._evaluate_idle(soc, export_w, now)
        elif self._state == HeaterState.HEATING:
            await self._evaluate_heating(soc, now)

    async def _evaluate_idle(self, soc: float, export_w: float, now: float) -> None:
        """IDLE state: check if we should start heating."""
        if soc > SOC_START_THRESHOLD and export_w >= EXPORT_MIN_W:
            if self._export_sustained_since is None:
                self._export_sustained_since = now
            elif now - self._export_sustained_since >= SUSTAIN_SECONDS:
                _LOGGER.info(
                    "Solar surplus detected: SoC=%.1f%%, export=%.0fW "
                    "(sustained %.0fs) — turning on water heater",
                    soc, export_w, now - self._export_sustained_since,
                )
                await self._turn_on()
                self._state = HeaterState.HEATING
                self._last_accumulate_time = now
                self._export_sustained_since = None
        else:
            # Reset sustain timer if conditions not met
            self._export_sustained_since = None

    async def _evaluate_heating(self, soc: float, now: float) -> None:
        """HEATING state: accumulate energy, check if we should stop."""
        # Accumulate energy
        self._accumulate_energy(now)

        if soc <= SOC_STOP_THRESHOLD:
            _LOGGER.info(
                "Battery SoC dropped to %.1f%% — turning off water heater "
                "(accumulated %.3f kWh)",
                soc, self._accumulated_kwh,
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
