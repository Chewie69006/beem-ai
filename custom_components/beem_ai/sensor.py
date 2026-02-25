"""Sensor and binary sensor entities for BeemAI."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Device info helpers
# ------------------------------------------------------------------

def _battery_device_info(entry: ConfigEntry) -> dict:
    """Return DeviceInfo for the Battery device."""
    return {
        "identifiers": {(DOMAIN, f"battery_{entry.entry_id}")},
        "name": "BeemAI Battery",
        "manufacturer": "Beem Energy",
        "model": "Battery System",
    }


def _solar_device_info(entry: ConfigEntry, index: int = 0) -> dict:
    """Return DeviceInfo for a Solar Array device."""
    return {
        "identifiers": {(DOMAIN, f"solar_{entry.entry_id}_{index}")},
        "name": f"BeemAI Solar Array {index + 1}",
        "manufacturer": "Beem Energy",
        "model": "Solar Array",
        "via_device": (DOMAIN, f"battery_{entry.entry_id}"),
    }


def _system_device_info(entry: ConfigEntry) -> dict:
    """Return DeviceInfo for the System device."""
    return {
        "identifiers": {(DOMAIN, f"system_{entry.entry_id}")},
        "name": "BeemAI System",
        "manufacturer": "Beem Energy",
        "model": "Optimization System",
        "via_device": (DOMAIN, f"battery_{entry.entry_id}"),
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BeemAI sensors from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    sensors: list[SensorEntity | BinarySensorEntity] = [
        # --- Battery device sensors ---
        BeemAISensor(
            coordinator, entry,
            key="battery_soc",
            name="Battery SoC",
            icon="mdi:battery",
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT,
            unit="%",
            value_fn=lambda c: round(c.state_store.battery.soc, 1),
            device_type="battery",
        ),
        BeemAISensor(
            coordinator, entry,
            key="solar_power",
            name="Solar Power",
            icon="mdi:solar-power",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            unit="W",
            value_fn=lambda c: round(c.state_store.battery.solar_power_w),
            device_type="battery",
        ),
        BeemAISensor(
            coordinator, entry,
            key="battery_power",
            name="Battery Power",
            icon="mdi:battery-charging",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            unit="W",
            value_fn=lambda c: round(c.state_store.battery.battery_power_w),
            device_type="battery",
        ),
        BeemAISensor(
            coordinator, entry,
            key="grid_power",
            name="Grid Power",
            icon="mdi:transmission-tower",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            unit="W",
            value_fn=lambda c: round(c.state_store.battery.meter_power_w),
            device_type="battery",
        ),
        BeemAISensor(
            coordinator, entry,
            key="consumption",
            name="Consumption",
            icon="mdi:home-lightning-bolt",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            unit="W",
            value_fn=lambda c: round(c.state_store.battery.consumption_w),
            device_type="battery",
        ),
        BeemAISensor(
            coordinator, entry,
            key="battery_soh",
            name="Battery SoH",
            icon="mdi:battery-heart-variant",
            device_class=None,
            state_class=SensorStateClass.MEASUREMENT,
            unit="%",
            value_fn=lambda c: round(c.state_store.battery.soh, 1),
            device_type="battery",
        ),
        BeemAISensor(
            coordinator, entry,
            key="optimal_charge_target",
            name="Optimal Charge Target",
            icon="mdi:target",
            device_class=None,
            state_class=SensorStateClass.MEASUREMENT,
            unit="%",
            value_fn=lambda c: round(c.state_store.plan.target_soc),
            device_type="battery",
        ),
        BeemAISensor(
            coordinator, entry,
            key="optimal_charge_power",
            name="Optimal Charge Power",
            icon="mdi:flash",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            unit="W",
            value_fn=lambda c: c.state_store.plan.charge_power_w,
            device_type="battery",
        ),

        # --- Solar device sensors (array 0 = total/first array) ---
        BeemAISensor(
            coordinator, entry,
            key="solar_forecast_today",
            name="Solar Forecast Today",
            icon="mdi:weather-sunny",
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
            unit="kWh",
            value_fn=lambda c: round(c.state_store.forecast.solar_today_kwh, 1),
            extra_fn=lambda c: {
                "sources": c.state_store.forecast.sources_used,
                "confidence": c.state_store.forecast.confidence,
            },
            device_type="solar",
        ),
        BeemAISensor(
            coordinator, entry,
            key="solar_forecast_tomorrow",
            name="Solar Forecast Tomorrow",
            icon="mdi:weather-sunny-alert",
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
            unit="kWh",
            value_fn=lambda c: round(c.state_store.forecast.solar_tomorrow_kwh, 1),
            device_type="solar",
        ),

        # --- System device sensors ---
        BeemAISensor(
            coordinator, entry,
            key="optimization_status",
            name="Optimization Status",
            icon="mdi:brain",
            device_class=None,
            state_class=None,
            unit=None,
            value_fn=lambda c: c.state_store.plan.reasoning or c.state_store.plan.phase,
            extra_fn=lambda c: {"phase": c.state_store.plan.phase},
            device_type="system",
        ),
        BeemAISensor(
            coordinator, entry,
            key="consumption_forecast_today",
            name="Consumption Forecast Today",
            icon="mdi:home-lightning-bolt",
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL,
            unit="kWh",
            value_fn=lambda c: round(c.state_store.forecast.consumption_today_kwh, 1),
            device_type="system",
        ),
        BeemAISensor(
            coordinator, entry,
            key="cost_savings_today",
            name="Cost Savings Today",
            icon="mdi:currency-eur",
            device_class=SensorDeviceClass.MONETARY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            unit="EUR",
            value_fn=lambda c: round(c.state_store.daily_savings_eur, 2),
            device_type="system",
        ),
    ]

    async_add_entities(sensors)


class BeemAISensor(CoordinatorEntity, SensorEntity):
    """A BeemAI sensor entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        *,
        key: str,
        name: str,
        icon: str,
        device_class: SensorDeviceClass | None,
        state_class: SensorStateClass | None,
        unit: str | None,
        value_fn,
        extra_fn=None,
        device_type: str = "battery",
        solar_index: int = 0,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_native_unit_of_measurement = unit
        self._value_fn = value_fn
        self._extra_fn = extra_fn
        self._entry = entry
        self._device_type = device_type
        self._solar_index = solar_index

    @property
    def device_info(self):
        """Return device info for grouping entities."""
        if self._device_type == "solar":
            return _solar_device_info(self._entry, self._solar_index)
        if self._device_type == "system":
            return _system_device_info(self._entry)
        return _battery_device_info(self._entry)

    @property
    def native_value(self):
        """Return the sensor value."""
        try:
            return self._value_fn(self.coordinator)
        except Exception:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        if self._extra_fn is None:
            return None
        try:
            return self._extra_fn(self.coordinator)
        except Exception:
            return None


class BeemAIBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """A BeemAI binary sensor entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        *,
        key: str,
        name: str,
        icon: str,
        device_class: BinarySensorDeviceClass | None,
        value_fn,
        device_type: str = "battery",
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_class = device_class
        self._value_fn = value_fn
        self._entry = entry
        self._device_type = device_type

    @property
    def device_info(self):
        """Return device info for grouping entities."""
        if self._device_type == "solar":
            return _solar_device_info(self._entry)
        if self._device_type == "system":
            return _system_device_info(self._entry)
        return _battery_device_info(self._entry)

    @property
    def is_on(self) -> bool | None:
        """Return True if the binary sensor is on."""
        try:
            return self._value_fn(self.coordinator)
        except Exception:
            return None
