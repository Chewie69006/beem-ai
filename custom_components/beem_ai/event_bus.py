"""Internal pub/sub event bus for BeemAI modules."""

import logging
from collections import defaultdict
from enum import Enum, auto
from typing import Any, Callable

log = logging.getLogger(__name__)


class Event(Enum):
    """Internal events for module communication."""

    BATTERY_DATA_UPDATED = auto()
    MQTT_CONNECTED = auto()
    MQTT_DISCONNECTED = auto()
    PLAN_UPDATED = auto()
    FORECAST_UPDATED = auto()
    TARIFF_CHANGED = auto()
    SAFETY_ALERT = auto()
    WATER_HEATER_CHANGED = auto()
    CONFIG_CHANGED = auto()
    SYSTEM_ENABLED = auto()
    SYSTEM_DISABLED = auto()


class EventBus:
    """Simple callback-based pub/sub for internal events."""

    def __init__(self):
        self._subscribers: dict[Event, list[Callable]] = defaultdict(list)

    def subscribe(self, event: Event, callback: Callable):
        """Register a callback for an event type."""
        self._subscribers[event].append(callback)

    def unsubscribe(self, event: Event, callback: Callable):
        """Remove a callback for an event type."""
        try:
            self._subscribers[event].remove(callback)
        except ValueError:
            pass

    def publish(self, event: Event, data: Any = None):
        """Fire all callbacks for an event type."""
        for callback in self._subscribers.get(event, []):
            try:
                callback(data)
            except Exception:
                log.exception("Error in event handler for %s", event.name)
