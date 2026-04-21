"""Select entities for BeemAI — battery mode and charge power control."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    EV_MODES,
    OPT_EV_CHARGER_MODE,
    OPT_WATER_HEATER_MODE,
    OPT_WH_MIN_DURATION_S,
    WH_MIN_DURATION_OPTIONS_S,
    WH_MODES,
)
from .sensor import _battery_device_info, _system_device_info

_LOGGER = logging.getLogger(__name__)

BATTERY_MODES = ["auto", "pause", "advanced"]
CHARGE_POWER_OPTIONS = ["500", "1000", "2500", "5000"]


def _format_duration(seconds: int) -> str:
    """Render a seconds-based duration as ``15m`` / ``1h`` / ``1h30m``."""
    minutes = seconds // 60
    h, m = divmod(minutes, 60)
    if h == 0:
        return f"{m}m"
    if m == 0:
        return f"{h}h"
    return f"{h}h{m:02d}m"


WH_MIN_DURATION_OPTIONS = [_format_duration(s) for s in WH_MIN_DURATION_OPTIONS_S]
_WH_LABEL_TO_SECONDS = dict(zip(WH_MIN_DURATION_OPTIONS, WH_MIN_DURATION_OPTIONS_S))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BeemAI select entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        BeemAIBatteryModeSelect(coordinator, entry),
        BeemAIChargePowerSelect(coordinator, entry),
    ]
    if coordinator.ev_charger is not None:
        entities.append(BeemAIEvChargerModeSelect(coordinator, entry))
    if coordinator.water_heater is not None:
        entities.append(BeemAIWaterHeaterModeSelect(coordinator, entry))
        entities.append(BeemAIWaterHeaterMinDurationSelect(coordinator, entry))
    async_add_entities(entities)


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


class BeemAIEvChargerModeSelect(CoordinatorEntity, SelectEntity):
    """Select entity for EV charger mode: Disabled / Auto / Manual."""

    _attr_has_entity_name = True
    _attr_name = "EV Charger Mode"
    _attr_icon = "mdi:ev-station"
    _attr_options = EV_MODES
    _attr_translation_key = "ev_charger_mode"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_ev_charger_mode"
        self._entry = entry

    @property
    def device_info(self):
        return _system_device_info(self._entry)

    @property
    def available(self) -> bool:
        return self.coordinator.ev_charger is not None

    @property
    def current_option(self) -> str | None:
        return self.coordinator.ev_charger_mode

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_ev_charger_mode(option)
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, OPT_EV_CHARGER_MODE: option},
        )
        self.async_write_ha_state()


class BeemAIWaterHeaterModeSelect(CoordinatorEntity, SelectEntity):
    """Select entity for water heater mode: Disabled / Auto."""

    _attr_has_entity_name = True
    _attr_name = "Water Heater Mode"
    _attr_icon = "mdi:water-boiler"
    _attr_options = WH_MODES
    _attr_translation_key = "water_heater_mode"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_water_heater_mode"
        self._entry = entry

    @property
    def device_info(self):
        return _system_device_info(self._entry)

    @property
    def available(self) -> bool:
        return self.coordinator.water_heater is not None

    @property
    def current_option(self) -> str | None:
        return self.coordinator.water_heater_mode

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_water_heater_mode(option)
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, OPT_WATER_HEATER_MODE: option},
        )
        self.async_write_ha_state()


class BeemAIWaterHeaterMinDurationSelect(CoordinatorEntity, SelectEntity):
    """Select entity for the water heater minimum heating duration."""

    _attr_has_entity_name = True
    _attr_name = "Water Heater Min Duration"
    _attr_icon = "mdi:timer-sand"
    _attr_options = WH_MIN_DURATION_OPTIONS
    _attr_translation_key = "wh_min_duration"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wh_min_duration"
        self._entry = entry

    @property
    def device_info(self):
        return _system_device_info(self._entry)

    @property
    def available(self) -> bool:
        return self.coordinator.water_heater is not None

    @property
    def current_option(self) -> str | None:
        return _format_duration(self.coordinator.wh_min_duration_s)

    async def async_select_option(self, option: str) -> None:
        seconds = _WH_LABEL_TO_SECONDS.get(option)
        if seconds is None:
            return
        self.coordinator.wh_min_duration_s = seconds
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, OPT_WH_MIN_DURATION_S: seconds},
        )
        self.async_write_ha_state()
