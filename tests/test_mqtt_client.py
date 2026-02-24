"""Unit tests for BeemMqttClient (async aiomqtt client)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.beem_ai.mqtt_client import BeemMqttClient, _FIELD_MAP
from custom_components.beem_ai.event_bus import Event


@pytest.fixture
def mock_api_client():
    """Fake async API client providing user_id and MQTT token."""
    client = AsyncMock()
    client.user_id = "uid-12345678"
    client.get_mqtt_token = AsyncMock(return_value="mqtt-jwt-token")
    return client


@pytest.fixture
def mqtt_client(mock_api_client, state_store, event_bus):
    """Create a BeemMqttClient with a mocked API client."""
    client = BeemMqttClient(
        api_client=mock_api_client,
        battery_serial="SN-001",
        state_store=state_store,
        event_bus=event_bus,
    )
    yield client
    client._cancel_watchdog()


# ------------------------------------------------------------------
# _handle_message()
# ------------------------------------------------------------------


class TestHandleMessage:
    def _make_msg(self, payload_dict):
        """Build a minimal aiomqtt-style message object."""
        msg = SimpleNamespace()
        msg.topic = "battery/SN-001/sys/streaming"
        msg.payload = json.dumps(payload_dict).encode()
        return msg

    def test_parses_json_and_updates_state(self, mqtt_client, state_store):
        """Valid JSON payload updates BatteryState via StateStore."""
        msg = self._make_msg({"soc": 72.5, "solarPower": 1200})

        mqtt_client._handle_message(msg)

        assert state_store.battery.soc == 72.5
        assert state_store.battery.solar_power_w == 1200

    def test_invalid_json_does_not_crash(self, mqtt_client):
        """Malformed payload is silently discarded."""
        msg = SimpleNamespace()
        msg.topic = "battery/SN-001/sys/streaming"
        msg.payload = b"not-json{{"

        # Should not raise.
        mqtt_client._handle_message(msg)

    def test_publishes_battery_data_updated_event(self, mqtt_client, event_bus):
        """A valid message fires the BATTERY_DATA_UPDATED event."""
        received = []
        event_bus.subscribe(Event.BATTERY_DATA_UPDATED, lambda d: received.append(d))

        msg = self._make_msg({"soc": 55.0})
        mqtt_client._handle_message(msg)

        assert len(received) == 1

    def test_field_mapping_all_fields(self, mqtt_client, state_store):
        """Every key in _FIELD_MAP is translated correctly."""
        payload = {
            "soc": 80.0,
            "solarPower": 2000,
            "batteryPower": 500,
            "meterPower": -300,
            "inverterPower": 1800,
            "mppt1Power": 700,
            "mppt2Power": 600,
            "mppt3Power": 500,
            "workingModeLabel": "solar_priority",
            "globalSoh": 97.5,
            "numberOfCycles": 42,
            "capacityInKwh": 13.4,
        }
        msg = self._make_msg(payload)

        mqtt_client._handle_message(msg)

        bat = state_store.battery
        assert bat.soc == 80.0
        assert bat.solar_power_w == 2000
        assert bat.battery_power_w == 500
        assert bat.meter_power_w == -300
        assert bat.inverter_power_w == 1800
        assert bat.mppt1_w == 700
        assert bat.mppt2_w == 600
        assert bat.mppt3_w == 500
        assert bat.working_mode == "solar_priority"
        assert bat.soh == 97.5
        assert bat.cycle_count == 42
        assert bat.capacity_kwh == 13.4

    def test_unknown_fields_ignored(self, mqtt_client, state_store):
        """Fields not in _FIELD_MAP are silently ignored."""
        msg = self._make_msg({"unknownField": 999, "soc": 60})

        mqtt_client._handle_message(msg)

        assert state_store.battery.soc == 60

    def test_empty_known_fields_no_event(self, mqtt_client, event_bus):
        """Message with no recognised fields does not fire an event."""
        received = []
        event_bus.subscribe(Event.BATTERY_DATA_UPDATED, lambda d: received.append(d))

        msg = self._make_msg({"randomKey": 42})
        mqtt_client._handle_message(msg)

        assert len(received) == 0


# ------------------------------------------------------------------
# Field mapping completeness
# ------------------------------------------------------------------


class TestFieldMap:
    def test_field_map_keys(self):
        """Verify _FIELD_MAP contains expected API field names."""
        expected_keys = {
            "soc", "solarPower", "batteryPower", "meterPower",
            "inverterPower", "mppt1Power", "mppt2Power", "mppt3Power",
            "workingModeLabel", "globalSoh", "numberOfCycles", "capacityInKwh",
        }
        assert set(_FIELD_MAP.keys()) == expected_keys


# ------------------------------------------------------------------
# connect() / disconnect() lifecycle
# ------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_connect_creates_loop_task(self, mqtt_client):
        """connect() creates a background loop task."""
        with patch.object(mqtt_client, "_run_loop", new_callable=AsyncMock):
            mqtt_client.connect()

            assert mqtt_client._loop_task is not None

    @pytest.mark.asyncio
    async def test_disconnect_sets_mqtt_connected_false(
        self, mqtt_client, state_store
    ):
        """disconnect() sets mqtt_connected to False."""
        state_store.mqtt_connected = True

        await mqtt_client.disconnect()

        assert state_store.mqtt_connected is False

    @pytest.mark.asyncio
    async def test_disconnect_cancels_loop_task(self, mqtt_client):
        """disconnect() cancels the loop task if running."""
        with patch.object(mqtt_client, "_run_loop", new_callable=AsyncMock):
            mqtt_client.connect()
            task = mqtt_client._loop_task

            await mqtt_client.disconnect()

            assert mqtt_client._loop_task is None


# ------------------------------------------------------------------
# reconfigure()
# ------------------------------------------------------------------


class TestReconfigure:
    @pytest.mark.asyncio
    async def test_reconfigure_updates_topic(self, mqtt_client):
        """reconfigure() updates the battery serial and topic."""
        with patch.object(mqtt_client, "_run_loop", new_callable=AsyncMock):
            mqtt_client.reconfigure({"beem_battery_serial": "SN-002"})

        assert mqtt_client._battery_serial == "SN-002"
        assert mqtt_client._topic == "battery/SN-002/sys/streaming"
