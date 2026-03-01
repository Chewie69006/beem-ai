"""Solcast API adapter for solar production forecasting (async).

Uses the Advanced PV Power endpoint which returns per-site forecasts
with P10/P50/P90 probability estimates.  The free hobbyist plan allows
10 API calls per day.  With N sites, each refresh costs N calls, so
effective refreshes/day = 10/N.

Multi-site support: accepts a list of site_ids, fetches each independently,
and sums hourly values across all sites.
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

import aiohttp

log = logging.getLogger(__name__)

MAX_REQUESTS_PER_DAY = 10


class SolcastSource:
    """Fetch advanced PV power forecasts from Solcast."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str | None,
        site_ids: list[str] | None = None,
        total_kwp: float | None = None,
    ):
        self._session = session
        self.api_key = api_key
        self.site_ids = site_ids or []
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

    def _budget_available(self, calls_needed: int = 1) -> bool:
        self._reset_if_new_day()
        return self._request_count + calls_needed <= MAX_REQUESTS_PER_DAY

    def _record_request(self):
        self._reset_if_new_day()
        self._request_count += 1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch(self) -> dict:
        """Fetch solar forecast from Solcast across all configured sites.

        Returns dict with keys: today, tomorrow, today_kwh, tomorrow_kwh,
        today_p10, today_p90, tomorrow_p10, tomorrow_p90.
        Returns empty dict if credentials are missing, budget is exhausted,
        or on any failure.
        """
        if not self.api_key or not self.site_ids:
            return {}

        calls_needed = len(self.site_ids)
        if not self._budget_available(calls_needed):
            log.warning(
                "Solcast daily budget insufficient for %d site(s) "
                "(%d/%d requests used today)",
                calls_needed,
                self._request_count,
                MAX_REQUESTS_PER_DAY,
            )
            return {}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

        all_forecasts: list[dict] = []

        for site_id in self.site_ids:
            url = (
                "https://api.solcast.com.au/data/forecast/advanced_pv_power"
                f"?resource_id={site_id}&format=json&period=PT60M"
            )
            log.info("Solcast fetching site %s via advanced PV power endpoint", site_id)

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
                log.exception("Solcast API request failed for site %s", site_id)
                return {}
            except ValueError:
                log.exception("Solcast returned invalid JSON for site %s", site_id)
                return {}

            forecasts = data.get("forecasts", [])
            all_forecasts.append(forecasts)

        try:
            return self._parse_multi(all_forecasts)
        except (KeyError, TypeError, IndexError):
            log.exception("Failed to parse Solcast response")
            return {}

    def reconfigure(self, config: dict) -> None:
        """Update configuration from options flow."""
        if config.get("solcast_api_key"):
            self.api_key = config["solcast_api_key"]
        if "solcast_site_ids" in config:
            self.site_ids = config["solcast_site_ids"]
        if "panel_arrays" in config:
            self.total_kwp = sum(a["kwp"] for a in config["panel_arrays"])

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_multi(self, all_forecasts: list[list[dict]]) -> dict:
        """Parse and sum forecasts from multiple sites."""
        today_date = date.today()
        tomorrow_date = today_date + timedelta(days=1)

        Bucket = dict[int, list[float]]
        today_p50: Bucket = defaultdict(list)
        today_p10: Bucket = defaultdict(list)
        today_p90: Bucket = defaultdict(list)
        tomorrow_p50: Bucket = defaultdict(list)
        tomorrow_p10: Bucket = defaultdict(list)
        tomorrow_p90: Bucket = defaultdict(list)

        for site_forecasts in all_forecasts:
            # Per-site hourly accumulators
            site_today_p50: dict[int, float] = defaultdict(float)
            site_today_p10: dict[int, float] = defaultdict(float)
            site_today_p90: dict[int, float] = defaultdict(float)
            site_tomorrow_p50: dict[int, float] = defaultdict(float)
            site_tomorrow_p10: dict[int, float] = defaultdict(float)
            site_tomorrow_p90: dict[int, float] = defaultdict(float)

            for entry in site_forecasts:
                period_end = datetime.fromisoformat(
                    entry["period_end"].replace("Z", "+00:00")
                )
                local_dt = period_end.astimezone()
                d = local_dt.date()
                hour = local_dt.hour

                # Advanced endpoint fields are in kW — convert to W
                pv50 = entry.get("pv_power_advanced", 0) * 1000.0
                pv10 = entry.get("pv_power_advanced10", 0) * 1000.0
                pv90 = entry.get("pv_power_advanced90", 0) * 1000.0

                if d == today_date:
                    site_today_p50[hour] += pv50
                    site_today_p10[hour] += pv10
                    site_today_p90[hour] += pv90
                elif d == tomorrow_date:
                    site_tomorrow_p50[hour] += pv50
                    site_tomorrow_p10[hour] += pv10
                    site_tomorrow_p90[hour] += pv90

            # Add this site's hourly values into the bucket lists for averaging
            for h, v in site_today_p50.items():
                today_p50[h].append(v)
            for h, v in site_today_p10.items():
                today_p10[h].append(v)
            for h, v in site_today_p90.items():
                today_p90[h].append(v)
            for h, v in site_tomorrow_p50.items():
                tomorrow_p50[h].append(v)
            for h, v in site_tomorrow_p10.items():
                tomorrow_p10[h].append(v)
            for h, v in site_tomorrow_p90.items():
                tomorrow_p90[h].append(v)

        def _sum_bucket(bucket: Bucket) -> dict[int, float]:
            """Sum across sites for each hour."""
            return {
                h: round(sum(vals), 1)
                for h, vals in sorted(bucket.items())
            }

        today = _sum_bucket(today_p50)
        tomorrow = _sum_bucket(tomorrow_p50)

        today_kwh = sum(today.values()) / 1000.0
        tomorrow_kwh = sum(tomorrow.values()) / 1000.0

        log.info(
            "Solcast forecast (%d site(s)): today=%.2f kWh, tomorrow=%.2f kWh",
            len(all_forecasts),
            today_kwh,
            tomorrow_kwh,
        )

        return {
            "today": today,
            "tomorrow": tomorrow,
            "today_kwh": round(today_kwh, 2),
            "tomorrow_kwh": round(tomorrow_kwh, 2),
            "today_p10": _sum_bucket(today_p10),
            "today_p90": _sum_bucket(today_p90),
            "tomorrow_p10": _sum_bucket(tomorrow_p10),
            "tomorrow_p90": _sum_bucket(tomorrow_p90),
        }
