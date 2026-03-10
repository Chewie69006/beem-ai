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
            key="ev_charger_charging",
            name="EV Charger",
            icon="mdi:ev-station",
            device_class=None,
            value_fn=lambda c: c.ev_charger.is_charging if c.ev_charger else False,
            device_type="system",
        ),
    ])
