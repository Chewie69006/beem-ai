"""Safety constraints and fallback logic for BeemAI."""

import logging
from datetime import datetime

from .event_bus import Event, EventBus
from .state_store import CurrentPlan, StateStore

log = logging.getLogger(__name__)


class SafetyManager:
    """Validates plans against safety constraints and triggers alerts."""

    def __init__(
        self,
        state: StateStore,
        event_bus: EventBus,
        min_soc_summer: int = 20,
        min_soc_winter: int = 50,
        winter_months: list[int] | None = None,
        stale_threshold_s: int = 300,
    ):
        self._state = state
        self._event_bus = event_bus
        self._min_soc_summer = min_soc_summer
        self._min_soc_winter = min_soc_winter
        self._winter_months = winter_months or [11, 12, 1, 2, 3]
        self._stale_threshold_s = stale_threshold_s

    def reconfigure(self, config: dict) -> None:
        """Update safety settings from ConfigManager."""
        if "min_soc_summer" in config:
            self._min_soc_summer = int(config["min_soc_summer"])
        if "min_soc_winter" in config:
            self._min_soc_winter = int(config["min_soc_winter"])
        if "winter_months" in config:
            self._winter_months = config["winter_months"]
        log.info(
            "SafetyManager reconfigured: summer=%d%% winter=%d%%",
            self._min_soc_summer,
            self._min_soc_winter,
        )

    @property
    def is_winter(self) -> bool:
        return datetime.now().month in self._winter_months

    @property
    def min_soc(self) -> int:
        return self._min_soc_winter if self.is_winter else self._min_soc_summer

    def validate_plan(self, plan: CurrentPlan) -> CurrentPlan:
        """Enforce safety constraints on a proposed plan. Returns corrected plan."""
        floor = self.min_soc

        if plan.min_soc < floor:
            log.warning(
                "Plan min_soc %d%% below safety floor %d%%, overriding",
                plan.min_soc,
                floor,
            )
            plan.min_soc = floor

        if plan.target_soc < floor:
            log.warning(
                "Plan target_soc %.0f%% below safety floor %d%%, overriding",
                plan.target_soc,
                floor,
            )
            plan.target_soc = float(floor)

        if plan.target_soc > 100:
            plan.target_soc = 100.0

        if plan.charge_power_w < 0:
            plan.charge_power_w = 0

        if plan.charge_power_w > 5000:
            log.warning("Charge power %dW exceeds max, capping at 5000W", plan.charge_power_w)
            plan.charge_power_w = 5000

        return plan

    def check_battery_stale(self) -> bool:
        """Check if MQTT data is stale (>5 min without update)."""
        last = self._state.battery.last_updated
        if last is None:
            log.warning("Battery data stale: no MQTT update received yet")
            return True
        age = (datetime.now() - last).total_seconds()
        if age > self._stale_threshold_s:
            log.warning("Battery data stale: last update %.0fs ago (threshold=%ds)",
                        age, self._stale_threshold_s)
            self._event_bus.publish(
                Event.SAFETY_ALERT,
                {"type": "stale_data", "age_seconds": age},
            )
            return True
        return False

    def check_constraints(self) -> list[str]:
        """Run all safety checks. Returns list of active alerts."""
        alerts = []
        battery = self._state.battery

        if self.check_battery_stale():
            alerts.append("Battery data is stale (no MQTT update)")

        if not self._state.mqtt_connected:
            alerts.append("MQTT disconnected")

        if not self._state.rest_available:
            alerts.append("REST API unavailable (rate limited or error)")

        if battery.soc < self.min_soc and battery.is_discharging:
            alerts.append(
                f"SoC {battery.soc:.0f}% below minimum {self.min_soc}% while discharging"
            )

        if battery.soh < 70:
            alerts.append(f"Battery health low: SoH {battery.soh:.0f}%")

        if alerts:
            log.warning("Safety check alerts: %s", "; ".join(alerts))
        return alerts

    def should_emergency_stop(self) -> bool:
        """Critical condition: SoC critically low while discharging."""
        battery = self._state.battery
        emergency_floor = max(10, self.min_soc - 10)
        triggered = battery.soc <= emergency_floor and battery.is_discharging
        if triggered:
            log.critical(
                "EMERGENCY STOP: SoC %.0f%% <= floor %d%% while discharging at %.0fW",
                battery.soc, emergency_floor, abs(battery.battery_power_w),
            )
        return triggered

    def get_safe_fallback_plan(self) -> CurrentPlan:
        """Return a conservative fallback plan for error scenarios."""
        log.warning("Activating safe fallback plan: auto mode, min_soc=%d%%", self.min_soc)
        return CurrentPlan(
            target_soc=float(self.min_soc),
            charge_power_w=0,
            allow_grid_charge=False,
            prevent_discharge=False,
            min_soc=self.min_soc,
            max_soc=100,
            phase="fallback",
            reasoning="Safety fallback: returning to auto mode",
            created_at=datetime.now(),
        )
