"""Select entity for BeemAI — battery mode control."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .sensor import _battery_device_info

_LOGGER = logging.getLogger(__name__)

BATTERY_MODES = ["auto", "advanced"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BeemAI select entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BeemAIBatteryModeSelect(coordinator, entry)])


class BeemAIBatteryModeSelect(CoordinatorEntity, SelectEntity):
    """Select entity for battery operating mode."""

    _attr_has_entity_name = True
    _attr_name = "Battery Mode"
    _attr_icon = "mdi:battery-sync"
    _attr_options = BATTERY_MODES
    _attr_translation_key = "battery_mode"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_battery_mode"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info for grouping entities."""
        return _battery_device_info(self._entry)

    @property
    def current_option(self) -> str | None:
        """Return the current battery mode."""
        return self.coordinator.state_store.control.mode

    async def async_select_option(self, option: str) -> None:
        """Set the battery mode."""
        await self.coordinator.async_set_battery_control(mode=option)
        self.async_write_ha_state()
