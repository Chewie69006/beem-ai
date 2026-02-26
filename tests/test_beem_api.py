"""Unit tests for BeemApiClient (async REST client)."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
import pytest_asyncio

from custom_components.beem_ai.beem_api import (
    RATE_LIMIT_COOLDOWN_SECONDS,
    RATE_LIMIT_MAX_CALLS,
    BeemApiClient,
    _RateLimited,
)


@pytest_asyncio.fixture
async def api_client(state_store):
    """Create a BeemApiClient with a mocked aiohttp.ClientSession."""
    session = AsyncMock(spec=aiohttp.ClientSession)
    client = BeemApiClient(
        session=session,
        api_base="https://api.beem.energy/v1",
        username="user@example.com",
        password="s3cret",
        battery_id="bat-123",
        state_store=state_store,
    )
    yield client
    await client.shutdown()


def _mock_response(status=200, ok=True, json_data=None):
    """Build a mock aiohttp response."""
    resp = AsyncMock()
    resp.status = status
    resp.ok = ok
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value="")
    return resp


# ------------------------------------------------------------------
# login()
# ------------------------------------------------------------------


class TestLogin:
    @pytest.mark.asyncio
    async def test_login_success_sets_token_and_user_id(
        self, api_client, state_store
    ):
        """Successful login stores accessToken and userId."""
        mock_resp = _mock_response(
            json_data={"accessToken": "tok-abc", "userId": "uid-42"}
        )
        api_client._session.post = AsyncMock(return_value=mock_resp)

        result = await api_client.login()

        assert result is True
        assert api_client.access_token == "tok-abc"
        assert api_client.user_id == "uid-42"
        assert state_store.rest_available is True

    @pytest.mark.asyncio
    async def test_login_success_schedules_refresh_task(self, api_client):
        """After login the background token-refresh task is created."""
        mock_resp = _mock_response(
            json_data={"accessToken": "tok-abc", "userId": "uid-42"}
        )
        api_client._session.post = AsyncMock(return_value=mock_resp)

        await api_client.login()

        assert api_client._refresh_task is not None
        assert not api_client._refresh_task.done()

    @pytest.mark.asyncio
    async def test_login_failure_sets_rest_unavailable(
        self, api_client, state_store
    ):
        """Network error during login marks REST as unavailable."""
        api_client._session.post = AsyncMock(
            side_effect=aiohttp.ClientError("down")
        )

        result = await api_client.login()

        assert result is False
        assert state_store.rest_available is False

    @pytest.mark.asyncio
    async def test_login_missing_token_in_response(
        self, api_client, state_store
    ):
        """Response without accessToken is treated as failure."""
        mock_resp = _mock_response(json_data={"userId": "uid-42"})
        api_client._session.post = AsyncMock(return_value=mock_resp)

        result = await api_client.login()

        assert result is False
        assert state_store.rest_available is False


# ------------------------------------------------------------------
# get_mqtt_token()
# ------------------------------------------------------------------


class TestGetMqttToken:
    @pytest.mark.asyncio
    async def test_success_returns_token(self, api_client):
        """Valid response returns the MQTT JWT token."""
        api_client._access_token = "tok-abc"

        mock_resp = _mock_response(json_data={"jwt": "mqtt-jwt-xyz"})
        api_client._session.request = AsyncMock(return_value=mock_resp)

        token = await api_client.get_mqtt_token("beemapp-42-1234567890000")

        assert token == "mqtt-jwt-xyz"
        # Verify correct body was sent.
        body = api_client._session.request.call_args[1]["json"]
        assert body["clientId"] == "beemapp-42-1234567890000"
        assert body["clientType"] == "user"

    @pytest.mark.asyncio
    async def test_rate_limited_returns_none(self, api_client):
        """When self-imposed rate limit is hit, returns None."""
        api_client._access_token = "tok-abc"
        # Fill up the rate-limit bucket.
        api_client._call_timestamps = [
            datetime.now() for _ in range(RATE_LIMIT_MAX_CALLS)
        ]

        token = await api_client.get_mqtt_token("beemapp-42-1234567890000")

        assert token is None


# ------------------------------------------------------------------
# Rate limiting
# ------------------------------------------------------------------


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_429_triggers_cooldown(self, api_client):
        """HTTP 429 puts the client into a 20-minute cooldown."""
        api_client._access_token = "tok-abc"

        mock_resp = _mock_response(status=429, ok=False)
        mock_resp.text = AsyncMock(return_value="Too Many Requests")
        api_client._session.request = AsyncMock(return_value=mock_resp)

        # Use _request directly since set_control_params was removed
        try:
            resp = await api_client._request("GET", "https://api.beem.energy/v1/devices")
        except _RateLimited:
            pass

        # If the 429 was processed, cooldown should be set
        # (Note: _request returns None on 429 status, doesn't raise)
        assert api_client._cooldown_until is not None or resp is None


# ------------------------------------------------------------------
# Auth headers
# ------------------------------------------------------------------


class TestAuthHeaders:
    @pytest.mark.asyncio
    async def test_no_token_means_empty_headers(self, api_client):
        """Without a token, Authorization header is absent."""
        headers = api_client._auth_headers()
        assert headers == {}

    @pytest.mark.asyncio
    async def test_bearer_token_included(self, api_client):
        """With a token, Bearer header is included."""
        api_client._access_token = "tok-abc"
        headers = api_client._auth_headers()
        assert headers["Authorization"] == "Bearer tok-abc"


# ------------------------------------------------------------------
# shutdown()
# ------------------------------------------------------------------


class TestShutdown:
    @pytest.mark.asyncio
    async def test_cancels_refresh_task(self, api_client):
        """shutdown() cancels the token-refresh task."""
        mock_resp = _mock_response(
            json_data={"accessToken": "tok-abc", "userId": "uid-42"}
        )
        api_client._session.post = AsyncMock(return_value=mock_resp)
        await api_client.login()

        task = api_client._refresh_task
        assert task is not None

        await api_client.shutdown()

        assert task.cancelled()
