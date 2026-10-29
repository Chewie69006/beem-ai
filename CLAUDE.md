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
- The plug reports a command a moment late, and the coordinator runs
  `_handle_overload()` then `evaluate()` in the same tick: `evaluate()`
  skips the tick while the switch still shows the pre-command state
  (`COMMAND_SETTLE_S`, 15 s).  Without it our own force-stop reads as a
  manual off mid-session and latches the window for the night
- A switch that never moved after our command (`last_changed` older than
  the command) did not get it — meross_lan drops commands over its cloud
  rate limit.  It is re-sent `COMMAND_RETRIES` (2) times, then the cooldown
  applies, but the window is never latched: the top-up retries after it
- Only Auto surplus sessions add to the fully-heated energy counter — not
  an off-peak session (the 22:00 reset lands inside it) nor Manual.  Known
  gap: rule 1 can still fire at dawn on a grid-charged battery (99 %, a few
  W of meter noise as "export", solar 0 W) and that session counts

## Water Heater Solar Session (Auto)
- Rule 1: SoC ≥ 95 % and exporting.  Rule 2: SoC ≥ threshold, battery
  charge ≥ threshold (1.5 kW here) and import ≤ `IMPORT_TOLERANCE_W`
  (50 W).  Either one, held for the "Water Heater Sustain Duration"
  number (5–120 s — the start delay only), turns the heater on
- While the heater is on, rule 2 adds the plug's measured draw
  (`wh_power_entity`) back to the battery charge, divided by
  `BATTERY_TO_PLUG_EFFICIENCY` (0.9): the surplus as if the heater were
  off.  Each switch of the heater (2.2 kW at the plug) moves the battery
  by ~2.45 kW (28/10, 9 switches) — the plug reading alone read ~300 W
  short.  No reading (no entity, unavailable) → raw battery charge:
  rule 2 reads false while it heats, so a rule-2 session runs
  min_duration + `SURPLUS_LOSS_STOP_S`, mostly on the battery
- Stop, once min_duration has run: both rules false for
  `SURPLUS_LOSS_STOP_S` (3 min, per user); the surplus must be back
  `GRACE_SECONDS` (15 s) to call it off, so a lone good tick (the
  battery swings ±300 W under the heater) doesn't restart the delay.
  28/10: passing clouds took the surplus away for ~1 min, and the old
  30 s stop cut those sessions just before the sun came back.  An
  appliance cycle over 3 min (up to ~5 min that day) or a steady
  surplus under the threshold still stops it

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
- A stop that doesn't take — switch still on at a tick after
  `_stop_session()` with no idle tick between (25–27/10, car idle: the
  Wallbox API answered 403 to every pause of the loops, unseen here since
  service calls don't block) — makes a *held* session (`_held_since`):
  parked at `MIN_CHARGE_AMPS`, no regulation and never stopped again while
  the car doesn't draw, other Auto stops after `STOP_RETRY_S` (15 min);
  overload stops still apply.  `_stop_session()` writes no amperage: the
  user's amps are owed (`_restore_amps`) and written on the first idle
  tick, never to a charger that may still be charging (`stop()` and
  Disabled still restore at once).  Every idle tick also drops
  `_no_demand_since` (a stale arm stopped a solar start on its first
  tick: "sustained 30302s")
- All of the schedule behaviour is gated by the "Follow Wallbox Schedule"
  switch (`OPT_EV_FOLLOW_SCHEDULE`, default on, stored in the options like
  the mode selects, assigned to `controller.follow_schedule`).  Read live:
  off pilots a running scheduled session from the next tick and never
  presses the button; turning it on in Auto (BeemAI enabled) calls
  `resume_schedule_if_idle()`.  Force's 32 A does not depend on it
- The options flow starts from the stored options, so saving the form
  keeps what the entities wrote (modes, thresholds, this switch)

## Grid Charge Reserve (EV on solar surplus only)
- After a grid charge, the battery holds energy bought for the house (the
  day's solar was forecast short).  `GridChargeTracker` detects it from
  live data, whatever started it: `clamp(import, 0, battery_power)` ≥
  `GRID_CHARGE_MIN_W` (200 W) for `GRID_CHARGE_SUSTAIN_S` (120 s) in a row
  — a load spike while charging from the panels is regulated away in
  seconds and never counts
- The battery stays reserved for `GRID_CHARGE_HOLD` (20 h) after the last
  grid-charging tick; the timestamp is persisted in
  `beem_ai_data/grid_charge.json` (a morning restart keeps the reserve)
- While reserved: the EV Auto target SoC is raised to at least
  `RESERVED_MIN_SOC` (95 %).  Off-peak top-up, Manual, Force and the
  Wallbox schedule are untouched
- The water heater is **not** held back (per user, 28/10): a solar
  surplus goes to the tank whatever filled the battery.  Rule 2 (SoC ≥
  threshold + charge ≥ threshold) already counts solar charge only — it
  requires import ≤ `IMPORT_TOLERANCE_W` (50 W), so a grid charge never
  meets it.  The Beem charges from the grid most nights: a reserve gate
  on rule 2 turned it off for good (28/10: SoC 55 %, +2.5 kW, no start)
- The tracker runs in `_on_battery_update()`, before the diverters and
  even while BeemAI is disabled (it is a measurement).  Exposed as the
  System binary sensor "Grid Charge Reserve"

## Energy History (charge-target inputs — read-only)
- Phase 1 of ROADMAP §10.  `EnergyHistory` reads Home Assistant's
  long-term statistics (recorder, hourly, kept forever) — no log of our
  own, and the past is there from the first run.  Nothing is driven from it
- `base = consumption − water heater − EV − heating − cooling`, per hour
  over `HOURLY_DAYS` (28).  Sources: our own Consumption / Solar Power
  sensors (found in the entity registry by unique ID), the WH power entity,
  `OPT_EV_CHARGING_POWER_SENSOR`, `OPT_HEATING_ENERGY_ENTITIES` /
  `OPT_COOLING_ENERGY_ENTITIES` (lists), `OPT_OUTDOOR_TEMP_ENTITY` — the
  options step "consumption" (last step) plus one field in the EV step
- Power sensors → hourly `mean` (W ≈ Wh); energy counters → hourly
  `change`; the recorder converts units (`UNITS`).  Daily heating / cooling
  and outdoor temperature over `HVAC_HISTORY_DAYS` (365) for the heating
  model
- Base load (talon) = median over the last 14 nights of the 01:00–05:00
  window average, never hour by hour: the Daikin cloud reports by 2-hour
  blocks, so heating lands in the wrong hour.  `OPT_BASE_LOAD_OVERRIDE_W`
  > 0 replaces it
- Refreshed one minute after start, every hour, and 5 s after an options
  update (one-shot, re-armed).  A failure only logs a warning
- System sensors "Base Load" (W) and "Energy History" (days, with the last
  7 days' breakdown and `missing_statistics` — configured entities the
  recorder has nothing for)

## Charge Suggestion (ROADMAP §10 phase 2 — information only)
- `charge_planner.py` is pure (no HA imports).  Window = the next peak
  period after an off-peak one (`peak_window()` over
  `TariffManager.is_in_cheapest_period`, minute steps): 05:26 → 21:26 here
- Hour by hour over the window: `house(h) = base(h) + heating_day ×
  heating_shape(h) + cooling_day × cooling_shape(h)` (base: mean of that
  clock hour over 14 days; heating_day: least-squares fit of daily heating
  vs `max(0, 17 − T_mean)` over the year, else last 7 days' average;
  shapes: when the heating ran, 14 days).  Only the hour total is clamped
  at 0 — a lagging heat-pump counter makes some hours' base negative
- Target = smallest SoC keeping `E(h) ≥ min_soc` through the window, with
  0.95 one-way losses and saturation at capacity × SoH; rounded up to 5 %.
  P50 sun = suggestion, P10 sun = "cautious"
- Inputs: `OPT_SOLAR_FORECAST_ENTITIES` (Solcast `detailedHourly`, else
  half-hourly `detailedForecast`), `OPT_WEATHER_ENTITY`
  (`weather.get_forecasts` hourly, mean of the window's day; else the last
  2 days' measured temperature)
- Recomputed after every history refresh (hourly) and at
  `OPT_SUGGESTION_TIME` (default 20:00), when `OPT_NOTIFY_SERVICE` (e.g.
  `mobile_app_x`) gets the notification — French if HA's language is
  French, tag `beem_ai_charge_target`.  No suggestion → the notification
  says why.  Sent whatever the Enabled switch: it drives nothing
- System sensor "Charge Target Suggestion" (%) — attributes: window,
  sun P10/P50/P90, consumption, heating, temperature and their sources,
  hourly breakdown; `reason` when there is none

## Multi-Device Structure
Three HA device types, each with distinct `DeviceInfo`:
- **Battery** (`battery_{entry_id}`): SoC, power, SoH, grid, consumption, charge target/power
- **Solar** (`solar_{entry_id}_{index}`): forecast today/tomorrow (via_device: battery)
- **System** (`system_{entry_id}`): optimization status, cost savings, consumption forecast, MQTT connected, grid charging recommended, enabled switch (via_device: battery)

Device info helpers in `sensor.py`: `_battery_device_info()`, `_solar_device_info()`, `_system_device_info()`
