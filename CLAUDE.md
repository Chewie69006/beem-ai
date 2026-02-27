# BeemAI Project Instructions

## Testing
- Always run tests through the venv: `.venv/bin/python -m pytest tests/`
- Never use system Python for test execution

## Configurable Tariff Periods
- Tariff periods are user-defined via options flow (up to 6 periods)
- Each period has: label (str), start (HH:MM), end (HH:MM), price (EUR/kWh)
- Periods are stored as JSON in `OPT_TARIFF_PERIODS_JSON`
- A default price (`OPT_TARIFF_DEFAULT_PRICE`) applies outside any period
- If no periods are configured, only the default price applies 24/7 (single tariff, labeled "HP")
- `TariffManager.is_in_cheapest_period()` checks if current time is in the lowest-price period
- `TariffManager.is_in_any_period()` checks if current time is in any configured period
- Periods can cross midnight (e.g. 23:00-02:00)

## Smart CFTG (Charge From The Grid)
- Enabled via `OPT_SMART_CFTG` toggle in options
- During off-peak charge phases (`offpeak_charge`, `cheapest_charge`), checks every 5 minutes:
  - If SoC > min_soc threshold: disables CFTG, allows battery discharge
  - If SoC <= threshold: enables CFTG at plan's charge power
  - If threshold == 0 (disabled): always allows discharge, no CFTG
- Interacts with optimizer phases: when smart_cftg is enabled, phase callbacks defer CFTG control to the monitor loop instead of immediately enabling grid charging

## Multi-Device Structure
Three HA device types, each with distinct `DeviceInfo`:
- **Battery** (`battery_{entry_id}`): SoC, power, SoH, grid, consumption, charge target/power
- **Solar** (`solar_{entry_id}_{index}`): forecast today/tomorrow (via_device: battery)
- **System** (`system_{entry_id}`): optimization status, cost savings, consumption forecast, MQTT connected, grid charging recommended, enabled switch (via_device: battery)

Device info helpers in `sensor.py`: `_battery_device_info()`, `_solar_device_info()`, `_system_device_info()`
