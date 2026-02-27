#!/usr/bin/env python3
"""BeemAI — API Reference & Response Capture

Calls every external API used by the integration and saves the raw responses
to files organized by provider and endpoint.

Usage:
    1. cp api_reference/config.py.sample api_reference/config.py
    2. Fill in your credentials in config.py
    3. Run: .venv/bin/python api_reference/fetch_all.py
    4. Responses are saved under api_reference/{provider}/{endpoint}.json

Each call also prints a summary to stdout.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import aiohttp

# =====================================================================
# LOAD CONFIG
# =====================================================================

try:
    from config import (
        BEEM_API_BASE,
        BEEM_EMAIL,
        BEEM_PASSWORD,
        LATITUDE,
        LONGITUDE,
        ARRAY_1_TILT,
        ARRAY_1_AZIMUTH,
        ARRAY_1_KWP,
        ARRAY_2_TILT,
        ARRAY_2_AZIMUTH,
        ARRAY_2_KWP,
        SOLCAST_API_KEY,
        SOLCAST_SITE_ID_1,
        SOLCAST_SITE_ID_2,
    )
except ImportError:
    print("ERROR: config.py not found.")
    print("  cp api_reference/config.py.sample api_reference/config.py")
    print("  Then fill in your credentials.")
    sys.exit(1)

# =====================================================================
# OUTPUT DIRECTORY
# =====================================================================

OUTPUT_DIR = Path(__file__).parent


def save_response(provider: str, endpoint: str, data) -> Path:
    """Save a JSON response to api_reference/{provider}/{endpoint}.json"""
    out_path = OUTPUT_DIR / provider / f"{endpoint}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  -> Saved to {out_path.relative_to(OUTPUT_DIR)}")
    return out_path


def save_request(provider: str, endpoint: str, data) -> Path:
    """Save a JSON request example to api_reference/{provider}/{endpoint}-request.json"""
    out_path = OUTPUT_DIR / provider / f"{endpoint}-request.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  -> Saved to {out_path.relative_to(OUTPUT_DIR)}")
    return out_path


def has_response(provider: str, endpoint: str) -> bool:
    """Check if a response file already exists."""
    return (OUTPUT_DIR / provider / f"{endpoint}.json").exists()


def pp(label: str, data):
    """Pretty-print a summary to stdout."""
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(json.dumps(data, indent=2, default=str))
    print()


# =====================================================================
# 1. BEEM — POST /user/login
# =====================================================================

async def beem_login(session: aiohttp.ClientSession) -> dict:
    """
    Authenticates with email/password. Returns JWT access token (~58 min TTL)
    and userId.

    Request:
        POST {BEEM_API_BASE}/user/login
        Body: {"email": "...", "password": "..."}

    Used by: beem_api.py -> BeemApiClient.login()
    Rate limit: None known. Token refreshed every ~50 min via re-login.
    """
    url = f"{BEEM_API_BASE}/user/login"
    payload = {"email": BEEM_EMAIL, "password": BEEM_PASSWORD}

    save_request("beem", "user/login", {
        "email": "your-email@example.com",
        "password": "your-password",
    })

    async with session.post(url, json=payload) as resp:
        resp.raise_for_status()
        data = await resp.json()

    save_response("beem", "user/login", data)
    pp("BEEM POST /user/login", {
        "accessToken": data.get("accessToken", "")[:20] + "...",
        "userId": data.get("userId"),
        "response_keys": list(data.keys()),
    })
    return data


# =====================================================================
# 2. BEEM — GET /devices
# =====================================================================

async def beem_get_devices(session: aiohttp.ClientSession, token: str) -> dict:
    """
    Returns all devices for the authenticated user. Each battery contains
    a solarEquipments[] array with panel configuration.

    Request:
        GET {BEEM_API_BASE}/devices
        Authorization: Bearer {token}

    Response shape:
        {
            "batteries": [{
                "id": "...",
                "serialNumber": "...",
                "solarEquipments": [{
                    "mpptId": "mppt1",
                    "orientation": 180,          # compass bearing (azimuth)
                    "tilt": 30,                  # degrees
                    "peakPower": 5000,           # watts (NOT kWp)
                    "solarPanelsInSeries": 10,
                    "solarPanelsInParallel": 1
                }, ...]
            }]
        }

    Used by:
        - config_flow.py -> _async_get_battery() (battery ID + serial discovery)
        - beem_api.py -> get_solar_equipments() (solar array config)
    """
    url = f"{BEEM_API_BASE}/devices"
    headers = {"Authorization": f"Bearer {token}"}

    async with session.get(url, headers=headers) as resp:
        resp.raise_for_status()
        data = await resp.json()

    save_response("beem", "devices", data)

    batteries = data.get("batteries") if isinstance(data, dict) else data
    if batteries:
        b = batteries[0]
        pp("BEEM GET /devices", {
            "battery_id": b.get("id"),
            "serialNumber": b.get("serialNumber"),
            "solarEquipments_count": len(b.get("solarEquipments", [])),
            "solarEquipments": b.get("solarEquipments", []),
        })
    return data


# =====================================================================
# 3. BEEM — POST /devices/mqtt/token
# =====================================================================

async def beem_get_mqtt_token(session: aiohttp.ClientSession, token: str, user_id: str) -> dict:
    """
    Obtains a JWT for authenticating to the Beem MQTT broker (AWS IoT).

    Request:
        POST {BEEM_API_BASE}/devices/mqtt/token
        Authorization: Bearer {token}
        Body: {"clientId": "beemapp-{userId}-{timestamp_ms}", "clientType": "user"}

    Response: {"jwt": "eyJ..."}

    Used by: beem_api.py -> get_mqtt_token()
    The JWT is used to connect to: wss://mqtt.beem.energy:8084/mqtt
    Topic subscribed: battery/{SERIAL}/sys/streaming
    """
    url = f"{BEEM_API_BASE}/devices/mqtt/token"
    headers = {"Authorization": f"Bearer {token}"}
    client_id = f"beemapp-{user_id}-{int(datetime.now().timestamp() * 1000)}"
    payload = {"clientId": client_id, "clientType": "user"}

    save_request("beem", "devices/mqtt/token", {
        "clientId": "beemapp-{userId}-{timestamp_ms}",
        "clientType": "user",
    })

    async with session.post(url, json=payload, headers=headers) as resp:
        resp.raise_for_status()
        data = await resp.json()

    save_response("beem", "devices/mqtt/token", data)
    pp("BEEM POST /devices/mqtt/token", {
        "clientId": client_id,
        "jwt": data.get("jwt", "")[:30] + "...",
    })
    return data


# =====================================================================
# 4. BEEM — PATCH /batteries/control-parameters
# =====================================================================

async def beem_set_control_params(session: aiohttp.ClientSession, token: str, battery_id: int | str) -> dict:
    """
    Sets battery operating mode and charge parameters.

    Request:
        PATCH {BEEM_API_BASE}/batteries/{battery_id}/control-parameters
        Authorization: Bearer {token}
        Body: {
            "mode": "advanced",
            "allowChargeFromGrid": false,
            "preventDischarge": false,
            "chargeFromGridMaxPower": 0,   # watts
            "minSoc": 20,
            "maxSoc": 100
        }

    Used by: optimization.py -> _set_battery_control()

    Phase parameter combinations:
        evening_hold:     preventDischarge=true,  allowCharge=false, power=0
        offpeak_charge:   preventDischarge=true,  allowCharge=true,  power={500-5000}
        cheapest_charge:  preventDischarge=true,  allowCharge=true,  power={500-5000}
        solar_mode:       preventDischarge=false, allowCharge=false, power=0
        auto (shutdown):  mode=auto, all defaults (minSoc/maxSoc ignored in auto mode)

    Rate limit: Self-imposed 10 calls/hour + 20-min cooldown on HTTP 429.
    Deduplication: Skips if params unchanged since last successful call.
    """
    url = f"{BEEM_API_BASE}/batteries/{battery_id}/control-parameters"
    headers = {"Authorization": f"Bearer {token}"}

    save_request("beem", "batteries/control-parameters", {
        "_description": "All phase examples with placeholder values",
        "solar_mode": {
            "mode": "advanced",
            "allowChargeFromGrid": False,
            "preventDischarge": False,
            "chargeFromGridMaxPower": 0,
            "minSoc": 20,
            "maxSoc": 100,
        },
        "evening_hold": {
            "mode": "advanced",
            "allowChargeFromGrid": False,
            "preventDischarge": True,
            "chargeFromGridMaxPower": 0,
            "minSoc": "{min_soc}",
            "maxSoc": 100,
        },
        "offpeak_charge": {
            "mode": "advanced",
            "allowChargeFromGrid": True,
            "preventDischarge": True,
            "chargeFromGridMaxPower": "{500|1000|2500|5000}",
            "minSoc": "{min_soc}",
            "maxSoc": 100,
        },
        "auto_shutdown": {
            "mode": "auto",
            "allowChargeFromGrid": False,
            "preventDischarge": False,
            "chargeFromGridMaxPower": 0,
            "minSoc": 20,
            "maxSoc": 100,
        },
    })

    # Actually send: solar_mode (safe, normal daytime operation)
    payload = {
        "mode": "advanced",
        "allowChargeFromGrid": False,
        "preventDischarge": False,
        "chargeFromGridMaxPower": 0,
        "minSoc": 20,
        "maxSoc": 100,
    }

    async with session.patch(url, json=payload, headers=headers) as resp:
        resp.raise_for_status()
        text = await resp.text()
        data = json.loads(text) if text.strip() else {"status": resp.status}

    save_response("beem", "batteries/control-parameters", data)
    pp("BEEM PATCH /batteries/control-parameters", {
        "payload_sent": payload,
        "response": data,
    })
    return data


# =====================================================================
# 5. BEEM — Intraday energy streams (consumption bootstrap)
# =====================================================================

async def beem_fetch_intraday(
    session: aiohttp.ClientSession, token: str, label: str, url: str, save_key: str,
) -> dict:
    """
    Generic fetcher for Beem intraday energy endpoints.

    All share the same request/response shape:
        GET {url}?from=...&to=...&scale=PT60M
        Authorization: Bearer {token}
        Accept: application/json

    Response:
        { "houses"|top-level: [{ "measures": [{"startDate": "...", "value": N}, ...] }] }

    value = Wh per hour interval.
    from/to MUST include timezone offset (e.g. +01:00).
    'to' should be midnight of the day after the last desired day.

    Three streams are combined for true house consumption:
        consumption = production + grid_import - grid_export

    Endpoints:
        - /production/energy/intraday                       (solar production)
        - /consumption/houses/active-energy/intraday        (grid import)
        - /consumption/houses/active-returned-energy/intraday (grid export)

    Used by: beem_api.py -> BeemApiClient.get_consumption_history()
    """
    from datetime import timedelta, timezone

    local_tz = datetime.now(timezone.utc).astimezone().tzinfo
    end = datetime.now(local_tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=7)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    params = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "scale": "PT60M",
    }

    async with session.get(url, headers=headers, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()

    save_response("beem", save_key, data)

    containers = data.get("houses") or data.get("devices") or [data]
    total_measures = sum(len(c.get("measures", [])) for c in containers)

    pp(f"BEEM GET {label}", {
        "params": params,
        "containers": len(containers),
        "total_measures": total_measures,
        "sample": (containers[0].get("measures", []) if containers else [])[:4],
    })
    return data


async def beem_all_intraday(session: aiohttp.ClientSession, token: str):
    """Fetch all three intraday streams needed for consumption bootstrap."""
    streams = {
        "production": (
            "/production/energy/intraday",
            f"{BEEM_API_BASE}/production/energy/intraday",
            "production/energy/intraday",
        ),
        "grid_import": (
            "/consumption/houses/active-energy/intraday",
            f"{BEEM_API_BASE}/consumption/houses/active-energy/intraday",
            "consumption/houses/active-energy/intraday",
        ),
        "grid_export": (
            "/consumption/houses/active-returned-energy/intraday",
            f"{BEEM_API_BASE}/consumption/houses/active-returned-energy/intraday",
            "consumption/houses/active-returned-energy/intraday",
        ),
    }
    for name, (label, url, save_key) in streams.items():
        if has_response("beem", save_key):
            print(f"  -- Skipping {save_key} (already exists)")
            continue
        await beem_fetch_intraday(session, token, label, url, save_key)


# =====================================================================
# 6. BEEM MQTT — Streaming Telemetry (reference, saved as static file)
# =====================================================================

def build_mqtt_reference(serial: str) -> dict:
    """Build the MQTT reference dict with the real serial number."""
    return {
        "_description": "MQTT streaming telemetry — not an HTTP call",
        "protocol": "MQTT over WebSocket (WSS)",
        "broker": "mqtt.beem.energy:8084/mqtt",
        "topic_pattern": "battery/{SERIAL}/sys/streaming",
        "topic_example": f"battery/{serial.upper()}/sys/streaming",
        "auth": "JWT from POST /devices/mqtt/token",
        "refresh_rate": "Real-time, every few seconds",
        "example_message": {
            "soc": 72.5,
            "solarPower": 3200.0,
            "batteryPower": 1500.0,
            "meterPower": -800.0,
            "inverterPower": 2400.0,
            "mppt1Power": 1800.0,
            "mppt2Power": 1400.0,
            "mppt3Power": 0.0,
            "workingModeLabel": "advanced",
            "globalSoh": 98.5,
            "numberOfCycles": 142,
            "capacityInKwh": 13.4,
        },
        "field_descriptions": {
            "soc": "State of charge (%), 0-100",
            "solarPower": "Total solar production (watts)",
            "batteryPower": "Battery power (watts). Positive = charging, negative = discharging",
            "meterPower": "Grid meter (watts). Positive = importing, negative = exporting",
            "inverterPower": "Inverter output (watts)",
            "mppt1Power": "MPPT 1 solar input (watts)",
            "mppt2Power": "MPPT 2 solar input (watts)",
            "mppt3Power": "MPPT 3 solar input (watts)",
            "workingModeLabel": "Current battery mode string",
            "globalSoh": "State of health (%), battery degradation indicator",
            "numberOfCycles": "Total charge/discharge cycles",
            "capacityInKwh": "Usable battery capacity (kWh)",
        },
        "mapping_to_state_store": {
            "soc": "battery.soc",
            "solarPower": "battery.solar_power_w",
            "batteryPower": "battery.battery_power_w",
            "meterPower": "battery.meter_power_w",
            "inverterPower": "battery.inverter_power_w",
            "mppt1Power": "battery.mppt1_w",
            "mppt2Power": "battery.mppt2_w",
            "mppt3Power": "battery.mppt3_w",
            "workingModeLabel": "battery.working_mode",
            "globalSoh": "battery.soh",
            "numberOfCycles": "battery.cycle_count",
            "capacityInKwh": "battery.capacity_kwh",
        },
        "computed_properties": {
            "battery.consumption_w": "solar_power_w + max(0, meter_power_w) + max(0, -battery_power_w)",
            "battery.export_power_w": "max(0, -meter_power_w)",
            "battery.import_power_w": "max(0, meter_power_w)",
            "battery.is_charging": "battery_power_w > 0",
            "battery.is_discharging": "battery_power_w < 0",
            "battery.is_importing": "meter_power_w > 0",
            "battery.is_exporting": "meter_power_w < 0",
        },
        "used_by": "mqtt_client.py -> BeemMqttClient",
    }


def save_mqtt_reference(serial: str):
    """Save the MQTT reference as a static JSON file."""
    ref = build_mqtt_reference(serial)
    save_response("beem", "mqtt-streaming", ref)
    pp("BEEM MQTT STREAMING (reference)", {
        "topic": ref["topic_example"],
        "fields": list(ref["example_message"].keys()),
    })


# =====================================================================
# 7. OPEN-METEO — GET /v1/forecast (Global Tilted Irradiance)
# =====================================================================

async def open_meteo_forecast(session: aiohttp.ClientSession, tilt: float, azimuth: float, kwp: float, array_index: int) -> dict:
    """
    Free solar irradiance forecast. No API key needed, no rate limit.
    Returns Global Tilted Irradiance (GTI) in W/m² per hour for 2 days.

    Request:
        GET https://api.open-meteo.com/v1/forecast
        Params: latitude, longitude, hourly=global_tilted_irradiance,
                tilt, azimuth (Open-Meteo convention), forecast_days=2, timezone=auto

    Azimuth convention:
        Open-Meteo: 0=South, 90=West, -90=East, 180=North
        Beem API:   0=North, 180=South, 270=West (compass bearing)
        The code converts automatically.

    Conversion to AC watts:
        ac_watts = (gti_wm2 / 1000) * kwp * 1000 * 0.95 * 0.85
                 = gti_wm2 * kwp * 0.8075

    Used by: forecasting/open_meteo.py -> OpenMeteoSource._fetch_for_array()
    Called: Once per solar array, results summed across arrays.
    Rate limit: None
    """
    # Convert compass bearing to Open-Meteo convention
    om_azimuth = azimuth
    if azimuth > 180 or azimuth < -180:
        om_azimuth = azimuth - 180
        if om_azimuth > 180:
            om_azimuth -= 360
        elif om_azimuth < -180:
            om_azimuth += 360

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": "global_tilted_irradiance",
        "tilt": tilt,
        "azimuth": om_azimuth,
        "forecast_days": 2,
        "timezone": "auto",
    }

    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()

    save_response("open_meteo", f"forecast_array_{array_index + 1}", data)

    times = data["hourly"]["time"]
    gti = data["hourly"]["global_tilted_irradiance"]
    sample = [
        {"time": t, "gti_wm2": g, "est_ac_watts": round(g * kwp * 0.8075, 1)}
        for t, g in zip(times, gti) if g and g > 0
    ][:6]

    pp(f"OPEN-METEO /v1/forecast (array {array_index + 1})", {
        "params": params,
        "azimuth_compass": azimuth,
        "azimuth_openmeteo": om_azimuth,
        "total_hours": len(times),
        "sample_nonzero": sample,
    })
    return data


# =====================================================================
# 8. FORECAST.SOLAR — GET /estimate/{lat}/{lon}/{tilt}/{azimuth}/{kwp}
# =====================================================================

async def forecast_solar(session: aiohttp.ClientSession, tilt: float, azimuth: float, kwp: float, array_index: int) -> dict:
    """
    Free PV production estimate. Returns watts per timestamp and daily Wh.

    Request:
        GET https://api.forecast.solar/estimate/{lat}/{lon}/{tilt}/{azimuth}/{kwp}

    Response shape:
        {
            "result": {
                "watts": {"2026-02-26 07:00:00": 120, ...},
                "watt_hours_day": {"2026-02-26": 25000, "2026-02-27": 28000},
                "watt_hours_period": {...},
                "watt_hours": {...}
            },
            "message": {"code": 0, ...}
        }

    Azimuth: Compass bearing (0=North, 180=South) — same convention as Beem API.

    Used by: forecasting/forecast_solar.py -> ForecastSolarSource._fetch_for_array()
    Called: Once per solar array, results summed.
    Rate limit: 12 requests/hour (tracked internally).
    """
    url = (
        f"https://api.forecast.solar/estimate/"
        f"{LATITUDE}/{LONGITUDE}/{tilt}/{azimuth}/{kwp}"
    )

    async with session.get(url) as resp:
        resp.raise_for_status()
        data = await resp.json()

    save_response("forecast_solar", f"estimate_array_{array_index + 1}", data)

    watts = data["result"]["watts"]
    wh_day = data["result"]["watt_hours_day"]
    sample = dict(list(watts.items())[:6])

    pp(f"FORECAST.SOLAR /estimate (array {array_index + 1})", {
        "url": url,
        "watt_hours_day": wh_day,
        "sample_watts": sample,
        "total_timestamps": len(watts),
    })
    return data


# =====================================================================
# 9. SOLCAST — GET /rooftop_sites/{site_id}/forecasts
# =====================================================================

async def solcast_forecast(session: aiohttp.ClientSession, site_id: str, site_index: int) -> dict:
    """
    Rooftop PV forecast with P10/P50/P90 confidence intervals.

    Request:
        GET https://api.solcast.com.au/rooftop_sites/{site_id}/forecasts
        Authorization: Bearer {api_key}

    Response shape:
        {
            "forecasts": [
                {
                    "pv_estimate": 2.5,      # P50 in kW
                    "pv_estimate10": 1.8,     # P10 (conservative) in kW
                    "pv_estimate90": 3.2,     # P90 (optimistic) in kW
                    "period_end": "2026-02-26T10:00:00.0000000Z",
                    "period": "PT30M"         # 30-minute intervals
                },
                ...
            ]
        }

    Notes:
        - Values are in kW (code converts to watts: * 1000)
        - period_end = end of the 30-min window
        - Each "site" on Solcast = one physical array (configured on solcast.com.au)
        - If you have 2 arrays, you need 2 sites and 2 API calls per refresh
        - Solcast already accounts for your array config — NO double-counting

    Used by: forecasting/solcast.py -> SolcastSource.fetch()
    Rate limit: 10 requests/day on free hobbyist plan (tracked internally).
    """
    url = f"https://api.solcast.com.au/rooftop_sites/{site_id}/forecasts"
    headers = {
        "Authorization": f"Bearer {SOLCAST_API_KEY}",
        "Accept": "application/json",
    }

    async with session.get(url, headers=headers) as resp:
        resp.raise_for_status()
        body = await resp.text()
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            print(f"  !! Solcast site {site_index + 1}: response is not JSON "
                  f"(content-type={resp.content_type}). "
                  f"Check your SOLCAST_API_KEY in config.py.")
            print(f"     Body preview: {body[:200]}")
            # Save raw response for debugging
            save_response("solcast", f"forecasts_site_{site_index + 1}_error", {
                "error": "Response is not JSON — likely invalid API key",
                "status": resp.status,
                "content_type": resp.content_type,
                "body_preview": body[:500],
            })
            return {}

    save_response("solcast", f"forecasts_site_{site_index + 1}", data)

    forecasts = data.get("forecasts", [])
    sample = [
        {
            "period_end": f["period_end"],
            "P10_kW": f.get("pv_estimate10", 0),
            "P50_kW": f.get("pv_estimate", 0),
            "P90_kW": f.get("pv_estimate90", 0),
        }
        for f in forecasts if f.get("pv_estimate", 0) > 0
    ][:6]

    pp(f"SOLCAST /rooftop_sites/forecasts (site {site_index + 1})", {
        "site_id": site_id,
        "total_periods": len(forecasts),
        "period_duration": "30 min",
        "units": "kW",
        "sample_nonzero": sample,
    })
    return data


# =====================================================================
# MAIN
# =====================================================================

async def main():
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:

        # ---- Beem API ----
        # Check which Beem responses are missing (login excluded — it's only a means)
        beem_missing = [
            ep for ep in [
                "devices",
                "devices/mqtt/token",
                "batteries/control-parameters",
                "production/energy/intraday",
                "consumption/houses/active-energy/intraday",
                "consumption/houses/active-returned-energy/intraday",
                "mqtt-streaming",
            ]
            if not has_response("beem", ep)
        ]

        token = None
        user_id = None
        battery_id = None
        battery_serial = ""

        if beem_missing:
            print(f"\n>>> BEEM API (missing: {', '.join(beem_missing)})")

            # Login required to fetch any missing Beem response
            login_data = await beem_login(session)
            token = login_data["accessToken"]
            user_id = login_data["userId"]

            # Always fetch devices to discover battery_id/serial
            devices_data = await beem_get_devices(session, token)
            batteries = devices_data.get("batteries") if isinstance(devices_data, dict) else devices_data
            if not batteries:
                print("ERROR: No batteries found in /devices response. Cannot continue.")
                return
            battery = batteries[0]
            battery_id = battery.get("id")
            battery_serial = battery.get("serialNumber", "")
            if not battery_id:
                print("ERROR: Battery has no 'id' field. Cannot continue.")
                return
            print(f"  Discovered battery: id={battery_id}, serial={battery_serial}")

            if not has_response("beem", "devices/mqtt/token"):
                await beem_get_mqtt_token(session, token, user_id)
            else:
                print("  -- Skipping devices/mqtt/token (already exists)")

            if not has_response("beem", "batteries/control-parameters"):
                await beem_set_control_params(session, token, battery_id)
            else:
                print("  -- Skipping batteries/control-parameters (already exists)")

            await beem_all_intraday(session, token)

            if not has_response("beem", "mqtt-streaming"):
                save_mqtt_reference(battery_serial)
            else:
                print("  -- Skipping mqtt-streaming (already exists)")
        else:
            print("\n>>> BEEM API — all responses cached, skipping")

        # ---- Open-Meteo (one call per array) ----
        print("\n>>> OPEN-METEO")
        for i, (tilt, azimuth, kwp) in enumerate([
            (ARRAY_1_TILT, ARRAY_1_AZIMUTH, ARRAY_1_KWP),
            (ARRAY_2_TILT, ARRAY_2_AZIMUTH, ARRAY_2_KWP),
        ]):
            if has_response("open_meteo", f"forecast_array_{i + 1}"):
                print(f"  -- Skipping array {i + 1} (already exists)")
                continue
            try:
                await open_meteo_forecast(session, tilt, azimuth, kwp, i)
            except Exception as e:
                print(f"  !! Open-Meteo array {i + 1} failed: {e}")

        # ---- Forecast.Solar (one call per array) ----
        print("\n>>> FORECAST.SOLAR")
        for i, (tilt, azimuth, kwp) in enumerate([
            (ARRAY_1_TILT, ARRAY_1_AZIMUTH, ARRAY_1_KWP),
            (ARRAY_2_TILT, ARRAY_2_AZIMUTH, ARRAY_2_KWP),
        ]):
            if has_response("forecast_solar", f"estimate_array_{i + 1}"):
                print(f"  -- Skipping array {i + 1} (already exists)")
                continue
            try:
                await forecast_solar(session, tilt, azimuth, kwp, i)
            except Exception as e:
                print(f"  !! Forecast.Solar array {i + 1} failed: {e}")

        # ---- Solcast (one call per site) ----
        print("\n>>> SOLCAST")
        for i, site_id in enumerate([SOLCAST_SITE_ID_1, SOLCAST_SITE_ID_2]):
            if has_response("solcast", f"forecasts_site_{i + 1}"):
                print(f"  -- Skipping site {i + 1} (already exists)")
                continue
            try:
                await solcast_forecast(session, site_id, i)
            except Exception as e:
                print(f"  !! Solcast site {i + 1} failed: {e}")

    print("\n" + "=" * 60)
    print("  All responses saved under api_reference/")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
