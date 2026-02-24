"""Constants for the BeemAI integration."""

from __future__ import annotations

DOMAIN = "beem_ai"
PLATFORMS = ["sensor", "binary_sensor", "switch"]

# --- Config entry data keys (set during config flow) ---
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_BATTERY_ID = "battery_id"
CONF_BATTERY_SERIAL = "battery_serial"
CONF_USER_ID = "user_id"
CONF_API_BASE = "api_base"

DEFAULT_API_BASE = "https://api-x.beem.energy/beemapp"

# --- Options keys ---
OPT_LOCATION_LAT = "location_lat"
OPT_LOCATION_LON = "location_lon"
OPT_SOLCAST_API_KEY = "solcast_api_key"
OPT_SOLCAST_SITE_ID = "solcast_site_id"
OPT_TARIFF_HP_PRICE = "tariff_hp_price"
OPT_TARIFF_HC_PRICE = "tariff_hc_price"
OPT_TARIFF_HSC_PRICE = "tariff_hsc_price"
OPT_MIN_SOC_SUMMER = "min_soc_summer"
OPT_MIN_SOC_WINTER = "min_soc_winter"
OPT_WATER_HEATER_SWITCH = "water_heater_switch_entity"
OPT_WATER_HEATER_POWER_ENTITY = "water_heater_power_entity"
OPT_WATER_HEATER_POWER_W = "water_heater_power_w"
OPT_PANEL_COUNT = "panel_count"
OPT_PANEL_ARRAYS_JSON = "panel_arrays_json"
OPT_DRY_RUN = "dry_run"

# --- Options defaults ---
DEFAULT_TARIFF_HP = 0.27
DEFAULT_TARIFF_HC = 0.21
DEFAULT_TARIFF_HSC = 0.16
DEFAULT_MIN_SOC_SUMMER = 20
DEFAULT_MIN_SOC_WINTER = 50
DEFAULT_WATER_HEATER_POWER_W = 2000
DEFAULT_PANEL_COUNT = 2
DEFAULT_DRY_RUN = False

# --- Sensor keys ---
SENSOR_BATTERY_SOC = "battery_soc"
SENSOR_SOLAR_POWER = "solar_power"
SENSOR_BATTERY_POWER = "battery_power"
SENSOR_GRID_POWER = "grid_power"
SENSOR_CONSUMPTION = "consumption"
SENSOR_BATTERY_SOH = "battery_soh"
SENSOR_OPTIMAL_CHARGE_TARGET = "optimal_charge_target"
SENSOR_OPTIMAL_CHARGE_POWER = "optimal_charge_power"
SENSOR_OPTIMIZATION_STATUS = "optimization_status"
SENSOR_SOLAR_FORECAST_TODAY = "solar_forecast_today"
SENSOR_SOLAR_FORECAST_TOMORROW = "solar_forecast_tomorrow"
SENSOR_CONSUMPTION_FORECAST_TODAY = "consumption_forecast_today"
SENSOR_COST_SAVINGS_TODAY = "cost_savings_today"

# Binary sensor keys
BINARY_SENSOR_MQTT_CONNECTED = "mqtt_connected"
BINARY_SENSOR_GRID_CHARGING = "grid_charging_recommended"

# Switch keys
SWITCH_ENABLED = "enabled"
