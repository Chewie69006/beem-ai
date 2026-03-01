"""Number entity for BeemAI — editable consumption forecast tomorrow."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
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
    """Set up BeemAI number entities from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        BeemAIConsumptionForecastTomorrowNumber(coordinator, entry),
        BeemAIChargePowerNumber(coordinator, entry),
        BeemAIMinSocNumber(coordinator, entry),
        BeemAIMaxSocNumber(coordinator, entry),
    ])


class BeemAIConsumptionForecastTomorrowNumber(CoordinatorEntity, NumberEntity):
    """Editable consumption forecast for tomorrow (kWh)."""

    _attr_has_entity_name = True
    _attr_name = "Consumption Forecast Tomorrow"
    _attr_icon = "mdi:home-lightning-bolt"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "kWh"
    _attr_mode = NumberMode.BOX
    _attr_translation_key = "consumption_forecast_tomorrow"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_consumption_forecast_tomorrow"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info for grouping entities."""
        return _system_device_info(self._entry)

    @property
    def native_value(self) -> float | None:
        """Return the current consumption forecast for tomorrow."""
        try:
            return round(
                self.coordinator.state_store.forecast.consumption_tomorrow_kwh, 1
            )
        except Exception:
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the consumption forecast override for tomorrow."""
        await self.coordinator.async_set_consumption_forecast_tomorrow(value)
        self.async_write_ha_state()


class BeemAIChargePowerNumber(CoordinatorEntity, NumberEntity):
    """Grid charge power limit (watts)."""

    _attr_has_entity_name = True
    _attr_name = "Charge Power"
    _attr_icon = "mdi:lightning-bolt"
    _attr_native_min_value = 0
    _attr_native_max_value = 5000
    _attr_native_step = 100
    _attr_native_unit_of_measurement = "W"
    _attr_mode = NumberMode.SLIDER
    _attr_translation_key = "charge_power"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_charge_power"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info for grouping entities."""
        return _battery_device_info(self._entry)

    @property
    def native_value(self) -> int | None:
        """Return the current charge power limit."""
        return self.coordinator.state_store.control.charge_from_grid_max_power

    async def async_set_native_value(self, value: float) -> None:
        """Set the charge power limit."""
        await self.coordinator.async_set_battery_control(
            charge_from_grid_max_power=int(value)
        )
        self.async_write_ha_state()


class BeemAIMinSocNumber(CoordinatorEntity, NumberEntity):
    """Minimum state of charge (%)."""

    _attr_has_entity_name = True
    _attr_name = "Min SoC"
    _attr_icon = "mdi:battery-arrow-down"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER
    _attr_translation_key = "min_soc"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_min_soc"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info for grouping entities."""
        return _battery_device_info(self._entry)

    @property
    def native_value(self) -> int | None:
        """Return the current minimum SoC."""
        return self.coordinator.state_store.control.min_soc

    async def async_set_native_value(self, value: float) -> None:
        """Set the minimum SoC."""
        await self.coordinator.async_set_battery_control(min_soc=int(value))
        self.async_write_ha_state()


class BeemAIMaxSocNumber(CoordinatorEntity, NumberEntity):
    """Maximum state of charge (%)."""

    _attr_has_entity_name = True
    _attr_name = "Max SoC"
    _attr_icon = "mdi:battery-arrow-up"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER
    _attr_translation_key = "max_soc"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_max_soc"
        self._entry = entry

    @property
    def device_info(self):
        """Return device info for grouping entities."""
        return _battery_device_info(self._entry)

    @property
    def native_value(self) -> int | None:
        """Return the current maximum SoC."""
        return self.coordinator.state_store.control.max_soc

    async def async_set_native_value(self, value: float) -> None:
        """Set the maximum SoC."""
        await self.coordinator.async_set_battery_control(max_soc=int(value))
        self.async_write_ha_state()
