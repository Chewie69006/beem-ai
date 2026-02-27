<p align="center">
  <img src="logo.png" alt="BeemAI Logo" width="256">
</p>

# BeemAI — Intelligent Energy Management for Beem Energy Batteries

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)

A HACS custom component for Home Assistant that takes autonomous control of a
[Beem Energy](https://www.beem.energy/) battery: it plans overnight charging,
responds to live solar production, and manages a water heater as a solar dump load.

> Inspired by [CharlesP44/Beem_Energy](https://github.com/CharlesP44/Beem_Energy).

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

| Step | Fields | Description |
|------|--------|-------------|
| 1. Login | Email, Password | Beem Energy account credentials |
| 2. Solcast (optional) | API Key, Site ID | Enhanced solar forecasting via Solcast |

The integration automatically discovers your battery ID and serial, and uses
Home Assistant's configured location for solar forecasting.

### Options (editable at any time)

| Field                      | Default | Description                                               |
|----------------------------|---------|-----------------------------------------------------------|
| Latitude / Longitude       | HA config | Installation location (optional override)              |
| Solcast API Key / Site ID  | —       | Optional premium solar forecast (10 calls/day)            |
| Default tariff price       | €0.27   | Peak electricity price in EUR/kWh                         |
| Tariff periods (1-6)       | None    | Custom periods with label, time range, and price  |
| Min SoC                    | 20 %    | Battery floor (applied year-round)                        |
| **Smart CFTG**             | Off     | Dynamic grid charging during off-peak based on SoC        |
| Water heater switch entity | —       | HA entity ID of the smart plug switch                     |
| Water heater power entity  | —       | HA entity ID of the power sensor on the plug              |
| Water heater power (W)     | 2000 W  | Nominal consumption of the water heater                   |
| **Dry-run mode**           | Off     | Log all commands without executing them (see below)       |

> **Note:** Solar panel arrays (tilt, azimuth, kWp, MPPT ID, panel layout) are fetched
> automatically from the Beem API on startup — no manual configuration needed.

---

## Devices & Entities

Entities are organized into three HA devices:

### BeemAI Battery
| Entity | Type | Description |
|--------|------|-------------|
| Battery SoC | Sensor | Battery state of charge (%) |
| Solar Power | Sensor | Solar production (W) |
| Battery Power | Sensor | Battery charge/discharge power (W) |
| Grid Power | Sensor | Grid import/export (W) |
| Consumption | Sensor | Estimated house consumption (W) |
| Battery SoH | Sensor | Battery health (%) |

### BeemAI Solar Array (one device per array, auto-discovered from API)
| Entity | Type | Description |
|--------|------|-------------|
| Capacity | Sensor | Peak power (kWp) |
| Tilt | Sensor | Panel tilt angle (°) |
| Azimuth | Sensor | Compass bearing (°) |
| MPPT ID | Sensor | MPPT identifier |
| Panels in Series | Sensor | Number of panels in series |
| Panels in Parallel | Sensor | Number of panels in parallel |

### BeemAI System
| Entity | Type | Description |
|--------|------|-------------|
| Solar Forecast Today | Sensor | Ensemble solar forecast for today (kWh) |
| Solar Forecast Tomorrow | Sensor | Ensemble solar forecast for tomorrow (kWh) |
| Optimization Status | Sensor | Current phase + reasoning text |
| Consumption Forecast Today | Sensor | Consumption forecast for today (kWh) |
| Cost Savings Today | Sensor | Estimated savings today (EUR) |
| Optimal Charge Target | Sensor | Tonight's target SoC (%) |
| Optimal Charge Power | Sensor | Planned charge power (W) |
| Allow Grid Charge | Sensor | Whether grid charging is active (on/off) |
| Prevent Discharge | Sensor | Whether discharge is blocked (on/off) |
| Battery Mode | Sensor | Current control mode (auto/advanced) |
| Min SoC | Sensor | Active minimum SoC floor (%) |
| MQTT Connected | Binary sensor | MQTT live-data connection status |
| Grid Charging Recommended | Binary sensor | Whether grid charging is planned |
| Enabled | Switch | Enable / disable the automation entirely |

---

## How It Works

### Lifecycle of a Typical Day

Here's what BeemAI does from evening to evening, in chronological order:

```
00:00  Nightly optimization → compute tonight's charge plan
       ├── Fetch solar forecast for today (P50 = ensemble median)
       ├── Estimate today's consumption (learned from your history)
       ├── Calculate: do I need to charge from the grid tonight?
       ├── Schedule phase transitions based on your tariff periods
       └── Daily reset: clear savings counter, save all data to disk

00:30  Off-peak phase starts (depends on your tariff config)
       └── If charging needed: enable grid charging at calculated power

02:00  Cheapest period starts (depends on your tariff config)
       └── Continue or start grid charging at cheapest rate

06:00  Off-peak ends → switch to solar_mode
       ├── Disable grid charging
       ├── Allow battery discharge (powers the house from battery + solar)
       └── Status shows: "Daytime: solar priority, battery discharge allowed"

Every 4h: Forecast refresh → re-calculate plan with updated numbers
       └── The "Optimization Status" sensor updates with new forecast values

Every 5 min: Intraday safety checks
       ├── Is MQTT data fresh?
       ├── Is SoC dangerously low while discharging? → Emergency stop
       └── Track actual vs forecast solar production

Every 5 min: Water heater evaluation (if configured)
Every 5 min: Smart CFTG check (if enabled)
```

### Battery Optimization — Details

#### What the Optimizer Decides

At midnight (and again every 4 hours when forecasts refresh), the optimizer calculates:

1. **Target SoC** — how full should the battery be by morning?
2. **Charge power** — at what wattage should it charge from the grid?
3. **Phase schedule** — when to start/stop grid charging

The key formula is simple:
```
deficit = today_consumption − today_solar_P50
```
- **P50** is the ensemble median solar estimate — the most likely production scenario
- **Consumption** includes house + water heater, learned from historical data

| Deficit | Target SoC | Why |
|---|---|---|
| ≤ 0 kWh (solar surplus) | 0% | No CFTG needed — solar covers everything |
| > 0 kWh | deficit / capacity × 100, rounded up to nearest 5% | Charge just enough to cover the gap |

**Example:** consumption = 20 kWh, solar = 12 kWh → deficit = 8 kWh → target = ceil(8 / 13.4 × 100) = **60%**

Adjustments:
- **Low confidence** (only 1 forecast source worked): +15%
- **Min SoC floor** from your settings
- Capped at 95% (always leave headroom for solar)

**Charge power** picks the smallest step from [500, 1000, 2500, 5000] W that can deliver the needed energy within the cheapest tariff window.

#### Phase Schedule (evening → morning)

Phase timing comes from your configured tariff periods:

| Phase | What happens | Battery state |
|---|---|---|
| `night_hold` | 00:00 → off-peak start | Discharge blocked, no grid charge |
| `offpeak_charge` | Off-peak start → cheapest start | Grid charging at calculated power (if needed) |
| `cheapest_charge` | Cheapest tariff period | Grid charging continues at cheapest rate |
| `solar_mode` | After off-peak ends → next evening | Discharge allowed, no grid charge — normal solar operation |

**"Daytime: solar priority, battery discharge allowed"** = the battery is in normal mode. Solar charges it, and it discharges to power your house when solar isn't enough. This is the expected daytime state.

#### Why the Plan Changes Every 4 Hours

The forecast refreshes every 4 hours and triggers a re-optimization. This is intentional:
- Morning forecasts are more accurate than last night's
- The plan adjusts to reality as the day progresses
- During `solar_mode`, the re-optimization produces the same outcome (solar mode stays active)
- The numbers in "Optimization Status" update to reflect latest forecast data

#### Intraday Monitoring (every 5 minutes)

Does **not** change the plan. Only monitors:
- Safety checks (stale data, low SoC, disconnections)
- Emergency stop if SoC is critically low while discharging
- Tracks actual vs. forecast solar deviation (logged if >20%)

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

Three sources merged into an equally-weighted ensemble:

| Source | Cost | Rate limit | How it uses your arrays |
|---|---|---|---|
| Open-Meteo | Free, no key | None | One API call **per array** (uses tilt, azimuth, kWp), sums results |
| Forecast.Solar | Free | 12 req/hour | One API call **per array** (uses tilt, azimuth, kWp), sums results |
| Solcast | Free hobbyist (10/day) | 10 req/day | **Single call** for the whole site — arrays are configured on Solcast's website, not duplicated locally |

Each source returns hourly watt values for today and tomorrow. Results are merged by
weighted average (currently equal weights — 1/N per active source).

**Confidence level**: 1 source = `low` → +15% charge target buffer, 2 = `medium`, 3 = `high`.

**Solcast does not double-count.** It fetches your site's total forecast (which already
includes all arrays you configured on solcast.com.au). Open-Meteo and Forecast.Solar make
separate per-array calls using tilt/azimuth/kWp from the Beem API and sum them.

---

### Configurable Tariff Periods

Define up to 6 custom tariff periods in Options. Each period has a label, start/end
time (HH:MM), and price. Periods can cross midnight (e.g. 23:00–02:00).

**Default (no periods configured):**

If no custom periods are defined, the default tariff price applies 24/7 (single flat rate, labeled "HP"). Configure off-peak periods in Options to enable overnight charging optimization.

Any time outside configured periods uses the default tariff price.

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

274 tests covering all modules.

---

## Technical Specifications

See [SPECS.md](SPECS.md) for detailed technical specifications of every subsystem,
including startup sequence, data flow, API contracts, and known issues.
