"""Water heater controller for BeemAI.

Controls a water heater via a Home Assistant smart plug, optimising
energy usage with a surplus-driven strategy and off-peak fallback.
"""

import logging
from datetime import datetime, time
from typing import Optional

from homeassistant.core import HomeAssistant

from .event_bus import Event, EventBus
from .state_store import StateStore
from .tariff_manager import TARIFF_HC, TARIFF_HP, TARIFF_HSC, TariffManager

log = logging.getLogger(__name__)

# Thresholds
_SOLAR_SURPLUS_MIN_W = 300.0
_DAILY_HEATING_MIN_KWH = 3.0
_HSC_FALLBACK_DEADLINE = time(22, 0)


class WaterHeaterController:
    """Surplus-driven controller for a water heater on a smart plug."""

    def __init__(
        self,
        hass: HomeAssistant,
        state_store: StateStore,
        event_bus: EventBus,
        tariff_manager: TariffManager,
        switch_entity: str,
        power_entity: str,
        heater_power_w: float,
    ):
        self._hass = hass
        self._state_store = state_store
        self._event_bus = event_bus
        self._tariff_manager = tariff_manager

        # HA entities
        self._switch_entity = switch_entity
        self._power_entity = power_entity

        # Configuration
        self._heater_power_w = heater_power_w

        # Runtime state
        self._is_on: bool = False
        self._daily_energy_kwh: float = 0.0
        self._last_decision: str = ""
        self._last_power_reading_time: Optional[datetime] = None
        self._solar_on: bool = False  # track if ON due to solar surplus

        log.info(
            "WaterHeaterController initialised: switch=%s heater=%dW",
            switch_entity,
            int(heater_power_w),
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def daily_energy_kwh(self) -> float:
        return self._daily_energy_kwh

    @property
    def last_decision(self) -> str:
        return self._last_decision

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate(self) -> str:
        """Run the simplified decision tree and actuate the water heater.

        Decision priority:
        1. System disabled -> OFF
        2. Solar surplus (export > 300W) -> ON
        3. No surplus + was solar-ON -> OFF
        4. HSC window + not heated enough today -> ON (daily fallback)
        5. HP tariff + grid importing -> OFF (cost protection)
        6. Default: maintain current state

        Returns the decision reason string.
        """
        # Accumulate energy before making decisions
        self._estimate_daily_energy()

        # 1. System disabled
        if not self._state_store.enabled:
            await self._turn_off("system disabled")
            self._solar_on = False
            return self._last_decision

        battery = self._state_store.battery
        tariff = self._tariff_manager.get_current_tariff()

        # 2. Solar surplus -> ON
        if battery.export_power_w > _SOLAR_SURPLUS_MIN_W:
            await self._turn_on("solar surplus heating")
            self._solar_on = True
            return self._last_decision

        # 3. No surplus + was solar-ON -> OFF
        if self._solar_on and battery.export_power_w <= _SOLAR_SURPLUS_MIN_W:
            await self._turn_off("solar surplus ended")
            self._solar_on = False
            return self._last_decision

        # 4. Off-peak fallback: HSC window + not heated enough today
        is_off_peak = tariff in (TARIFF_HSC, TARIFF_HC)
        if (
            is_off_peak
            and self._daily_energy_kwh < _DAILY_HEATING_MIN_KWH
        ):
            now = datetime.now()
            if now.time() >= _HSC_FALLBACK_DEADLINE or tariff == TARIFF_HSC:
                await self._turn_on("off-peak fallback")
                return self._last_decision

        # 5. Grid import during HP -> OFF
        if tariff == TARIFF_HP and battery.is_importing:
            await self._turn_off("avoiding HP import")
            return self._last_decision

        # 6. Default: keep current state
        self._last_decision = "maintaining current state"
        return self._last_decision

    def reset_daily(self) -> None:
        """Reset daily counters. Call at midnight."""
        log.info(
            "Daily reset: energy=%.2f kWh",
            self._daily_energy_kwh,
        )
        self._daily_energy_kwh = 0.0
        self._last_power_reading_time = None
        self._solar_on = False

    def reconfigure(self, config: dict) -> None:
        """Update configuration from ConfigManager."""
        switch = config.get("water_heater_switch_entity")
        if switch:
            self._switch_entity = switch
        power = config.get("water_heater_power_entity")
        if power:
            self._power_entity = power
        power_w = config.get("water_heater_power_w")
        if power_w is not None:
            self._heater_power_w = float(power_w)
        log.info("WaterHeaterController reconfigured")

    # ------------------------------------------------------------------
    # Actuation
    # ------------------------------------------------------------------

    async def _turn_on(self, reason: str) -> None:
        """Turn the water heater ON via the smart plug."""
        if not self._is_on:
            try:
                await self._hass.services.async_call(
                    "homeassistant", "turn_on",
                    {"entity_id": self._switch_entity},
                )
            except Exception:
                log.exception("Failed to turn on %s", self._switch_entity)
                return
            self._is_on = True
            log.info("Water heater ON: %s", reason)
            self._event_bus.publish(
                Event.WATER_HEATER_CHANGED,
                {"state": "on", "reason": reason},
            )
        self._last_decision = reason

    async def _turn_off(self, reason: str) -> None:
        """Turn the water heater OFF via the smart plug."""
        if self._is_on:
            try:
                await self._hass.services.async_call(
                    "homeassistant", "turn_off",
                    {"entity_id": self._switch_entity},
                )
            except Exception:
                log.exception("Failed to turn off %s", self._switch_entity)
                return
            self._is_on = False
            log.info("Water heater OFF: %s", reason)
            self._event_bus.publish(
                Event.WATER_HEATER_CHANGED,
                {"state": "off", "reason": reason},
            )
        self._last_decision = reason

    # ------------------------------------------------------------------
    # Energy tracking
    # ------------------------------------------------------------------

    def _estimate_daily_energy(self) -> None:
        """Track cumulative energy based on power readings from HA."""
        now = datetime.now()

        try:
            state = self._hass.states.get(self._power_entity)
            if state is None or state.state in (
                "unknown",
                "unavailable",
            ):
                return
            power_w = float(state.state)
        except (ValueError, TypeError):
            return

        if self._last_power_reading_time is not None:
            elapsed_h = (
                now - self._last_power_reading_time
            ).total_seconds() / 3600.0
            if power_w > 0:
                self._daily_energy_kwh += (power_w / 1000.0) * elapsed_h

        self._last_power_reading_time = now
