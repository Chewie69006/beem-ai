"""Forecast.Solar API adapter for solar production forecasting (async).

Free tier allows 12 requests per hour.  The adapter tracks its own request
budget and silently skips fetches when the budget is exhausted.
Supports multiple panel arrays -- each array requires a separate API call.
"""

import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import aiohttp

log = logging.getLogger(__name__)

MAX_REQUESTS_PER_HOUR = 12


class ForecastSolarSource:
    """Fetch PV production estimates from Forecast.Solar."""

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
        self.name = "forecast_solar"

        # Rate-limit tracking: list of monotonic timestamps of past requests
        self._request_timestamps: list[float] = []

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _prune_old_timestamps(self):
        """Remove timestamps older than 1 hour."""
        cutoff = time.monotonic() - 3600
        self._request_timestamps = [
            t for t in self._request_timestamps if t > cutoff
        ]

    def _budget_available(self, needed: int = 1) -> bool:
        self._prune_old_timestamps()
        return len(self._request_timestamps) + needed <= MAX_REQUESTS_PER_HOUR

    def _record_request(self):
        self._request_timestamps.append(time.monotonic())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch(self) -> dict:
        """Fetch solar production forecast for all panel arrays.

        Returns dict with keys: today, tomorrow, today_kwh, tomorrow_kwh.
        Returns empty dict on failure or if rate-limit budget is exhausted.
        """
        num_arrays = len(self.panel_arrays)
        if not self._budget_available(needed=num_arrays):
            log.warning(
                "Forecast.Solar rate-limit budget insufficient for %d arrays "
                "(%d/%d requests in the last hour)",
                num_arrays,
                len(self._request_timestamps),
                MAX_REQUESTS_PER_HOUR,
            )
            return {}

        total_today: dict[int, float] = {}
        total_tomorrow: dict[int, float] = {}
        total_today_kwh = 0.0
        total_tomorrow_kwh = 0.0
        any_success = False

        for array in self.panel_arrays:
            result = await self._fetch_for_array(
                array["tilt"], array["azimuth"], array["kwp"]
            )
            if not result:
                continue
            any_success = True
            for hour, watts in result["today"].items():
                total_today[hour] = total_today.get(hour, 0.0) + watts
            for hour, watts in result["tomorrow"].items():
                total_tomorrow[hour] = total_tomorrow.get(hour, 0.0) + watts
            total_today_kwh += result.get("today_kwh", 0.0)
            total_tomorrow_kwh += result.get("tomorrow_kwh", 0.0)

        if not any_success:
            return {}

        total_today = {h: round(w, 1) for h, w in sorted(total_today.items())}
        total_tomorrow = {h: round(w, 1) for h, w in sorted(total_tomorrow.items())}

        log.info(
            "Forecast.Solar forecast (%d arrays): today=%.2f kWh, tomorrow=%.2f kWh",
            num_arrays,
            total_today_kwh,
            total_tomorrow_kwh,
        )

        return {
            "today": total_today,
            "tomorrow": total_tomorrow,
            "today_kwh": round(total_today_kwh, 2),
            "tomorrow_kwh": round(total_tomorrow_kwh, 2),
        }

    def reconfigure(self, config: dict) -> None:
        """Update configuration from options flow."""
        self.lat = config.get("location_lat", self.lat)
        self.lon = config.get("location_lon", self.lon)
        if "panel_arrays" in config:
            self.panel_arrays = config["panel_arrays"]

    # ------------------------------------------------------------------
    # Per-array fetch
    # ------------------------------------------------------------------

    @staticmethod
    def _compass_to_solar_azimuth(compass: float) -> float:
        """Convert compass bearing to solar azimuth (0=South, -90=East, 90=West).

        Beem API returns compass bearing: 0=North, 90=East, 180=South, 270=West.
        Forecast.Solar expects: -180=North, -90=East, 0=South, 90=West, 180=North.
        """
        az = 180.0 - compass
        # Wrap to -180..180
        while az > 180:
            az -= 360
        while az < -180:
            az += 360
        return az

    async def _fetch_for_array(
        self, tilt: float, azimuth: float, kwp: float
    ) -> dict:
        """Fetch forecast for a single panel array."""
        solar_azimuth = self._compass_to_solar_azimuth(azimuth)
        url = (
            f"https://api.forecast.solar/estimate/"
            f"{self.lat}/{self.lon}/{tilt}/{solar_azimuth}/{kwp}"
        )

        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                self._record_request()
        except aiohttp.ClientError:
            log.exception(
                "Forecast.Solar API request failed for tilt=%s azimuth=%s",
                tilt,
                azimuth,
            )
            return {}
        except ValueError:
            log.exception("Forecast.Solar returned invalid JSON")
            return {}

        try:
            return self._parse(data)
        except (KeyError, TypeError, IndexError):
            log.exception("Failed to parse Forecast.Solar response")
            return {}

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse(data: dict) -> dict:
        watts_raw: dict[str, float] = data["result"]["watts"]
        wh_day: dict[str, float] = data["result"]["watt_hours_day"]

        today_date = date.today()
        tomorrow_date = today_date + timedelta(days=1)

        # Aggregate per-timestamp watts into hourly buckets via averaging
        today_buckets: dict[int, list[float]] = defaultdict(list)
        tomorrow_buckets: dict[int, list[float]] = defaultdict(list)

        for ts_str, watts in watts_raw.items():
            dt = datetime.fromisoformat(ts_str)
            hour = dt.hour

            if dt.date() == today_date:
                today_buckets[hour].append(watts)
            elif dt.date() == tomorrow_date:
                tomorrow_buckets[hour].append(watts)

        today: dict[int, float] = {
            h: round(sum(vals) / len(vals), 1)
            for h, vals in sorted(today_buckets.items())
        }
        tomorrow: dict[int, float] = {
            h: round(sum(vals) / len(vals), 1)
            for h, vals in sorted(tomorrow_buckets.items())
        }

        # Daily kWh from the API's own watt_hours_day field
        today_kwh = wh_day.get(today_date.isoformat(), 0.0) / 1000.0
        tomorrow_kwh = wh_day.get(tomorrow_date.isoformat(), 0.0) / 1000.0

        return {
            "today": today,
            "tomorrow": tomorrow,
            "today_kwh": round(today_kwh, 2),
            "tomorrow_kwh": round(tomorrow_kwh, 2),
        }
