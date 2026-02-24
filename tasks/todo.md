# BeemAI Enhancement: Config UI + Multi-Panel + Water Heater Simplification

## Phase 1 — ConfigManager + CONFIG_CHANGED event
- [x] Add CONFIG_CHANGED to Event enum
- [x] Create `ha/config_manager.py`
- [x] Persist config to `data/config_state.json`

## Phase 2 — Multi-Panel Array Support
- [x] Update OpenMeteoSource for multi-array
- [x] Update ForecastSolarSource for multi-array
- [x] Update SolcastSource (proportional scaling)
- [x] Update SolarForecast aggregator (reconfigure method)
- [x] Update apps.yaml format (backward compat)

## Phase 3 — Water Heater Simplification
- [x] Remove temp_entity, target_temp, tank_liters, thermal model
- [x] Simplify decision tree (surplus + off-peak fallback)
- [x] Update apps.yaml

## Phase 4 — Reconfigure Methods + Wiring
- [x] Add reconfigure() to all modules (REST, MQTT, Tariff, Safety, Optimization, WaterHeater, Forecast sources)
- [x] Wire ConfigManager in beem_ai.py
- [x] Dispatch CONFIG_CHANGED to all modules

## Phase 5 — Tests
- [x] Create test_config_manager.py (15 tests)
- [x] Update test_water_heater.py (16 tests — surplus-driven, off-peak fallback, reconfigure, energy tracking)
- [x] Update test_solar_forecast.py (19 tests — added reconfigure propagation)
- [x] All 240 tests pass, 24 Python files validated

## Review
- 240 tests passing (up from 221)
- All 24 source files pass syntax validation
- No temperature-related code in water heater controller
- Multi-panel arrays sum forecasts from N panel configurations
- ConfigManager creates HA input_* entities and persists to JSON
- All modules have reconfigure() methods wired via CONFIG_CHANGED event
