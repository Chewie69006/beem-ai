"""Select entities for BeemAI — battery mode and charge power control."""

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

BATTERY_MODES = ["auto", "pause", "advanced"]
CHARGE_POWER_OPTIONS = ["500", "1000", "2500", "5000"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BeemAI select entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        BeemAIBatteryModeSelect(coordinator, entry),
        BeemAIChargePowerSelect(coordinator, entry),
    ])


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
    def available(self) -> bool:
        """Available when the API allows mode changes."""
        return self.coordinator.state_store.control.can_change_mode

    @property
    def current_option(self) -> str | None:
        """Return the current battery mode."""
        return self.coordinator.state_store.control.mode

    async def async_select_option(self, option: str) -> None:
        """Set the battery mode."""
        await self.coordinator.async_set_battery_control(mode=option)
        self.async_write_ha_state()


class BeemAIChargePowerSelect(CoordinatorEntity, SelectEntity):
    """Select entity for grid charge power (discrete values)."""

    _attr_has_entity_name = True
    _attr_name = "Charge Power"
    _attr_icon = "mdi:lightning-bolt"
    _attr_options = CHARGE_POWER_OPTIONS
    _attr_unit_of_measurement = "W"
    _attr_translation_key = "charge_power"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_charge_power"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info for grouping entities."""
        return _battery_device_info(self._entry)

    @property
    def available(self) -> bool:
        """Available only when mode is advanced."""
        return self.coordinator.state_store.control.mode == "advanced"

    @property
    def current_option(self) -> str | None:
        """Return the current charge power as string."""
        return str(self.coordinator.state_store.control.charge_from_grid_max_power)

    async def async_select_option(self, option: str) -> None:
        """Set the charge power."""
        await self.coordinator.async_set_battery_control(
            charge_from_grid_max_power=int(option)
        )
        self.async_write_ha_state()
