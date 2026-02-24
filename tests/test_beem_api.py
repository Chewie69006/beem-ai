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
async def api_client(state_store, event_bus):
    """Create a BeemApiClient with a mocked aiohttp.ClientSession."""
    session = AsyncMock(spec=aiohttp.ClientSession)
    client = BeemApiClient(
        session=session,
        api_base="https://api.beem.energy/v1",
        username="user@example.com",
        password="s3cret",
        battery_id="bat-123",
        state_store=state_store,
        event_bus=event_bus,
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

        mock_resp = _mock_response(json_data={"token": "mqtt-jwt-xyz"})
        api_client._session.request = AsyncMock(return_value=mock_resp)

        token = await api_client.get_mqtt_token()

        assert token == "mqtt-jwt-xyz"

    @pytest.mark.asyncio
    async def test_rate_limited_returns_none(self, api_client):
        """When self-imposed rate limit is hit, returns None."""
        api_client._access_token = "tok-abc"
        # Fill up the rate-limit bucket.
        api_client._call_timestamps = [
            datetime.now() for _ in range(RATE_LIMIT_MAX_CALLS)
        ]

        token = await api_client.get_mqtt_token()

        assert token is None


# ------------------------------------------------------------------
# set_control_params()
# ------------------------------------------------------------------


class TestSetControlParams:
    @pytest.mark.asyncio
    async def test_sends_correct_patch_body(self, api_client):
        """PATCH includes all expected control fields."""
        api_client._access_token = "tok-abc"

        mock_resp = _mock_response()
        api_client._session.request = AsyncMock(return_value=mock_resp)

        await api_client.set_control_params(
            mode="advanced",
            allow_grid_charge=True,
            prevent_discharge=False,
            min_soc=30,
            max_soc=90,
            charge_power=1000,
        )

        call_args = api_client._session.request.call_args
        assert call_args[0][0] == "PATCH"
        assert "bat-123/control-parameters" in call_args[0][1]
        body = call_args[1]["json"]
        assert body["mode"] == "advanced"
        assert body["allowChargeFromGrid"] is True
        assert body["preventDischarge"] is False
        assert body["minSoc"] == 30
        assert body["maxSoc"] == 90
        assert body["chargeFromGridMaxPower"] == 1000

    @pytest.mark.asyncio
    async def test_deduplication_skips_unchanged_params(self, api_client):
        """Sending the same params twice only makes one HTTP call."""
        api_client._access_token = "tok-abc"

        mock_resp = _mock_response()
        api_client._session.request = AsyncMock(return_value=mock_resp)

        await api_client.set_control_params(min_soc=20, max_soc=100)
        await api_client.set_control_params(min_soc=20, max_soc=100)

        assert api_client._session.request.call_count == 1


# ------------------------------------------------------------------
# set_auto_mode()
# ------------------------------------------------------------------


class TestSetAutoMode:
    @pytest.mark.asyncio
    async def test_sends_auto_mode(self, api_client):
        """set_auto_mode() sends mode='auto'."""
        api_client._access_token = "tok-abc"

        mock_resp = _mock_response()
        api_client._session.request = AsyncMock(return_value=mock_resp)

        await api_client.set_auto_mode()

        body = api_client._session.request.call_args[1]["json"]
        assert body["mode"] == "auto"
        assert body["allowChargeFromGrid"] is False
        assert body["preventDischarge"] is False


# ------------------------------------------------------------------
# Rate limiting
# ------------------------------------------------------------------


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_ten_calls_allowed(self, api_client):
        """The first 10 calls within the window pass through."""
        api_client._access_token = "tok-abc"

        mock_resp = _mock_response()
        api_client._session.request = AsyncMock(return_value=mock_resp)

        for i in range(RATE_LIMIT_MAX_CALLS):
            result = await api_client.set_control_params(
                charge_power=i  # vary to avoid dedup
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_eleventh_call_blocked(self, api_client):
        """The 11th call is blocked by the self-imposed rate limit."""
        api_client._access_token = "tok-abc"

        mock_resp = _mock_response()
        api_client._session.request = AsyncMock(return_value=mock_resp)

        # Fire 10 calls.
        for i in range(RATE_LIMIT_MAX_CALLS):
            await api_client.set_control_params(charge_power=i)

        # 11th must be blocked.
        result = await api_client.set_control_params(charge_power=9999)
        assert result is False

    @pytest.mark.asyncio
    async def test_429_triggers_cooldown(self, api_client):
        """HTTP 429 puts the client into a 20-minute cooldown."""
        api_client._access_token = "tok-abc"

        mock_resp = _mock_response(status=429, ok=False)
        mock_resp.text = AsyncMock(return_value="Too Many Requests")
        api_client._session.request = AsyncMock(return_value=mock_resp)

        result = await api_client.set_control_params(charge_power=500)

        assert result is False
        assert api_client._cooldown_until is not None
        # Cooldown should be roughly 20 minutes from now.
        delta = (api_client._cooldown_until - datetime.now()).total_seconds()
        assert delta > RATE_LIMIT_COOLDOWN_SECONDS - 5


# ------------------------------------------------------------------
# Auth headers
# ------------------------------------------------------------------


class TestAuthHeaders:
    @pytest.mark.asyncio
    async def test_bearer_token_included(self, api_client):
        """Authenticated requests include the Bearer token header."""
        api_client._access_token = "tok-abc"

        mock_resp = _mock_response()
        api_client._session.request = AsyncMock(return_value=mock_resp)

        await api_client.set_control_params(charge_power=100)

        headers = api_client._session.request.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer tok-abc"

    @pytest.mark.asyncio
    async def test_no_token_means_empty_headers(self, api_client):
        """Without a token, Authorization header is absent."""
        headers = api_client._auth_headers()
        assert headers == {}


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
