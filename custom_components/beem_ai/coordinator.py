"""DataUpdateCoordinator for BeemAI — orchestrates data collection modules."""

from __future__ import annotations

import json
import logging
import os
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
    OPT_LOCATION_LAT,
    OPT_LOCATION_LON,
    OPT_SOLCAST_API_KEY,
    OPT_SOLCAST_SITE_ID,
    OPT_TARIFF_DEFAULT_PRICE,
    OPT_TARIFF_PERIODS_JSON,
)
from .consumption_analyzer import ConsumptionAnalyzer
from .forecast_tracker import ForecastTracker
from .forecasting.forecast_solar import ForecastSolarSource
from .forecasting.open_meteo import OpenMeteoSource
from .forecasting.solar_forecast import SolarForecast
from .forecasting.solcast import SolcastSource
from .mqtt_client import BeemMqttClient
from .state_store import StateStore
from .tariff_manager import TariffManager

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=2)
FORECAST_INTERVAL = timedelta(hours=1)


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
        self._forecast: SolarForecast | None = None
        self._consumption: ConsumptionAnalyzer | None = None
        self._forecast_tracker: ForecastTracker | None = None

        # Solar panel arrays fetched from Beem API
        self.panel_arrays: list[dict] = []

        # Schedule handles
        self._daily_reset_unsub = None

        # Persistence directory (set in async_setup)
        self._data_dir: str | None = None

    async def async_setup(self) -> None:
        """Create all modules, log in, start MQTT, schedule tasks."""
        data = self._entry.data
        options = self._entry.options

        # Data directory for persistence
        data_dir = self.hass.config.path("beem_ai_data")
        os.makedirs(data_dir, exist_ok=True)
        self._data_dir = data_dir

        # Restore persisted state before anything else reads it
        _LOGGER.info("Loading persisted state from %s", data_dir)
        self.state_store.load_forecast(data_dir)

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

        # Fetch solar equipment config from API
        self.panel_arrays = await self._fetch_panel_arrays()

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

        # Forecasting
        sources = self._build_forecast_sources(options)
        self._forecast = SolarForecast(
            state_store=self.state_store,
            sources=sources,
        )

        # Analytics
        self._consumption = ConsumptionAnalyzer(data_dir=data_dir)
        self._consumption.load()

        # Bootstrap consumption if no learned data yet
        if not self._consumption.has_learned_data():
            await self._bootstrap_consumption()

        self._forecast_tracker = ForecastTracker(data_dir=data_dir)
        self._forecast_tracker.load()

        # Start MQTT (connect() is synchronous — it creates a background task)
        _LOGGER.info("Starting MQTT client")
        self._mqtt_client.connect()

        # Schedule recurring tasks
        _LOGGER.info("Scheduling recurring tasks (forecast refresh, daily reset)")
        self._schedule_tasks()

        # Initial forecast fetch
        await self._refresh_forecasts()

        _LOGGER.info("BeemAI coordinator setup complete")

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

    def _build_forecast_sources(self, options: dict) -> list:
        """Instantiate forecast sources from options."""
        # Use HA's configured location as fallback
        ha_lat = getattr(self.hass.config, 'latitude', 0.0)
        ha_lon = getattr(self.hass.config, 'longitude', 0.0)
        lat = float(options.get(OPT_LOCATION_LAT, 0) or ha_lat)
        lon = float(options.get(OPT_LOCATION_LON, 0) or ha_lon)
        panel_arrays = self.panel_arrays

        sources = [
            OpenMeteoSource(
                session=self._session, lat=lat, lon=lon, panel_arrays=panel_arrays
            ),
            ForecastSolarSource(
                session=self._session, lat=lat, lon=lon, panel_arrays=panel_arrays
            ),
        ]

        solcast_key = options.get(OPT_SOLCAST_API_KEY)
        solcast_site = options.get(OPT_SOLCAST_SITE_ID)
        if solcast_key and solcast_site:
            total_kwp = sum(a["kwp"] for a in panel_arrays)
            sources.append(SolcastSource(
                session=self._session,
                api_key=solcast_key,
                site_id=solcast_site,
                total_kwp=total_kwp,
            ))

        return sources

    def _schedule_tasks(self) -> None:
        """Set up recurring schedules."""
        # Forecast refresh every hour
        unsub = async_track_time_interval(
            self.hass,
            self._forecast_loop,
            FORECAST_INTERVAL,
        )
        self._unsub_listeners.append(unsub)

        # Daily reset at midnight — use time interval and check hour
        self._daily_reset_unsub = async_track_time_interval(
            self.hass,
            self._check_daily_reset,
            timedelta(minutes=1),
        )

    # ---- Coordinator update ----

    async def _async_update_data(self):
        """Periodic catch-up refresh (every 2 min)."""
        # This just triggers entity updates via coordinator pattern
        return {
            "battery_soc": self.state_store.battery.soc,
            "mqtt_connected": self.state_store.mqtt_connected,
            "enabled": self.state_store.enabled,
        }

    # ---- Event handlers ----

    def _on_battery_update(self):
        """Handle battery data update from MQTT."""
        # Record consumption
        if self._consumption:
            self._consumption.record_consumption(
                self.state_store.battery.consumption_w
            )
        # Trigger entity updates
        self.async_set_updated_data({
            "battery_soc": self.state_store.battery.soc,
            "mqtt_connected": self.state_store.mqtt_connected,
        })

    # ---- Scheduled callbacks ----

    async def _forecast_loop(self, _now=None) -> None:
        """Hourly forecast refresh."""
        _LOGGER.info("Hourly forecast refresh triggered")
        await self._refresh_forecasts()
        if self._data_dir:
            self.state_store.save_forecast(self._data_dir)

    _last_reset_date = None

    async def _check_daily_reset(self, _now=None) -> None:
        """Check if it's midnight for daily reset."""
        from datetime import datetime
        now = datetime.now()
        if now.hour == 0 and now.minute == 0:
            today = now.date()
            if self._last_reset_date != today:
                self._last_reset_date = today
                await self._daily_reset()

    async def _daily_reset(self) -> None:
        """Midnight reset of daily counters."""
        if self._consumption:
            self._consumption.save()
        if self._forecast_tracker:
            self._forecast_tracker.save()
        if self._data_dir:
            self.state_store.save_forecast(self._data_dir)
            _LOGGER.info("Persisted forecast state to disk")
        _LOGGER.info("Daily counters reset")

    async def _refresh_forecasts(self) -> None:
        """Refresh solar and consumption forecasts."""
        try:
            if self._forecast:
                await self._forecast.refresh()

            if self._consumption:
                today_kwh = self._consumption.get_forecast_kwh_today()
                tomorrow_kwh = self._consumption.get_forecast_kwh_tomorrow()
                hourly = self._consumption.get_hourly_consumption_forecast_tomorrow()
                self.state_store.update_forecast(
                    consumption_today_kwh=today_kwh,
                    consumption_tomorrow_kwh=tomorrow_kwh,
                    consumption_hourly=hourly,
                )

            f = self.state_store.forecast
            _LOGGER.info(
                "Forecasts updated: solar_today=%.1f kWh, solar_tomorrow=%.1f kWh, "
                "consumption_tomorrow=%.1f kWh, confidence=%s, sources=%s",
                f.solar_today_kwh, f.solar_tomorrow_kwh,
                f.consumption_tomorrow_kwh, f.confidence, f.sources_used,
            )
        except Exception:
            _LOGGER.exception("Failed to refresh forecasts")

    # ---- Options update ----

    async def async_options_updated(self, options: dict) -> None:
        """Reconfigure tariff and forecast modules when options change."""
        _LOGGER.info("Options changed — reconfiguring modules")

        config = dict(options)
        config["panel_arrays"] = self.panel_arrays
        # Ensure tariff periods JSON is passed through to reconfigure
        tariff_periods = self._parse_tariff_periods(options)
        if tariff_periods is not None:
            config["tariff_periods_json"] = options.get(OPT_TARIFF_PERIODS_JSON, "")

        if self._tariff:
            self._tariff.reconfigure(config)

        # Rebuild forecast sources with new config
        if self._forecast:
            self._forecast.reconfigure(config)

    # ---- Enable/disable ----

    async def async_set_enabled(self, enabled: bool) -> None:
        """Toggle the system on/off."""
        self.state_store.enabled = enabled
        if enabled:
            _LOGGER.info("BeemAI enabled by user")
        else:
            _LOGGER.info("BeemAI disabled by user")

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

        # Stop MQTT
        if self._mqtt_client:
            await self._mqtt_client.disconnect()

        # Save analytics and state
        if self._consumption:
            self._consumption.save()
        if self._forecast_tracker:
            self._forecast_tracker.save()
        if self._data_dir:
            self.state_store.save_forecast(self._data_dir)

        # Close API client
        if self._api_client:
            await self._api_client.shutdown()

        # Close HTTP session
        if self._session:
            await self._session.close()

        _LOGGER.info("BeemAI shutdown complete")
