"""Constants for the BeemAI integration."""

from __future__ import annotations

DOMAIN = "beem_ai"
PLATFORMS = ["sensor", "binary_sensor", "switch", "number", "select"]

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
OPT_SOLCAST_SITE_IDS_JSON = "solcast_site_ids_json"
OPT_TARIFF_DEFAULT_PRICE = "tariff_default_price"
OPT_TARIFF_PERIODS_JSON = "tariff_periods_json"
OPT_TARIFF_PERIOD_COUNT = "tariff_period_count"
OPT_WATER_HEATER_SWITCH = "water_heater_switch_entity"
OPT_WATER_HEATER_POWER_SENSOR = "water_heater_power_entity"
OPT_EV_CHARGER_TOGGLE = "ev_charger_toggle_entity"
OPT_EV_CHARGER_POWER = "ev_charger_power_entity"
OPT_WH_SOC_THRESHOLD = "wh_soc_threshold"
OPT_WH_CHARGE_POWER_THRESHOLD = "wh_charge_power_threshold"
OPT_EV_TARGET_SOC = "ev_target_soc"
OPT_EV_SOC_HYSTERESIS = "ev_soc_hysteresis"
OPT_EV_CHARGER_MODE = "ev_charger_mode"
OPT_EV_REQUIRE_WATER_HEATER = "ev_require_water_heater"
OPT_WATER_HEATER_MODE = "water_heater_mode"
OPT_WH_MIN_DURATION_S = "wh_min_duration_s"
OPT_WH_SUSTAIN_S = "wh_sustain_s"

DEFAULT_EV_REQUIRE_WATER_HEATER = True

# --- Water heater modes ---
WH_MODE_DISABLED = "Disabled"
WH_MODE_AUTO = "Auto"
WH_MODES = [WH_MODE_DISABLED, WH_MODE_AUTO]
DEFAULT_WATER_HEATER_MODE = WH_MODE_AUTO

# --- EV charger modes ---
EV_MODE_DISABLED = "Disabled"
EV_MODE_AUTO = "Auto"
EV_MODE_MANUAL = "Manual"
EV_MODES = [EV_MODE_DISABLED, EV_MODE_AUTO, EV_MODE_MANUAL]
DEFAULT_EV_CHARGER_MODE = EV_MODE_AUTO

# --- Water heater durations ---
# Minimum heating duration options (seconds), 15m → 2h in 15m steps
WH_MIN_DURATION_OPTIONS_S = [
    15 * 60, 30 * 60, 45 * 60, 60 * 60,
    75 * 60, 90 * 60, 105 * 60, 120 * 60,
]
DEFAULT_WH_MIN_DURATION_S = 15 * 60

# Sustain stepper bounds (seconds)
WH_SUSTAIN_MIN_S = 5
WH_SUSTAIN_MAX_S = 120
WH_SUSTAIN_STEP_S = 5
DEFAULT_WH_SUSTAIN_S = 30

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
# Binary sensor keys
BINARY_SENSOR_MQTT_CONNECTED = "mqtt_connected"

# Switch keys
SWITCH_ENABLED = "enabled"
