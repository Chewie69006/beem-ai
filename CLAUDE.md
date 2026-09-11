# BeemAI Project Instructions

## Home Assistant Access
- SSH to HA: `ssh root@192.168.1.50`
- Retrieve integration logs: `ssh root@192.168.1.50 -- tail -n 150000 -f config/beem_ai_data/beem_ai.log` (limited to 150000 lines)

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

## System Enabled Switch
- `state_store.enabled` (the System device's "Enabled" switch) is a hard
  master off, and a hard gate on all outbound device control
- Disabling calls `_stop_device_control()` — `EvChargerController.stop()` and
  `WaterHeaterController.stop()`, unconditionally, whoever started the session
- While disabled: `_evaluate_surplus_diverters()` returns immediately, so the
  water heater rules, the EV amperage regulation and the 7 kW overload
  coordination are all skipped; mode changes are stored but not applied;
  `async_shutdown()` touches nothing
- The switch is a `RestoreEntity`: the state survives restarts and config
  entry reloads (the store defaults to enabled)
- EV mode `Manual` is **not** "the user's own session" — it is a manual
  trigger of piloted charging (e.g. leaving for the weekend).  Don't use
  `_start_mode` to decide whether BeemAI "owns" a session

## Nothing Else Stops a Running Session
- An options change must never cut a charge: `_setup_ev_charger()` /
  `_setup_water_heater()` touch a live controller **only** when its entity
  IDs actually change (compare `ev_charger.entity_ids` /
  `water_heater.switch_entity_id`), then `reconfigure()` in place — never
  recreate, which would drop `_saved_amps` and `_start_mode`
- Thresholds are passed into `evaluate()` on every tick, so a new value is
  applied by the next MQTT update with no resync
- `async_shutdown()` leaves the EV charger strictly alone — not stopped, and
  not re-set to the saved amperage (a car jumping back to 32 A during a
  restart is how you trip the 7 kW breaker unattended).  Only
  `WaterHeaterController.release_control()` runs, turning off a session
  BeemAI itself commanded (`_commanded_on`)

## Multi-Device Structure
Three HA device types, each with distinct `DeviceInfo`:
- **Battery** (`battery_{entry_id}`): SoC, power, SoH, grid, consumption, charge target/power
- **Solar** (`solar_{entry_id}_{index}`): forecast today/tomorrow (via_device: battery)
- **System** (`system_{entry_id}`): optimization status, cost savings, consumption forecast, MQTT connected, grid charging recommended, enabled switch (via_device: battery)

Device info helpers in `sensor.py`: `_battery_device_info()`, `_solar_device_info()`, `_system_device_info()`
