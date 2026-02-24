"""Config flow for BeemAI integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import (
    CONF_API_BASE,
    CONF_BATTERY_ID,
    CONF_BATTERY_SERIAL,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEFAULT_API_BASE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class BeemAIConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BeemAI."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        """Get the options flow handler."""
        from .options_flow import BeemAIOptionsFlow

        return BeemAIOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — Beem Energy credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            try:
                access_token, user_id = await self._async_login(email, password)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_id)
                self._abort_if_unique_id_configured()

                try:
                    battery_id, battery_serial = await self._async_get_battery(
                        access_token, user_id
                    )
                except NoDevicesFound:
                    errors["base"] = "no_devices_found"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=f"Beem ({email})",
                        data={
                            CONF_EMAIL: email,
                            CONF_PASSWORD: password,
                            CONF_BATTERY_ID: battery_id,
                            CONF_BATTERY_SERIAL: battery_serial,
                            CONF_USER_ID: user_id,
                            CONF_API_BASE: DEFAULT_API_BASE,
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=USER_SCHEMA,
            errors=errors,
        )

    async def _async_login(
        self, email: str, password: str
    ) -> tuple[str, str]:
        """Log in to Beem Energy API and return (access_token, user_id)."""
        url = f"{DEFAULT_API_BASE}/user/login"
        payload = {"email": email, "password": password}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 401:
                        raise InvalidAuth
                    resp.raise_for_status()
                    data = await resp.json()
        except InvalidAuth:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("Cannot connect to Beem API: %s", err)
            raise CannotConnect from err

        access_token = data.get("accessToken")
        user_id = data.get("userId")
        if not access_token or not user_id:
            raise InvalidAuth

        return access_token, str(user_id)

    async def _async_get_battery(
        self, access_token: str, user_id: str
    ) -> tuple[str, str]:
        """Fetch the first battery and return (battery_id, serial_number)."""
        url = f"{DEFAULT_API_BASE}/users/{user_id}/batteries"
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("Cannot fetch batteries: %s", err)
            raise CannotConnect from err

        if not data:
            raise NoDevicesFound

        battery = data[0]
        battery_id = battery.get("id")
        battery_serial = battery.get("serialNumber")

        if not battery_id:
            raise NoDevicesFound

        return str(battery_id), str(battery_serial or "")


class InvalidAuth(Exception):
    """Error to indicate invalid authentication."""


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


class NoDevicesFound(Exception):
    """Error to indicate no batteries were found."""
