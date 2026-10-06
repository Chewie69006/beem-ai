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
