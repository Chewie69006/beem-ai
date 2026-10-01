# Water heater off-peak top-up

Goal: 5 min after the off-peak (cheapest) period starts, if the water heater
is not fully heated, run it from the grid until it finishes or the period ends.

## Plan
- [x] `water_heater_controller.py`: `offpeak` kwarg on `evaluate()`, Auto-only
      branch (start / hold / stop at period end), `OFFPEAK_START_DELAY_S`
- [x] Per-window latch `_offpeak_done` (daily reset lands inside the window)
- [x] Off-peak session ends on thermostat cut, without the day lockout
- [x] `force_stop_overload()` arms the 15 min cooldown (no same-tick restart)
- [x] `coordinator._wh_offpeak()`: in cheapest period now AND 5 min ago
- [x] Tests + `.venv/bin/python -m pytest tests/`

## Decisions
- "HC" = `is_in_cheapest_period()`; no periods configured → rule inactive.
- Always on in Auto, no option toggle; delay is a constant.
- An off-peak session never sets `_fully_heated`, so next-day solar surplus
  can still top up.
- No power entity → "finished" is undetectable, switch stays on until the
  period ends (the heater's own thermostat regulates).
- The battery is not held back: while the heater runs off-peak it can
  discharge into it unless CFTG / the optimizer phase says otherwise.

## Review
Implemented, 10 new tests, full suite: 369 passed. Not yet tried on the real
HA instance.
