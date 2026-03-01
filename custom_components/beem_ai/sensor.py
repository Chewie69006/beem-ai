"""Sensor and binary sensor entities for BeemAI."""

from __future__ import annotations

import json
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
        # --- System device sensors ---
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
            device_type="system",
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
            device_type="system",
        ),
    ]

    # Parse Solcast site ID mappings for per-array sensors
    solcast_site_map: dict[int, str] = {}
    raw_site_ids = entry.options.get("solcast_site_ids_json", "")
    if raw_site_ids:
        try:
            for sid_entry in json.loads(raw_site_ids):
                solcast_site_map[sid_entry["array_index"]] = sid_entry["site_id"]
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    # --- Per-array solar sensors (creates a device per panel array) ---
    for idx, array in enumerate(coordinator.panel_arrays):
        tilt = array.get("tilt", 30)
        azimuth = array.get("azimuth", 180)
        kwp = array.get("kwp", 5.0)
        mppt_id = array.get("mppt_id")
        panels_series = array.get("panels_in_series")
        panels_parallel = array.get("panels_in_parallel")
        solcast_site_id = solcast_site_map.get(idx, "Not configured")

        sensors.append(BeemAISensor(
            coordinator, entry,
            key=f"solar_array_{idx + 1}_capacity",
            name=f"Array {idx + 1} Capacity",
            icon="mdi:solar-panel-large",
            device_class=None,
            state_class=None,
            unit="kWp",
            value_fn=lambda c, _v=kwp: _v,
            device_type="solar",
            solar_index=idx,
        ))
        sensors.append(BeemAISensor(
            coordinator, entry,
            key=f"solar_array_{idx + 1}_tilt",
            name=f"Array {idx + 1} Tilt",
            icon="mdi:angle-acute",
            device_class=None,
            state_class=None,
            unit="°",
            value_fn=lambda c, _v=tilt: _v,
            device_type="solar",
            solar_index=idx,
        ))
        sensors.append(BeemAISensor(
            coordinator, entry,
            key=f"solar_array_{idx + 1}_azimuth",
            name=f"Array {idx + 1} Azimuth",
            icon="mdi:compass",
            device_class=None,
            state_class=None,
            unit="°",
            value_fn=lambda c, _v=azimuth: _v,
            device_type="solar",
            solar_index=idx,
        ))
        sensors.append(BeemAISensor(
            coordinator, entry,
            key=f"solar_array_{idx + 1}_mppt_id",
            name=f"Array {idx + 1} MPPT ID",
            icon="mdi:identifier",
            device_class=None,
            state_class=None,
            unit=None,
            value_fn=lambda c, _v=mppt_id: _v,
            device_type="solar",
            solar_index=idx,
        ))
        sensors.append(BeemAISensor(
            coordinator, entry,
            key=f"solar_array_{idx + 1}_panels_in_series",
            name=f"Array {idx + 1} Panels in Series",
            icon="mdi:solar-panel",
            device_class=None,
            state_class=None,
            unit=None,
            value_fn=lambda c, _v=panels_series: _v,
            device_type="solar",
            solar_index=idx,
        ))
        sensors.append(BeemAISensor(
            coordinator, entry,
            key=f"solar_array_{idx + 1}_panels_in_parallel",
            name=f"Array {idx + 1} Panels in Parallel",
            icon="mdi:solar-panel",
            device_class=None,
            state_class=None,
            unit=None,
            value_fn=lambda c, _v=panels_parallel: _v,
            device_type="solar",
            solar_index=idx,
        ))
        sensors.append(BeemAISensor(
            coordinator, entry,
            key=f"solar_array_{idx + 1}_solcast_site_id",
            name=f"Array {idx + 1} Solcast Site ID",
            icon="mdi:cloud-outline",
            device_class=None,
            state_class=None,
            unit=None,
            value_fn=lambda c, _v=solcast_site_id: _v,
            device_type="solar",
            solar_index=idx,
        ))

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
