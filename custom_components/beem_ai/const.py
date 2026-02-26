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
OPT_TARIFF_DEFAULT_PRICE = "tariff_default_price"
OPT_TARIFF_PERIODS_JSON = "tariff_periods_json"
OPT_TARIFF_PERIOD_COUNT = "tariff_period_count"

# --- Options defaults ---
DEFAULT_TARIFF_DEFAULT_PRICE = 0.27
DEFAULT_TARIFF_PERIOD_COUNT = 2

# --- Sensor keys ---
SENSOR_BATTERY_SOC = "battery_soc"
SENSOR_SOLAR_POWER = "solar_power"
SENSOR_BATTERY_POWER = "battery_power"
SENSOR_GRID_POWER = "grid_power"
SENSOR_CONSUMPTION = "consumption"
SENSOR_BATTERY_SOH = "battery_soh"
SENSOR_SOLAR_FORECAST_TODAY = "solar_forecast_today"
SENSOR_SOLAR_FORECAST_TOMORROW = "solar_forecast_tomorrow"
SENSOR_CONSUMPTION_FORECAST_TODAY = "consumption_forecast_today"

# Binary sensor keys
BINARY_SENSOR_MQTT_CONNECTED = "mqtt_connected"

# Switch keys
SWITCH_ENABLED = "enabled"
