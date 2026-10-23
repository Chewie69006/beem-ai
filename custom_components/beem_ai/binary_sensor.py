"""Binary sensor entities for BeemAI."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .sensor import BeemAIBinarySensor


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up BeemAI binary sensors from a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([
        BeemAIBinarySensor(
            coordinator, entry,
            key="mqtt_connected",
            name="MQTT Connected",
            icon="mdi:wifi",
            device_class=BinarySensorDeviceClass.CONNECTIVITY,
            value_fn=lambda c: c.state_store.mqtt_connected,
            device_type="system",
        ),
        BeemAIBinarySensor(
            coordinator, entry,
            key="water_heater_heating",
            name="Water Heater",
            icon="mdi:water-boiler",
            device_class=None,
            value_fn=lambda c: c.water_heater.is_heating if c.water_heater else False,
            device_type="system",
        ),
        BeemAIBinarySensor(
            coordinator, entry,
            key="water_heater_fully_heated",
            name="Water Heater Fully Heated",
            icon="mdi:water-boiler-alert",
            device_class=None,
            value_fn=lambda c: c.water_heater.fully_heated if c.water_heater else False,
            device_type="system",
        ),
        BeemAIBinarySensor(
            coordinator, entry,
            key="grid_charge_reserve",
            name="Grid Charge Reserve",
            icon="mdi:transmission-tower-import",
            device_class=None,
            value_fn=lambda c: c.grid_charge.is_reserved(),
            attrs_fn=_grid_charge_attrs,
            device_type="system",
        ),
    ])


def _grid_charge_attrs(coordinator) -> dict:
    """When the grid last charged the battery, and when the reserve ends."""
    tracker = coordinator.grid_charge
    last = tracker.last_charge_at
    until = tracker.reserved_until
    return {
        "grid_charging": tracker.charging,
        "last_grid_charge": last.isoformat() if last else None,
        "reserved_until": until.isoformat() if until else None,
    }
