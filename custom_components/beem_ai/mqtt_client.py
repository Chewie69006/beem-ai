"""Async MQTT client for receiving live battery data from Beem Energy."""

import asyncio
import json
import logging
import time
from typing import Optional

import aiomqtt

from .event_bus import Event, EventBus
from .state_store import StateStore

log = logging.getLogger(__name__)

# MQTT broker settings.
MQTT_HOST = "mqtt.beem.energy"
MQTT_PORT = 8084
MQTT_PATH = "/mqtt"

# Token refresh interval (50 minutes, matching REST JWT cadence).
TOKEN_REFRESH_SECONDS = 50 * 60

# Reconnection backoff parameters.
RECONNECT_MIN_SECONDS = 1
RECONNECT_MAX_SECONDS = 60

# Safety: if disconnected for this long, force auto mode.
DISCONNECT_SAFETY_SECONDS = 15 * 60

# Field mapping from Beem MQTT JSON to StateStore BatteryState attributes.
_FIELD_MAP = {
    "soc": "soc",
    "solarPower": "solar_power_w",
    "batteryPower": "battery_power_w",
    "meterPower": "meter_power_w",
    "inverterPower": "inverter_power_w",
    "mppt1Power": "mppt1_w",
    "mppt2Power": "mppt2_w",
    "mppt3Power": "mppt3_w",
    "workingModeLabel": "working_mode",
    "globalSoh": "soh",
    "numberOfCycles": "cycle_count",
    "capacityInKwh": "capacity_kwh",
}


class _TokenExpired(Exception):
    """Raised by the refresh timer to force reconnection with a new token."""


class BeemMqttClient:
    """Subscribes to Beem MQTT streaming topic and updates shared state."""

    def __init__(
        self,
        api_client,  # BeemApiClient — used for get_mqtt_token(), set_auto_mode()
        battery_serial: str,
        state_store: StateStore,
        event_bus: EventBus,
    ):
        self._api_client = api_client
        self._battery_serial = battery_serial
        self._state_store = state_store
        self._event_bus = event_bus

        self._topic = f"battery/{battery_serial.upper()}/sys/streaming"

        # Main loop task.
        self._loop_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        # Reconnection backoff.
        self._backoff = RECONNECT_MIN_SECONDS

        # Disconnect watchdog task.
        self._watchdog_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Start the MQTT connection loop."""
        if self._loop_task is not None and not self._loop_task.done():
            log.debug("MQTT: connect called but loop already running")
            return

        self._stop_event.clear()
        self._loop_task = asyncio.create_task(self._run_loop())
        log.info("MQTT: connection loop started")

    async def disconnect(self) -> None:
        """Stop the MQTT connection loop and clean up."""
        self._stop_event.set()
        self._cancel_watchdog()

        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

        self._state_store.mqtt_connected = False
        log.info("MQTT: disconnected")

    def reconfigure(self, config: dict) -> None:
        """Update battery serial from config. Triggers reconnect if changed."""
        new_serial = config.get("beem_battery_serial")
        if new_serial and new_serial != self._battery_serial:
            self._battery_serial = new_serial
            self._topic = f"battery/{new_serial}/sys/streaming"
            log.info("MQTT: battery serial changed, reconnecting")
            # Cancel the current loop; it will not restart because we
            # immediately start a new one.
            if self._loop_task is not None and not self._loop_task.done():
                self._loop_task.cancel()
            self._stop_event.clear()
            self._topic = f"battery/{new_serial.upper()}/sys/streaming"
            self._loop_task = asyncio.create_task(self._run_loop())

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Main MQTT loop with reconnection and exponential backoff."""
        while not self._stop_event.is_set():
            try:
                user_id = self._api_client.user_id
                if not user_id:
                    log.error("MQTT: no user_id available, retrying in %ds", self._backoff)
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, RECONNECT_MAX_SECONDS)
                    continue

                # clientId must match between the token request and the MQTT
                # connection — iOS format: beemapp-{userId}-{timestamp_ms}
                client_id = f"beemapp-{user_id}-{int(time.time() * 1000)}"
                mqtt_token = await self._api_client.get_mqtt_token(client_id)

                if not mqtt_token:
                    log.error("MQTT: failed to obtain token, retrying in %ds", self._backoff)
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, RECONNECT_MAX_SECONDS)
                    continue

                async with aiomqtt.Client(
                    hostname=MQTT_HOST,
                    port=MQTT_PORT,
                    transport="websockets",
                    websocket_path=MQTT_PATH,
                    username="unused",
                    password=mqtt_token,
                    tls_params=aiomqtt.TLSParameters(),
                    keepalive=60,
                    identifier=client_id,
                ) as client:
                    self._state_store.mqtt_connected = True
                    self._backoff = RECONNECT_MIN_SECONDS
                    self._event_bus.publish(Event.MQTT_CONNECTED)
                    self._cancel_watchdog()

                    await client.subscribe(self._topic)
                    log.info("MQTT: connected — subscribed to %s", self._topic)

                    refresh_task = asyncio.create_task(self._token_refresh_timer())
                    try:
                        async for message in client.messages:
                            self._handle_message(message)
                    finally:
                        refresh_task.cancel()
                        try:
                            await refresh_task
                        except asyncio.CancelledError:
                            pass

            except _TokenExpired:
                log.info("MQTT: token expired, reconnecting with fresh token")
            except aiomqtt.MqttError as exc:
                log.warning("MQTT: connection error: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("MQTT: unexpected error")

            # We are now disconnected.
            self._state_store.mqtt_connected = False
            self._event_bus.publish(Event.MQTT_DISCONNECTED)
            self._start_watchdog()

            if not self._stop_event.is_set():
                log.info("MQTT: reconnecting in %ds", self._backoff)
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, RECONNECT_MAX_SECONDS)

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    def _handle_message(self, message: aiomqtt.Message) -> None:
        """Parse a streaming battery data message and update shared state."""
        try:
            payload = json.loads(message.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("MQTT: invalid JSON payload on %s", message.topic)
            return

        updates = {}
        for api_field, state_field in _FIELD_MAP.items():
            if api_field in payload:
                updates[state_field] = payload[api_field]

        if not updates:
            log.debug("MQTT: message had no recognised battery fields")
            return

        self._state_store.update_battery(**updates)
        self._event_bus.publish(Event.BATTERY_DATA_UPDATED)

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    async def _token_refresh_timer(self) -> None:
        """Sleep then raise _TokenExpired to force reconnection with a new token."""
        await asyncio.sleep(TOKEN_REFRESH_SECONDS)
        raise _TokenExpired

    # ------------------------------------------------------------------
    # Disconnect safety watchdog
    # ------------------------------------------------------------------

    def _start_watchdog(self) -> None:
        """Start a task that forces auto-mode if disconnected too long."""
        self._cancel_watchdog()
        self._watchdog_task = asyncio.create_task(self._watchdog_worker())
        log.debug(
            "MQTT: disconnect watchdog set for %d minutes",
            DISCONNECT_SAFETY_SECONDS // 60,
        )

    def _cancel_watchdog(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None

    async def _watchdog_worker(self) -> None:
        """After DISCONNECT_SAFETY_SECONDS, force the battery to auto mode."""
        await asyncio.sleep(DISCONNECT_SAFETY_SECONDS)

        if self._state_store.mqtt_connected:
            return

        log.warning(
            "MQTT: disconnected for >%d min — forcing battery to auto mode",
            DISCONNECT_SAFETY_SECONDS // 60,
        )
        self._event_bus.publish(
            Event.SAFETY_ALERT,
            {"reason": "mqtt_disconnect_timeout"},
        )

        try:
            await self._api_client.set_auto_mode()
        except Exception:
            log.exception("MQTT: failed to set auto mode via API")
