# BeemAI — Intelligent Energy Management for Beem Energy Batteries

A HACS custom component for Home Assistant that takes autonomous control of a
[Beem Energy](https://www.beem.energy/) battery: it plans overnight charging,
responds to live solar production, and manages a water heater as a solar dump load.

---

## Requirements

- Home Assistant 2024.4+
- A Beem Energy battery with API access (email + password)
- Python packages installed automatically by HA: `aiohttp`, `aiomqtt`

---

## Installation

### Via HACS (recommended)

1. HACS → ⋮ → **Custom Repositories** → paste repo URL → category **Integration**
2. Install **BeemAI** → restart Home Assistant
3. Settings → Devices & Services → **Add Integration** → search *BeemAI*

### Manual

Copy `custom_components/beem_ai/` into your HA config `custom_components/` folder
and restart.

---

## Configuration

### Config Flow (one-time)

| Field    | Description                              |
|----------|------------------------------------------|
| Email    | Beem Energy account email                |
| Password | Beem Energy account password             |

The integration automatically discovers your battery ID and serial.

### Options (editable at any time)

| Field                      | Default | Description                                               |
|----------------------------|---------|-----------------------------------------------------------|
| Latitude / Longitude       | —       | Installation location for solar forecasting               |
| Solcast API Key / Site ID  | —       | Optional premium solar forecast (10 calls/day)            |
| HP / HC / HSC tariff price | €0.27 / €0.21 / €0.16 | French EJP electricity prices (EUR/kWh)   |
| Min SoC summer             | 20 %    | Battery floor in summer months                            |
| Min SoC winter             | 50 %    | Battery floor in winter months (Nov–Mar)                  |
| Water heater switch entity | —       | HA entity ID of the smart plug switch                     |
| Water heater power entity  | —       | HA entity ID of the power sensor on the plug              |
| Water heater power (W)     | 2000 W  | Nominal consumption of the water heater                   |
| Solar panel arrays         | 2       | Number of panel orientations; tilt / azimuth / kWp each   |
| **Dry-run mode**           | Off     | Log all commands without executing them (see below)       |

---

## Entities Created

| Entity                               | Type          | Description                               |
|--------------------------------------|---------------|-------------------------------------------|
| `sensor.beem_ai_battery_soc`         | Sensor        | Battery state of charge (%)               |
| `sensor.beem_ai_solar_power`         | Sensor        | Solar production (W)                      |
| `sensor.beem_ai_battery_power`       | Sensor        | Battery charge/discharge power (W)        |
| `sensor.beem_ai_grid_power`          | Sensor        | Grid import/export (W)                    |
| `sensor.beem_ai_consumption`         | Sensor        | Estimated house consumption (W)           |
| `sensor.beem_ai_battery_soh`         | Sensor        | Battery health (%)                        |
| `sensor.beem_ai_optimal_charge_target` | Sensor     | Tonight's target SoC (%)                 |
| `sensor.beem_ai_optimal_charge_power`  | Sensor     | Planned charge power (W)                  |
| `sensor.beem_ai_optimization_status`   | Sensor     | Current phase + reasoning text            |
| `sensor.beem_ai_solar_forecast_today`  | Sensor     | Solar forecast for today (kWh)            |
| `sensor.beem_ai_solar_forecast_tomorrow` | Sensor   | Solar forecast for tomorrow (kWh)         |
| `sensor.beem_ai_consumption_forecast_today` | Sensor | Consumption forecast for today (kWh)  |
| `sensor.beem_ai_cost_savings_today`  | Sensor        | Estimated savings today (EUR)             |
| `binary_sensor.beem_ai_mqtt_connected` | Binary sensor | MQTT live-data connection status        |
| `binary_sensor.beem_ai_grid_charging_recommended` | Binary sensor | Whether grid charging is planned |
| `switch.beem_ai_enabled`            | Switch        | Enable / disable the automation entirely  |

---

## How It Works

### Battery Optimization

The engine runs two loops:

#### Evening Optimization (21:00 every day)

Called once at 21:00, plans the entire overnight charge strategy for the next morning.

1. Fetches tomorrow's solar forecast (P10 — conservative estimate)
2. Estimates tomorrow's consumption from learned household patterns
3. Calculates the **net solar balance** = solar forecast − consumption forecast
4. Determines **target SoC** based on how much grid charge is needed:

| Condition | Net balance | Target SoC |
|---|---|---|
| Very sunny | > 80% of capacity | 20% (leave room for solar) |
| Moderate sun | > 0 kWh | Night consumption + 10% buffer |
| Slightly cloudy | > −5 kWh | 60–80%, adjusted for deficit |
| Heavy deficit | ≤ −5 kWh | 80–95%, blended with current SoC |

5. Applies a **winter floor** (min 50% in Nov–Mar)
6. Applies a **confidence adjustment** (+15% if forecast confidence is low)
7. Picks the smallest charge power step that reaches target in 4 hours: 500 W → 1000 W → 2500 W → 5000 W
8. Decides if the cheap HC window (23:00–02:00) is also needed

#### Phase Schedule (evening → morning)

| Phase | Time | Action |
|---|---|---|
| `evening_hold` | 21:00–23:00 | Hold: no discharge, no grid charge |
| `hc_phase` | 23:00–02:00 | Grid charge at calculated power (if needed) |
| `hsc_phase` | 02:00–06:00 | Grid charge at full power (cheapest rate) |
| `solar_mode` | 06:00+ | Release to solar priority |

#### Intraday Monitoring (every 5 minutes)

- Runs safety checks (stale MQTT data, SoC below floor, low battery health)
- **Emergency stop**: if SoC is critically low while discharging, switches to safe fallback plan
- Tracks actual vs. forecast solar for accuracy learning

---

### Water Heater Control

Evaluated every 5 minutes. Rules are checked in priority order — first match wins.

| # | Condition | Action | Notes |
|---|---|---|---|
| 1 | System disabled (`switch.beem_ai_enabled` = off) | **OFF** | Clears all mode flags |
| 2 | Grid export ≥ heater power (e.g. 2300 W) | **ON** — *solar surplus* | You're already exporting at least as much as the heater draws — turning it on has zero grid impact |
| 3 | Was ON via rule 2, export now < 50% of heater power | **OFF** — *hysteresis exit* | Avoids rapid cycling when a cloud briefly passes |
| 4 | Battery charging power > house consumption + 200 W **AND** solar forecast for next 2 hours ≥ 70% of current production | **ON** — *storage surplus* | Solar fills the battery faster than the house consumes; forecast confirms it won't be a brief peak |
| 4x | Was ON via rule 4, conditions no longer hold | **OFF** | |
| 5 | Battery SoC ≥ 90% **AND** solar production ≥ 300 W | **ON** — *battery full* | Battery is nearly full and sun is still shining; better to heat water than waste solar |
| 6 | Was ON via rule 5, SoC < 85% or solar gone | **OFF** — *hysteresis exit* | 5% hysteresis prevents flickering near the 90% threshold |
| 7 | Off-peak tariff (HSC or HC) **AND** daily heating < 3 kWh **AND** (in HSC window OR after 22:00) | **ON** — *off-peak fallback* | Guarantees the tank gets enough energy on days with little sun |
| 8 | Peak tariff (HP) **AND** grid import > 0 | **OFF** — *cost protection* | Don't heat with expensive electricity |
| 9 | None of the above | Maintain current state | |

**Hysteresis summary**: rules 2 and 5 have separate "exit" conditions (rules 3 and 6) with lower
thresholds so the heater doesn't toggle every 5 minutes near the boundary.

---

### Solar Forecasting

Three sources merged into a weighted ensemble:

| Source | Cost | Rate limit | Notes |
|---|---|---|---|
| Open-Meteo | Free, no key | None | Global Tilted Irradiance → DC output conversion |
| Forecast.Solar | Free | 12 req/hour | Per-array API calls |
| Solcast | Paid (10 free/day) | 10 req/day | P10 / P50 / P90 confidence intervals |

Weights are computed from each source's historical accuracy (30-day rolling MAE).
Sources that persistently over- or under-predict are down-weighted automatically.

---

### French Tariff Schedule (EJP)

| Tariff | Hours | Default price |
|---|---|---|
| HSC (super off-peak) | 02:00–06:00 | €0.16/kWh |
| HC (off-peak) | 23:00–02:00, 06:00–07:00 | €0.21/kWh |
| HP (peak) | 07:00–23:00 | €0.27/kWh |

Prices are configurable in Options.

---

### Consumption Learning

Uses an **Exponential Moving Average** (α = 0.1) with **Welford's online algorithm**
for variance, across 168 buckets (7 days × 24 hours). This learns your household's
typical consumption pattern per day-of-week and hour, used to refine the evening
optimization's charge target.

---

## Dry-Run Mode

Enable **Dry-run mode** in Options to make BeemAI log every command it would send
without actually sending it. All battery control commands and water heater actuations
appear as `WARNING` log entries prefixed with `[DRY RUN]`.

Useful for verifying the logic on your installation before letting it control hardware.

Check logs at: Settings → System → Logs → filter by `beem_ai`.

---

## Safety

- **Emergency stop**: if SoC falls critically low while discharging, BeemAI immediately
  switches to a safe fallback plan (prevent discharge, no grid charge).
- **MQTT watchdog**: if live data is lost for more than 15 minutes, BeemAI calls the REST
  API to put the battery in automatic mode.
- **Stale data detection**: `SafetyManager` warns if MQTT data is more than 5 minutes old.

---

## Development

```bash
# Install test dependencies
python -m venv .venv && .venv/bin/pip install pytest pytest-asyncio aiohttp aiomqtt voluptuous

# Run tests
.venv/bin/python -m pytest tests/ -v
```

242 tests covering all modules.
