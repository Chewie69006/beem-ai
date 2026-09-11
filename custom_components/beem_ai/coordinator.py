"""DataUpdateCoordinator for BeemAI — orchestrates data collection modules."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from datetime import timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .beem_api import BeemApiClient
from .const import (
    CONF_API_BASE,
    CONF_BATTERY_ID,
    CONF_BATTERY_SERIAL,
    CONF_EMAIL,
    CONF_PASSWORD,
    DEFAULT_API_BASE,
    DEFAULT_TARIFF_DEFAULT_PRICE,
    DOMAIN,
    OPT_TARIFF_DEFAULT_PRICE,
    OPT_TARIFF_PERIODS_JSON,
    OPT_EV_CHARGER_MODE,
    OPT_EV_CHARGER_POWER,
    OPT_EV_CHARGER_STATUS,
    OPT_EV_CHARGER_TOGGLE,
    OPT_EV_REQUIRE_WATER_HEATER,
    OPT_EV_TARGET_SOC,
    OPT_EV_SOC_HYSTERESIS,
    OPT_WATER_HEATER_SWITCH,
    OPT_WH_CHARGE_POWER_THRESHOLD,
    OPT_WATER_HEATER_MODE,
    WH_MODE_DISABLED,
    OPT_WH_MIN_DURATION_S,
    OPT_WH_SOC_THRESHOLD,
    OPT_WH_SUSTAIN_S,
    OPT_WH_POWER_ENTITY,
    OPT_WH_FULLY_HEATED_THRESHOLD,
    DEFAULT_WH_FULLY_HEATED_THRESHOLD,
    DEFAULT_EV_CHARGER_MODE,
    DEFAULT_EV_REQUIRE_WATER_HEATER,
    DEFAULT_WATER_HEATER_MODE,
    DEFAULT_WH_MIN_DURATION_S,
    DEFAULT_WH_SUSTAIN_S,
)
from .consumption_analyzer import ConsumptionAnalyzer
from .mqtt_client import BeemMqttClient
from .state_store import StateStore
from .tariff_manager import TariffManager
from .ev_charger_controller import EvChargerController
from .water_heater_controller import WaterHeaterController

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=2)
# Mirrors water_heater_controller.MAX_CONSUMPTION_W — the coordinator
# owns the overload coordination (throttle EV, then cut WH after a
# short grace) so the two diverters don't act on stale views.
OVERLOAD_THRESHOLD_W = 7000
OVERLOAD_WH_FORCE_STOP_GRACE_S = 15.0


class BeemAICoordinator(DataUpdateCoordinator):
    """Orchestrates all BeemAI data collection modules."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self._entry = entry
        self._session: aiohttp.ClientSession | None = None
        self._unsub_listeners: list = []

        # Core
        self.state_store = StateStore()

        # Modules (created in async_setup)
        self._api_client: BeemApiClient | None = None
        self._mqtt_client: BeemMqttClient | None = None
        self._tariff: TariffManager | None = None
        self._consumption: ConsumptionAnalyzer | None = None
        self._water_heater: WaterHeaterController | None = None
        self._ev_charger: EvChargerController | None = None
        # Monotonic timestamp of the first tick observing overload
        # (cons ≥ OVERLOAD_THRESHOLD_W with positive import).  Cleared
        # when the overload clears.  Used to delay WH force-stop until
        # after the EV throttle has had a chance to bring us back.
        self._overload_started_at: float | None = None

        # Solar panel arrays fetched from Beem API
        self.panel_arrays: list[dict] = []

        # Water heater / EV charger configurable thresholds
        # (persisted via config entry options)
        options = entry.options
        self.wh_soc_threshold: float = float(
            options.get(OPT_WH_SOC_THRESHOLD, 95.0)
        )
        self.wh_charge_power_threshold: float = float(
            options.get(OPT_WH_CHARGE_POWER_THRESHOLD, 500.0)
        )
        self.ev_target_soc: float = float(
            options.get(OPT_EV_TARGET_SOC, 95.0)
        )
        self.ev_soc_hysteresis: float = float(
            options.get(OPT_EV_SOC_HYSTERESIS, 5.0)
        )
        self.ev_charger_mode: str = str(
            options.get(OPT_EV_CHARGER_MODE, DEFAULT_EV_CHARGER_MODE)
        )
        self.wh_min_duration_s: int = int(
            options.get(OPT_WH_MIN_DURATION_S, DEFAULT_WH_MIN_DURATION_S)
        )
        self.wh_sustain_s: int = int(
            options.get(OPT_WH_SUSTAIN_S, DEFAULT_WH_SUSTAIN_S)
        )
        self.ev_require_water_heater: bool = bool(
            options.get(
                OPT_EV_REQUIRE_WATER_HEATER, DEFAULT_EV_REQUIRE_WATER_HEATER
            )
        )
        self.water_heater_mode: str = str(
            options.get(OPT_WATER_HEATER_MODE, DEFAULT_WATER_HEATER_MODE)
        )
        self.wh_power_entity: str = str(
            options.get(OPT_WH_POWER_ENTITY, "")
        )
        self.wh_fully_heated_threshold: float = float(
            options.get(
                OPT_WH_FULLY_HEATED_THRESHOLD,
                DEFAULT_WH_FULLY_HEATED_THRESHOLD,
            )
        )

        # Schedule handles
        self._daily_reset_unsub = None
        self._last_reset_date = None

        # Persistence directory (set in async_setup)
        self._data_dir: str | None = None
        self._file_log_handler: logging.Handler | None = None

    @property
    def water_heater(self) -> WaterHeaterController | None:
        """Return the water heater controller (if configured)."""
        return self._water_heater

    @property
    def ev_charger(self) -> EvChargerController | None:
        """Return the EV charger controller (if configured)."""
        return self._ev_charger

    async def async_setup(self) -> None:
        """Create all modules, log in, start MQTT, schedule tasks."""
        data = self._entry.data
        options = self._entry.options

        # Data directory for persistence
        data_dir = self.hass.config.path("beem_ai_data")
        os.makedirs(data_dir, exist_ok=True)
        self._data_dir = data_dir

        # Set up persistent log file
        self._setup_file_logging(data_dir)

        # HTTP session
        self._session = aiohttp.ClientSession()

        # REST client
        self._api_client = BeemApiClient(
            session=self._session,
            api_base=data.get(CONF_API_BASE, DEFAULT_API_BASE),
            username=data[CONF_EMAIL],
            password=data[CONF_PASSWORD],
            battery_id=data[CONF_BATTERY_ID],
            state_store=self.state_store,
        )
        await self._api_client.login()

        # Fetch solar equipment config and current control parameters from API
        self.panel_arrays = await self._fetch_panel_arrays()
        await self._refresh_control_params()

        # MQTT client
        self._mqtt_client = BeemMqttClient(
            api_client=self._api_client,
            battery_serial=data[CONF_BATTERY_SERIAL],
            state_store=self.state_store,
            on_update=self._on_battery_update,
        )

        # Tariff manager
        tariff_periods = self._parse_tariff_periods(options)
        self._tariff = TariffManager(
            default_price=options.get(OPT_TARIFF_DEFAULT_PRICE, DEFAULT_TARIFF_DEFAULT_PRICE),
            periods=tariff_periods,
        )

        # Analytics
        self._consumption = ConsumptionAnalyzer(data_dir=data_dir)
        self._consumption.load()

        # Bootstrap consumption if no learned data yet
        if not self._consumption.has_learned_data():
            await self._bootstrap_consumption()

        # Water heater controller (optional)
        self._setup_water_heater(options)

        # EV charger controller (optional, second-priority after water heater)
        self._setup_ev_charger(options)

        # Start MQTT (connect() is synchronous — it creates a background task)
        _LOGGER.info("Starting MQTT client")
        self._mqtt_client.connect()

        # Schedule recurring tasks
        _LOGGER.info("Scheduling recurring tasks (daily reset)")
        self._schedule_tasks()

        _LOGGER.info("BeemAI coordinator setup complete")

    def _setup_file_logging(self, data_dir: str) -> None:
        """Add a RotatingFileHandler to the beem_ai logger for persistent logs."""
        log_path = os.path.join(data_dir, "beem_ai.log")
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3,
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        # Attach to the package-level logger so all beem_ai modules log to file
        pkg_logger = logging.getLogger("custom_components.beem_ai")
        # Allow DEBUG through to our handler (HA's handlers filter independently)
        pkg_logger.setLevel(logging.DEBUG)
        # Avoid adding duplicate handlers on config entry reload
        if not any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            and getattr(h, "baseFilename", None) == handler.baseFilename
            for h in pkg_logger.handlers
        ):
            pkg_logger.addHandler(handler)
        self._file_log_handler = handler
        _LOGGER.info("Persistent log file: %s (5 MB × 3 backups)", log_path)

    @staticmethod
    def _parse_tariff_periods(options: dict) -> list[dict] | None:
        """Parse tariff periods from options JSON."""
        raw = options.get(OPT_TARIFF_PERIODS_JSON, "")
        if raw:
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
        return None

    async def _fetch_panel_arrays(self) -> list[dict]:
        """Fetch solar equipment from Beem API and convert to internal format."""
        equipments = await self._api_client.get_solar_equipments()
        if not equipments:
            _LOGGER.warning("No solar equipments from API, using single default array")
            return [{"tilt": 30, "azimuth": 180, "kwp": 5.0}]

        arrays = []
        for eq in equipments:
            arrays.append({
                "tilt": eq.get("tilt", 30),
                "azimuth": eq.get("orientation", 180),
                "kwp": eq.get("peakPower", 5000) / 1000.0,
                "mppt_id": eq.get("mpptId"),
                "panels_in_series": eq.get("solarPanelsInSeries"),
                "panels_in_parallel": eq.get("solarPanelsInParallel"),
            })

        _LOGGER.info("Fetched %d solar array(s) from API: %s", len(arrays), arrays)
        return arrays

    async def _bootstrap_consumption(self) -> None:
        """Seed consumption analyzer from Beem API history on fresh install."""
        _LOGGER.info("No learned consumption data — bootstrapping from API history")
        try:
            raw = await self._api_client.get_consumption_history(days=30)
            if not raw:
                _LOGGER.warning("No consumption history returned from API")
                return

            # Group by (weekday, hour)
            history: dict[tuple[int, int], list[float]] = {}
            for ts, watts in raw:
                key = (ts.weekday(), ts.hour)
                history.setdefault(key, []).append(watts)

            count = self._consumption.seed_from_history(history)
            self._consumption.save()
            _LOGGER.info(
                "Bootstrapped consumption from %d data points over 30 days",
                count,
            )
        except Exception:
            _LOGGER.exception("Failed to bootstrap consumption from API history")

    def _schedule_tasks(self) -> None:
        """Set up recurring schedules."""
        # Daily reset at midnight — use time interval and check hour
        self._daily_reset_unsub = async_track_time_interval(
            self.hass,
            self._check_daily_reset,
            timedelta(minutes=1),
        )

    # ---- Coordinator update ----

    async def _async_update_data(self):
        """Periodic catch-up refresh (every 2 min)."""
        # Refresh control parameters from API each cycle
        await self._refresh_control_params()
        return {
            "battery_soc": self.state_store.battery.soc,
            "mqtt_connected": self.state_store.mqtt_connected,
            "enabled": self.state_store.enabled,
        }

    # ---- Event handlers ----

    def _setup_water_heater(self, options: dict) -> None:
        """Create, reconfigure, or destroy the water heater controller.

        Reconfigures the existing controller in place when one already
        exists — recreating it on every options update (e.g. a mode
        switch, which also writes to config entry options) would wipe
        its in-memory daily energy accumulator and fully-heated flag.
        """
        switch_id = options.get(OPT_WATER_HEATER_SWITCH, "")
        if self._water_heater is not None:
            if switch_id:
                self._water_heater.reconfigure(switch_id)
            else:
                self._water_heater = None
        elif switch_id:
            self._water_heater = WaterHeaterController(
                hass=self.hass,
                switch_entity_id=switch_id,
            )
            _LOGGER.info(
                "Water heater controller configured: switch=%s",
                switch_id,
            )

    def _setup_ev_charger(self, options: dict) -> None:
        """Create or destroy the EV charger controller based on options."""
        toggle_id = options.get(OPT_EV_CHARGER_TOGGLE, "")
        power_id = options.get(OPT_EV_CHARGER_POWER, "")
        status_id = options.get(OPT_EV_CHARGER_STATUS, "") or None
        if toggle_id and power_id:
            self._ev_charger = EvChargerController(
                hass=self.hass,
                toggle_entity_id=toggle_id,
                power_entity_id=power_id,
                status_entity_id=status_id,
            )
            _LOGGER.info(
                "EV charger controller configured: toggle=%s, power=%s, status=%s",
                toggle_id, power_id, status_id,
            )
        else:
            self._ev_charger = None

    def _on_battery_update(self):
        """Handle battery data update from MQTT."""
        # Record consumption
        if self._consumption:
            self._consumption.record_consumption(
                self.state_store.battery.consumption_w
            )
        # Evaluate surplus diverters (water heater first, then EV charger)
        if self._water_heater or self._ev_charger:
            soc = self.state_store.battery.soc
            export_w = self.state_store.battery.export_power_w
            self.hass.async_create_task(
                self._evaluate_surplus_diverters(soc, export_w)
            )
        # Trigger entity updates (without logging noise)
        self.async_update_listeners()

    async def _evaluate_surplus_diverters(
        self, soc: float, export_w: float
    ) -> None:
        """Evaluate water heater then EV charger sequentially.

        No-op while the system is disabled: the Enabled switch means
        "BeemAI sends no commands to the water heater or the EV
        charger", so the overload trim, the amperage regulation and
        the start/stop rules all stay out of the way until it is
        turned back on.
        """
        if not self.state_store.enabled:
            _LOGGER.debug(
                "Surplus diverters skipped — BeemAI is disabled",
            )
            return

        battery = self.state_store.battery
        consumption_w = battery.consumption_w
        import_w = battery.import_power_w

        await self._handle_overload(consumption_w, import_w)

        if self._water_heater:
            await self._water_heater.evaluate(
                soc,
                export_w=export_w,
                charge_power_w=battery.battery_power_w,
                consumption_w=consumption_w,
                import_w=import_w,
                soc_threshold=self.wh_soc_threshold,
                charge_power_threshold=self.wh_charge_power_threshold,
                sustain_seconds=self.wh_sustain_s,
                min_duration_s=self.wh_min_duration_s,
                mode=self.water_heater_mode,
                power_entity_id=self.wh_power_entity or None,
                fully_heated_threshold_wh=self.wh_fully_heated_threshold,
            )
        # Note: the EV controller's per-tick overload reduction still
        # runs inside its evaluate() — that's the actual amps-trim
        # action.  The coordinator's role here is purely to time the
        # WH force-stop once the EV intervention has failed to bring
        # consumption back under the threshold.
        if self._ev_charger:
            # wh_heating=None means "no prerequisite".  Pass None when:
            #   - no WH configured, or
            #   - the Require-WH option is off, or
            #   - WH mode is Disabled (the user explicitly told the WH
            #     not to run — forcing the EV to wait on it would
            #     deadlock the whole diverter chain).
            wh_disabled = self.water_heater_mode == WH_MODE_DISABLED
            if (
                self._water_heater
                and self.ev_require_water_heater
                and not wh_disabled
            ):
                wh_heating = self._water_heater.is_heating
            else:
                wh_heating = None
            await self._ev_charger.evaluate(
                soc,
                meter_power_w=battery.meter_power_w,
                battery_power_w=battery.battery_power_w,
                solar_power_w=battery.solar_power_w,
                consumption_w=consumption_w,
                water_heater_heating=wh_heating,
                target_soc=self.ev_target_soc,
                soc_hysteresis=self.ev_soc_hysteresis,
                mode=self.ev_charger_mode,
            )

    async def _handle_overload(
        self, consumption_w: float, import_w: float
    ) -> None:
        """Coordinate diverters when the house is over the breaker limit.

        The EV controller's evaluate() trims its own amps once we reach
        OVERLOAD_THRESHOLD_W with positive import.  This method is the
        slower second stage: if consumption stays over the threshold
        for OVERLOAD_WH_FORCE_STOP_GRACE_S (typically because the EV
        already hit its 6A floor or wasn't drawing much to begin with),
        we force-stop the water heater — bypassing its min-duration
        engagement.
        """
        now = time.monotonic()
        overloaded = (
            consumption_w >= OVERLOAD_THRESHOLD_W and import_w > 0
        )
        if not overloaded:
            if self._overload_started_at is not None:
                _LOGGER.info(
                    "Overload cleared (cons=%.0fW, import=%.0fW)",
                    consumption_w, import_w,
                )
            self._overload_started_at = None
            return

        if self._overload_started_at is None:
            self._overload_started_at = now
            _LOGGER.warning(
                "Overload detected (cons=%.0fW, import=%.0fW) — "
                "EV will throttle this tick; WH force-stop in %.0fs "
                "if not resolved",
                consumption_w, import_w, OVERLOAD_WH_FORCE_STOP_GRACE_S,
            )
            return

        sustained = now - self._overload_started_at
        if (
            sustained >= OVERLOAD_WH_FORCE_STOP_GRACE_S
            and self._water_heater is not None
            and self._water_heater.is_heating
        ):
            _LOGGER.warning(
                "Overload sustained %.0fs (cons=%.0fW, import=%.0fW) — "
                "force-stopping water heater",
                sustained, consumption_w, import_w,
            )
            await self._water_heater.force_stop_overload(consumption_w)
            # Don't reset the timer — if we're still overloaded next
            # tick (e.g., big external load), the EV controller will
            # keep trimming and we'll log nothing new from here.

    # ---- Scheduled callbacks ----

    async def _check_daily_reset(self, _now=None) -> None:
        """Check if it's the daily reset hour (start of cheapest tariff period, rounded up)."""
        from datetime import datetime
        now = datetime.now()
        reset_hour = self._tariff.get_daily_reset_hour() if self._tariff else 0
        if now.hour == reset_hour and now.minute == 0:
            today = now.date()
            if self._last_reset_date != today:
                self._last_reset_date = today
                await self._daily_reset()

    async def _daily_reset(self) -> None:
        """Daily reset: persist analytics, reset water heater accumulators."""
        if self._consumption:
            self._consumption.save()

        if self._water_heater:
            self._water_heater.reset_daily()

        _LOGGER.info("Daily reset complete")

    # ---- Pre-optimization API refresh (#9) ----

    async def async_refresh_battery_from_api(self) -> bool:
        """Refresh battery state from REST API before optimization.

        Call this immediately before running optimization to ensure
        the state store has fresh data even if MQTT is stale.
        Returns True if the state store was updated.
        """
        if not self._api_client:
            return False

        data = await self._api_client.get_battery_state()
        if not data:
            _LOGGER.warning("API refresh: no battery state returned from REST API")
            return False

        # Map API fields to state store fields
        field_map = {
            "soc": "soc",
            "solarPower": "solar_power_w",
            "batteryPower": "battery_power_w",
            "meterPower": "meter_power_w",
            "inverterPower": "inverter_power_w",
            "globalSoh": "soh",
        }

        updates = {}
        for api_field, store_field in field_map.items():
            if api_field in data and data[api_field] is not None:
                updates[store_field] = float(data[api_field])

        if not updates:
            _LOGGER.info("API refresh: no state fields in API response")
            return False

        # Log discrepancy between MQTT and API values
        mqtt_soc = self.state_store.battery.soc
        api_soc = updates.get("soc")
        if api_soc is not None and abs(api_soc - mqtt_soc) > 2.0:
            _LOGGER.warning(
                "API refresh: SoC discrepancy — MQTT=%.1f%%, API=%.1f%% "
                "(diff=%.1f%%, MQTT data may be stale)",
                mqtt_soc, api_soc, abs(api_soc - mqtt_soc),
            )

        self.state_store.update_battery(**updates)
        _LOGGER.info(
            "API refresh: updated battery state from REST API: %s", updates
        )
        return True

    # ---- Options update ----

    async def async_options_updated(self, options: dict) -> None:
        """Reconfigure tariff module when options change."""
        _LOGGER.info("Options changed — reconfiguring modules")

        config = dict(options)
        # Ensure tariff periods JSON is passed through to reconfigure
        tariff_periods = self._parse_tariff_periods(options)
        if tariff_periods is not None:
            config["tariff_periods_json"] = options.get(OPT_TARIFF_PERIODS_JSON, "")

        if self._tariff:
            self._tariff.reconfigure(config)

        # Reconfigure water heater and EV charger controllers.  The new
        # controllers read the live entity state on every evaluate, so no
        # explicit resync is needed after recreation — they pick up the
        # current physical state of the switch automatically.
        self._setup_water_heater(options)
        self._setup_ev_charger(options)

        # Refresh persisted thresholds
        self.wh_soc_threshold = float(
            options.get(OPT_WH_SOC_THRESHOLD, 95.0)
        )
        self.wh_charge_power_threshold = float(
            options.get(OPT_WH_CHARGE_POWER_THRESHOLD, 500.0)
        )
        self.ev_target_soc = float(
            options.get(OPT_EV_TARGET_SOC, 95.0)
        )
        self.ev_soc_hysteresis = float(
            options.get(OPT_EV_SOC_HYSTERESIS, 5.0)
        )
        self.ev_charger_mode = str(
            options.get(OPT_EV_CHARGER_MODE, DEFAULT_EV_CHARGER_MODE)
        )
        self.wh_min_duration_s = int(
            options.get(OPT_WH_MIN_DURATION_S, DEFAULT_WH_MIN_DURATION_S)
        )
        self.wh_sustain_s = int(
            options.get(OPT_WH_SUSTAIN_S, DEFAULT_WH_SUSTAIN_S)
        )
        self.ev_require_water_heater = bool(
            options.get(
                OPT_EV_REQUIRE_WATER_HEATER, DEFAULT_EV_REQUIRE_WATER_HEATER
            )
        )
        self.water_heater_mode = str(
            options.get(OPT_WATER_HEATER_MODE, DEFAULT_WATER_HEATER_MODE)
        )
        self.wh_power_entity = str(
            options.get(OPT_WH_POWER_ENTITY, "")
        )
        self.wh_fully_heated_threshold = float(
            options.get(
                OPT_WH_FULLY_HEATED_THRESHOLD,
                DEFAULT_WH_FULLY_HEATED_THRESHOLD,
            )
        )

    # ---- EV charger mode control ----

    async def async_set_ev_charger_mode(self, mode: str) -> None:
        """Change the EV charger mode and apply the immediate side effects.

        The mode is always recorded, but while BeemAI is disabled it
        stays inert — no command reaches the charger until the system
        is enabled again.
        """
        self.ev_charger_mode = mode
        if self._ev_charger:
            if self.state_store.enabled:
                await self._ev_charger.handle_mode_change(mode)
            else:
                _LOGGER.info(
                    "EV charger mode set to %s while BeemAI is disabled "
                    "— stored, no command sent",
                    mode,
                )
            self.async_update_listeners()

    # ---- Water heater mode control ----

    async def async_set_water_heater_mode(self, mode: str) -> None:
        """Change the water heater mode and apply the immediate side effects.

        As with the EV charger, the mode is recorded but inert while
        BeemAI is disabled.
        """
        self.water_heater_mode = mode
        if self._water_heater:
            if self.state_store.enabled:
                await self._water_heater.handle_mode_change(mode)
            else:
                _LOGGER.info(
                    "Water heater mode set to %s while BeemAI is disabled "
                    "— stored, no command sent",
                    mode,
                )
            self.async_update_listeners()

    # ---- Enable/disable ----

    async def async_set_enabled(self, enabled: bool) -> None:
        """Toggle the system on/off.

        Disabling is a full stand-down, not just a pause of the
        decision loop: every controller hands its device back to the
        user (see ``_release_device_control``) so the EV charger can be
        driven from the Wallbox app without BeemAI clamping amps or
        enforcing the 7 kW ceiling.  Re-enabling resumes on the next
        MQTT tick, adopting whatever state the devices are in.
        """
        if self.state_store.enabled == enabled:
            return

        self.state_store.enabled = enabled
        if enabled:
            _LOGGER.info("BeemAI enabled by user — control resumes")
        else:
            _LOGGER.info(
                "BeemAI disabled by user — releasing device control",
            )
            await self._release_device_control()
        self.async_update_listeners()

    async def _release_device_control(self) -> None:
        """Stand down: let each controller hand its device back."""
        self._overload_started_at = None
        if self._ev_charger:
            await self._ev_charger.release_control()
        if self._water_heater:
            await self._water_heater.release_control()

    # ---- Control parameter refresh ----

    async def _refresh_control_params(self) -> None:
        """Fetch control parameters from API and update local state."""
        if not self._api_client:
            return

        data = await self._api_client.get_control_parameters()
        if not data:
            return

        # Map camelCase API response → snake_case ControlState fields
        field_map = {
            "mode": "mode",
            "allowChargeFromGrid": "allow_charge_from_grid",
            "preventDischarge": "prevent_discharge",
            "chargeFromGridMaxPower": "charge_from_grid_max_power",
            "minSoc": "min_soc",
            "maxSoc": "max_soc",
            "canChangeMode": "can_change_mode",
        }

        updates = {}
        for api_key, store_key in field_map.items():
            if api_key in data and data[api_key] is not None:
                updates[store_key] = data[api_key]

        if updates:
            self.state_store.update_control(**updates)
            _LOGGER.info("Control params refreshed from API: %s", updates)

    # ---- Battery control ----

    async def async_set_battery_control(self, **kwargs) -> bool:
        """Update battery control parameters via the Beem API.

        Accepts any subset of: mode, allow_charge_from_grid, prevent_discharge,
        charge_from_grid_max_power, min_soc, max_soc.  Only changed fields are
        sent; after a successful PATCH, re-fetches from API (source of truth).
        """
        if not self._api_client:
            return False

        # Map snake_case kwargs → camelCase API params
        key_map = {
            "mode": "mode",
            "allow_charge_from_grid": "allowChargeFromGrid",
            "prevent_discharge": "preventDischarge",
            "charge_from_grid_max_power": "chargeFromGridMaxPower",
            "min_soc": "minSoc",
            "max_soc": "maxSoc",
        }

        params = {}
        for local_key, api_key in key_map.items():
            if local_key in kwargs:
                params[api_key] = kwargs[local_key]

        if not params:
            return True

        success = await self._api_client.set_control_parameters(params)
        if success:
            # Re-fetch from API — it is the source of truth.
            await self._refresh_control_params()
            self.async_update_listeners()
            _LOGGER.info("Battery control updated: %s", kwargs)
        else:
            _LOGGER.warning("Failed to set battery control: %s", kwargs)
        return success

    # ---- Shutdown ----

    async def async_shutdown(self) -> None:
        """Clean shutdown of all modules."""
        _LOGGER.info("BeemAI shutting down...")

        # Cancel scheduled listeners
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()

        if self._daily_reset_unsub:
            self._daily_reset_unsub()

        # Turn off the loads we may be driving — but only while we're
        # actually in charge.  A disabled BeemAI has already stood down
        # and must not touch the charger or the heater on the way out.
        if self.state_store.enabled:
            # EV charger first (before water heater)
            if self._ev_charger and self._ev_charger.is_charging:
                await self._ev_charger.stop()

            if self._water_heater and self._water_heater.is_heating:
                await self._water_heater._turn_off()
                self._water_heater._clear_session()
        else:
            _LOGGER.info(
                "Shutdown while disabled — leaving EV charger and water "
                "heater untouched",
            )

        # Stop MQTT
        if self._mqtt_client:
            await self._mqtt_client.disconnect()

        # Save analytics
        if self._consumption:
            self._consumption.save()

        # Close API client
        if self._api_client:
            await self._api_client.shutdown()

        # Close HTTP session
        if self._session:
            await self._session.close()

        # Remove file log handler
        if self._file_log_handler:
            pkg_logger = logging.getLogger("custom_components.beem_ai")
            pkg_logger.removeHandler(self._file_log_handler)
            self._file_log_handler.close()

        _LOGGER.info("BeemAI shutdown complete")
