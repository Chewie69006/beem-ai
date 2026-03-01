"""Thread-safe shared state container for BeemAI."""

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
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


@dataclass
class ControlState:
    """Battery control parameters (mirrors API control-parameters)."""

    mode: str = "auto"                    # "auto" | "advanced"
    allow_charge_from_grid: bool = False
    prevent_discharge: bool = False
    charge_from_grid_max_power: int = 0   # watts, 0-5000
    min_soc: int = 20                     # %, 0-100
    max_soc: int = 100                    # %, 0-100


class StateStore:
    """Thread-safe container for all shared state."""

    def __init__(self):
        self._lock = threading.RLock()
        self._battery = BatteryState()
        self._forecast = ForecastData()
        self._control = ControlState()
        self._enabled = True
        self._mqtt_connected = False
        self._rest_available = True

    @property
    def battery(self) -> BatteryState:
        with self._lock:
            return self._battery

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

    def update_forecast(self, **kwargs):
        """Update forecast fields atomically."""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._forecast, key):
                    setattr(self._forecast, key, value)
            self._forecast.last_updated = datetime.now()

    # ---- Persistence ----

    def save_forecast(self, data_dir: str) -> None:
        """Serialize ForecastData to data_dir/forecast_state.json."""
        path = os.path.join(data_dir, "forecast_state.json")
        with self._lock:
            data = asdict(self._forecast)
        # Convert datetime
        val = data.get("last_updated")
        if isinstance(val, datetime):
            data["last_updated"] = val.isoformat()
        # Convert dict keys to strings for JSON (hourly dicts have int keys)
        for dict_key in (
            "solar_today", "solar_tomorrow",
            "solar_today_p10", "solar_today_p90",
            "solar_tomorrow_p10", "solar_tomorrow_p90",
            "consumption_hourly",
        ):
            d = data.get(dict_key)
            if isinstance(d, dict):
                data[dict_key] = {str(k): v for k, v in d.items()}
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except OSError:
            _LOGGER.exception("Failed to save forecast state")

    def load_forecast(self, data_dir: str) -> bool:
        """Restore ForecastData from data_dir/forecast_state.json. Returns True if loaded."""
        path = os.path.join(data_dir, "forecast_state.json")
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                data = json.load(f)
            # Convert last_updated
            val = data.get("last_updated")
            if isinstance(val, str):
                try:
                    data["last_updated"] = datetime.fromisoformat(val)
                except (ValueError, TypeError):
                    data["last_updated"] = None
            # Convert dict keys back to int (hourly forecasts)
            for dict_key in (
                "solar_today", "solar_tomorrow",
                "solar_today_p10", "solar_today_p90",
                "solar_tomorrow_p10", "solar_tomorrow_p90",
                "consumption_hourly",
            ):
                d = data.get(dict_key)
                if isinstance(d, dict):
                    data[dict_key] = {int(k): v for k, v in d.items()}
            with self._lock:
                self._forecast = ForecastData(**data)
            _LOGGER.info(
                "Restored forecast from disk: today=%.1f kWh, tomorrow=%.1f kWh",
                data.get("solar_today_kwh", 0), data.get("solar_tomorrow_kwh", 0),
            )
            return True
        except (json.JSONDecodeError, TypeError, ValueError, OSError) as exc:
            _LOGGER.warning("Failed to load forecast state: %s", exc)
            return False
