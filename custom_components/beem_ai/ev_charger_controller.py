"""EV charger controller — second-priority solar surplus diverter.

Starts at the best amperage calculated from current export surplus, then
adjusts live each MQTT cycle.  If the house starts importing from the grid,
the amperage is reduced.  Goal: never draw from the grid for EV charging.

The water heater must be ON to *start* charging, but once charging has
begun the EV continues independently — only SoC ≤ 90% stops it.
"""

from __future__ import annotations

import enum
import logging
import time

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Thresholds (same as water heater)
SOC_START_THRESHOLD = 95.0
SOC_STOP_THRESHOLD = 90.0
EXPORT_MIN_W = 500
SUSTAIN_SECONDS = 30

# Amperage limits
MIN_CHARGE_AMPS = 6
MAX_CHARGE_AMPS = 32

# Watts per amp (single-phase 230 V)
WATTS_PER_AMP = 230


class ChargerState(enum.Enum):
    """EV charger state machine states."""

    IDLE = "idle"
    CHARGING = "charging"


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
        self._export_sustained_since: float | None = None
        self._current_amps: int = MIN_CHARGE_AMPS

    # -- Public properties --

    @property
    def is_charging(self) -> bool:
        """Return True if EV charger is currently on."""
        return self._state == ChargerState.CHARGING

    @property
    def current_amps(self) -> int:
        """Return the current charging amperage."""
        return self._current_amps

    # -- Core evaluate (called on every MQTT update, after water heater) --

    async def evaluate(
        self, soc: float, export_w: float, water_heater_heating: bool
    ) -> None:
        """Evaluate state machine and act."""
        now = time.monotonic()

        if self._state == ChargerState.IDLE:
            await self._evaluate_idle(soc, export_w, water_heater_heating, now)
        elif self._state == ChargerState.CHARGING:
            await self._evaluate_charging(soc, export_w)

    async def _evaluate_idle(
        self,
        soc: float,
        export_w: float,
        water_heater_heating: bool,
        now: float,
    ) -> None:
        """IDLE state: check if we should start charging."""
        if (
            water_heater_heating
            and soc > SOC_START_THRESHOLD
            and export_w >= EXPORT_MIN_W
        ):
            if self._export_sustained_since is None:
                self._export_sustained_since = now
                _LOGGER.info(
                    "EV charger: surplus detected — SoC=%.1f%%, export=%.0fW, "
                    "water heater ON, waiting %ds sustained",
                    soc, export_w, SUSTAIN_SECONDS,
                )
            elif now - self._export_sustained_since >= SUSTAIN_SECONDS:
                start_amps = max(
                    MIN_CHARGE_AMPS,
                    min(MAX_CHARGE_AMPS, int(export_w / WATTS_PER_AMP)),
                )
                _LOGGER.info(
                    "EV charger: surplus sustained %.0fs — SoC=%.1f%%, "
                    "export=%.0fW — starting EV charging at %dA",
                    now - self._export_sustained_since,
                    soc, export_w, start_amps,
                )
                self._current_amps = start_amps
                await self._turn_on()
                self._state = ChargerState.CHARGING
                self._export_sustained_since = None
        else:
            self._export_sustained_since = None

    async def _evaluate_charging(
        self,
        soc: float,
        export_w: float,
    ) -> None:
        """CHARGING state: regulate amps or stop on low SoC."""
        if soc < SOC_STOP_THRESHOLD:
            _LOGGER.info(
                "EV charger: SoC dropped to %.1f%% — stopping EV charging", soc
            )
            await self._turn_off()
            self._state = ChargerState.IDLE
            return

        # Regulate amperage based on surplus
        await self._regulate_amps(export_w)

    async def _regulate_amps(self, export_w: float) -> None:
        """Adjust charging amps to absorb export surplus without grid import.

        - export_w > 0: surplus available → increase amps
        - export_w < 0: importing from grid → decrease amps
        """
        delta_amps = int(export_w / WATTS_PER_AMP)
        target_amps = self._current_amps + delta_amps
        new_amps = max(MIN_CHARGE_AMPS, min(MAX_CHARGE_AMPS, target_amps))

        if new_amps != self._current_amps:
            _LOGGER.info(
                "EV charger: adjusting %dA → %dA (export=%.0fW, delta=%+dA)",
                self._current_amps, new_amps, export_w, delta_amps,
            )
            self._current_amps = new_amps
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
        """Stop EV charging."""
        await self._hass.services.async_call(
            "homeassistant",
            "turn_off",
            {"entity_id": self._toggle_entity_id},
        )

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
