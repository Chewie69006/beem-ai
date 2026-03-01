"""Async REST API client for Beem Energy — authentication and data retrieval."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

from .state_store import StateStore

log = logging.getLogger(__name__)

# Auth token refresh interval (50 minutes, JWT TTL is 58 minutes).
TOKEN_REFRESH_SECONDS = 50 * 60

# Self-imposed rate limit: 10 API calls per hour.
RATE_LIMIT_MAX_CALLS = 10
RATE_LIMIT_WINDOW_SECONDS = 3600

# Cooldown after receiving HTTP 429 (20 minutes).
RATE_LIMIT_COOLDOWN_SECONDS = 20 * 60

# Request timeout for all HTTP calls (seconds).
REQUEST_TIMEOUT = 15


class BeemApiClient:
    """Handles authentication and data retrieval via the Beem Energy REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_base: str,
        username: str,
        password: str,
        battery_id: str,
        state_store: StateStore,
    ):
        self._session = session
        self._api_base = api_base.rstrip("/")
        self._username = username
        self._password = password
        self._battery_id = battery_id
        self._state_store = state_store

        # Auth state.
        self._access_token: Optional[str] = None
        self._user_id: Optional[str] = None
        self._token_expiry: Optional[datetime] = None
        self._refresh_task: Optional[asyncio.Task] = None

        # Rate limiting.
        self._call_timestamps: list[datetime] = []
        self._cooldown_until: Optional[datetime] = None

        # Deduplication for control parameter writes.
        self._last_sent_control: Optional[dict] = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def login(self) -> bool:
        """Authenticate with the Beem API and obtain an access token.

        Returns True on success, False on failure.
        """
        url = f"{self._api_base}/user/login"
        payload = {"email": self._username, "password": self._password}

        log.info("REST: logging in to %s", url)
        try:
            resp = await self._session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            )
            resp.raise_for_status()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.exception("REST: login request failed")
            self._state_store.rest_available = False
            return False

        data = await resp.json()
        self._access_token = data.get("accessToken")
        self._user_id = data.get("userId")

        if not self._access_token or not self._user_id:
            log.error("REST: login response missing accessToken or userId")
            self._state_store.rest_available = False
            return False

        self._token_expiry = datetime.now() + timedelta(seconds=TOKEN_REFRESH_SECONDS)
        self._state_store.rest_available = True
        await self._schedule_token_refresh()

        log.info("REST: login successful (userId=%s)", self._user_id)
        return True

    async def refresh_token(self) -> bool:
        """Proactively refresh the auth token by re-authenticating."""
        log.info("REST: refreshing auth token")
        return await self.login()

    @property
    def user_id(self) -> Optional[str]:
        return self._user_id

    @property
    def access_token(self) -> Optional[str]:
        return self._access_token

    # ------------------------------------------------------------------
    # MQTT token
    # ------------------------------------------------------------------

    async def get_mqtt_token(self, client_id: str) -> Optional[str]:
        """Obtain a JWT token for MQTT broker authentication.

        ``client_id`` must match the identifier used when connecting to the
        MQTT broker.  The iOS app uses: ``beemapp-{userId}-{timestamp_ms}``.

        Returns the JWT token string or None on failure.
        """
        url = f"{self._api_base}/devices/mqtt/token"
        log.info("REST: requesting MQTT token (clientId=%s)", client_id)

        payload = {"clientId": client_id, "clientType": "user"}

        try:
            resp = await self._request("POST", url, json=payload)
        except _RateLimited:
            log.warning("REST: rate-limited — cannot fetch MQTT token")
            return None

        if resp is None:
            return None

        data = await resp.json()
        token = data.get("jwt")
        if not token:
            log.error("REST: MQTT token response missing 'jwt' field: %s", data)
            return None

        log.info("REST: MQTT token obtained")
        return token

    # ------------------------------------------------------------------
    # Device discovery
    # ------------------------------------------------------------------

    async def get_solar_equipments(self) -> list[dict]:
        """Fetch solar equipment config from GET /devices.

        Returns the ``solarEquipments`` list for the configured battery,
        or an empty list on any failure.
        """
        url = f"{self._api_base}/devices"
        log.info("REST: fetching solar equipments from %s", url)

        try:
            resp = await self._request("GET", url)
        except _RateLimited:
            log.warning("REST: rate-limited — cannot fetch solar equipments")
            return []

        if resp is None:
            return []

        try:
            data = await resp.json()
        except Exception:
            log.exception("REST: failed to parse /devices response")
            return []

        batteries = data.get("batteries") if isinstance(data, dict) else data
        if not batteries:
            log.warning("REST: no batteries in /devices response")
            return []

        for battery in batteries:
            if str(battery.get("id")) == str(self._battery_id):
                equipments = battery.get("solarEquipments", [])
                log.info(
                    "REST: found %d solar equipment(s) for battery %s",
                    len(equipments),
                    self._battery_id,
                )
                return equipments

        log.warning(
            "REST: battery %s not found in /devices response", self._battery_id
        )
        return []

    # ------------------------------------------------------------------
    # Battery state (pre-optimization refresh)
    # ------------------------------------------------------------------

    async def get_battery_state(self) -> Optional[dict]:
        """Fetch current battery state from the REST API.

        Tries GET /batteries/{id} first, falls back to GET /devices.
        Returns a dict with state fields (soc, solarPower, batteryPower,
        meterPower, etc.) or None if unavailable.
        """
        # Try dedicated battery endpoint first
        url = f"{self._api_base}/batteries/{self._battery_id}"
        log.info("REST: fetching battery state from %s", url)

        try:
            resp = await self._request("GET", url)
        except _RateLimited:
            log.warning("REST: rate-limited — cannot fetch battery state")
            return None

        if resp is not None:
            try:
                data = await resp.json()
                if isinstance(data, dict) and "soc" in data:
                    log.info("REST: got battery state (SoC=%.1f%%)", data["soc"])
                    return data
            except Exception:
                log.debug("REST: /batteries/{id} did not return parseable state")

        # Fallback: extract from /devices
        log.debug("REST: falling back to /devices for battery state")
        devices_url = f"{self._api_base}/devices"
        try:
            resp = await self._request("GET", devices_url)
        except _RateLimited:
            return None

        if resp is None:
            return None

        try:
            data = await resp.json()
            batteries = data.get("batteries") if isinstance(data, dict) else data
            if batteries:
                for battery in batteries:
                    if str(battery.get("id")) == str(self._battery_id):
                        if "soc" in battery:
                            log.info(
                                "REST: got battery state from /devices (SoC=%.1f%%)",
                                battery["soc"],
                            )
                        return battery
        except Exception:
            log.exception("REST: failed to parse /devices for battery state")

        return None

    # ------------------------------------------------------------------
    # Consumption history (bootstrap)
    # ------------------------------------------------------------------

    async def get_consumption_history(
        self, days: int = 30
    ) -> list[tuple[datetime, float]]:
        """Fetch intraday consumption history from the Beem API.

        True house consumption from energy balance:
          consumption = production + grid_import
                      - grid_export - battery_charged + battery_discharged

        Production alone overcounts (includes solar→battery). Subtracting
        battery_charged and adding battery_discharged isolates what the
        house actually used.

        Returns a list of (timestamp, watts) pairs representing real
        household consumption (Wh per hour ≈ average watts).
        """
        from datetime import timezone

        local_tz = datetime.now(timezone.utc).astimezone().tzinfo
        end = (
            datetime.now(local_tz)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )
        start = end - timedelta(days=days)

        endpoints = {
            "production": f"{self._api_base}/production/energy/intraday",
            "grid_import": (
                f"{self._api_base}/consumption/houses/"
                f"active-energy/intraday"
            ),
            "grid_export": (
                f"{self._api_base}/consumption/houses/"
                f"active-returned-energy/intraday"
            ),
            "battery_charged": (
                f"{self._api_base}/batteries/{self._battery_id}/"
                f"energy-charged/intraday"
            ),
            "battery_discharged": (
                f"{self._api_base}/batteries/{self._battery_id}/"
                f"energy-discharged/intraday"
            ),
        }

        # Fetch all five streams: {stream: {iso_str: wh_value}}
        streams: dict[str, dict[str, float]] = {}
        for stream_name, base_url in endpoints.items():
            stream_data = await self._fetch_intraday_stream(
                stream_name, base_url, start, end, days,
            )
            streams[stream_name] = stream_data

        # Combine: consumption = prod + import - export - charged + discharged
        production = streams.get("production", {})
        grid_import = streams.get("grid_import", {})
        grid_export = streams.get("grid_export", {})
        bat_charged = streams.get("battery_charged", {})
        bat_discharged = streams.get("battery_discharged", {})

        all_timestamps = sorted(
            set(production) | set(grid_import) | set(grid_export)
            | set(bat_charged) | set(bat_discharged)
        )

        results: list[tuple[datetime, float]] = []
        for ts_str in all_timestamps:
            prod = production.get(ts_str, 0.0)
            imp = grid_import.get(ts_str, 0.0)
            exp = grid_export.get(ts_str, 0.0)
            charged = bat_charged.get(ts_str, 0.0)
            discharged = bat_discharged.get(ts_str, 0.0)
            consumption = prod + imp - exp - charged + discharged
            # Clamp to zero — rounding errors can produce small negatives
            consumption = max(0.0, consumption)
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                results.append((ts, consumption))
            except (ValueError, TypeError):
                continue

        log.info(
            "REST: computed %d consumption points from %d prod + %d import "
            "- %d export - %d charged + %d discharged over %d days",
            len(results),
            len(production),
            len(grid_import),
            len(grid_export),
            len(bat_charged),
            len(bat_discharged),
            days,
        )
        return results

    async def _fetch_intraday_stream(
        self,
        name: str,
        base_url: str,
        start: datetime,
        end: datetime,
        days: int,
    ) -> dict[str, float]:
        """Fetch one intraday stream in 7-day chunks.

        Returns {iso_timestamp_str: wh_value} keyed by startDate.
        """
        data_map: dict[str, float] = {}
        chunk_days = 7
        chunks = (days + chunk_days - 1) // chunk_days

        for i in range(chunks):
            chunk_start = start + timedelta(days=i * chunk_days)
            chunk_end = min(start + timedelta(days=(i + 1) * chunk_days), end)

            params = {
                "from": chunk_start.isoformat(),
                "to": chunk_end.isoformat(),
                "scale": "PT60M",
            }

            log.info(
                "REST: fetching %s chunk %d/%d (%s to %s)",
                name, i + 1, chunks,
                params["from"][:10], params["to"][:10],
            )

            try:
                headers = self._auth_headers()
                headers["Accept"] = "application/json"
                kwargs = {
                    "timeout": aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    "headers": headers,
                    "params": params,
                }
                resp = await self._session.request("GET", base_url, **kwargs)

                if resp.status == 429:
                    log.warning("REST: 429 during %s fetch, stopping", name)
                    break

                if not resp.ok:
                    body = await resp.text()
                    log.warning(
                        "REST: %s chunk %d returned %d: %s",
                        name, i + 1, resp.status, body[:200],
                    )
                    continue

                resp_data = await resp.json()
                # Response key varies: "houses", "devices", or "batteries"
                containers = (
                    resp_data.get("houses")
                    or resp_data.get("devices")
                    or resp_data.get("batteries")
                    or [resp_data]
                )
                if not containers:
                    continue

                # Sum across all containers (production has multiple
                # devices, one per MPPT — we need total production)
                for container in containers:
                    for m in container.get("measures", []):
                        ts_str = m.get("startDate")
                        value = m.get("value")
                        if ts_str is not None and value is not None:
                            data_map[ts_str] = (
                                data_map.get(ts_str, 0.0) + float(value)
                            )

            except (aiohttp.ClientError, asyncio.TimeoutError):
                log.exception("REST: %s chunk %d failed", name, i + 1)
                continue

            # 1s delay between chunks
            if i < chunks - 1:
                await asyncio.sleep(1)

        log.info("REST: fetched %d %s data points", len(data_map), name)
        return data_map

    # ------------------------------------------------------------------
    # Battery control parameters
    # ------------------------------------------------------------------

    async def set_control_parameters(
        self,
        *,
        mode: str,
        allow_charge_from_grid: bool,
        prevent_discharge: bool,
        charge_power: int,
        min_soc: int,
        max_soc: int,
    ) -> bool:
        """Send battery control parameters via PATCH /batteries/{id}/control-parameters.

        Returns True on success, False on failure or rate-limit.
        Deduplicates: skips the call if params are unchanged since last send.
        """
        body = {
            "mode": mode,
            "allowChargeFromGrid": allow_charge_from_grid,
            "preventDischarge": prevent_discharge,
            "chargeFromGridMaxPower": charge_power,
            "minSoc": min_soc,
            "maxSoc": max_soc,
        }

        # Deduplicate — skip if nothing changed.
        if body == self._last_sent_control:
            log.debug("REST: control params unchanged, skipping PATCH")
            return True

        url = f"{self._api_base}/batteries/{self._battery_id}/control-parameters"
        log.info("REST: PATCH %s — %s", url, body)

        try:
            resp = await self._request("PATCH", url, json=body)
        except _RateLimited:
            log.warning("REST: rate-limited — cannot set control parameters")
            return False

        if resp is None:
            return False

        self._last_sent_control = body
        log.info("REST: control parameters updated successfully")
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict:
        """Build Authorization headers using the current access token."""
        if not self._access_token:
            return {}
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _request(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> Optional[aiohttp.ClientResponse]:
        """Execute an authenticated HTTP request with rate-limit enforcement.

        Returns the ClientResponse on success, None on failure.
        Raises _RateLimited if the call would violate the rate limit.
        """
        if not self._check_rate_limit():
            raise _RateLimited()

        kwargs.setdefault("timeout", aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._auth_headers())

        try:
            resp = await self._session.request(method, url, **kwargs)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.exception("REST: %s %s network error", method, url)
            self._state_store.rest_available = False
            return None

        self._record_call()

        if resp.status == 429:
            log.warning(
                "REST: 429 Too Many Requests — entering %d min cooldown",
                RATE_LIMIT_COOLDOWN_SECONDS // 60,
            )
            self._cooldown_until = datetime.now() + timedelta(
                seconds=RATE_LIMIT_COOLDOWN_SECONDS
            )
            return None

        if not resp.ok:
            body = await resp.text()
            log.error(
                "REST: %s %s returned %d: %s",
                method,
                url,
                resp.status,
                body[:200],
            )
            return None

        self._state_store.rest_available = True
        return resp

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _check_rate_limit(self) -> bool:
        """Return True if a new call is allowed."""
        now = datetime.now()

        # Honour 429 cooldown.
        if self._cooldown_until and now < self._cooldown_until:
            remaining = (self._cooldown_until - now).total_seconds()
            log.debug("REST: still in cooldown (%.0fs remaining)", remaining)
            return False

        # Clear expired cooldown.
        self._cooldown_until = None

        # Prune timestamps outside the sliding window.
        cutoff = now - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
        self._call_timestamps = [ts for ts in self._call_timestamps if ts > cutoff]

        if len(self._call_timestamps) >= RATE_LIMIT_MAX_CALLS:
            log.warning(
                "REST: self-imposed rate limit reached (%d/%d per hour)",
                len(self._call_timestamps),
                RATE_LIMIT_MAX_CALLS,
            )
            return False

        return True

    def _record_call(self):
        """Record a successful API call timestamp for rate limiting."""
        self._call_timestamps.append(datetime.now())

    # ------------------------------------------------------------------
    # Token refresh scheduling
    # ------------------------------------------------------------------

    async def _schedule_token_refresh(self):
        """Start (or restart) the background task for proactive token refresh."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(self._token_refresh_loop())

    async def _token_refresh_loop(self):
        """Background coroutine that refreshes the token after the configured interval."""
        try:
            await asyncio.sleep(TOKEN_REFRESH_SECONDS)
            await self.refresh_token()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("REST: token refresh failed, retrying in 60s")
            await asyncio.sleep(60)
            await self.refresh_token()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def shutdown(self):
        """Release resources. Call when the integration unloads.

        Note: the aiohttp session is NOT closed here — it is injected
        and owned by the coordinator.
        """
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        log.info("REST: client shut down")


class _RateLimited(Exception):
    """Raised internally when an API call is blocked by rate limiting."""
