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
        self._saved_amps: int | None = None  # user's setting before we took over

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
        self,
        soc: float,
        export_w: float,
        solar_power_w: float,
        consumption_w: float,
        water_heater_heating: bool,
    ) -> None:
        """Evaluate state machine and act."""
        now = time.monotonic()

        if self._state == ChargerState.IDLE:
            await self._evaluate_idle(
                soc, export_w, solar_power_w, consumption_w,
                water_heater_heating, now,
            )
        elif self._state == ChargerState.CHARGING:
            await self._evaluate_charging(soc, solar_power_w, consumption_w)

    async def _evaluate_idle(
        self,
        soc: float,
        export_w: float,
        solar_power_w: float,
        consumption_w: float,
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
                # EV not drawing yet → consumption_w is home only
                start_amps = self._compute_target_amps(
                    solar_power_w, consumption_w, ev_drawing=False
                )
                _LOGGER.info(
                    "EV charger: surplus sustained %.0fs — SoC=%.1f%%, "
                    "solar=%.0fW, consumption=%.0fW — starting at %dA",
                    now - self._export_sustained_since,
                    soc, solar_power_w, consumption_w, start_amps,
                )
                self._saved_amps = self._read_current_amps()
                self._current_amps = start_amps
                _LOGGER.info(
                    "EV charger: saved user amps=%s before taking over",
                    self._saved_amps,
                )
                await self._turn_on()
                self._state = ChargerState.CHARGING
                self._export_sustained_since = None
        else:
            self._export_sustained_since = None

    async def _evaluate_charging(
        self,
        soc: float,
        solar_power_w: float,
        consumption_w: float,
    ) -> None:
        """CHARGING state: regulate amps or stop on low SoC."""
        if soc < SOC_STOP_THRESHOLD:
            _LOGGER.info(
                "EV charger: SoC dropped to %.1f%% — stopping EV charging", soc
            )
            await self._turn_off()
            self._state = ChargerState.IDLE
            return

        # Regulate amperage based on solar surplus
        await self._regulate_amps(solar_power_w, consumption_w)

    async def _regulate_amps(
        self, solar_power_w: float, consumption_w: float
    ) -> None:
        """Set charging amps to absorb solar surplus without grid draw.

        Formula: target = floor(surplus / 230) + 1
        The +1A buffer slightly over-draws so we use a tiny bit of battery
        rather than exporting.
        """
        new_amps = self._compute_target_amps(
            solar_power_w, consumption_w, ev_drawing=True
        )

        if new_amps != self._current_amps:
            _LOGGER.info(
                "EV charger: adjusting %dA → %dA "
                "(solar=%.0fW, consumption=%.0fW, surplus=%.0fW)",
                self._current_amps, new_amps, solar_power_w, consumption_w,
                solar_power_w - consumption_w + self._current_amps * WATTS_PER_AMP,
            )
            self._current_amps = new_amps
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
