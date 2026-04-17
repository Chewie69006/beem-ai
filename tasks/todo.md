# Task: Fix EV Charger Phantom-Surplus Feedback Loop

## Problem
Overnight bug: EV charger ramped from 6 A → 32 A despite real solar ≈ 700 W,
consumption ≈ 500 W. Logs showed "surplus=6874W" — pure phantom.

Root cause: `_compute_target_amps` used
`surplus_w = solar - consumption + ev_power_w`
assuming `consumption_w` *included* EV draw. The user's telemetry reports
consumption *excluding* EV draw, so every 1 A ramp added 230 W of fake
surplus, creating a positive feedback loop until MAX_CHARGE_AMPS.

Also: SoC-stop never fired because stop required `at_min_amps`, and we
were never at min due to the runaway ramp.

## Fix: headroom model

Drive regulation from raw grid + battery telemetry only:

    headroom_w = -meter_power_w + battery_power_w
    delta_amps = floor(headroom_w / 230)
    target    = current_amps + delta_amps

Because `meter_power_w` and `battery_power_w` already reflect the
current EV draw, the loop is self-correcting regardless of how
`consumption_w` accounts for the EV.

## Changes
- [x] `ev_charger_controller.py`
  - `evaluate()`: replace `export_w` with `meter_power_w` + `battery_power_w`
  - Start gate: `headroom_w >= START_HEADROOM_W` (= 6 A × 230 V = 1380 W)
  - Stop gate: `battery_power_w < 0` instead of `solar < consumption`
  - Regulation: `_regulate_amps(headroom_w, ...)` using `current + delta`
  - New `EMERGENCY_SHRINK_W = 500` bypasses throttle on heavy import
- [x] `coordinator.py` — pass `battery.meter_power_w` / `battery.battery_power_w`
- [x] `tests/test_ev_charger_controller.py` — full rewrite of `_eval` helper
  and every scenario to use explicit meter/battery values; added two
  phantom-surplus regression tests

## Test results
- 47 / 47 EV-charger tests pass
- 325 / 325 total tests pass
