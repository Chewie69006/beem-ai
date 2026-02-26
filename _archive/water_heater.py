"""Water heater controller for BeemAI.

Controls a water heater via a Home Assistant smart plug using a
surplus-driven strategy with off-peak fallback.

Decision tree (evaluated every 5 minutes, highest priority first):

  1. System disabled                                    → OFF
  2. Grid export ≥ heater_power_w                       → ON  [solar surplus mode]
  3. Was solar-ON, export < heater_power_w / 2          → OFF [hysteresis exit]
  4. Charging power > house consumption AND forecast OK → ON  [storage surplus mode]
  5. Battery SoC ≥ 90 % AND solar producing             → ON  [battery-full mode]
  6. Was battery-ON, SoC < 85 % OR no solar             → OFF [hysteresis exit]
  7. Off-peak (HSC/HC) AND day < 3 kWh                  → ON  [off-peak fallback]
  8. HP tariff AND grid import                           → OFF [cost protection]
  9. Default                                             → maintain current state

Rules 3 and 6 use hysteresis so the heater does not flicker when
export/SoC sit near the threshold.

Rule 4 ("storage surplus") fires when:
  - More solar is flowing into the battery than the house is consuming
    (i.e. the battery would be full soon and the surplus is real)
  - The solar forecast for the next 2 hours is ≥ 70 % of current production
    (i.e. the conditions are expected to persist long enough to be worth it)
"""

import logging
from datetime import datetime, time
from typing import Optional

from homeassistant.core import HomeAssistant

from .event_bus import Event, EventBus
from .state_store import StateStore
from .tariff_manager import TARIFF_HC, TARIFF_HP, TARIFF_HSC, TariffManager

log = logging.getLogger(__name__)

# --- Thresholds ---

# Minimum active solar production to trigger the battery-full rule.
_SOLAR_MIN_PRODUCTION_W = 300.0

# Battery SoC at which we consider it "full enough" to divert solar to heating.
_BATTERY_FULL_SOC = 90.0

# Hysteresis: once battery-full mode is active, stay ON until SoC drops below this.
_BATTERY_FULL_SOC_HYSTERESIS = 85.0

# Hysteresis: once solar-surplus mode is active, stay ON until export drops
# below this fraction of heater_power_w (avoids rapid cycling when clouds pass).
_SOLAR_SURPLUS_HYSTERESIS_FACTOR = 0.5

# Storage surplus rule: charging_power must exceed consumption by at least
# this margin (watts) to avoid triggering on noise.
_STORAGE_SURPLUS_MARGIN_W = 200.0

# Storage surplus rule: the solar forecast for the next 2 hours must be at
# least this fraction of current production to consider it "stable".
_FORECAST_CONTINUATION_RATIO = 0.70

# Minimum daily heating before off-peak fallback is skipped.
_DAILY_HEATING_MIN_KWH = 3.0

# Off-peak fallback: only trigger HC window after this time-of-day.
_HC_FALLBACK_DEADLINE = time(22, 0)


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
        dry_run: bool = False,
    ):
        self._hass = hass
        self._state_store = state_store
        self._event_bus = event_bus
        self._tariff_manager = tariff_manager

        self._switch_entity = switch_entity
        self._power_entity = power_entity
        self._heater_power_w = heater_power_w
        self._dry_run = dry_run

        # Runtime state
        self._is_on: bool = False
        self._daily_energy_kwh: float = 0.0
        self._last_decision: str = ""
        self._last_power_reading_time: Optional[datetime] = None

        # Mode flags — track why the heater is currently ON so we know
        # when to exit each mode cleanly.
        self._solar_on: bool = False    # ON because of grid export surplus
        self._storage_on: bool = False  # ON because charging > consumption + forecast ok
        self._battery_on: bool = False  # ON because battery is near full

        log.info(
            "WaterHeaterController initialised: switch=%s heater=%.0fW dry_run=%s",
            switch_entity,
            heater_power_w,
            dry_run,
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
        """Run the decision tree and actuate the water heater.

        Returns the decision reason string.
        """
        self._estimate_daily_energy()

        battery = self._state_store.battery
        tariff = self._tariff_manager.get_current_tariff()
        export_w = battery.export_power_w
        solar_w = battery.solar_power_w
        soc = battery.soc

        log.debug(
            "Water heater eval: is_on=%s, soc=%.0f%%, solar=%.0fW, export=%.0fW, "
            "consumption=%.0fW, tariff=%s, daily_energy=%.2f kWh",
            self._is_on, soc, solar_w, export_w,
            battery.consumption_w, tariff, self._daily_energy_kwh,
        )

        # 1. System disabled — clear all mode flags and turn off.
        if not self._state_store.enabled:
            self._solar_on = False
            self._battery_on = False
            await self._turn_off("system disabled")
            return self._last_decision

        # 2. Solar surplus: exporting at least as much as the heater draws.
        #    Turning on is grid-neutral (we're sending that energy out anyway).
        if export_w >= self._heater_power_w:
            self._solar_on = True
            await self._turn_on(
                f"solar surplus: exporting {export_w:.0f} W ≥ heater {self._heater_power_w:.0f} W"
            )
            return self._last_decision

        # 3. Exit solar-surplus mode with hysteresis.
        #    Don't turn off immediately when a cloud passes — wait until
        #    export falls below 50 % of heater power before switching off.
        if self._solar_on and export_w < self._heater_power_w * _SOLAR_SURPLUS_HYSTERESIS_FACTOR:
            self._solar_on = False
            await self._turn_off(
                f"solar surplus ended: export {export_w:.0f} W < "
                f"{self._heater_power_w * _SOLAR_SURPLUS_HYSTERESIS_FACTOR:.0f} W hysteresis"
            )
            # Fall through — maybe another rule keeps it on (e.g. battery full).

        # 4. Storage surplus + stable forecast.
        #    Charging power exceeds house consumption: the solar production is so
        #    high that after powering the house the remainder flows into the battery.
        #    We also verify the forecast says this will last (prevents turning on
        #    just before a cloud that would make the heater import expensive power).
        charging_w = battery.battery_power_w if battery.battery_power_w > 0 else 0.0
        if (
            charging_w > battery.consumption_w + _STORAGE_SURPLUS_MARGIN_W
            and self._forecast_is_stable(solar_w)
        ):
            self._storage_on = True
            await self._turn_on(
                f"storage surplus: charging {charging_w:.0f} W > "
                f"consumption {battery.consumption_w:.0f} W, forecast stable"
            )
            return self._last_decision

        # Exit storage-surplus mode if conditions no longer hold.
        if self._storage_on and (
            charging_w <= battery.consumption_w + _STORAGE_SURPLUS_MARGIN_W
            or not self._forecast_is_stable(solar_w)
        ):
            self._storage_on = False
            await self._turn_off(
                f"storage surplus ended: charging {charging_w:.0f} W, "
                f"consumption {battery.consumption_w:.0f} W"
            )
            return self._last_decision

        # 5. Battery near full and solar is actively producing.  (rule 5)
        #    Better to heat water now than to export cheap energy or clip solar.
        if soc >= _BATTERY_FULL_SOC and solar_w >= _SOLAR_MIN_PRODUCTION_W:
            self._battery_on = True
            await self._turn_on(
                f"battery full: SoC {soc:.0f}% ≥ {_BATTERY_FULL_SOC:.0f}%, "
                f"solar {solar_w:.0f} W producing"
            )
            return self._last_decision

        # 6. Exit battery-full mode with hysteresis.
        #    Turn off when SoC has dropped meaningfully or sun has gone.
        #    Return immediately — battery-full is an intentional phase exit,
        #    not a transition that should be "caught" by a lower-priority rule.
        if self._battery_on and (
            soc < _BATTERY_FULL_SOC_HYSTERESIS or solar_w < _SOLAR_MIN_PRODUCTION_W
        ):
            self._battery_on = False
            await self._turn_off(
                f"battery-full mode ended: SoC {soc:.0f}%, solar {solar_w:.0f} W"
            )
            return self._last_decision

        # 7. Off-peak fallback: ensure the tank gets its minimum daily energy
        #    during cheap tariff hours.
        is_off_peak = self._tariff_manager.is_in_any_period()
        if is_off_peak and self._daily_energy_kwh < _DAILY_HEATING_MIN_KWH:
            now = datetime.now()
            # During cheapest period: always allow.
            # During other off-peak: only after 22:00
            # to avoid heating at the start of off-peak when it's still daytime.
            is_cheapest = self._tariff_manager.is_in_cheapest_period()
            if is_cheapest or now.time() >= _HC_FALLBACK_DEADLINE:
                await self._turn_on(
                    f"off-peak fallback: {tariff} tariff, "
                    f"only {self._daily_energy_kwh:.2f} kWh heated today"
                )
                return self._last_decision

        # 8. Cost protection: don't import expensive peak electricity to heat water.
        is_peak = not self._tariff_manager.is_in_any_period()
        if is_peak and battery.is_importing:
            await self._turn_off(
                f"HP tariff + grid import {battery.import_power_w:.0f} W: avoiding cost"
            )
            return self._last_decision

        # 9. Default: maintain current state.
        self._last_decision = "maintaining current state"
        return self._last_decision

    def reset_daily(self) -> None:
        """Reset daily counters at midnight."""
        log.info("Daily reset: water heater energy=%.2f kWh", self._daily_energy_kwh)
        self._daily_energy_kwh = 0.0
        self._last_power_reading_time = None
        self._solar_on = False
        self._storage_on = False
        self._battery_on = False

    def _forecast_is_stable(self, current_solar_w: float) -> bool:
        """Return True if the solar forecast for the next 2 hours is ≥ 70% of now.

        Falls back to True when no forecast data is available (optimistic default)
        so the rule still fires on first run before the forecast populates.
        """
        if current_solar_w < _SOLAR_MIN_PRODUCTION_W:
            return False

        forecast = self._state_store.forecast
        if forecast is None or not forecast.solar_today:
            return True  # no data yet — assume stable

        now_hour = datetime.now().hour
        next_hours = [h for h in (now_hour + 1, now_hour + 2) if h < 24]
        if not next_hours:
            return False  # after 22:00 — don't assume evening production

        forecasted = [forecast.solar_today.get(h, 0.0) for h in next_hours]
        avg_forecast_w = sum(forecasted) / len(forecasted)
        return avg_forecast_w >= current_solar_w * _FORECAST_CONTINUATION_RATIO

    def reconfigure(self, config: dict) -> None:
        """Update configuration from options."""
        switch = config.get("water_heater_switch_entity")
        if switch:
            self._switch_entity = switch
        power = config.get("water_heater_power_entity")
        if power:
            self._power_entity = power
        power_w = config.get("water_heater_power_w")
        if power_w is not None:
            self._heater_power_w = float(power_w)
        dry_run = config.get("dry_run")
        if dry_run is not None:
            self._dry_run = bool(dry_run)
        log.info(
            "WaterHeaterController reconfigured: heater=%.0fW dry_run=%s",
            self._heater_power_w,
            self._dry_run,
        )

    # ------------------------------------------------------------------
    # Actuation
    # ------------------------------------------------------------------

    async def _turn_on(self, reason: str) -> None:
        """Turn the water heater ON (or log in dry-run mode)."""
        if self._dry_run:
            log.warning("[DRY RUN] would turn ON water heater — %s", reason)
            self._last_decision = f"[DRY RUN] {reason}"
            return

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
            log.info("Water heater ON — %s", reason)
            self._event_bus.publish(
                Event.WATER_HEATER_CHANGED,
                {"state": "on", "reason": reason},
            )
        self._last_decision = reason

    async def _turn_off(self, reason: str) -> None:
        """Turn the water heater OFF (or log in dry-run mode)."""
        if self._dry_run:
            log.warning("[DRY RUN] would turn OFF water heater — %s", reason)
            self._last_decision = f"[DRY RUN] {reason}"
            return

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
            log.info("Water heater OFF — %s", reason)
            self._event_bus.publish(
                Event.WATER_HEATER_CHANGED,
                {"state": "off", "reason": reason},
            )
        self._last_decision = reason

    # ------------------------------------------------------------------
    # Energy tracking
    # ------------------------------------------------------------------

    def _estimate_daily_energy(self) -> None:
        """Accumulate energy from the power sensor using trapezoidal integration."""
        now = datetime.now()

        try:
            state = self._hass.states.get(self._power_entity)
            if state is None or state.state in ("unknown", "unavailable"):
                return
            power_w = float(state.state)
        except (ValueError, TypeError):
            return

        if self._last_power_reading_time is not None:
            elapsed_h = (now - self._last_power_reading_time).total_seconds() / 3600.0
            if power_w > 0:
                self._daily_energy_kwh += (power_w / 1000.0) * elapsed_h

        self._last_power_reading_time = now
