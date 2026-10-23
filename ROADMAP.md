# BeemAI — Roadmap

Planned improvements and fixes identified from SPECS.md review and user feedback.

---

## ~~0. Complete Refactoring — Strip to Core~~ ✓ DONE

**Branch:** `feature-refactoring`

**Goal:** Remove all automation/optimization logic and rebuild from a clean foundation. The integration should only expose real-time data from the Beem API and MQTT — no decisions, no control, no forecasting. Features will be re-added incrementally on top of this clean base.

### What stays

**3 HA devices with entities sourced directly from API/MQTT:**

**BeemAI Battery** (from MQTT telemetry):
- Battery SoC (%)
- Solar Power (W)
- Battery Power (W)
- Grid Power (W)
- Consumption (W, computed)
- Battery SoH (%)

**BeemAI Solar Array** × N (from `GET /devices` → `solarEquipments[]`):
- Capacity (kWp)
- Tilt (°)
- Azimuth (°)
- MPPT ID
- Panels in Series
- Panels in Parallel

**BeemAI System** (forecasts + connectivity):
- Solar Forecast Today (kWh)
- Solar Forecast Tomorrow (kWh)
- Consumption Forecast Today (kWh)
- MQTT Connected (binary sensor)
- Enabled (switch)

### What gets removed

| File | Reason |
|---|---|
| `optimization.py` | Phase scheduling, target SoC, charge power logic |
| `water_heater.py` | Water heater control rules |
| `safety_manager.py` | Seasonal min SoC, emergency stop |
| `tariff_manager.py` | Strip decision helpers (cheapest window, next HC/HSC). Keep: period storage, `current_tariff()`, `is_in_any_period()` |
| `event_bus.py` | Internal event system (only used by optimization) |

### What stays (data collection — no decisions)

| File | Role |
|---|---|
| `consumption_analyzer.py` | EMA consumption learning (keeps recording, used later by optimization) |
| `forecast_tracker.py` | Per-source accuracy tracking (keeps recording, used later for weights) |
| `forecasting/solar_forecast.py` | Ensemble merging |
| `forecasting/open_meteo.py` | Open-Meteo source |
| `forecasting/forecast_solar.py` | Forecast.Solar source |
| `forecasting/solcast.py` | Solcast source |

### What gets simplified

| File | Changes |
|---|---|
| `coordinator.py` | Remove optimization/water heater/tariff/safety scheduling. Keep: login, fetch solar equipment, MQTT connection, forecast refresh, consumption recording, periodic HA entity refresh |
| `state_store.py` | Remove `PlanData`, `daily_savings_eur`. Keep: `BatteryData`, `ForecastData`, `mqtt_connected`, `enabled` |
| `sensor.py` | Remove optimization sensors (optimization status, charge target/power, allow grid charge, prevent discharge, battery mode, min SoC, cost savings). Keep: Battery sensors, Solar Array sensors, forecast sensors (solar today/tomorrow, consumption today) |
| `binary_sensor.py` | Remove `grid_charging_recommended`. Keep: `mqtt_connected` |
| `switch.py` | Keep `enabled` switch |
| `options_flow.py` | Remove water heater, CFTG, min SoC, dry-run options. Keep: tariff periods + default price, Solcast API key/site ID, location override |
| `config_flow.py` | Keep as-is (login + optional Solcast step) |
| `const.py` | Remove unused option constants (water heater, CFTG, min SoC, dry-run) |
| `__init__.py` | Remove platform registrations for removed entities |

### What gets preserved (not deleted, moved to reference)

Move removed files to `_archive/` so the logic is available when re-implementing features on top of the clean base. This avoids losing the algorithms while keeping the active codebase minimal.

### Re-implementation order (roadmap items below)

After the refactoring, features are added back one at a time, each as a clean implementation following the updated SPECS.md:
1. Nightly optimization (simple deficit math from SPECS.md §4.1)
2. Tariff-aware phase scheduling
3. Water heater control
4. Log persistence
5. ...

---

## ~~1. Multi-Site Solcast Support~~ ✓ DONE

Per-array Solcast Site IDs via options flow (init → solcast → tariffs). Each array gets its own `solcast_site_{i}_id` field. Stored as `OPT_SOLCAST_SITE_IDS_JSON`. SolcastSource fetches each site independently and sums hourly P10/P50/P90 values. Budget shared across all sites (10/day). Config flow simplified — no more solcast step during initial setup. Diagnostic sensor per solar array shows configured site ID. Migration from legacy single `solcast_site_id` handled automatically.

---

## 2. Use P50 for Planning + Accuracy-Weighted Ensemble

**Problem (planning):** The optimizer uses P10 (conservative estimate) to compute `net_balance`, which systematically underestimates solar production. In `optimization.py:86-88`:
```python
production_p10 = production_kwh * 0.7
if forecast.solar_tomorrow_p10:
    production_p10 = sum(forecast.solar_tomorrow_p10.values()) / 1000.0
```
This causes over-charging from the grid on days where solar would have been sufficient.

**Problem (weights):** `ForecastTracker.get_weights()` is fully implemented and tested, but `SolarForecast.refresh()` never calls it. All sources are weighted equally regardless of their historical accuracy.

**Proposed change:**
1. **Switch planning to P50.** Use the ensemble's merged P50 forecast (already stored as `solar_tomorrow_kwh`) as the primary input for `net_balance`, instead of P10.
2. **Wire up accuracy-weighted ensemble.** After each forecast refresh, call `forecast_tracker.get_weights(source_names)` and pass the result to `solar_forecast.set_weights()`. Sources that consistently over- or under-predict are automatically down-weighted.
3. **Remove the blanket P10 pessimism.** The confidence adjustment (+15% for `low` confidence) remains as a safety margin for days with only 1 active source, but the systematic -30% P10 haircut is replaced by data-driven weights.

This means: if Solcast has been more accurate than Open-Meteo over the last 30 days, Solcast's P50 contribution gets a higher weight in the ensemble. The plan trusts the forecast more when it's been proven accurate, and trusts it less when sources disagree or have poor track records.

---

## ~~3. Reduce Forecast Refresh Interval to 4 Hours~~ ✓ DONE

Implemented in `coordinator.py` — `FORECAST_INTERVAL` changed from 1 hour to 4 hours. Persistent log file also added.

---

## 4. ~~Populate `consumption_today_kwh`~~ ✓ DONE

Implemented in `coordinator.py:_refresh_forecasts()` — sets `consumption_today_kwh` from `ConsumptionAnalyzer.get_forecast_kwh_today()`.

---

## 5. Forecast Deviation → Plan Adjustment

**Problem:** `_track_forecast_deviation` logs when actual solar differs from forecast by >20%, but takes no action.

**Possible improvement:** If actual solar is significantly below forecast by midday, proactively adjust (e.g., reduce water heater aggressiveness, update intraday reasoning). This is complex and should be carefully designed to avoid over-reacting to temporary clouds.

---

## ~~6. Battery Mode Control~~ ✓ DONE

Implemented battery control entities matching the Beem_Energy reference integration:
- **Battery Mode** (`select`): `auto` / `pause` / `advanced` — available when `canChangeMode` is true
- **Allow Charge From Grid** (`switch`): available only in advanced mode
- **Prevent Discharge** (`switch`): available only in advanced mode
- **Charge Power** (`select`): discrete values `500|1000|2500|5000` W — available only in advanced mode
- **Min SoC** (`number`, %, BOX): range 10–50, step 1 — available only in advanced mode
- **Max SoC** (`number`, %, BOX): range 50–100, step 1 — available only in advanced mode

API is source of truth: `GET /batteries/{id}/control-parameters` fetched on startup + every 2-min poll + after each PATCH. Only changed fields sent in PATCH (partial camelCase params).

---

## ~~7. Migrate Solcast to Advanced PV Power Endpoint~~ ✗ NOT FEASIBLE

The `advanced_pv_power` endpoint returns **403 Forbidden** on the free hobbyist plan. This endpoint requires a paid Solcast subscription. Staying with `rooftop_sites/{site_id}/forecasts` which works on the hobbyist plan and provides the same P10/P50/P90 data.

---

## ~~8. Persist Logs to Disk~~ ✓ DONE

Implemented in `coordinator.py:_setup_file_logging()` — `RotatingFileHandler` writes to `beem_ai_data/beem_ai.log` (5 MB × 3 backups).

---

## ~~9. Refresh State from REST API Before Optimization~~ ✓ DONE

Implemented `async_refresh_battery_from_api()` in `coordinator.py` — ready to be called before each optimization run. Maps API fields (`soc`, `solarPower`, `batteryPower`, `meterPower`, `inverterPower`, `globalSoh`) to state store fields. Logs SoC discrepancy warning when MQTT and API values differ by >2%. MQTT remains the primary real-time source; this is a point-in-time refresh for optimization freshness.


---

## 10. Overnight Charge Target Suggestion (planned — not implemented)

**Goal:** at a configurable time (default 20:00, before off-peak starts at
21:26), send a notification to the HA mobile app with tomorrow's solar
forecast and a suggested battery target for tonight's off-peak charge.
Then actionable buttons (apply / refuse / custom value), then automatic
application.

### User setup (answers, 23 Oct)
- Today the target is set by hand in the Beem app: it is the Max SoC
  (see "Driving the battery during off-peak").  BeemAI already drives the
  same API fields as the CharlesP44 integration (mode, allow charge from
  grid, prevent discharge, charge power, min / max SoC): no extra
  integration needed.
- Off-peak 21:26 → 05:26 (configured in BeemAI): the battery has to carry
  the house from 05:26 to 21:26.
- Notify service unknown: the options flow will list the
  `notify.mobile_app_*` services to pick from.
- Weather: HA's default Met.no entity is enough for a temperature forecast
  (hourly via `weather.get_forecasts`); Météo-France optional.
- Heating: Daikin reversible heat pumps through the `daikin_onecta`
  integration, plus a pellet stove that isn't connected.  Four units —
  `salon`, `chambre_maelys`, `chambre_parentale`, `bureau`:
  - `sensor.<unit>_climatecontrol_heating_daily_electrical_consumption` and
    `…_cooling_daily_electrical_consumption` (kWh, `total_increasing`,
    reset at midnight, 0.1 kWh steps; the Daikin cloud reports by 2-hour
    blocks, so 2-hourly at best — daily totals are what the model needs);
  - `sensor.<unit>_climatecontrol_outdoor_temperature` (all four read the
    same, 13.5 °C on 23 Oct) — the history of outdoor temperature;
  - order of magnitude: heating ≈ 870 kWh since January, 42 kWh in October
    so far, ≈ 33 kWh this week (~7 kWh/day since the cold came, about half
    a battery), the salon about two thirds of it; cooling ≈ 1 310 kWh this
    year, so the summer evenings matter too.
  In the options: a multi-select of heating / cooling energy sensors and
  one outdoor temperature sensor — nothing Daikin-specific in the code.
- Wallbox power is available through its integration.

### Inputs
- **Solar** — the HA Solcast integration (BJReplay/ha-solcast-solar), entity
  configurable (default `sensor.solcast_pv_forecast_forecast_tomorrow`):
  state = P50 kWh, attributes `estimate10` / `estimate90`, and
  `detailedHourly` (`period_start`, `pv_estimate10/pv_estimate/pv_estimate90`
  in kW) — the hourly breakdown must be enabled in the Solcast options.  No
  extra Solcast API calls (the hobbyist quota stays with that integration).
- **Base load** — hourly energy (last ~28 days) of the Beem consumption
  **minus** the water heater (plug power), the EV (Wallbox power) and the
  Daikin units: the first two run on surplus or off-peak straight from the
  grid, the Daikin is modelled on its own (below).  `ConsumptionAnalyzer`
  can't serve: its EMA (α = 0.1 per MQTT sample) follows the last ~minute of
  each weekday-hour bucket, and its Welford mean is all-time (no season).
- **Talon** (always-on floor) — median night base load (01:00–05:00, 14
  days); a configurable override (W) as a fallback.
- **Heating** — Daikin energy per hour vs outdoor temperature, learned from
  the Daikin's own outdoor sensor (history) and forecast from the weather
  entity (tomorrow, hourly).  Heating degree-hours
  (`max(0, 17 − T)`), plus cooling degree-hours for summer.
  The pellet stove: a "Pellet stove" switch on the System device — days it
  is on are left out of the learning, and when it's on for tomorrow the
  heating forecast drops (learned ratio, 0 to begin with).
- **Bootstrap from the HA recorder** — long-term statistics (hourly) of the
  Daikin energy and outdoor temperature, BeemAI consumption / solar, the
  water-heater plug and the Wallbox: the model can start from last winter
  instead of waiting weeks for data.
- **Tariffs** — tonight's off-peak end and tomorrow's next off-peak start
  (`TariffManager`); battery capacity, `min_soc`, round-trip efficiency.

### Off-peak night
Night heating runs inside off-peak (21:26–05:26).  It should run on the grid
at the off-peak price — not out of a battery that is then recharged the same
night, with its losses on top — unless the battery holds more than tomorrow
needs (see "Driving the battery during off-peak").

### Algorithm
Hour-by-hour simulation over the peak window `[05:26, 21:26)`:
`E(h+1) = min(capacity, E(h) − (base(h) + heating(h) − solar(h)))`.  The
suggested target is the smallest start energy for which `E(h) ≥ min_soc`
all the way to 21:26, rounded up to 5 %, clamped to `[min_soc, 100]`.
Solving it per hour (not on daily totals) catches the cold morning before
sunrise and the evening peak, which a sunny midday can't carry over once
the battery is full.  Run with solar P50 × learned correction (suggested)
and P10 (shown as the cautious value).  No grid charge when
`HC price / efficiency ≥ HP price`.

### Notification
- Time: `time` entity on the System device (default 20:00); target: a
  `notify.mobile_app_*` service picked in the options.
- Message: forecast P50 (P10–P90), consumption forecast (of which heating at
  the forecast temperature), suggested target, yesterday's recap.
- Actions (companion app; Android shows 3 at most): `APPLY` (suggested %),
  a `textInput` action for a custom % (`reply_text`), `REFUSE`.  BeemAI
  listens to the `mobile_app_notification_action` event; a `tag` lets an
  update (forecast moved, stove switched on) replace the notification.

### Driving the battery during off-peak (phase 3+)
**Observed on the box (log, 22–23 Oct).**  Nothing switches at 21:26 or
05:26 — the parameters stayed: advanced mode, charge from grid allowed at 5000 W, discharge not
prevented, Min SoC 10 %, Max SoC 99 % (set to 100 % by hand on 23 Oct,
09:15).  The app's percentage is **Max SoC**.  Every evening the user
either turns charge from grid on and sets Max SoC, or turns it off; the
setting then stays as it is through the next day.  With it on, the box
itself:
- runs normally in peak hours — no grid charge, discharges to cover the
  house, charges from the sun;
- holds during off-peak — does not power the house (user: even when it
  starts the window above Max SoC: 50 % set, held at 65 % all night);
- charges **late in the window**, up to Max SoC: 9 % at 02:30, ~5.5 kW
  from the grid, 99 % by ~04:50, held until 05:26, back to normal after.
  It knows the off-peak hours without the app storing them.

So the box already does the just-in-time charge.  BeemAI's part:
- **Apply a target = charge from grid on + Max SoC = target**, before
  21:26 — the user's evening routine, done for them.  Refused (or no
  target needed): charge from grid off.
- **05:26: charge from grid off, Max SoC back to 100 %**, so the sun can
  fill the battery and nothing charges from the grid the next night unless
  that evening's decision says so.  Left on at 100 %, the box would fill
  the battery from the grid the next night by default.
- **SoC above the target at 21:26** (the 65 % case): leave charge from grid
  off, so the battery powers the house (night heating included) down to
  the target; then turn it on — the box holds at Max SoC.
  Spent at night, the excess saves the off-peak kWh and leaves room for
  tomorrow's sun.
- **Charge power** stays the user's setting (5000 W today).  The car charges
  over the whole window and the Wallbox throttles itself on the house
  consumption, so a lower power would only spread the battery's share over
  more of the night — a possible refinement, not needed.
- A watchdog checks the battery follows each command within a few MQTT
  ticks (battery power sign vs the expected state) and otherwise puts the
  parameters back and notifies.
- Auto mode (phase 4): apply unless refused before 21:26.

Once the target exists, the diverters' grid-charge reserve should key off
"a target was planned for today" rather than off a detected grid charge
alone: a battery already above the target holds energy planned for the
house too.

### Feedback loop
Logged daily (`beem_ai_data/target_history.json`): forecast P10/P50/P90 vs
actual solar, predicted vs actual base load and heating, suggested /
applied target and the answer (apply, refuse, custom value), SoC at 05:26,
minimum SoC during peak hours, SoC at 21:26.  The last one is the real error
signal: ideal is reaching `min_soc` just as off-peak starts.  It feeds an
EMA correction of the solar forecast and of the margin.  Refusals and
custom values show where the user's judgement and the model disagree.

### Phases
1. ✓ **Energy history** — implemented: `energy_history.py` reads the
   recorder's long-term statistics instead of keeping a log of its own
   (bootstrap and log are the same thing), inputs in the options (heating /
   cooling counters, outdoor temperature, EV power sensor, base-load
   override), System sensors "Base Load" and "Energy History".
2. Solcast, weather and notify-service options; suggestion sensor +
   evening notification (information only).
3. Notification actions + applying the target.
4. Auto mode, learned corrections.

### Open questions
- None blocking phase 1.
