"""Switch entity for BeemAI system enable/disable."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BeemAI switches from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BeemAIEnabledSwitch(coordinator, entry)])


class BeemAIEnabledSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable the BeemAI system."""

    _attr_has_entity_name = True
    _attr_name = "Enabled"
    _attr_icon = "mdi:robot"
    _attr_translation_key = "enabled"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info for grouping entities."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "BeemAI Battery",
            "manufacturer": "Beem Energy",
            "model": "Battery System",
        }

    @property
    def is_on(self) -> bool:
        """Return True if BeemAI is enabled."""
        return self.coordinator.state_store.enabled

    async def async_turn_on(self, **kwargs) -> None:
        """Enable BeemAI."""
        await self.coordinator.async_set_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable BeemAI."""
        await self.coordinator.async_set_enabled(False)
        self.async_write_ha_state()
