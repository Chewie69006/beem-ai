"""Tests for BeemAI config flow."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from custom_components.beem_ai.config_flow import (
    BeemAIConfigFlow,
    InvalidAuth,
    CannotConnect,
    NoDevicesFound,
)
from custom_components.beem_ai.const import (
    CONF_API_BASE,
    CONF_BATTERY_ID,
    CONF_BATTERY_SERIAL,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEFAULT_API_BASE,
    DOMAIN,
)


@pytest.fixture
def mock_flow():
    """Create a config flow instance with mocked hass."""
    flow = BeemAIConfigFlow()
    flow.hass = MagicMock()
    # Mock async_set_unique_id and _abort_if_unique_id_configured
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow.async_show_form = MagicMock()
    flow.async_create_entry = MagicMock()
    return flow


VALID_USER_INPUT = {
    CONF_EMAIL: "user@example.com",
    CONF_PASSWORD: "s3cret",
}


# ------------------------------------------------------------------
# async_step_user
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_user_shows_form(mock_flow):
    """No input shows the user form."""
    await mock_flow.async_step_user(user_input=None)

    mock_flow.async_show_form.assert_called_once()
    call_kwargs = mock_flow.async_show_form.call_args
    assert call_kwargs.kwargs["step_id"] == "user"
    assert call_kwargs.kwargs["errors"] == {}


@pytest.mark.asyncio
async def test_step_user_valid_credentials(mock_flow):
    """Valid email+password proceeds to solcast step."""
    mock_flow._async_login = AsyncMock(return_value=("tok-abc", "uid-42"))
    mock_flow._async_get_battery = AsyncMock(return_value=("bat-1", "SN-001"))

    # Step 1: user credentials → proceeds to solcast step
    await mock_flow.async_step_user(user_input=VALID_USER_INPUT)

    mock_flow._async_login.assert_awaited_once_with("user@example.com", "s3cret")
    mock_flow.async_set_unique_id.assert_awaited_once_with("uid-42")
    mock_flow._abort_if_unique_id_configured.assert_called_once()
    mock_flow._async_get_battery.assert_awaited_once_with("tok-abc", "uid-42")

    # Should show solcast form (not create entry directly)
    mock_flow.async_show_form.assert_called_once()
    assert mock_flow.async_show_form.call_args.kwargs["step_id"] == "solcast"

    # Verify login data stored
    assert mock_flow._login_data[CONF_EMAIL] == "user@example.com"
    assert mock_flow._login_data[CONF_BATTERY_ID] == "bat-1"


@pytest.mark.asyncio
async def test_step_solcast_creates_entry(mock_flow):
    """Solcast step creates the entry with login data and options."""
    mock_flow._login_data = {
        CONF_EMAIL: "user@example.com",
        CONF_PASSWORD: "s3cret",
        CONF_BATTERY_ID: "bat-1",
        CONF_BATTERY_SERIAL: "SN-001",
        CONF_USER_ID: "uid-42",
        CONF_API_BASE: DEFAULT_API_BASE,
    }
    mock_flow.hass.config.latitude = 48.85
    mock_flow.hass.config.longitude = 2.35

    await mock_flow.async_step_solcast(user_input={
        "solcast_api_key": "key-123",
        "solcast_site_id": "site-456",
    })

    mock_flow.async_create_entry.assert_called_once()
    call_kwargs = mock_flow.async_create_entry.call_args.kwargs
    assert call_kwargs["data"][CONF_EMAIL] == "user@example.com"
    assert call_kwargs["data"][CONF_BATTERY_ID] == "bat-1"
    assert call_kwargs["options"]["solcast_api_key"] == "key-123"
    assert call_kwargs["options"]["location_lat"] == 48.85


@pytest.mark.asyncio
async def test_step_user_invalid_auth(mock_flow):
    """401 response surfaces invalid_auth error."""
    mock_flow._async_login = AsyncMock(side_effect=InvalidAuth)

    await mock_flow.async_step_user(user_input=VALID_USER_INPUT)

    mock_flow.async_show_form.assert_called_once()
    errors = mock_flow.async_show_form.call_args.kwargs["errors"]
    assert errors == {"base": "invalid_auth"}


@pytest.mark.asyncio
async def test_step_user_cannot_connect(mock_flow):
    """Network error surfaces cannot_connect error."""
    mock_flow._async_login = AsyncMock(side_effect=CannotConnect)

    await mock_flow.async_step_user(user_input=VALID_USER_INPUT)

    mock_flow.async_show_form.assert_called_once()
    errors = mock_flow.async_show_form.call_args.kwargs["errors"]
    assert errors == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_step_user_no_devices(mock_flow):
    """Empty battery list surfaces no_devices_found error."""
    mock_flow._async_login = AsyncMock(return_value=("tok-abc", "uid-42"))
    mock_flow._async_get_battery = AsyncMock(side_effect=NoDevicesFound)

    await mock_flow.async_step_user(user_input=VALID_USER_INPUT)

    mock_flow.async_show_form.assert_called_once()
    errors = mock_flow.async_show_form.call_args.kwargs["errors"]
    assert errors == {"base": "no_devices_found"}


@pytest.mark.asyncio
async def test_step_user_cannot_connect_on_battery_fetch(mock_flow):
    """Network error during battery fetch surfaces cannot_connect error."""
    mock_flow._async_login = AsyncMock(return_value=("tok-abc", "uid-42"))
    mock_flow._async_get_battery = AsyncMock(side_effect=CannotConnect)

    await mock_flow.async_step_user(user_input=VALID_USER_INPUT)

    mock_flow.async_show_form.assert_called_once()
    errors = mock_flow.async_show_form.call_args.kwargs["errors"]
    assert errors == {"base": "cannot_connect"}


@pytest.mark.asyncio
async def test_step_user_already_configured(mock_flow):
    """Duplicate unique_id aborts the flow."""
    from homeassistant.data_entry_flow import AbortFlow

    mock_flow._async_login = AsyncMock(return_value=("tok-abc", "uid-42"))
    mock_flow._abort_if_unique_id_configured = MagicMock(side_effect=AbortFlow("already_configured"))

    with pytest.raises(AbortFlow, match="already_configured"):
        await mock_flow.async_step_user(user_input=VALID_USER_INPUT)
