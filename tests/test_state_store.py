"""Tests for BeemAI StateStore and BatteryState."""

import threading
from datetime import datetime

from custom_components.beem_ai.state_store import BatteryState, ControlState, StateStore


# ── BatteryState property tests ─────────────────────────────────────


class TestBatteryStateProperties:
    def test_is_charging_positive_power(self):
        b = BatteryState(battery_power_w=500.0)
        assert b.is_charging is True

    def test_is_charging_zero_power(self):
        b = BatteryState(battery_power_w=0.0)
        assert b.is_charging is False

    def test_is_charging_negative_power(self):
        b = BatteryState(battery_power_w=-500.0)
        assert b.is_charging is False

    def test_is_discharging_negative_power(self):
        b = BatteryState(battery_power_w=-200.0)
        assert b.is_discharging is True

    def test_is_discharging_zero_power(self):
        b = BatteryState(battery_power_w=0.0)
        assert b.is_discharging is False

    def test_is_discharging_positive_power(self):
        b = BatteryState(battery_power_w=200.0)
        assert b.is_discharging is False

    def test_is_importing_positive_meter(self):
        b = BatteryState(meter_power_w=1000.0)
        assert b.is_importing is True

    def test_is_importing_zero_meter(self):
        b = BatteryState(meter_power_w=0.0)
        assert b.is_importing is False

    def test_is_exporting_negative_meter(self):
        b = BatteryState(meter_power_w=-500.0)
        assert b.is_exporting is True

    def test_is_exporting_zero_meter(self):
        b = BatteryState(meter_power_w=0.0)
        assert b.is_exporting is False

    def test_export_power_w_when_exporting(self):
        b = BatteryState(meter_power_w=-750.0)
        assert b.export_power_w == 750.0

    def test_export_power_w_when_importing(self):
        b = BatteryState(meter_power_w=300.0)
        assert b.export_power_w == 0.0

    def test_import_power_w_when_importing(self):
        b = BatteryState(meter_power_w=1200.0)
        assert b.import_power_w == 1200.0

    def test_import_power_w_when_exporting(self):
        b = BatteryState(meter_power_w=-400.0)
        assert b.import_power_w == 0.0

    def test_consumption_w_solar_plus_import(self):
        # solar 2000W + importing 500W, battery charging 100W
        # consumption = 2000 + 500 - 100 = 2400W
        b = BatteryState(solar_power_w=2000.0, meter_power_w=500.0, battery_power_w=100.0)
        assert b.consumption_w == 2400.0

    def test_consumption_w_with_discharge(self):
        # solar 1000W + no grid + discharge 500W (battery_power_w=-500)
        # consumption = 1000 + 0 - (-500) = 1500W
        b = BatteryState(solar_power_w=1000.0, meter_power_w=0.0, battery_power_w=-500.0)
        assert b.consumption_w == 1500.0

    def test_consumption_w_solar_charging_battery(self):
        # solar 3000W, battery charging 1500W, no grid
        # consumption = 3000 + 0 - 1500 = 1500W
        b = BatteryState(solar_power_w=3000.0, meter_power_w=0.0, battery_power_w=1500.0)
        assert b.consumption_w == 1500.0

    def test_consumption_w_solar_exporting(self):
        # solar 2000W, exporting 500W to grid (meter=-500), no battery
        # consumption = 2000 + (-500) - 0 = 1500W
        b = BatteryState(solar_power_w=2000.0, meter_power_w=-500.0, battery_power_w=0.0)
        assert b.consumption_w == 1500.0

    def test_consumption_w_clamped_to_zero(self):
        # Measurement noise could yield negative — clamp to 0
        b = BatteryState(solar_power_w=0.0, meter_power_w=100.0, battery_power_w=200.0)
        assert b.consumption_w == 0.0

    def test_consumption_w_all_zero(self):
        b = BatteryState()
        assert b.consumption_w == 0.0


# ── StateStore update_battery ────────────────────────────────────────


class TestUpdateBattery:
    def test_update_battery_sets_fields(self, state_store):
        state_store.update_battery(soc=85.0, solar_power_w=3000.0)
        assert state_store.battery.soc == 85.0
        assert state_store.battery.solar_power_w == 3000.0

    def test_update_battery_sets_last_updated(self, state_store):
        before = datetime.now()
        state_store.update_battery(soc=50.0)
        after = datetime.now()
        assert state_store.battery.last_updated is not None
        assert before <= state_store.battery.last_updated <= after

    def test_update_battery_ignores_unknown_fields(self, state_store):
        # Should not raise
        state_store.update_battery(nonexistent_field=42, soc=70.0)
        assert state_store.battery.soc == 70.0

    def test_update_battery_atomic_multiple_fields(self, state_store):
        state_store.update_battery(
            soc=90.0,
            battery_power_w=2500.0,
            meter_power_w=-1000.0,
            working_mode="charging",
        )
        b = state_store.battery
        assert b.soc == 90.0
        assert b.battery_power_w == 2500.0
        assert b.meter_power_w == -1000.0
        assert b.working_mode == "charging"


# ── StateStore update_forecast ───────────────────────────────────────


class TestUpdateForecast:
    def test_update_forecast_sets_fields(self, state_store):
        state_store.update_forecast(solar_today_kwh=15.5, confidence="high")
        assert state_store.forecast.solar_today_kwh == 15.5
        assert state_store.forecast.confidence == "high"

    def test_update_forecast_sets_last_updated(self, state_store):
        before = datetime.now()
        state_store.update_forecast(solar_today_kwh=10.0)
        after = datetime.now()
        assert state_store.forecast.last_updated is not None
        assert before <= state_store.forecast.last_updated <= after

    def test_update_forecast_with_dict_fields(self, state_store):
        hourly = {8: 500, 9: 1200, 10: 2000}
        state_store.update_forecast(solar_today=hourly)
        assert state_store.forecast.solar_today == hourly


# ── StateStore thread safety ─────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_battery_updates_no_crash(self, state_store):
        errors = []

        def updater(thread_id):
            try:
                for i in range(100):
                    state_store.update_battery(soc=float(i), solar_power_w=float(thread_id * 100))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=updater, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # soc should be a valid value set by one of the threads
        assert 0.0 <= state_store.battery.soc <= 99.0


# ── StateStore properties ────────────────────────────────────────────


class TestStateStoreProperties:
    def test_enabled_default_true(self, state_store):
        assert state_store.enabled is True

    def test_enabled_setter(self, state_store):
        state_store.enabled = False
        assert state_store.enabled is False

    def test_mqtt_connected_default_false(self, state_store):
        assert state_store.mqtt_connected is False

    def test_mqtt_connected_setter(self, state_store):
        state_store.mqtt_connected = True
        assert state_store.mqtt_connected is True

    def test_rest_available_default_true(self, state_store):
        assert state_store.rest_available is True

    def test_rest_available_setter(self, state_store):
        state_store.rest_available = False
        assert state_store.rest_available is False


# ── ControlState ───────────────────────────────────────────────────


class TestControlState:
    def test_defaults(self):
        c = ControlState()
        assert c.mode == "auto"
        assert c.allow_charge_from_grid is False
        assert c.prevent_discharge is False
        assert c.charge_from_grid_max_power == 0
        assert c.min_soc == 20
        assert c.max_soc == 100

    def test_update_control_sets_fields(self, state_store):
        state_store.update_control(mode="advanced", min_soc=10)
        assert state_store.control.mode == "advanced"
        assert state_store.control.min_soc == 10

    def test_update_control_ignores_unknown(self, state_store):
        state_store.update_control(nonexistent=42, max_soc=90)
        assert state_store.control.max_soc == 90

    def test_control_property_returns_state(self, state_store):
        assert isinstance(state_store.control, ControlState)
