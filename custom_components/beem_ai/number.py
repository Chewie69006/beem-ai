"""Number entity for BeemAI — editable consumption forecast tomorrow and SoC limits."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    OPT_EV_START_SOC_THRESHOLD,
    OPT_EV_STOP_SOC_THRESHOLD,
    OPT_WH_CHARGE_POWER_THRESHOLD,
    OPT_WH_SOC_THRESHOLD,
    OPT_WH_SUSTAIN_S,
    WH_SUSTAIN_MAX_S,
    WH_SUSTAIN_MIN_S,
    WH_SUSTAIN_STEP_S,
)
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
        BeemAIMinSocNumber(coordinator, entry),
        BeemAIMaxSocNumber(coordinator, entry),
        BeemAIWaterHeaterSocThreshold(coordinator, entry),
        BeemAIWaterHeaterChargePowerThreshold(coordinator, entry),
        BeemAIWaterHeaterSustainDuration(coordinator, entry),
        BeemAIEvStartSocThreshold(coordinator, entry),
        BeemAIEvStopSocThreshold(coordinator, entry),
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


class BeemAIMinSocNumber(CoordinatorEntity, NumberEntity):
    """Minimum state of charge (%)."""

    _attr_has_entity_name = True
    _attr_name = "Min SoC"
    _attr_icon = "mdi:battery-arrow-down"
    _attr_native_min_value = 10
    _attr_native_max_value = 50
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.BOX
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
    def available(self) -> bool:
        """Available only when mode is advanced."""
        return self.coordinator.state_store.control.mode == "advanced"

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
    _attr_native_min_value = 50
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.BOX
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
    def available(self) -> bool:
        """Available only when mode is advanced."""
        return self.coordinator.state_store.control.mode == "advanced"

    @property
    def native_value(self) -> int | None:
        """Return the current maximum SoC."""
        return self.coordinator.state_store.control.max_soc

    async def async_set_native_value(self, value: float) -> None:
        """Set the maximum SoC."""
        await self.coordinator.async_set_battery_control(max_soc=int(value))
        self.async_write_ha_state()


class BeemAIWaterHeaterSocThreshold(CoordinatorEntity, NumberEntity):
    """Water heater SoC threshold (%)."""

    _attr_has_entity_name = True
    _attr_name = "Water Heater SoC Threshold"
    _attr_icon = "mdi:battery-check"
    _attr_native_min_value = 50
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.BOX
    _attr_translation_key = "wh_soc_threshold"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wh_soc_threshold"
        self._entry = entry

    @property
    def device_info(self):
        return _system_device_info(self._entry)

    @property
    def available(self) -> bool:
        return self.coordinator.water_heater is not None

    @property
    def native_value(self) -> float | None:
        return self.coordinator.wh_soc_threshold

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.wh_soc_threshold = value
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, OPT_WH_SOC_THRESHOLD: value},
        )
        self.async_write_ha_state()


class BeemAIWaterHeaterChargePowerThreshold(CoordinatorEntity, NumberEntity):
    """Water heater charging power threshold (W)."""

    _attr_has_entity_name = True
    _attr_name = "Water Heater Charge Power Threshold"
    _attr_icon = "mdi:lightning-bolt"
    _attr_native_min_value = 0
    _attr_native_max_value = 5000
    _attr_native_step = 50
    _attr_native_unit_of_measurement = "W"
    _attr_mode = NumberMode.BOX
    _attr_translation_key = "wh_charge_power_threshold"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wh_charge_power_threshold"
        self._entry = entry

    @property
    def device_info(self):
        return _system_device_info(self._entry)

    @property
    def available(self) -> bool:
        return self.coordinator.water_heater is not None

    @property
    def native_value(self) -> float | None:
        return self.coordinator.wh_charge_power_threshold

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.wh_charge_power_threshold = value
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, OPT_WH_CHARGE_POWER_THRESHOLD: value},
        )
        self.async_write_ha_state()


class BeemAIWaterHeaterSustainDuration(CoordinatorEntity, NumberEntity):
    """Water heater sustain duration (seconds) — up/down stepper.

    Replaces the old hardcoded 30s.  How long the start conditions must
    be continuously satisfied before the heater turns on.
    """

    _attr_has_entity_name = True
    _attr_name = "Water Heater Sustain Duration"
    _attr_icon = "mdi:timer-sync-outline"
    _attr_native_min_value = WH_SUSTAIN_MIN_S
    _attr_native_max_value = WH_SUSTAIN_MAX_S
    _attr_native_step = WH_SUSTAIN_STEP_S
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX
    _attr_translation_key = "wh_sustain_duration"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wh_sustain_duration"
        self._entry = entry

    @property
    def device_info(self):
        return _system_device_info(self._entry)

    @property
    def available(self) -> bool:
        return self.coordinator.water_heater is not None

    @property
    def native_value(self) -> float | None:
        return float(self.coordinator.wh_sustain_s)

    async def async_set_native_value(self, value: float) -> None:
        seconds = int(value)
        self.coordinator.wh_sustain_s = seconds
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, OPT_WH_SUSTAIN_S: seconds},
        )
        self.async_write_ha_state()


class BeemAIEvStartSocThreshold(CoordinatorEntity, NumberEntity):
    """EV charger start SoC threshold (%).

    Charging begins once battery SoC reaches this level (and solar surplus
    is sustained).
    """

    _attr_has_entity_name = True
    _attr_name = "EV Charger Start SoC Threshold"
    _attr_icon = "mdi:battery-charging-high"
    _attr_native_min_value = 50
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.BOX
    _attr_translation_key = "ev_start_soc_threshold"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_ev_start_soc_threshold"
        self._entry = entry

    @property
    def device_info(self):
        return _system_device_info(self._entry)

    @property
    def available(self) -> bool:
        return self.coordinator.ev_charger is not None

    @property
    def native_value(self) -> float | None:
        return self.coordinator.ev_start_soc_threshold

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.ev_start_soc_threshold = value
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, OPT_EV_START_SOC_THRESHOLD: value},
        )
        self.async_write_ha_state()


class BeemAIEvStopSocThreshold(CoordinatorEntity, NumberEntity):
    """EV charger stop / minimum SoC threshold (%).

    When the EV is drawing its minimum amperage (6 A) and solar production
    drops, charging stops once battery SoC falls below this level
    (AUTO mode only — manual mode ignores it).
    """

    _attr_has_entity_name = True
    _attr_name = "EV Charger Stop SoC Threshold"
    _attr_icon = "mdi:battery-charging-low"
    _attr_native_min_value = 40
    _attr_native_max_value = 99
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.BOX
    _attr_translation_key = "ev_stop_soc_threshold"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_ev_stop_soc_threshold"
        self._entry = entry

    @property
    def device_info(self):
        return _system_device_info(self._entry)

    @property
    def available(self) -> bool:
        return self.coordinator.ev_charger is not None

    @property
    def native_value(self) -> float | None:
        return self.coordinator.ev_stop_soc_threshold

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.ev_stop_soc_threshold = value
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, OPT_EV_STOP_SOC_THRESHOLD: value},
        )
        self.async_write_ha_state()
