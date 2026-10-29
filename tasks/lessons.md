# Lessons

## "Solar surplus only" means where the energy comes from (28/10)
- Wrong: holding the water heater to "battery ≥ 95 % and exporting" after
  a night grid charge.  The Beem charges from the grid most nights, so the
  gate switched the SoC rule off for good (SoC 55 %, +2.5 kW, heater idle).
- Rule: solar surplus = solar going into the battery or the grid, i.e. no
  import.  Check the import, not the SoC or what filled the battery.  A
  battery reserve may hold back the EV, never the water heater.
- Before adding a gate on a rule, check how often its trigger fires on the
  real box (here: every night), not only on the day that motivated it.

## Replay the real log before calling a control fix done (28/10)
- Wrong: adding the plug's reading back and declaring the 15 min
  cycling fixed.  The plug (AC) read 2.2 kW, the battery (DC) moved by
  2.45 kW, and clouds / appliance cycles still cut sessions after 30 s.
- Rule: measure a load's effect from real switches (mean before/after,
  solar-corrected), not from its own meter.  Replay the day's MQTT log
  through the rule with the candidate constants, and look for a cliff
  (240 s → 4 stops, 300 s → 1) before picking one.
- A long stop window needs a noise-proof clear (grace), or one lucky
  tick keeps a fading session alive.

## Tuning values that cost battery are the user's call (28/10)
- I picked a 10 min stop delay; the user wanted 3 min.  Show the replay
  table (value → stops, heating, battery kWh) and let them pick, rather
  than choosing the long end myself.
