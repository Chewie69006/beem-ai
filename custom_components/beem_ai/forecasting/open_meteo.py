"""Open-Meteo API adapter for solar irradiance forecasting (async).

Free API -- no key required, no rate limit.
Uses Global Tilted Irradiance (GTI) to estimate PV output for a specific
panel tilt and azimuth.  Supports multiple panel arrays by fetching GTI
for each and summing the hourly output.
"""

import logging
from datetime import date, datetime, timedelta

import aiohttp

log = logging.getLogger(__name__)

# Conversion constants
INVERTER_EFFICIENCY = 0.95
SYSTEM_LOSS_FACTOR = 0.85


class OpenMeteoSource:
    """Fetch solar irradiance from Open-Meteo and convert to PV output."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        lat: float,
        lon: float,
        panel_arrays: list[dict],
    ):
        self._session = session
        self.lat = lat
        self.lon = lon
        self.panel_arrays = panel_arrays
        self.name = "open_meteo"

    async def fetch(self) -> dict:
        """Fetch GTI forecast for each panel array and sum output.

        Returns dict with keys: today, tomorrow, today_kwh, tomorrow_kwh.
        Each today/tomorrow value is {hour_int: watts_float}.
        Returns empty dict on any failure.
        """
        total_today: dict[int, float] = {}
        total_tomorrow: dict[int, float] = {}

        for array in self.panel_arrays:
            result = await self._fetch_for_array(
                array["tilt"], array["azimuth"], array["kwp"]
            )
            if not result:
                continue
            for hour, watts in result["today"].items():
                total_today[hour] = total_today.get(hour, 0.0) + watts
            for hour, watts in result["tomorrow"].items():
                total_tomorrow[hour] = total_tomorrow.get(hour, 0.0) + watts

        if not total_today and not total_tomorrow:
            return {}

        # Round after summing
        total_today = {h: round(w, 1) for h, w in sorted(total_today.items())}
        total_tomorrow = {h: round(w, 1) for h, w in sorted(total_tomorrow.items())}

        today_kwh = sum(total_today.values()) / 1000.0
        tomorrow_kwh = sum(total_tomorrow.values()) / 1000.0

        log.info(
            "Open-Meteo forecast (%d arrays): today=%.2f kWh, tomorrow=%.2f kWh",
            len(self.panel_arrays),
            today_kwh,
            tomorrow_kwh,
        )

        return {
            "today": total_today,
            "tomorrow": total_tomorrow,
            "today_kwh": round(today_kwh, 2),
            "tomorrow_kwh": round(tomorrow_kwh, 2),
        }

    def reconfigure(self, config: dict) -> None:
        """Update configuration from options flow."""
        self.lat = config.get("location_lat", self.lat)
        self.lon = config.get("location_lon", self.lon)
        if "panel_arrays" in config:
            self.panel_arrays = config["panel_arrays"]

    # ------------------------------------------------------------------

    async def _fetch_for_array(self, tilt: float, azimuth: float, kwp: float) -> dict:
        """Fetch GTI forecast for a single panel array."""
        try:
            async with self._session.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": self.lat,
                    "longitude": self.lon,
                    "hourly": "global_tilted_irradiance",
                    "tilt": tilt,
                    "azimuth": azimuth,
                    "forecast_days": 2,
                    "timezone": "auto",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except aiohttp.ClientError:
            log.exception(
                "Open-Meteo API request failed for tilt=%s azimuth=%s", tilt, azimuth
            )
            return {}
        except ValueError:
            log.exception("Open-Meteo returned invalid JSON")
            return {}

        try:
            return self._parse(data, kwp)
        except (KeyError, TypeError, IndexError):
            log.exception("Failed to parse Open-Meteo response")
            return {}

    @staticmethod
    def _gti_to_ac_watts(gti_wm2: float, kwp: float) -> float:
        """Convert Global Tilted Irradiance (W/m2) to estimated AC output (W)."""
        if gti_wm2 is None or gti_wm2 < 0:
            return 0.0
        return (
            (gti_wm2 / 1000.0)
            * kwp
            * 1000.0
            * INVERTER_EFFICIENCY
            * SYSTEM_LOSS_FACTOR
        )

    def _parse(self, data: dict, kwp: float) -> dict:
        times = data["hourly"]["time"]
        gti_values = data["hourly"]["global_tilted_irradiance"]

        today_date = date.today()
        tomorrow_date = today_date + timedelta(days=1)

        today: dict[int, float] = {}
        tomorrow: dict[int, float] = {}

        for ts, gti in zip(times, gti_values):
            dt = datetime.fromisoformat(ts)
            hour = dt.hour
            ac_w = self._gti_to_ac_watts(gti, kwp)

            if dt.date() == today_date:
                today[hour] = round(ac_w, 1)
            elif dt.date() == tomorrow_date:
                tomorrow[hour] = round(ac_w, 1)

        today_kwh = sum(today.values()) / 1000.0
        tomorrow_kwh = sum(tomorrow.values()) / 1000.0

        return {
            "today": today,
            "tomorrow": tomorrow,
            "today_kwh": round(today_kwh, 2),
            "tomorrow_kwh": round(tomorrow_kwh, 2),
        }
