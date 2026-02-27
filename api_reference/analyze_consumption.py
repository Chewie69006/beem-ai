#!/usr/bin/env python3
"""Analyze consumption from the 3 saved Beem API intraday streams.

Computes: consumption = production + grid_import - grid_export

Usage:
    .venv/bin/python api_reference/analyze_consumption.py
"""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DIR = Path(__file__).parent

STREAMS = {
    "production": DIR / "beem/production/energy/intraday.json",
    "grid_import": DIR / "beem/consumption/houses/active-energy/intraday.json",
    "grid_export": DIR / "beem/consumption/houses/active-returned-energy/intraday.json",
    "bat_charged": DIR / "beem/batteries/energy-charged/intraday.json",
    "bat_discharged": DIR / "beem/batteries/energy-discharged/intraday.json",
}


def load_stream(path: Path) -> dict[str, float]:
    """Load a stream file and return {startDate: total_wh}."""
    with open(path) as f:
        data = json.load(f)

    containers = data.get("houses") or data.get("devices") or [data]
    result: dict[str, float] = {}
    for container in containers:
        for m in container.get("measures", []):
            ts = m.get("startDate")
            val = m.get("value")
            if ts is not None and val is not None:
                result[ts] = result.get(ts, 0.0) + float(val)
    return result


def main():
    # Load all streams
    streams = {}
    for name, path in STREAMS.items():
        if not path.exists():
            print(f"WARNING: {path} not found — run fetch_all.py first")
            streams[name] = {}
        else:
            streams[name] = load_stream(path)
            print(f"Loaded {name}: {len(streams[name])} data points")

    prod = streams["production"]
    imp = streams["grid_import"]
    exp = streams["grid_export"]
    charged = streams["bat_charged"]
    discharged = streams["bat_discharged"]

    all_ts = sorted(set(prod) | set(imp) | set(exp) | set(charged) | set(discharged))
    if not all_ts:
        print("No data found.")
        return

    # consumption = production + grid_import - grid_export - battery_charged + battery_discharged
    Row = tuple[datetime, float, float, float, float, float, float]
    hourly: list[Row] = []
    for ts_str in all_ts:
        p = prod.get(ts_str, 0.0)
        i = imp.get(ts_str, 0.0)
        e = exp.get(ts_str, 0.0)
        ch = charged.get(ts_str, 0.0)
        di = discharged.get(ts_str, 0.0)
        consumption = max(0.0, p + i - e - ch + di)
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        hourly.append((ts, consumption, p, i, e, ch, di))

    # --- Hourly breakdown ---
    W = 105
    print(f"\n{'=' * W}")
    print(f"  HOURLY BREAKDOWN ({len(hourly)} hours)")
    print(f"  Formula: consumption = production + grid_import - grid_export - bat_charged + bat_discharged")
    print(f"{'=' * W}")
    print(
        f"{'Timestamp':<22} {'Consumption':>12} {'Production':>11} {'Grid Imp':>9} "
        f"{'Grid Exp':>9} {'Bat Chg':>9} {'Bat Dis':>9}"
    )
    print("-" * W)

    for ts, cons, p, i, e, ch, di in hourly:
        local = ts.strftime("%Y-%m-%d %H:%M")
        print(
            f"{local:<22} {cons:>10.0f}Wh {p:>9.0f}Wh {i:>7.0f}Wh "
            f"{e:>7.0f}Wh {ch:>7.0f}Wh {di:>7.0f}Wh"
        )

    # --- Daily summary ---
    keys = ["consumption", "production", "grid_import", "grid_export", "bat_charged", "bat_discharged"]
    daily: dict[str, dict[str, float]] = defaultdict(lambda: {k: 0.0 for k in keys})
    for ts, cons, p, i, e, ch, di in hourly:
        day = ts.strftime("%Y-%m-%d")
        daily[day]["consumption"] += cons
        daily[day]["production"] += p
        daily[day]["grid_import"] += i
        daily[day]["grid_export"] += e
        daily[day]["bat_charged"] += ch
        daily[day]["bat_discharged"] += di

    print(f"\n{'=' * W}")
    print(f"  DAILY SUMMARY ({len(daily)} days)")
    print(f"{'=' * W}")
    print(
        f"{'Day':<14} {'Consumption':>12} {'Production':>12} {'Grid Imp':>10} "
        f"{'Grid Exp':>10} {'Bat Chg':>10} {'Bat Dis':>10}"
    )
    print("-" * W)

    total = {k: 0.0 for k in keys}
    for day in sorted(daily):
        d = daily[day]
        print(
            f"{day:<14}"
            f" {d['consumption']/1000:>10.2f}kWh"
            f" {d['production']/1000:>10.2f}kWh"
            f" {d['grid_import']/1000:>8.2f}kWh"
            f" {d['grid_export']/1000:>8.2f}kWh"
            f" {d['bat_charged']/1000:>8.2f}kWh"
            f" {d['bat_discharged']/1000:>8.2f}kWh"
        )
        for k in total:
            total[k] += d[k]

    print("-" * W)
    n = max(len(daily), 1)
    print(
        f"{'TOTAL':<14}"
        f" {total['consumption']/1000:>10.2f}kWh"
        f" {total['production']/1000:>10.2f}kWh"
        f" {total['grid_import']/1000:>8.2f}kWh"
        f" {total['grid_export']/1000:>8.2f}kWh"
        f" {total['bat_charged']/1000:>8.2f}kWh"
        f" {total['bat_discharged']/1000:>8.2f}kWh"
    )
    print(
        f"{'AVG/DAY':<14}"
        f" {total['consumption']/1000/n:>10.2f}kWh"
        f" {total['production']/1000/n:>10.2f}kWh"
        f" {total['grid_import']/1000/n:>8.2f}kWh"
        f" {total['grid_export']/1000/n:>8.2f}kWh"
        f" {total['bat_charged']/1000/n:>8.2f}kWh"
        f" {total['bat_discharged']/1000/n:>8.2f}kWh"
    )


if __name__ == "__main__":
    main()
