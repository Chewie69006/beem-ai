# BeemAI — Roadmap

Planned improvements and fixes identified from SPECS.md review and user feedback.

---

## 0. Complete Refactoring — Strip to Core

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

## 1. Multi-Site Solcast Support

**Problem:** The current code accepts a single `solcast_site_id` in options. If the user has multiple solar arrays configured as separate Solcast sites, only one site's forecast is fetched — the other array is missing from Solcast's contribution to the ensemble.

**Proposed solution:**
- Fetch solar arrays from Beem API (already done) — each array has a unique `mppt_id`
- In the options flow, add a per-array Solcast Site ID field (keyed by `mppt_id` or array index)
- Store as JSON in options: `solcast_site_ids_json` → `[{"array_index": 0, "site_id": "xxx"}, ...]`
- Alternatively: accept a comma-separated list of site IDs in the existing single field
- `SolcastSource` becomes `SolcastMultiSiteSource`: fetches each site independently, sums hourly values (same pattern as Open-Meteo/Forecast.Solar)
- P10/P90 values are summed across sites
- Rate budget (10/day) is shared across all sites — with 2 sites, each refresh costs 2 API calls

**Simplest option:** Comma-separated site IDs in the existing field. Requires minimal options flow changes. Each site is fetched independently and results summed.

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

## 3. Reduce Forecast Refresh Interval to 4 Hours

**Problem:** Forecasts refresh every hour (`FORECAST_INTERVAL = timedelta(hours=1)` in `coordinator.py`). Solcast's free plan allows 10 calls/day. With 1 site that's already 24 calls/day (only 10 succeed, rest are skipped). With 2 sites (roadmap #1), it's 48 attempted calls. Open-Meteo and Forecast.Solar are less constrained but still wasteful — solar forecasts don't change dramatically hour-to-hour.

**Fix:** Change `FORECAST_INTERVAL` from 1 hour to 4 hours. This gives 6 refreshes/day:
- With 1 Solcast site: 6 calls/day (well within budget)
- With 2 Solcast sites: 12 calls/day (slightly over, but Solcast's budget tracker will skip the last refresh gracefully)
- Forecast.Solar: 12 calls/day with 2 arrays (well within 12/hour limit)

Refreshes would land at roughly: startup, +4h, +8h, +12h, +16h, +20h — covering the key planning windows (morning, midday, evening).

**Also:** Remove the re-optimization after each forecast refresh during daytime (ties into roadmap #5). The evening optimization at 21:00 remains the primary planning trigger.

---

## 4. ~~Populate `consumption_today_kwh`~~ ✓ DONE

Implemented in `coordinator.py:_refresh_forecasts()` — sets `consumption_today_kwh` from `ConsumptionAnalyzer.get_forecast_kwh_today()`.

---

## 5. Implement Cost Savings Tracking

**Problem:** `daily_savings_eur` is declared, reset at midnight, and exposed as a sensor, but no code ever increments it. The "Cost Savings Today" sensor always shows 0.

**Fix:** In the intraday loop or battery update handler, calculate savings when the battery discharges during peak hours (avoiding grid import at peak price). Use `tariff.calculate_savings_vs_hp()` which already exists.

---

## 6. Avoid Confusing Daytime Re-optimization Logs

**Problem:** Forecast refresh triggers `run_nightly_optimization()` even during daytime. This creates log entries like "Nightly plan: target=X%" at 14:00, which is confusing. The plan briefly switches to `night_hold` before immediately scheduling `solar_mode`.

**Options:**
- A) Only re-run full optimization between 00:00–06:00; during daytime, just update forecast data without re-planning
- B) Add a `run_daytime_reforecast()` that updates the forecast numbers in the plan reasoning without changing phases
- C) If current phase is `solar_mode`, skip the phase scheduling and only update the reasoning text

---

## 7. Forecast Deviation → Plan Adjustment

**Problem:** `_track_forecast_deviation` logs when actual solar differs from forecast by >20%, but takes no action.

**Possible improvement:** If actual solar is significantly below forecast by midday, proactively adjust (e.g., reduce water heater aggressiveness, update intraday reasoning). This is complex and should be carefully designed to avoid over-reacting to temporary clouds.

---

## 8. Battery Mode Sensor Accuracy

**Problem:** The "Battery Mode" sensor shows "advanced" if `charge_power_w > 0`, else "auto". But the API always sends `mode="advanced"` for all commands. The sensor doesn't reflect what was actually sent.

**Fix:** Track the actual mode sent to the API in StateStore and use that for the sensor value.

---

## 9. Migrate Solcast to Advanced PV Power Endpoint

**Problem:** The current code uses the legacy Solcast endpoint:
```
GET https://api.solcast.com.au/rooftop_sites/{site_id}/forecasts
```
Solcast recommends the newer "Advanced PV Power" model, which uses a more sophisticated PV simulation (based on pvlib-python with proprietary extensions) including snow soiling, albedo, and better temperature/wind derating.

**New endpoint:**
```
GET https://api.solcast.com.au/data/forecast/advanced_pv_power?resource_id={resource_id}
```

**Key differences:**
- Response fields change: `pv_power` → `pv_power_advanced`, `pv_power10` → `pv_power_advanced10`, `pv_power90` → `pv_power_advanced90`
- Uses `resource_id` query param instead of path param `site_id` (value is the same site ID)
- Supports up to 14 days ahead (vs 7 for legacy)
- Configurable resolution: 5, 15, or 60 minutes via `period` param (e.g. `period=PT60M`)
- Site configuration (tilt, azimuth, capacity) is managed via `resources/pv_power_site` endpoints on Solcast's side — not passed per request
- Same rate limit (10 calls/day on free plan)

**Changes needed:**
- `solcast.py`: Update URL from `rooftop_sites/{site_id}/forecasts` to `data/forecast/advanced_pv_power?resource_id={site_id}&format=json&period=PT60M`
- `solcast.py`: Parse `pv_power_advanced` / `pv_power_advanced10` / `pv_power_advanced90` instead of `pv_power` / `pv_power10` / `pv_power90`
- `api_reference/fetch_all.py`: Update Solcast fetch function with new endpoint
- No config flow changes — `resource_id` is the same value as the current `site_id`

---

## 10. Persist Logs to Disk

**Problem:** All BeemAI logging goes to Home Assistant's in-memory log (Settings → System → Logs). Once HA restarts or the log rotates, historical entries are lost. This makes it hard to debug issues that happened hours or days ago.

**Proposed solution:**
- Add a dedicated file handler for the `beem_ai` logger
- Write to `{HA_config}/beem_ai_data/beem_ai.log`
- Use `RotatingFileHandler` with a sensible max size (e.g. 5 MB, 3 backups = 20 MB max)
- Log level: `DEBUG` to file, keep HA console at `INFO`
- Initialize in `coordinator.async_setup()` alongside other data directory setup
- Include timestamps, module name, and log level in format

**Benefits:**
- Full debug history survives HA restarts
- Can be inspected via SSH/file manager without HA UI
- Rotation prevents unbounded disk growth

---

## 11. Replace Live MQTT Consumption Tracking with Daily API Fetch

**Problem:** `ConsumptionAnalyzer` currently learns from two sources: (1) one-time bootstrap via `seed_from_history()` on fresh install, and (2) live `record_consumption()` calls from the MQTT handler in `coordinator.py`. If MQTT is disconnected (HA restart, network issue, Beem outage), hours of consumption data are lost and the EMA buckets for those hours never update.

**Proposed change:**
- Drop `record_consumption()` from the MQTT path in `coordinator.py`
- Add a daily scheduled task (e.g., at 00:15 or 01:00) that fetches yesterday's data from all 5 Beem API intraday streams (`production`, `grid_import`, `grid_export`, `battery_charged`, `battery_discharged`)
- Compute true house consumption using the energy balance formula: `prod + import - export - charged + discharged`
- Feed the 24 hourly values into the EMA via `seed_from_history()`
- Save immediately after seeding

**Benefits:**
- No gaps from MQTT downtime — the API always has the full picture
- Single source of truth for consumption learning (API, not MQTT)
- Less code in the MQTT handler (no more `record_consumption()` calls)
- More accurate: uses the full 5-stream energy balance instead of the MQTT-derived consumption estimate
- Runs once per day = 5 API calls, well within rate limits
