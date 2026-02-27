"""Solcast API adapter for solar production forecasting (async).

Solcast provides P10/P50/P90 probability estimates.  The free hobbyist plan
allows 10 API calls per day, so the adapter tracks daily usage and silently
skips fetches once the budget is exhausted.

Note: Solcast uses site_id which is tied to one physical panel config.
For multi-panel arrays, users need multiple Solcast sites. This adapter
keeps a single site but can scale output proportionally if the total kWp
from panel arrays differs from the Solcast site config.
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

import aiohttp

log = logging.getLogger(__name__)

MAX_REQUESTS_PER_DAY = 10


class SolcastSource:
    """Fetch rooftop PV forecasts from Solcast."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str | None,
        site_id: str | None,
        total_kwp: float | None = None,
    ):
        self._session = session
        self.api_key = api_key
        self.site_id = site_id
        self.total_kwp = total_kwp
        self.name = "solcast"

        # Daily budget tracking
        self._request_count: int = 0
        self._request_date: date | None = None

    # ------------------------------------------------------------------
    # Budget tracking
    # ------------------------------------------------------------------

    def _reset_if_new_day(self):
        today = date.today()
        if self._request_date != today:
            self._request_count = 0
            self._request_date = today

    def _budget_available(self) -> bool:
        self._reset_if_new_day()
        return self._request_count < MAX_REQUESTS_PER_DAY

    def _record_request(self):
        self._reset_if_new_day()
        self._request_count += 1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch(self) -> dict:
        """Fetch solar forecast from Solcast.

        Returns dict with keys: today, tomorrow, today_kwh, tomorrow_kwh,
        today_p10, today_p90, tomorrow_p10, tomorrow_p90.
        Returns empty dict if credentials are missing, budget is exhausted,
        or on any failure.
        """
        if not self.api_key or not self.site_id:
            return {}

        if not self._budget_available():
            log.warning(
                "Solcast daily budget exhausted (%d/%d requests today)",
                self._request_count,
                MAX_REQUESTS_PER_DAY,
            )
            return {}

        url = f"https://api.solcast.com.au/rooftop_sites/{self.site_id}/forecasts"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

        try:
            async with self._session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                self._record_request()
        except aiohttp.ClientError:
            log.exception("Solcast API request failed")
            return {}
        except ValueError:
            log.exception("Solcast returned invalid JSON")
            return {}

        try:
            return self._parse(data)
        except (KeyError, TypeError, IndexError):
            log.exception("Failed to parse Solcast response")
            return {}

    def reconfigure(self, config: dict) -> None:
        """Update configuration from options flow."""
        if config.get("solcast_api_key"):
            self.api_key = config["solcast_api_key"]
        if config.get("solcast_site_id"):
            self.site_id = config["solcast_site_id"]
        if "panel_arrays" in config:
            self.total_kwp = sum(a["kwp"] for a in config["panel_arrays"])

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse(self, data: dict) -> dict:  # noqa: C901
        forecasts = data["forecasts"]

        today_date = date.today()
        tomorrow_date = today_date + timedelta(days=1)

        # Collect 30-min intervals into hourly buckets
        Bucket = dict[int, list[float]]
        today_p50: Bucket = defaultdict(list)
        today_p10: Bucket = defaultdict(list)
        today_p90: Bucket = defaultdict(list)
        tomorrow_p50: Bucket = defaultdict(list)
        tomorrow_p10: Bucket = defaultdict(list)
        tomorrow_p90: Bucket = defaultdict(list)

        for entry in forecasts:
            period_end = datetime.fromisoformat(
                entry["period_end"].replace("Z", "+00:00")
            )
            # Use local date for bucketing
            local_dt = period_end.astimezone()
            d = local_dt.date()
            hour = local_dt.hour

            # Values are in kW -- convert to W
            pv50 = entry.get("pv_estimate", 0) * 1000.0
            pv10 = entry.get("pv_estimate10", 0) * 1000.0
            pv90 = entry.get("pv_estimate90", 0) * 1000.0

            if d == today_date:
                today_p50[hour].append(pv50)
                today_p10[hour].append(pv10)
                today_p90[hour].append(pv90)
            elif d == tomorrow_date:
                tomorrow_p50[hour].append(pv50)
                tomorrow_p10[hour].append(pv10)
                tomorrow_p90[hour].append(pv90)

        def _avg_bucket(bucket: Bucket) -> dict[int, float]:
            return {
                h: round(sum(vals) / len(vals), 1)
                for h, vals in sorted(bucket.items())
            }

        today = _avg_bucket(today_p50)
        tomorrow = _avg_bucket(tomorrow_p50)

        today_kwh = sum(today.values()) / 1000.0
        tomorrow_kwh = sum(tomorrow.values()) / 1000.0

        log.info(
            "Solcast forecast: today=%.2f kWh, tomorrow=%.2f kWh",
            today_kwh,
            tomorrow_kwh,
        )

        return {
            "today": today,
            "tomorrow": tomorrow,
            "today_kwh": round(today_kwh, 2),
            "tomorrow_kwh": round(tomorrow_kwh, 2),
            "today_p10": _avg_bucket(today_p10),
            "today_p90": _avg_bucket(today_p90),
            "tomorrow_p10": _avg_bucket(tomorrow_p10),
            "tomorrow_p90": _avg_bucket(tomorrow_p90),
        }
