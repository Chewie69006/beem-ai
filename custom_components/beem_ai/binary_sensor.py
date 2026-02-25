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
            key="grid_charging_recommended",
            name="Grid Charging Recommended",
            icon="mdi:battery-charging-wireless",
            device_class=None,
            value_fn=lambda c: c.state_store.plan.allow_grid_charge,
            device_type="system",
        ),
    ])
