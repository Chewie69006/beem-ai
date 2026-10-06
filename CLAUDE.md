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

## Water Heater Off-Peak Top-Up
- Auto mode only: once the cheapest tariff period has been running for
  `OFFPEAK_START_DELAY_S` (5 min), a heater that is not `fully_heated` is
  switched on from the grid — `coordinator._wh_offpeak()` passes the flag
  into `evaluate(offpeak=...)` on every tick
- The session ignores the SoC / surplus stops; it ends on the thermostat cut
  (power < 50 W for 60 s, after power was seen) or when the period ends
- `_offpeak_done` latches one decision per window: the daily reset happens
  at the start of the period (inside the window, or inside the 5 min delay
  for a period starting on the hour) and clears `_fully_heated`, which must
  not restart a full tank — `reset_daily()` carries it into the latch, and
  the latch clears only on the window's closing edge.  An off-peak session
  never sets `_fully_heated`
- Switching the heater off by hand during an off-peak session sets the latch:
  no restart until the next window
- `force_stop_overload()` arms the 15 min cooldown, otherwise the rule would
  switch the heater straight back on

## EV Wallbox Schedule (Auto) and Full Power
- Force Charge sets `FULL_POWER_AMPS` (32 A) once at start (or when selected
  on a running session), then never writes the amperage again.  A session
  merely *adopted* in Force (restart, resume from the app) keeps its amps
- In Auto, a charge the Wallbox starts by itself is `StartMode.SCHEDULE` when
  the last idle tick saw status `Scheduled` or we pressed "Resume schedule"
  since (`_idle_scheduled`, refreshed on idle ticks only — a switch still
  reading on right after our own pause must never become a 32 A session).
  It gets 32 A once, then the Force contract: no regulation, no SoC /
  no-demand stop, no 7 kW trim, and `is_hands_off()` suspends the
  coordinator's `_handle_overload()`.  Under Manual it is piloted again
- A pause via the API takes the Wallbox off its schedule.  When
  `OPT_EV_CHARGER_RESUME_SCHEDULE` (button) is set, `_stop_session()` presses
  it after every Auto-rule stop, and so does selecting Auto while the
  charger is off.  Never on Disabled / master off / Manual stops.  The button
  is plain config, assigned on every options update — not in `entity_ids`

## Multi-Device Structure
Three HA device types, each with distinct `DeviceInfo`:
- **Battery** (`battery_{entry_id}`): SoC, power, SoH, grid, consumption, charge target/power
- **Solar** (`solar_{entry_id}_{index}`): forecast today/tomorrow (via_device: battery)
- **System** (`system_{entry_id}`): optimization status, cost savings, consumption forecast, MQTT connected, grid charging recommended, enabled switch (via_device: battery)

Device info helpers in `sensor.py`: `_battery_device_info()`, `_solar_device_info()`, `_system_device_info()`
