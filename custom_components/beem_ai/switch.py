"""Switch entity for BeemAI system enable/disable."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .sensor import _battery_device_info, _system_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BeemAI switches from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        BeemAIEnabledSwitch(coordinator, entry),
        BeemAIAllowGridChargeSwitch(coordinator, entry),
        BeemAIPreventDischargeSwitch(coordinator, entry),
    ])


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
        return _system_device_info(self._entry)

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


class BeemAIAllowGridChargeSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to allow/disallow charging the battery from the grid."""

    _attr_has_entity_name = True
    _attr_name = "Allow Grid Charge"
    _attr_icon = "mdi:transmission-tower-import"
    _attr_translation_key = "allow_grid_charge"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_allow_grid_charge"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info for grouping entities."""
        return _battery_device_info(self._entry)

    @property
    def is_on(self) -> bool:
        """Return True if grid charging is allowed."""
        return self.coordinator.state_store.control.allow_charge_from_grid

    async def async_turn_on(self, **kwargs) -> None:
        """Allow grid charging."""
        await self.coordinator.async_set_battery_control(allow_charge_from_grid=True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disallow grid charging."""
        await self.coordinator.async_set_battery_control(allow_charge_from_grid=False)
        self.async_write_ha_state()


class BeemAIPreventDischargeSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to prevent/allow battery discharge."""

    _attr_has_entity_name = True
    _attr_name = "Prevent Discharge"
    _attr_icon = "mdi:battery-lock"
    _attr_translation_key = "prevent_discharge"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_prevent_discharge"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info for grouping entities."""
        return _battery_device_info(self._entry)

    @property
    def is_on(self) -> bool:
        """Return True if discharge is prevented."""
        return self.coordinator.state_store.control.prevent_discharge

    async def async_turn_on(self, **kwargs) -> None:
        """Prevent discharge."""
        await self.coordinator.async_set_battery_control(prevent_discharge=True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Allow discharge."""
        await self.coordinator.async_set_battery_control(prevent_discharge=False)
        self.async_write_ha_state()
