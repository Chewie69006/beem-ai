# EV Charger "Force Charge" mode

Goal: a mode that starts the charger immediately regardless of SoC/solar and
then keeps hands off the amperage — the user drives the current from the
Wallbox app.

## Plan
- [x] `const.py`: add `EV_MODE_FORCE = "Force Charge"`, append to `EV_MODES`
- [x] `ev_charger_controller.py`:
  - [x] `EvMode.FORCE` (same literal), `StartMode.FORCE`
  - [x] `start_force()` — turn_on WITHOUT `_set_amps`
  - [x] `handle_mode_change("Force Charge")` → clear `_saved_amps`, start if idle
  - [x] `_evaluate_charging`: Force short-circuits before no-demand /
        regulation, and does not enforce the ≥7 kW overload stop
  - [x] idle decision string no longer says "Manual"
- [x] Invariant: `_set_amps` never called while in Force (all 5 call sites)
- [x] Tests mirroring the Manual blocks + enum/EV_MODES parity test
- [x] `.venv/bin/python -m pytest tests/`

## Decisions
- Overload ≥7 kW does NOT stop the charger in Force (user decision):
  the mode accepts exceeding the household limit.
- No-demand status stop is skipped in Force (a slow-to-wake car would
  otherwise be killed 60s after start).
- One-shot: start on mode select, no re-assert after restart/stop.

## Review
Implemented. `custom_components/beem_ai/const.py` gains `EV_MODE_FORCE`;
`ev_charger_controller.py` gains `EvMode.FORCE` / `StartMode.FORCE`,
`start_force()`, a Force branch in `handle_mode_change()` and a Force
short-circuit at the top of `_evaluate_charging()`.

`_set_amps` call sites audited — none reachable in Force:
1. `start_manual` → Force uses `start_force` (no amps write)
2. `_evaluate_idle` → Auto-only branch
3. overload trim → Force returns before it
4. `_regulate_amps` → short-circuited
5. `_turn_off_and_restore` → `_saved_amps` forced to None on entering Force

9 new tests + an `EV_MODES`/`EvMode` parity test. Full suite: 327 passed.

## Follow-up fix (pre-existing bug, surfaced by Force)
`coordinator._setup_ev_charger()` recreated the controller on *every*
options update.  Selecting a mode writes `OPT_EV_CHARGER_MODE` to the
entry options, so the freshly-issued start's session state (including
`_pending_start_since`) was thrown away immediately — a slow Wallbox
turn_on was then abandoned with no retry.  It now reuses the controller
when the entity IDs are unchanged, mirroring `_setup_water_heater()`.
3 extra tests. Full suite: 330 passed.

## Force + overload: full opt-out
Per user: in Force Charge the house may exceed 7 kW.
- `EvChargerController`: no stop, no trim (debug log only).
- `coordinator._handle_overload()`: suspended entirely while EV mode is
  Force AND the charger is on — the water heater is no longer sacrificed.
  The grace timer resets so it starts fresh if Force ends while still
  overloaded.  Force selected with the charger *off* still protects.
3 coordinator tests. Full suite: 333 passed.
