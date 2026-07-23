"""Thread-safe shared state container for BeemAI."""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

_LOGGER = logging.getLogger(__name__)


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
        """Estimated household consumption (energy balance)."""
        return max(0.0, self.solar_power_w + self.meter_power_w - self.battery_power_w)


@dataclass
class ControlState:
    """Battery control parameters (mirrors API control-parameters)."""

    mode: str = "auto"                    # "auto" | "pause" | "advanced"
    allow_charge_from_grid: bool = False
    prevent_discharge: bool = False
    charge_from_grid_max_power: int = 0   # watts: 500|1000|2500|5000
    min_soc: int = 20                     # %, 10-50
    max_soc: int = 100                    # %, 50-100
    can_change_mode: bool = True          # API: canChangeMode


class StateStore:
    """Thread-safe container for all shared state."""

    def __init__(self):
        self._lock = threading.RLock()
        self._battery = BatteryState()
        self._control = ControlState()
        self._enabled = True
        self._mqtt_connected = False
        self._rest_available = True

    @property
    def battery(self) -> BatteryState:
        with self._lock:
            return self._battery

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
    def control(self) -> ControlState:
        with self._lock:
            return self._control

    def update_control(self, **kwargs):
        """Update control state fields atomically."""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._control, key):
                    setattr(self._control, key, value)

    def update_battery(self, **kwargs):
        """Update battery state fields atomically."""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._battery, key):
                    setattr(self._battery, key, value)
            self._battery.last_updated = datetime.now()
