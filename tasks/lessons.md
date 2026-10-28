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
