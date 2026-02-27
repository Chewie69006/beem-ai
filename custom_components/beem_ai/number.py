"""Number entity for BeemAI — editable consumption forecast."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .sensor import _system_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BeemAI number entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BeemAIConsumptionForecastNumber(coordinator, entry)])


class BeemAIConsumptionForecastNumber(CoordinatorEntity, NumberEntity):
    """Editable consumption forecast for today (kWh)."""

    _attr_has_entity_name = True
    _attr_name = "Consumption Forecast Today"
    _attr_icon = "mdi:home-lightning-bolt"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "kWh"
    _attr_mode = NumberMode.BOX
    _attr_translation_key = "consumption_forecast_today"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_consumption_forecast_today"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info for grouping entities."""
        return _system_device_info(self._entry)

    @property
    def native_value(self) -> float | None:
        """Return the current consumption forecast."""
        try:
            return round(
                self.coordinator.state_store.forecast.consumption_today_kwh, 1
            )
        except Exception:
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the consumption forecast override."""
        await self.coordinator.async_set_consumption_forecast(value)
        self.async_write_ha_state()
