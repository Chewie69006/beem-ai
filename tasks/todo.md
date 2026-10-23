# EV: honour the Wallbox schedule in Auto, 32 A for schedule / Force

Question: does Auto let the Wallbox schedule start the car?  Before this
change, no: a scheduled start was adopted as MANUAL and piloted with the Auto
rules — trimmed under 7 kW, shrunk toward 6 A on night import, paused at the
SoC floor (house battery under target − hysteresis) or after 60 s of
"waiting for car demand".  And any BeemAI pause (end of a solar session) is a
manual stop that takes the Wallbox off its schedule.

## Plan
- [x] `StartMode.SCHEDULE`: Auto adoption after an idle tick in `Scheduled`
      (or after a hand-back) → 32 A once, then Force-like hands-off
- [x] Force Charge: 32 A at start / when selected on a running session
- [x] `is_hands_off(mode)` → coordinator suspends overload handling for
      Force and schedule sessions
- [x] Optional `OPT_EV_CHARGER_RESUME_SCHEDULE` button, pressed after Auto
      stops and when Auto is selected with the charger off
- [x] Tests + `.venv/bin/python -m pytest tests/`

## Decisions
- Schedule session = Force contract, 7 kW limit included (32 A alone is
  7.4 kW): the user asked for 32 A.  WH off-peak top-up + EV at 32 A can
  reach ~10 kW at night.
- Only Auto defers to the schedule; Manual pilots it.
- Not classified as schedule: resume from `Paused`, plug-in during the
  window, HA restart mid-session (no persisted state).  With the button
  configured these self-heal at BeemAI's first stop.
- Re-enabling the master switch still commands nothing (no hand-back).
- No status entity → no schedule detection (previous behaviour).

## Review
Implemented.  Also made `test_externally_turned_off_clears_session`
deterministic: it read the real monotonic clock and failed on a host booted
less than ~2 min before (pre-existing).  Full suite: 397 passed.  Not yet
tried on the real HA instance — Wallbox pause / resume-schedule behaviour is
from the HA integration source and its GitHub discussion, not observed here.

## Follow-up: "Follow Wallbox Schedule" switch
Per user: a switch to turn the schedule behaviour on/off — on keeps it,
off restores the previous Auto behaviour.
- [x] `OPT_EV_FOLLOW_SCHEDULE` (default on), `BeemAIEvFollowScheduleSwitch`
      on the System device, only with an EV charger; persisted in the
      options like the mode selects
- [x] Gates: SCHEDULE adoption, the hands-off branch / `is_hands_off()`,
      the hand-back after Auto stops and on Auto selection
- [x] Read live: off mid-session → piloted from the next tick.  On in
      Auto (BeemAI enabled) → `resume_schedule_if_idle()`
- [x] Force Charge's 32 A is independent of the switch
- [x] Options flow now starts from the stored options: saving the form
      used to reset every entity-written option (modes, thresholds, this
      switch) to its default — pre-existing
Full suite: 417 passed.

## Fix: water heater locked out for the night after an overload force-stop
7 Oct, 22:07 — 36 min of off-peak heating, then nothing until 05:26.
- [x] Root cause: `_handle_overload()` → `force_stop_overload()` then
      `evaluate()` in the same tick; the plug still read "on", so our own
      stop was taken for an external on, then an external off mid-session
      → `_offpeak_done` latched
- [x] `COMMAND_SETTLE_S` (15 s): `evaluate()` skips the tick while the
      switch still shows the pre-command state; past it the old
      external-transition handling applies unchanged
- [x] Tests: `FakeHass.lag` / `flush()`; slow-switch stop and start,
      command that never lands, manual off inside the settle window, and
      the 22:07 replay through `_evaluate_surplus_diverters`.  All four
      lag tests fail with the guard disabled
- [ ] Not done: the ~45 s before a Wallbox-scheduled charge is adopted as
      hands-off still costs one pointless 15 min pause
Full suite: 422 passed.  Not deployed to the HA box.

## Fix: water heater drains a grid-charged battery
Per user: with night grid charging (cold, rainy days), once SoC is over the
50 % threshold and the battery charges, the heater restarts — whatever filled
the battery.  Wanted: heater (and EV) start on solar surplus only.
- [x] Root cause: rule 2 = SoC ≥ threshold + battery charging ≥ 500 W with no
      import.  The import check already keeps the grid's own charge out, but
      the SoC part counts grid-bought energy: a 600 W morning trickle into a
      battery grid-charged to 70 % starts a 2-3 kW heater, which runs on the
      battery for the 15 min floor, stops, restarts 30 s later… down to 40 %
- [x] `GridChargeTracker`: grid → battery (`clamp(import, 0, battery)`) ≥
      200 W for 120 s in a row; reserve held 20 h after the last such tick,
      persisted in `beem_ai_data/grid_charge.json`
- [x] While reserved: WH rule 2 off (rule 1 = SoC ≥ 95 % and exporting
      only), EV Auto target raised to ≥ 95 %
- [x] "Grid Charge Reserve" binary sensor (System) with timestamps
- [x] Tests: tracker, WH rule gating, coordinator replay of the rainy
      morning (fails with the gate disabled), EV target
- [x] `test_externally_turned_off_clears_session` pinned to a patched clock
      again: since `COMMAND_SETTLE_S` it failed on hosts up < ~17 min
- [ ] Not seen on the real HA box: no log access from here.  To confirm the
      cause, grep the log for "Solar surplus detected" on a grid-charged day

## Charge-target suggestion — phase 1: energy history
Per user: start ROADMAP §10, phase 1 (inputs + history, nothing driven).
- [x] Read HA's long-term statistics (`statistics_during_period`, recorder
      executor) instead of keeping our own hourly log: the history and the
      "bootstrap" are the same query, and last winter is already there
- [x] Options: EV charging power sensor (EV step); new last step
      "consumption": heating / cooling energy counters (multi), outdoor
      temperature, base-load override.  Labels in strings/en/fr — en/fr had
      no water-heater / EV steps at all, added
- [x] `EnergyHistory`: hourly base = consumption − WH − EV − heating −
      cooling (28 days), daily heating / cooling vs temperature (365 days),
      base load = median of 01:00–05:00 window averages (14 nights) — window,
      not hourly, because the Daikin cloud reports in 2-hour blocks
- [x] Coordinator: sources (own sensors via the entity registry), refresh
      at +60 s, hourly, and 5 s after an options update; warning on failure
- [x] Sensors "Base Load" and "Energy History" (System)
- [x] Solcast / weather / notify service moved to phase 2, where they're used

## Charge-target suggestion — phase 2: suggestion + notification
- [x] `charge_planner.py`, pure: peak window from the tariff, Solcast
      hourly / half-hourly parsing, forecast mean temperature, heating fit
      (least squares vs heating degrees, slope ≥ 0), consumption model from
      the history (base per clock hour, heating / cooling shapes), hourly
      battery simulation with losses and saturation, P50 / P10 targets
      rounded up to 5 %, notification text (fr / en)
- [x] Hour totals clamped, not the base: a lagging heat-pump counter
      makes some hours' base negative and its heating share carries it back
- [x] Coordinator: suggestion after each history refresh; notification at
      the configured time (`async_track_time_change`, re-armed on options
      update); a missing suggestion is notified with its reason
- [x] Options step "suggestion"; notify services listed from HA; the
      field is a suggested value so it can be cleared
- [x] Sensor "Charge Target Suggestion" (System)
- [x] Checked on a real HA 2026.10 (scratch): `weather.get_forecasts`
      response parsed, notify payload with `data.tag` accepted
- [ ] Next (phase 3): actions on the notification, applying the target
