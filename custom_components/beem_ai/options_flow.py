"""Options flow for BeemAI integration."""

from __future__ import annotations

import json
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import OptionsFlow, ConfigFlowResult
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import (
    DEFAULT_MIN_SOC_SUMMER,
    DEFAULT_MIN_SOC_WINTER,
    DEFAULT_PANEL_COUNT,
    DEFAULT_TARIFF_HC,
    DEFAULT_TARIFF_HP,
    DEFAULT_TARIFF_HSC,
    DEFAULT_DRY_RUN,
    DEFAULT_WATER_HEATER_POWER_W,
    OPT_DRY_RUN,
    OPT_LOCATION_LAT,
    OPT_LOCATION_LON,
    OPT_MIN_SOC_SUMMER,
    OPT_MIN_SOC_WINTER,
    OPT_PANEL_ARRAYS_JSON,
    OPT_PANEL_COUNT,
    OPT_SOLCAST_API_KEY,
    OPT_SOLCAST_SITE_ID,
    OPT_TARIFF_HC_PRICE,
    OPT_TARIFF_HP_PRICE,
    OPT_TARIFF_HSC_PRICE,
    OPT_WATER_HEATER_POWER_ENTITY,
    OPT_WATER_HEATER_POWER_W,
    OPT_WATER_HEATER_SWITCH,
)

_LOGGER = logging.getLogger(__name__)


class BeemAIOptionsFlow(OptionsFlow):
    """Handle BeemAI options."""

    def __init__(self, config_entry) -> None:
        """Initialise options flow."""
        self._panel_count: int = DEFAULT_PANEL_COUNT
        self._options: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First step — general settings."""
        if user_input is not None:
            self._panel_count = user_input.get(OPT_PANEL_COUNT, DEFAULT_PANEL_COUNT)
            self._options = user_input
            return await self.async_step_panels()

        current = self.config_entry.options

        schema = vol.Schema(
            {
                vol.Required(
                    OPT_LOCATION_LAT,
                    default=current.get(OPT_LOCATION_LAT),
                ): vol.Coerce(float),
                vol.Required(
                    OPT_LOCATION_LON,
                    default=current.get(OPT_LOCATION_LON),
                ): vol.Coerce(float),
                vol.Optional(
                    OPT_SOLCAST_API_KEY,
                    default=current.get(OPT_SOLCAST_API_KEY, ""),
                ): str,
                vol.Optional(
                    OPT_SOLCAST_SITE_ID,
                    default=current.get(OPT_SOLCAST_SITE_ID, ""),
                ): str,
                vol.Required(
                    OPT_TARIFF_HP_PRICE,
                    default=current.get(OPT_TARIFF_HP_PRICE, DEFAULT_TARIFF_HP),
                ): vol.Coerce(float),
                vol.Required(
                    OPT_TARIFF_HC_PRICE,
                    default=current.get(OPT_TARIFF_HC_PRICE, DEFAULT_TARIFF_HC),
                ): vol.Coerce(float),
                vol.Required(
                    OPT_TARIFF_HSC_PRICE,
                    default=current.get(OPT_TARIFF_HSC_PRICE, DEFAULT_TARIFF_HSC),
                ): vol.Coerce(float),
                vol.Required(
                    OPT_MIN_SOC_SUMMER,
                    default=current.get(OPT_MIN_SOC_SUMMER, DEFAULT_MIN_SOC_SUMMER),
                ): vol.All(int, vol.Range(min=0, max=100)),
                vol.Required(
                    OPT_MIN_SOC_WINTER,
                    default=current.get(OPT_MIN_SOC_WINTER, DEFAULT_MIN_SOC_WINTER),
                ): vol.All(int, vol.Range(min=0, max=100)),
                vol.Optional(
                    OPT_WATER_HEATER_SWITCH,
                    default=current.get(OPT_WATER_HEATER_SWITCH, ""),
                ): EntitySelector(EntitySelectorConfig(domain="switch")),
                vol.Optional(
                    OPT_WATER_HEATER_POWER_ENTITY,
                    default=current.get(OPT_WATER_HEATER_POWER_ENTITY, ""),
                ): EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power")),
                vol.Required(
                    OPT_WATER_HEATER_POWER_W,
                    default=current.get(
                        OPT_WATER_HEATER_POWER_W, DEFAULT_WATER_HEATER_POWER_W
                    ),
                ): vol.All(int, vol.Range(min=0)),
                vol.Required(
                    OPT_PANEL_COUNT,
                    default=current.get(OPT_PANEL_COUNT, DEFAULT_PANEL_COUNT),
                ): vol.All(int, vol.Range(min=1, max=6)),
                vol.Optional(
                    OPT_DRY_RUN,
                    default=current.get(OPT_DRY_RUN, DEFAULT_DRY_RUN),
                ): bool,
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_panels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Second step — per-panel configuration."""
        if user_input is not None:
            panels = []
            for i in range(1, self._panel_count + 1):
                panels.append(
                    {
                        "tilt": user_input.get(f"panel_{i}_tilt", 30),
                        "azimuth": user_input.get(f"panel_{i}_azimuth", 180),
                        "kwp": user_input.get(f"panel_{i}_kwp", 5.0),
                    }
                )
            self._options[OPT_PANEL_ARRAYS_JSON] = json.dumps(panels)
            return self.async_create_entry(title="", data=self._options)

        # Load existing panel data for defaults.
        existing_panels: list[dict] = []
        raw = self.config_entry.options.get(OPT_PANEL_ARRAYS_JSON)
        if raw:
            try:
                existing_panels = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                existing_panels = []

        fields: dict[vol.Marker, Any] = {}
        for i in range(1, self._panel_count + 1):
            existing = existing_panels[i - 1] if i - 1 < len(existing_panels) else {}
            fields[
                vol.Required(
                    f"panel_{i}_tilt",
                    default=existing.get("tilt", 30),
                )
            ] = vol.All(int, vol.Range(min=0, max=90))
            fields[
                vol.Required(
                    f"panel_{i}_azimuth",
                    default=existing.get("azimuth", 180),
                )
            ] = vol.All(int, vol.Range(min=0, max=360))
            fields[
                vol.Required(
                    f"panel_{i}_kwp",
                    default=existing.get("kwp", 5.0),
                )
            ] = vol.All(vol.Coerce(float), vol.Range(min=0.1, max=50.0))

        return self.async_show_form(
            step_id="panels",
            data_schema=vol.Schema(fields),
        )
