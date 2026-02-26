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
