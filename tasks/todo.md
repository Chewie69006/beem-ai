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

## Investigation: water heater barely ran on 27/10 (no code change)
Per user.  Deployed code = repo (b8a7b71).  No plug history (no API from
SSH): heater draw inferred from house consumption steps (~2.4 kW).
- Tank already hot: night top-up 26/10 22:03 → ~00:00 (thermostat cut).
  27/10: switch on 05:21–06:53 and 09:02–10:51, draw only 09:14–09:19 and
  09:30–09:39 (thermostat cut); 10:27–10:43 battery 100 %, ~2 kW exported,
  heater on and drawing nothing.  Same on 26/10: plug on 12:15–13:54,
  battery 100 %, up to 1.6 kW exported, nothing drawn (night 21:31 → 02:23)
- 10:44–12:57 car took 1.4–2.5 kW of surplus (adopted at 32 A → overload,
  then 6–11 A); heater short bursts only (~1.5–2 kWh for the whole day)
- 12:57 stop at SoC 92 %; afternoon solar ≈ house load, SoC never back to
  95 %.  Grid-charge reserve (02:03–04:21) kept rule 2 off, but the battery
  never charged ≥ 1500 W, so rule 2 would not have fired either
- Reheat at night: 21:31–22:53 ~2.4 kW (~3.3 kWh), battery 36 → 9 %, then
  grid; clean thermostat cut.  User test 28/10 09:36: +2.2–2.6 kW, heats
- Cause: the nightly top-up fills the tank every night, so the next day's
  surplus has little to heat and `fully_heated` (≥ 2500 Wh of surplus +
  thermostat cut) is rarely reached.  Trade-off, not a bug: skipping the
  27/10 top-up would have left ~3.3 kWh unheated for the 28/10 morning
  (battery 44 % at 09:30).  Possible lever, user's call: forecast-gated
  top-up.  Daytime figure is an estimate (car draw mixed in)
- [ ] Side finding: Wallbox loop every ~65 s (no car demand → stop →
      resume schedule → adopted MANUAL at 32 A), 110×/day; 10:44 overload

## Fix: grid-charge reserve no longer blocks the water heater
Per user (28/10): "dès lors que j'ai un surplus solaire, on doit
l'utiliser pour le chauffe-eau".  Seen at 10:40: grid charge 02:03–03:04
(9 → 50 %), then SoC 55 % and +2.5 kW of solar into the battery, heater
idle (rule 2 gated by the reserve; the decision is DEBUG-only).
- [x] Rule 2 ungated: `grid_charged` removed from the WH controller and
      its coordinator call.  "Solar charge" needs no new code: rule 2
      already requires import ≤ 50 W (`test_rule2_does_not_fire_when_grid_charging`)
- [x] Reserve kept for the EV only (target ≥ 95 %); tracker docstring and
      "Grid charging detected" log text updated; CLAUDE.md section
- [x] Tests: the 3 WH reserve tests deleted; coordinator replay of 28/10
      (night grid charge doesn't start the heater, morning solar charge
      does) — fails on HEAD (git archive copy), passes now.  504 passed
- [x] Follow-up (per user): rule 2 counted the heater's own draw as lost
      surplus (2.5 kW surplus − 2.4 kW heater = +100 W < 1500 W → stop
      after every min_duration, restart 30 s later).  While on, the
      plug's power is added back to the battery charge.  Tests: keeps
      running on its own draw (fails with the line patched out), still
      stops at 400 W left.  511 passed
- [ ] Not deployed to the HA box

## Fix: Wallbox stop/resume loop, and 32 A after a stop that doesn't take
Per user (28/10), from the 27/10 investigation.  ~110 cycles on 27/10
(00:23–02:00, 10:25–10:44): no-demand stop → restore 32 A → Resume
schedule → switch still on 5 s later → adopted MANUAL at 32 A → stop 60 s
later…  At 10:44 the car drew those 32 A (7995 W, import 4928 W).
- [x] Root cause (HA journal, DEBUG): the pauses in the loops failed —
      `pauseChargingSession` → 403 Forbidden from api.wall-box.com
      (`/v3/chargers/1145334/remote-action`; a few 502), logged by HA core
      as "Error executing service" 0.5 s later and unseen by the
      controller since service calls don't block.  340 re-adoptions 4–9 s
      after our stop, never an idle tick; the 7 loops that ended did so on
      a SCHEDULE adoption after one.  A stale `_no_demand_since` (30302 s,
      81609 s) made the first stops immediate
- [x] Held session (`_held_since`): on at a tick after `_stop_session()`
      with no idle tick between → parked at 6 A, no regulation and no stop
      while the car doesn't draw, other Auto stops after `STOP_RETRY_S`
      (15 min); one warning log.  Overload stops still apply
- [x] User amps owed (`_restore_amps`), written on the first idle tick
      only; Force clears the debt.  `_no_demand_since` dropped on idle
- [x] Tests: 27/10 replay (pause never shown / pause then immediate
      restart), late pause (12:41) → 6 A, no 32 A while on, 32 A once off,
      then SCHEDULE; SoC-floor retry bound; stale arm.  Red on HEAD
      (scratch copy): 4 replays fail on behaviour, the retry test on the
      missing attribute; 3 existing tests adapted to the deferred restore
      (one idle tick before checking amps).  Full suite 509 passed
      (incl. the WH session's uncommitted changes)
- [ ] Not deployed to the HA box
- [ ] Trade-offs: a session held at night charges at 6 A, not the
      schedule's 32 A, until a stop lands and an idle tick is seen; an
      adopted non-schedule session still sits at its amps (up to 32) for
      the 60 s no-demand arm before the first stop
- [ ] Follow-ups: why api.wall-box.com forbids the pause — every loop
      pause (car idle) got 403, while the 11:30:03 one (car drawing) went
      through and only the re-pause 5 s later failed; 27/10 00:23 the
      26/10 21:30 SCHEDULE session came back MANUAL — the switch read off
      or unknown for a tick, cause unconfirmed (no journal pass there)
