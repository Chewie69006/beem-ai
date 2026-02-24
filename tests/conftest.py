"""Shared test fixtures for BeemAI HACS component tests.

Homeassistant stubs are injected into sys.modules BEFORE any
custom_components import so that the package loads without requiring
a real HA installation.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Homeassistant stubs — must run before any custom_components.beem_ai import
# ---------------------------------------------------------------------------

def _mod(name: str) -> ModuleType:
    m = ModuleType(name)
    sys.modules[name] = m
    return m


# Top-level packages
_mod("homeassistant")
_mod("homeassistant.components")
_mod("homeassistant.helpers")

# homeassistant.core
_ha_core = _mod("homeassistant.core")
_ha_core.HomeAssistant = MagicMock

# homeassistant.config_entries
_ha_ce = _mod("homeassistant.config_entries")
_ha_ce.ConfigEntry = MagicMock
_ha_ce.ConfigFlowResult = dict


class _ConfigFlow:
    """Minimal ConfigFlow base for testing."""
    VERSION = 1

    def __init_subclass__(cls, domain: str = "", **kwargs):
        super().__init_subclass__(**kwargs)

    def __init__(self):
        self._abort_reason = None

    def _abort_if_unique_id_configured(self):
        pass

    async def async_set_unique_id(self, uid):
        pass

    def async_create_entry(self, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(self, step_id, data_schema=None, errors=None):
        return {"type": "form", "step_id": step_id, "errors": errors or {}}

    def async_abort(self, reason):
        return {"type": "abort", "reason": reason}


class _OptionsFlow:
    """Minimal OptionsFlow base for testing."""

    def async_create_entry(self, data):
        return {"type": "create_entry", "data": data}

    def async_show_form(self, step_id, data_schema=None, errors=None):
        return {"type": "form", "step_id": step_id, "errors": errors or {}}


_ha_ce.ConfigFlow = _ConfigFlow
_ha_ce.OptionsFlow = _OptionsFlow

# homeassistant.helpers.event
_ha_ev = _mod("homeassistant.helpers.event")
_ha_ev.async_call_later = MagicMock(return_value=MagicMock())
_ha_ev.async_track_time_interval = MagicMock(return_value=MagicMock())

# homeassistant.helpers.update_coordinator
_ha_uc = _mod("homeassistant.helpers.update_coordinator")


class _DataUpdateCoordinator:
    """Minimal DataUpdateCoordinator for testing."""

    def __init__(self, hass, logger, *, name, update_interval, update_method=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.update_method = update_method
        self.data = None

    async def async_config_entry_first_refresh(self):
        pass

    def async_set_updated_data(self, data):
        self.data = data


class _CoordinatorEntity:
    """Minimal CoordinatorEntity for testing."""

    def __init__(self, coordinator):
        self.coordinator = coordinator


_ha_uc.DataUpdateCoordinator = _DataUpdateCoordinator
_ha_uc.CoordinatorEntity = _CoordinatorEntity

# homeassistant.data_entry_flow
_ha_def = _mod("homeassistant.data_entry_flow")


class AbortFlow(Exception):
    def __init__(self, reason: str = ""):
        self.reason = reason
        super().__init__(reason)


_ha_def.AbortFlow = AbortFlow
_ha_def.FlowResult = dict

# homeassistant.helpers.entity_platform
_ha_ep = _mod("homeassistant.helpers.entity_platform")
_ha_ep.AddEntitiesCallback = MagicMock

# homeassistant.components.sensor
_ha_sensor = _mod("homeassistant.components.sensor")


class _SensorDeviceClass:
    BATTERY = "battery"
    ENERGY = "energy"
    MONETARY = "monetary"
    POWER = "power"


class _SensorStateClass:
    MEASUREMENT = "measurement"
    TOTAL = "total"
    TOTAL_INCREASING = "total_increasing"


_ha_sensor.SensorEntity = MagicMock
_ha_sensor.SensorDeviceClass = _SensorDeviceClass
_ha_sensor.SensorStateClass = _SensorStateClass

# homeassistant.components.binary_sensor
_ha_bs = _mod("homeassistant.components.binary_sensor")


class _BinarySensorDeviceClass:
    CONNECTIVITY = "connectivity"


_ha_bs.BinarySensorEntity = MagicMock
_ha_bs.BinarySensorDeviceClass = _BinarySensorDeviceClass

# homeassistant.components.switch
_ha_sw = _mod("homeassistant.components.switch")
_ha_sw.SwitchEntity = MagicMock

# ---------------------------------------------------------------------------
# Now it is safe to import from custom_components.beem_ai
# ---------------------------------------------------------------------------

import sys as _sys
from pathlib import Path

# Ensure repo root is on sys.path
_repo_root = Path(__file__).parent.parent
if str(_repo_root) not in _sys.path:
    _sys.path.insert(0, str(_repo_root))

from custom_components.beem_ai.event_bus import EventBus
from custom_components.beem_ai.state_store import StateStore


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_store():
    return StateStore()


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def mock_hass():
    """Minimal mock of HomeAssistant for unit tests."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states = MagicMock()
    hass.config = MagicMock()
    hass.config.path = MagicMock(return_value="/tmp/beem_ai_test_data")
    return hass
