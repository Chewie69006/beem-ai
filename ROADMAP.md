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

## 6. Battery Mode Sensor Accuracy

**Problem:** The "Battery Mode" sensor shows "advanced" if `charge_power_w > 0`, else "auto". But the API always sends `mode="advanced"` for all commands. The sensor doesn't reflect what was actually sent.

**Fix:** Track the actual mode sent to the API in StateStore and use that for the sensor value.

---

## ~~7. Migrate Solcast to Advanced PV Power Endpoint~~ ✗ NOT FEASIBLE

The `advanced_pv_power` endpoint returns **403 Forbidden** on the free hobbyist plan. This endpoint requires a paid Solcast subscription. Staying with `rooftop_sites/{site_id}/forecasts` which works on the hobbyist plan and provides the same P10/P50/P90 data.

---

## ~~8. Persist Logs to Disk~~ ✓ DONE

Implemented in `coordinator.py:_setup_file_logging()` — `RotatingFileHandler` writes to `beem_ai_data/beem_ai.log` (5 MB × 3 backups).

---

## 9. Refresh State from REST API Before Optimization

**Problem:** MQTT is the primary real-time data source, but if MQTT has been disconnected or data is stale when the optimizer runs, the optimization plan is based on outdated SoC/power values.

**Proposed solution:** Immediately before running optimization, fetch current battery state from the Beem REST API to ensure the optimizer always works with fresh data.

**Approach:**
- Before each optimization run, call the REST API to get current SoC and battery state
- Update the state store with API values so the optimizer sees fresh data
- MQTT remains the primary source for real-time entity updates — this is only a pre-optimization refresh
- Log when the API-fetched SoC differs significantly from the last MQTT value (indicates stale MQTT data)

