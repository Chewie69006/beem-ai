"""Core decision engine for BeemAI energy optimization."""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later

from .event_bus import Event, EventBus
from .state_store import CurrentPlan, StateStore
from .safety_manager import SafetyManager
from .tariff_manager import TariffManager

log = logging.getLogger(__name__)

POWER_STEPS = [500, 1000, 2500, 5000]


class OptimizationEngine:
    """Optimizes battery charging strategy based on forecasts and tariffs."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_client,  # BeemApiClient
        state: StateStore,
        event_bus: EventBus,
        tariff: TariffManager,
        safety: SafetyManager,
        data_dir: str,
    ):
        self._hass = hass
        self._api_client = api_client
        self._state = state
        self._event_bus = event_bus
        self._tariff = tariff
        self._safety = safety
        self._data_dir = data_dir
        self._log_file = os.path.join(data_dir, "optimization_log.json")
        self._scheduled_handles: list[Callable] = []
        self._cumulative_solar_actual_wh = 0.0
        self._cumulative_solar_forecast_wh = 0.0
        self._last_intraday_hour = -1
        self._dry_run: bool = False
        self._smart_cftg: bool = False

        # Instance vars for phase callbacks (async_call_later cannot pass kwargs)
        self._offpeak_charge_needed: bool = False
        self._offpeak_charge_power: int = 0

    def reconfigure(self, config: dict) -> None:
        """Update configuration from options."""
        dry_run = config.get("dry_run")
        if dry_run is not None:
            self._dry_run = bool(dry_run)
        smart_cftg = config.get("smart_cftg")
        if smart_cftg is not None:
            self._smart_cftg = bool(smart_cftg)
        log.info(
            "OptimizationEngine reconfigured: dry_run=%s, smart_cftg=%s",
            self._dry_run, self._smart_cftg,
        )

    # ---- Evening optimization (21:00) ----

    async def run_evening_optimization(self, _kwargs=None):
        """Main evening planning algorithm. Called at 21:00."""
        if not self._state.enabled:
            log.info("System disabled, skipping evening optimization")
            return

        log.info("=== Running evening optimization ===")

        forecast = self._state.forecast
        battery = self._state.battery

        production_kwh = forecast.solar_tomorrow_kwh
        consumption_kwh = forecast.consumption_tomorrow_kwh
        current_soc = battery.soc
        capacity = battery.capacity_kwh

        # Use P10 (conservative) for planning
        production_p10 = production_kwh * 0.7  # Rough P10 if not available
        if forecast.solar_tomorrow_p10:
            production_p10 = sum(forecast.solar_tomorrow_p10.values()) / 1000.0

        net_balance = production_p10 - consumption_kwh

        # Determine target SoC
        target_soc = self._calculate_target_soc(
            net_balance, capacity, current_soc, production_kwh
        )

        # Determine charge power based on cheapest window duration
        cheapest_window = self._tariff.next_cheapest_window()
        if cheapest_window:
            window_hours = (cheapest_window[1] - cheapest_window[0]).total_seconds() / 3600.0
        else:
            window_hours = 4.0  # fallback

        charge_power = self._calculate_charge_power(
            current_soc, target_soc, capacity, window_hours=window_hours
        )

        # Need additional off-peak charging outside cheapest window?
        need_offpeak = self._needs_offpeak_charging(
            current_soc, target_soc, capacity, charge_power, window_hours
        )

        # Build plan
        plan = CurrentPlan(
            target_soc=target_soc,
            charge_power_w=charge_power,
            allow_grid_charge=charge_power > 0,
            prevent_discharge=True,
            min_soc=self._safety.min_soc,
            max_soc=int(min(target_soc + 5, 100)),
            phase="evening_hold",
            reasoning=self._build_reasoning(
                production_kwh, production_p10, consumption_kwh,
                net_balance, target_soc, charge_power
            ),
            created_at=datetime.now(),
        )

        plan = self._safety.validate_plan(plan)
        self._state.set_plan(plan)
        self._event_bus.publish(Event.PLAN_UPDATED, plan)

        # Cancel previous scheduled callbacks
        self._cancel_scheduled()

        # Store phase params as instance vars for async_call_later callbacks
        self._offpeak_charge_needed = need_offpeak
        self._offpeak_charge_power = charge_power

        # Schedule phase transitions from tariff periods
        self._schedule_phases(plan, charge_power, need_offpeak)

        # Apply immediate phase (evening hold: prevent discharge)
        await self._apply_evening_hold()

        self._log_decision(plan, {
            "production_kwh": production_kwh,
            "production_p10": production_p10,
            "consumption_kwh": consumption_kwh,
            "net_balance": net_balance,
            "current_soc": current_soc,
        })

        log.info(
            "Evening plan: target=%.0f%%, power=%dW, grid_charge=%s, reasoning=%s",
            target_soc, charge_power, charge_power > 0, plan.reasoning,
        )

    def _calculate_target_soc(
        self, net_balance: float, capacity: float, current_soc: float,
        production_kwh: float
    ) -> float:
        """Determine optimal overnight charge target."""
        floor = float(self._safety.min_soc)
        is_winter = self._safety.is_winter

        if net_balance > capacity * 0.8:
            # Very sunny: leave room for solar
            target = max(floor, 20.0)
            category = "very_sunny"
        elif net_balance > 0:
            # Moderate sun: cover night consumption + buffer
            night_kwh = self._estimate_night_consumption()
            target_kwh = night_kwh * 1.1  # 10% buffer
            target = (target_kwh / capacity) * 100.0
            target = min(target, 75.0)
            target = max(target, floor)
            category = "moderate_sun"
        elif net_balance > -5:
            # Slightly cloudy
            deficit_pct = abs(net_balance / capacity) * 100.0
            target = 60.0 + deficit_pct * 5.0
            target = min(target, 80.0)
            target = max(target, floor)
            category = "slightly_cloudy"
        else:
            # Heavy deficit
            deficit_kwh = abs(net_balance)
            target = (deficit_kwh / capacity) * 100.0 + current_soc * 0.3
            target = min(target, 95.0)
            target = max(target, floor)
            category = "heavy_deficit"

        # Confidence adjustment
        confidence = self._state.forecast.confidence
        if confidence == "low":
            target = min(target + 15.0, 95.0)

        # Winter floor
        if is_winter:
            target = max(target, 50.0)

        log.info(
            "Target SoC: %.0f%% (category=%s, net=%.1f kWh, confidence=%s)",
            target, category, net_balance, confidence,
        )
        return round(target, 0)

    def _estimate_night_consumption(self) -> float:
        """Estimate consumption from 21:00 to 07:00 in kWh."""
        forecast = self._state.forecast
        total_w = 0.0
        for hour in list(range(21, 24)) + list(range(0, 7)):
            total_w += forecast.consumption_hourly.get(hour, 300)  # 300W default
        return total_w / 1000.0  # Convert Wh to kWh

    def _calculate_charge_power(
        self, current_soc: float, target_soc: float, capacity: float,
        window_hours: float = 4.0
    ) -> int:
        """Pick minimum charge power step to reach target in the cheapest window."""
        if target_soc <= current_soc:
            return 0

        needed_kwh = (target_soc - current_soc) / 100.0 * capacity
        needed_w = needed_kwh * 1000.0 / max(window_hours, 1.0)

        for step in POWER_STEPS:
            if step >= needed_w:
                return step

        return POWER_STEPS[-1]

    def _needs_offpeak_charging(
        self, current_soc: float, target_soc: float, capacity: float,
        cheapest_power: int, window_hours: float
    ) -> bool:
        """Check if additional off-peak charging is needed beyond cheapest window."""
        if cheapest_power == 0:
            return False
        cheapest_kwh = cheapest_power * window_hours / 1000.0
        needed_kwh = (target_soc - current_soc) / 100.0 * capacity
        return needed_kwh > cheapest_kwh

    def _schedule_phases(self, plan: CurrentPlan, charge_power: int, need_offpeak: bool):
        """Schedule phase transitions from tariff periods via HA async_call_later."""
        now = datetime.now()

        # Get the off-peak and cheapest windows from tariff manager
        offpeak_window = self._tariff.next_off_peak_window()
        cheapest_window = self._tariff.next_cheapest_window()

        if offpeak_window is None:
            # No periods configured — use legacy fixed times
            today = now.date()
            tomorrow = today + timedelta(days=1)
            offpeak_start = datetime.combine(today, datetime.strptime("23:00", "%H:%M").time())
            if offpeak_start <= now:
                offpeak_start += timedelta(days=1)
            cheapest_start = datetime.combine(tomorrow, datetime.strptime("02:00", "%H:%M").time())
            solar_start = datetime.combine(tomorrow, datetime.strptime("06:00", "%H:%M").time())
        else:
            offpeak_start = offpeak_window[0]
            solar_start = offpeak_window[1]
            if cheapest_window:
                cheapest_start = cheapest_window[0]
            else:
                cheapest_start = offpeak_start

        # Phase 1: now until off-peak start — evening hold (already applied)

        # Phase 2: off-peak start — charge at secondary rate if needed
        if offpeak_start > now:
            offpeak_delay = (offpeak_start - now).total_seconds()
            h = async_call_later(self._hass, offpeak_delay, self._apply_offpeak_phase)
            self._scheduled_handles.append(h)

        # Phase 3: cheapest window start — charge at cheapest rate
        if cheapest_start > now and cheapest_start != offpeak_start:
            cheapest_delay = (cheapest_start - now).total_seconds()
            h = async_call_later(self._hass, cheapest_delay, self._apply_cheapest_phase)
            self._scheduled_handles.append(h)

        # Phase 4: after off-peak ends — solar mode
        if solar_start > now:
            solar_delay = (solar_start - now).total_seconds()
            h = async_call_later(self._hass, solar_delay, self._apply_solar_mode)
            self._scheduled_handles.append(h)

        log.info(
            "Scheduled phases: offpeak@%s (charge=%s), cheapest@%s, solar@%s",
            offpeak_start.strftime("%H:%M"), need_offpeak,
            cheapest_start.strftime("%H:%M"), solar_start.strftime("%H:%M"),
        )

    def _cancel_scheduled(self):
        """Cancel all previously scheduled phase transitions."""
        for handle in self._scheduled_handles:
            try:
                handle()
            except Exception:
                pass
        self._scheduled_handles.clear()

    # ---- Phase callbacks ----

    async def _apply_evening_hold(self):
        """Phase 1: Prevent discharge, no grid charge yet."""
        await self._set_battery_control(
            prevent_discharge=True,
            allow_grid_charge=False,
            min_soc=self._safety.min_soc,
            max_soc=100,
            charge_power=0,
        )
        self._state.update_plan(phase="evening_hold")
        log.info("Phase: evening_hold — preventing discharge")

    async def _apply_offpeak_phase(self, _now=None):
        """Phase 2: Off-peak charging if needed (secondary rate)."""
        charge = self._offpeak_charge_needed
        power = self._offpeak_charge_power

        if charge and power > 0:
            if self._smart_cftg:
                # Defer to smart CFTG monitor — just set phase name
                self._state.update_plan(phase="offpeak_charge")
                log.info("Phase: offpeak_charge — smart CFTG will manage charging")
            else:
                await self._set_battery_control(
                    prevent_discharge=True,
                    allow_grid_charge=True,
                    min_soc=self._safety.min_soc,
                    max_soc=int(self._state.plan.target_soc),
                    charge_power=power,
                )
                self._state.update_plan(phase="offpeak_charge")
                log.info("Phase: offpeak_charge — charging at %dW", power)
        else:
            self._state.update_plan(phase="offpeak_hold")
            log.info("Phase: offpeak_hold — no off-peak charging needed")

    async def _apply_cheapest_phase(self, _now=None):
        """Phase 3: Cheapest-rate charging."""
        power = self._offpeak_charge_power

        if power > 0:
            if self._smart_cftg:
                self._state.update_plan(phase="cheapest_charge")
                log.info("Phase: cheapest_charge — smart CFTG will manage charging")
            else:
                await self._set_battery_control(
                    prevent_discharge=True,
                    allow_grid_charge=True,
                    min_soc=self._safety.min_soc,
                    max_soc=int(self._state.plan.target_soc),
                    charge_power=power,
                )
                self._state.update_plan(phase="cheapest_charge")
                log.info("Phase: cheapest_charge — charging at %dW (cheapest rate)", power)
        else:
            log.info("Phase: cheapest — no charging needed")

    async def _apply_solar_mode(self, _now=None):
        """Phase 4: Release to solar mode."""
        await self._set_battery_control(
            prevent_discharge=False,
            allow_grid_charge=False,
            min_soc=self._safety.min_soc,
            max_soc=100,
            charge_power=0,
        )
        plan = CurrentPlan(
            target_soc=self._state.plan.target_soc,
            charge_power_w=0,
            allow_grid_charge=False,
            prevent_discharge=False,
            min_soc=self._safety.min_soc,
            max_soc=100,
            phase="solar_mode",
            reasoning="Daytime: solar priority, battery discharge allowed",
            created_at=datetime.now(),
        )
        plan = self._safety.validate_plan(plan)
        self._state.set_plan(plan)
        self._event_bus.publish(Event.PLAN_UPDATED, plan)

        # Reset intraday tracking
        self._cumulative_solar_actual_wh = 0.0
        self._cumulative_solar_forecast_wh = 0.0
        self._last_intraday_hour = -1

        log.info("Phase: solar_mode — battery in solar priority")

    async def _set_battery_control(
        self, prevent_discharge: bool, allow_grid_charge: bool,
        min_soc: int, max_soc: int, charge_power: int
    ):
        """Send control command to battery via REST API client."""
        if self._dry_run:
            log.warning(
                "[DRY RUN] would set battery control: prevent_discharge=%s "
                "allow_grid_charge=%s min_soc=%d max_soc=%d charge_power=%d W",
                prevent_discharge, allow_grid_charge, min_soc, max_soc, charge_power,
            )
            return
        await self._api_client.set_control_params(
            mode="advanced",
            allow_grid_charge=allow_grid_charge,
            prevent_discharge=prevent_discharge,
            min_soc=min_soc,
            max_soc=max_soc,
            charge_power=charge_power,
        )

    # ---- Smart CFTG (Charge From The Grid) ----

    async def check_smart_cftg(self):
        """Smart CFTG: dynamically toggle grid charging based on SoC vs threshold.

        Called every 5 minutes by the coordinator. Only acts during off-peak
        phases when smart_cftg is enabled.
        """
        if not self._smart_cftg:
            return

        if not self._state.enabled:
            return

        now = datetime.now()
        plan = self._state.plan

        # Only act during off-peak charge phases
        if plan.phase not in ("offpeak_charge", "cheapest_charge"):
            return

        # Must be in a cheapest-tariff period
        if not self._tariff.is_in_cheapest_period(now):
            # In off-peak but not cheapest — check if in any period
            if not self._tariff.is_in_any_period(now):
                return

        current_soc = self._state.battery.soc
        threshold = self._safety.min_soc

        if threshold == 0:
            # Min SoC disabled — always allow discharge (no CFTG)
            await self._set_battery_control(
                prevent_discharge=False,
                allow_grid_charge=False,
                min_soc=0,
                max_soc=100,
                charge_power=0,
            )
            log.debug("Smart CFTG: threshold=0, disabling CFTG, allowing discharge")
            return

        if current_soc > threshold:
            # SoC above threshold — disable CFTG, allow discharge
            await self._set_battery_control(
                prevent_discharge=False,
                allow_grid_charge=False,
                min_soc=threshold,
                max_soc=100,
                charge_power=0,
            )
            log.info(
                "Smart CFTG: SoC %.0f%% > threshold %d%% — disabling CFTG, allowing discharge",
                current_soc, threshold,
            )
        else:
            # SoC at or below threshold — enable CFTG at plan's charge power
            charge_power = self._offpeak_charge_power
            target_soc = int(plan.target_soc)
            await self._set_battery_control(
                prevent_discharge=True,
                allow_grid_charge=True,
                min_soc=threshold,
                max_soc=target_soc,
                charge_power=charge_power,
            )
            log.info(
                "Smart CFTG: SoC %.0f%% <= threshold %d%% — enabling CFTG at %dW",
                current_soc, threshold, charge_power,
            )

    # ---- Intraday loop (every 5 min) ----

    async def run_intraday_check(self, _kwargs=None):
        """Intraday monitoring and adjustment. Called every 5 min."""
        if not self._state.enabled:
            return

        battery = self._state.battery
        plan = self._state.plan

        # Safety pre-check
        alerts = self._safety.check_constraints()
        if alerts:
            log.warning("Safety alerts: %s", alerts)

        if self._safety.should_emergency_stop():
            log.critical("Emergency: SoC critically low while discharging!")
            fallback = self._safety.get_safe_fallback_plan()
            self._state.set_plan(fallback)
            self._event_bus.publish(Event.PLAN_UPDATED, fallback)
            await self._set_battery_control(
                prevent_discharge=True,
                allow_grid_charge=False,
                min_soc=fallback.min_soc,
                max_soc=100,
                charge_power=0,
            )
            return

        # Only track forecast deviation during solar hours
        now = datetime.now()
        if 7 <= now.hour <= 19 and plan.phase == "solar_mode":
            self._track_forecast_deviation()

        # Update HA entities (done via event bus in entity_publisher)
        self._event_bus.publish(Event.BATTERY_DATA_UPDATED)

    def _track_forecast_deviation(self):
        """Track actual vs forecast solar production."""
        now = datetime.now()
        hour = now.hour
        forecast = self._state.forecast
        battery = self._state.battery

        # Accumulate actual solar (convert 5-min sample to Wh)
        self._cumulative_solar_actual_wh += battery.solar_power_w * (5.0 / 60.0)

        # Accumulate forecast (hourly, add once per hour)
        if hour != self._last_intraday_hour:
            forecast_w = forecast.solar_today.get(hour, 0)
            self._cumulative_solar_forecast_wh += forecast_w
            self._last_intraday_hour = hour

        # Check deviation
        if self._cumulative_solar_forecast_wh > 0:
            ratio = self._cumulative_solar_actual_wh / self._cumulative_solar_forecast_wh
            deviation = abs(1.0 - ratio)

            if deviation > 0.20:
                log.info(
                    "Forecast deviation: %.0f%% (actual=%.0f Wh, forecast=%.0f Wh)",
                    deviation * 100,
                    self._cumulative_solar_actual_wh,
                    self._cumulative_solar_forecast_wh,
                )

    # ---- Helpers ----

    def _build_reasoning(
        self, prod_kwh, prod_p10, cons_kwh, net, target, power
    ) -> str:
        """Build human-readable reasoning string."""
        parts = [
            f"Forecast: {prod_kwh:.1f} kWh (P10: {prod_p10:.1f})",
            f"Consumption: {cons_kwh:.1f} kWh",
            f"Net: {net:+.1f} kWh",
            f"Target: {target:.0f}%",
        ]
        if power > 0:
            cheapest_label, _ = self._tariff.get_cheapest_tariff()
            parts.append(f"Charge: {power}W @ {cheapest_label}")
        else:
            parts.append("No grid charging needed")
        return " | ".join(parts)

    def _log_decision(self, plan: CurrentPlan, context: dict):
        """Persist optimization decision to log file."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "target_soc": plan.target_soc,
            "charge_power": plan.charge_power_w,
            "phase": plan.phase,
            "reasoning": plan.reasoning,
            "context": context,
        }

        try:
            log_data = []
            if os.path.exists(self._log_file):
                with open(self._log_file) as f:
                    log_data = json.load(f)

            log_data.append(entry)
            # Keep last 90 days (~90 entries)
            log_data = log_data[-90:]

            with open(self._log_file, "w") as f:
                json.dump(log_data, f, indent=2)
        except Exception:
            log.exception("Failed to write optimization log")
