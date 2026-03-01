"""Options flow for BeemAI integration."""

from __future__ import annotations

import json
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import OptionsFlow, ConfigFlowResult

from .const import (
    DEFAULT_TARIFF_DEFAULT_PRICE,
    DEFAULT_TARIFF_PERIOD_COUNT,
    DOMAIN,
    OPT_LOCATION_LAT,
    OPT_LOCATION_LON,
    OPT_SOLCAST_API_KEY,
    OPT_SOLCAST_SITE_IDS_JSON,
    OPT_TARIFF_DEFAULT_PRICE,
    OPT_TARIFF_PERIOD_COUNT,
    OPT_TARIFF_PERIODS_JSON,
)

_LOGGER = logging.getLogger(__name__)


class BeemAIOptionsFlow(OptionsFlow):
    """Handle BeemAI options."""

    def __init__(self, config_entry) -> None:
        """Initialise options flow."""
        self._tariff_period_count: int = DEFAULT_TARIFF_PERIOD_COUNT
        self._options: dict[str, Any] = {}
        self._panel_array_count: int = 0

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First step — general settings."""
        if user_input is not None:
            self._tariff_period_count = user_input.get(
                OPT_TARIFF_PERIOD_COUNT, DEFAULT_TARIFF_PERIOD_COUNT
            )
            self._options = user_input
            return await self.async_step_solcast()

        current = self.config_entry.options

        schema = vol.Schema(
            {
                vol.Optional(
                    OPT_LOCATION_LAT,
                    default=current.get(OPT_LOCATION_LAT, 0.0),
                ): vol.Coerce(float),
                vol.Optional(
                    OPT_LOCATION_LON,
                    default=current.get(OPT_LOCATION_LON, 0.0),
                ): vol.Coerce(float),
                vol.Optional(
                    OPT_SOLCAST_API_KEY,
                    default=current.get(OPT_SOLCAST_API_KEY, ""),
                ): str,
                vol.Required(
                    OPT_TARIFF_DEFAULT_PRICE,
                    default=current.get(OPT_TARIFF_DEFAULT_PRICE, DEFAULT_TARIFF_DEFAULT_PRICE),
                ): vol.Coerce(float),
                vol.Required(
                    OPT_TARIFF_PERIOD_COUNT,
                    default=current.get(OPT_TARIFF_PERIOD_COUNT, DEFAULT_TARIFF_PERIOD_COUNT),
                ): vol.All(int, vol.Range(min=1, max=6)),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_solcast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Second step — per-array Solcast Site IDs."""
        if user_input is not None:
            # Serialize site_id mappings to JSON
            site_ids = []
            for i in range(self._panel_array_count):
                sid = user_input.get(f"solcast_site_{i}_id", "").strip()
                if sid:
                    site_ids.append({"array_index": i, "site_id": sid})
            self._options[OPT_SOLCAST_SITE_IDS_JSON] = json.dumps(site_ids)
            return await self.async_step_tariffs()

        # Discover array count from coordinator
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        panel_arrays = getattr(coordinator, "panel_arrays", []) if coordinator else []
        self._panel_array_count = max(len(panel_arrays), 1)

        # Load existing site_id mappings
        existing_map: dict[int, str] = {}
        raw = self.config_entry.options.get(OPT_SOLCAST_SITE_IDS_JSON, "")
        if raw:
            try:
                for entry in json.loads(raw):
                    existing_map[entry["array_index"]] = entry["site_id"]
            except (json.JSONDecodeError, TypeError, KeyError):
                pass

        fields: dict[vol.Marker, Any] = {}
        for i in range(self._panel_array_count):
            fields[
                vol.Optional(
                    f"solcast_site_{i}_id",
                    default=existing_map.get(i, ""),
                )
            ] = str

        return self.async_show_form(
            step_id="solcast",
            data_schema=vol.Schema(fields),
        )

    async def async_step_tariffs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Third step — configurable tariff periods."""
        if user_input is not None:
            periods = []
            for i in range(1, self._tariff_period_count + 1):
                periods.append(
                    {
                        "label": user_input.get(f"tariff_{i}_label", f"Period {i}"),
                        "start": user_input.get(f"tariff_{i}_start", "00:00"),
                        "end": user_input.get(f"tariff_{i}_end", "00:00"),
                        "price": user_input.get(f"tariff_{i}_price", 0.20),
                    }
                )
            self._options[OPT_TARIFF_PERIODS_JSON] = json.dumps(periods)
            return self.async_create_entry(title="", data=self._options)

        # Load existing tariff period data for defaults
        existing_periods: list[dict] = []
        raw = self.config_entry.options.get(OPT_TARIFF_PERIODS_JSON)
        if raw:
            try:
                existing_periods = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                existing_periods = []

        fields: dict[vol.Marker, Any] = {}
        for i in range(1, self._tariff_period_count + 1):
            existing = existing_periods[i - 1] if i - 1 < len(existing_periods) else {}
            fields[
                vol.Required(
                    f"tariff_{i}_label",
                    default=existing.get("label", f"Period {i}"),
                )
            ] = str
            fields[
                vol.Required(
                    f"tariff_{i}_start",
                    default=existing.get("start", "00:00"),
                )
            ] = str
            fields[
                vol.Required(
                    f"tariff_{i}_end",
                    default=existing.get("end", "00:00"),
                )
            ] = str
            fields[
                vol.Required(
                    f"tariff_{i}_price",
                    default=existing.get("price", 0.20),
                )
            ] = vol.Coerce(float)

        return self.async_show_form(
            step_id="tariffs",
            data_schema=vol.Schema(fields),
        )
