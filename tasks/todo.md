# Task: Configurable EV Charger Start/Stop SoC Thresholds

## Goal
Replicate the water heater threshold pattern for the EV charger:
- **Start threshold** (default **90%**): SoC must reach this to begin charging
- **Stop/minimum threshold** (default **85%**): when EV is at minimum amps (6A) and solar is low, charging stops once SoC drops below this
- Both values exposed as HA Number entities, persisted to `ConfigEntry.options` (reboot-persistent)

## Changes

- [x] `const.py` — add `OPT_EV_START_SOC_THRESHOLD` and `OPT_EV_STOP_SOC_THRESHOLD`
- [x] `coordinator.py` — initialize `ev_start_soc_threshold` / `ev_stop_soc_threshold` from options (ctor + options-update handler), pass to `_ev_charger.evaluate(...)`
- [x] `ev_charger_controller.py` — remove module-level `SOC_START_THRESHOLD` / `SOC_STOP_THRESHOLD` constants, accept thresholds via `evaluate()` → `_evaluate_idle()` / `_evaluate_charging()`
- [x] `number.py` — add `BeemAIEvStartSocThreshold` + `BeemAIEvStopSocThreshold` classes, register in `async_setup_entry`
- [x] `tests/test_ev_charger_controller.py` — update imports & calls to pass thresholds
- [x] Run tests via venv: all 320 tests pass

## Review

### Changes
- **Defaults**: start = 90 %, stop = 85 % (was hard-coded 95 / 90).
- **Persistence**: both values are written to `ConfigEntry.options` on every change via `hass.config_entries.async_update_entry(...)`. Home Assistant persists those to `core.config_entries` on disk automatically, so the values survive a reboot.
- **UI**: two new Number entities on the "System" device, bounds 50–100 (start) and 40–99 (stop), step 1 %, mode BOX — same UX as the water heater thresholds.
- **Controller**: thresholds are now supplied per call (`evaluate(... start_soc_threshold=..., stop_soc_threshold=...)`), so runtime edits take effect on the next MQTT tick without a restart.
- **Availability**: the Number entities show as unavailable when the EV charger is not configured (mirrors the water-heater threshold pattern).

### Test results
- 42 / 42 EV-charger tests pass
- 320 / 320 total tests pass
