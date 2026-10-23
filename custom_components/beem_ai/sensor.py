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
        # --- System device sensors ---
        BeemAISensor(
            coordinator, entry,
            key="water_heater_energy_today",
            name="Water Heater Energy Today",
            icon="mdi:water-boiler",
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            unit="Wh",
            value_fn=lambda c: (
                round(c.water_heater.energy_today_wh)
                if c.water_heater else 0
            ),
            device_type="system",
        ),
        BeemAISensor(
            coordinator, entry,
            key="base_load",
            name="Base Load",
            icon="mdi:home-lightning-bolt-outline",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            unit="W",
            value_fn=lambda c: _round_or_none(c.base_load_w),
            extra_fn=_base_load_attrs,
            device_type="system",
        ),
        BeemAISensor(
            coordinator, entry,
            key="energy_history",
            name="Energy History",
            icon="mdi:chart-timeline-variant",
            device_class=None,
            state_class=None,
            unit="d",
            value_fn=lambda c: len(c.energy_history.complete_days()),
            extra_fn=_energy_history_attrs,
            device_type="system",
        ),
        BeemAISensor(
            coordinator, entry,
            key="charge_target_suggestion",
            name="Charge Target Suggestion",
            icon="mdi:battery-clock",
            device_class=None,
            state_class=None,
            unit="%",
            value_fn=lambda c: (
                c.suggestion.target_soc if c.suggestion else None
            ),
            extra_fn=_suggestion_attrs,
            device_type="system",
        ),
    ]

    # --- Per-array solar sensors (creates a device per panel array) ---
    for idx, array in enumerate(coordinator.panel_arrays):
        tilt = array.get("tilt", 30)
        azimuth = array.get("azimuth", 180)
        kwp = array.get("kwp", 5.0)
        mppt_id = array.get("mppt_id")
        panels_series = array.get("panels_in_series")
        panels_parallel = array.get("panels_in_parallel")

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
    async_add_entities(sensors)


def _round_or_none(value: float | None) -> int | None:
    return None if value is None else round(value)


def _base_load_attrs(coordinator) -> dict[str, Any]:
    """Where the base load comes from, and the nights it was learned on."""
    history = coordinator.energy_history
    learned = history.base_load_w()
    nights = history.night_base_loads_w()
    return {
        "source": (
            "override" if coordinator.base_load_override_w > 0
            else "learned" if learned is not None
            else "no data"
        ),
        "learned_w": _round_or_none(learned),
        "nights": [round(n) for n in nights[-14:]],
    }


def _energy_history_attrs(coordinator) -> dict[str, Any]:
    """The last days' breakdown, and how far back the history goes."""
    history = coordinator.energy_history
    hvac_days, hvac_since = history.hvac_history()
    return {
        "updated": (
            history.updated.isoformat() if history.updated else None
        ),
        "hvac_history_days": hvac_days,
        "hvac_history_since": hvac_since.isoformat() if hvac_since else None,
        "missing_statistics": history.missing,
        "last_days": [d.as_dict() for d in history.complete_days()[-7:]],
    }


def _suggestion_attrs(coordinator) -> dict[str, Any]:
    """What tonight's target was computed from, or why there is none."""
    if coordinator.suggestion is None:
        return {"reason": coordinator.suggestion_reason}
    return coordinator.suggestion.as_attributes()


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
        attrs_fn=None,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_class = device_class
        self._value_fn = value_fn
        self._attrs_fn = attrs_fn
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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes, if the entity defines any."""
        if self._attrs_fn is None:
            return None
        try:
            return self._attrs_fn(self.coordinator)
        except Exception:
            return None
