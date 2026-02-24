"""Thread-safe shared state container for BeemAI."""

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class BatteryState:
    """Live battery data from MQTT."""

    soc: float = 0.0
    solar_power_w: float = 0.0
    battery_power_w: float = 0.0  # +charge / -discharge
    meter_power_w: float = 0.0  # +import / -export
    inverter_power_w: float = 0.0
    mppt1_w: float = 0.0
    mppt2_w: float = 0.0
    mppt3_w: float = 0.0
    working_mode: str = "unknown"
    soh: float = 100.0
    cycle_count: int = 0
    capacity_kwh: float = 13.4
    last_updated: Optional[datetime] = None

    @property
    def is_charging(self) -> bool:
        return self.battery_power_w > 0

    @property
    def is_discharging(self) -> bool:
        return self.battery_power_w < 0

    @property
    def is_importing(self) -> bool:
        return self.meter_power_w > 0

    @property
    def is_exporting(self) -> bool:
        return self.meter_power_w < 0

    @property
    def export_power_w(self) -> float:
        return max(0.0, -self.meter_power_w)

    @property
    def import_power_w(self) -> float:
        return max(0.0, self.meter_power_w)

    @property
    def consumption_w(self) -> float:
        """Estimated household consumption."""
        return self.solar_power_w + self.import_power_w + max(0.0, -self.battery_power_w)


@dataclass
class CurrentPlan:
    """Active optimization plan."""

    target_soc: float = 50.0
    charge_power_w: int = 0
    allow_grid_charge: bool = False
    prevent_discharge: bool = False
    min_soc: int = 20
    max_soc: int = 100
    phase: str = "idle"  # idle, evening_hold, hc_charge, hsc_charge, solar_mode
    reasoning: str = ""
    created_at: Optional[datetime] = None
    next_transition: Optional[datetime] = None


@dataclass
class ForecastData:
    """Solar and consumption forecasts."""

    # Hourly solar forecast (hour -> watts)
    solar_today: dict = field(default_factory=dict)
    solar_tomorrow: dict = field(default_factory=dict)

    # Confidence intervals
    solar_today_p10: dict = field(default_factory=dict)
    solar_today_p90: dict = field(default_factory=dict)
    solar_tomorrow_p10: dict = field(default_factory=dict)
    solar_tomorrow_p90: dict = field(default_factory=dict)

    # Daily totals (kWh)
    solar_today_kwh: float = 0.0
    solar_tomorrow_kwh: float = 0.0

    # Consumption forecast
    consumption_today_kwh: float = 0.0
    consumption_tomorrow_kwh: float = 0.0
    consumption_hourly: dict = field(default_factory=dict)

    last_updated: Optional[datetime] = None
    sources_used: list = field(default_factory=list)
    confidence: str = "low"  # low, medium, high


class StateStore:
    """Thread-safe container for all shared state."""

    def __init__(self):
        self._lock = threading.RLock()
        self._battery = BatteryState()
        self._plan = CurrentPlan()
        self._forecast = ForecastData()
        self._enabled = True
        self._mqtt_connected = False
        self._rest_available = True
        self._daily_savings_eur = 0.0

    @property
    def battery(self) -> BatteryState:
        with self._lock:
            return self._battery

    @property
    def plan(self) -> CurrentPlan:
        with self._lock:
            return self._plan

    @property
    def forecast(self) -> ForecastData:
        with self._lock:
            return self._forecast

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        with self._lock:
            self._enabled = value

    @property
    def mqtt_connected(self) -> bool:
        with self._lock:
            return self._mqtt_connected

    @mqtt_connected.setter
    def mqtt_connected(self, value: bool):
        with self._lock:
            self._mqtt_connected = value

    @property
    def rest_available(self) -> bool:
        with self._lock:
            return self._rest_available

    @rest_available.setter
    def rest_available(self, value: bool):
        with self._lock:
            self._rest_available = value

    @property
    def daily_savings_eur(self) -> float:
        with self._lock:
            return self._daily_savings_eur

    @daily_savings_eur.setter
    def daily_savings_eur(self, value: float):
        with self._lock:
            self._daily_savings_eur = value

    def update_battery(self, **kwargs):
        """Update battery state fields atomically."""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._battery, key):
                    setattr(self._battery, key, value)
            self._battery.last_updated = datetime.now()

    def update_plan(self, **kwargs):
        """Update plan fields atomically."""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._plan, key):
                    setattr(self._plan, key, value)

    def update_forecast(self, **kwargs):
        """Update forecast fields atomically."""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._forecast, key):
                    setattr(self._forecast, key, value)
            self._forecast.last_updated = datetime.now()

    def set_plan(self, plan: CurrentPlan):
        """Replace entire plan atomically."""
        with self._lock:
            self._plan = plan
