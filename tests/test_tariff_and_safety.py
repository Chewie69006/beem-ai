"""Tests for BeemAI TariffManager and SafetyManager."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.beem_ai.tariff_manager import (
    TARIFF_HC,
    TARIFF_HP,
    TARIFF_HSC,
    TariffManager,
)
from custom_components.beem_ai.safety_manager import SafetyManager
from custom_components.beem_ai.event_bus import Event
from custom_components.beem_ai.state_store import CurrentPlan


# ── Shared fixtures ──────────────────────────────────────────────────


@pytest.fixture
def tariff():
    return TariffManager(hp_price=0.1841, hc_price=0.1470, hsc_price=0.1296)


@pytest.fixture
def safety(state_store, event_bus):
    return SafetyManager(state_store, event_bus)


# ── TariffManager: get_tariff_at ─────────────────────────────────────


class TestGetTariffAt:
    """Verify tariff classification at every boundary."""

    def _dt(self, hour, minute=0):
        return datetime(2025, 6, 15, hour, minute, 0)

    def test_midnight_is_hc(self, tariff):
        assert tariff.get_tariff_at(self._dt(0, 0)) == TARIFF_HC

    def test_0100_is_hc(self, tariff):
        assert tariff.get_tariff_at(self._dt(1, 0)) == TARIFF_HC

    def test_0159_is_hc(self, tariff):
        assert tariff.get_tariff_at(self._dt(1, 59)) == TARIFF_HC

    def test_0200_is_hsc(self, tariff):
        assert tariff.get_tariff_at(self._dt(2, 0)) == TARIFF_HSC

    def test_0359_is_hsc(self, tariff):
        assert tariff.get_tariff_at(self._dt(3, 59)) == TARIFF_HSC

    def test_0559_is_hsc(self, tariff):
        assert tariff.get_tariff_at(self._dt(5, 59)) == TARIFF_HSC

    def test_0600_is_hc(self, tariff):
        assert tariff.get_tariff_at(self._dt(6, 0)) == TARIFF_HC

    def test_0659_is_hc(self, tariff):
        assert tariff.get_tariff_at(self._dt(6, 59)) == TARIFF_HC

    def test_0700_is_hp(self, tariff):
        assert tariff.get_tariff_at(self._dt(7, 0)) == TARIFF_HP

    def test_1200_is_hp(self, tariff):
        assert tariff.get_tariff_at(self._dt(12, 0)) == TARIFF_HP

    def test_2259_is_hp(self, tariff):
        assert tariff.get_tariff_at(self._dt(22, 59)) == TARIFF_HP

    def test_2300_is_hc(self, tariff):
        assert tariff.get_tariff_at(self._dt(23, 0)) == TARIFF_HC

    def test_2359_is_hc(self, tariff):
        assert tariff.get_tariff_at(self._dt(23, 59)) == TARIFF_HC


# ── TariffManager: get_price_kwh ─────────────────────────────────────


class TestGetPriceKwh:
    def test_hp_price(self, tariff):
        assert tariff.get_price_kwh(TARIFF_HP) == 0.1841

    def test_hc_price(self, tariff):
        assert tariff.get_price_kwh(TARIFF_HC) == 0.1470

    def test_hsc_price(self, tariff):
        assert tariff.get_price_kwh(TARIFF_HSC) == 0.1296

    def test_current_tariff_price(self, tariff):
        # get_price_kwh with no argument should use current tariff
        price = tariff.get_price_kwh()
        assert price in (0.1841, 0.1470, 0.1296)


# ── TariffManager: get_windows_today ─────────────────────────────────


class TestGetWindowsToday:
    def test_windows_cover_full_day(self, tariff):
        windows = tariff.get_windows_today()
        assert len(windows) >= 4  # At least HC, HSC, HC, HP, HC

        # First window starts at midnight
        today = datetime.now().date()
        assert windows[0].start.date() == today
        assert windows[0].start.hour == 0
        assert windows[0].start.minute == 0

        # Last window ends at midnight next day
        assert windows[-1].end.date() == today + timedelta(days=1)

    def test_windows_are_contiguous(self, tariff):
        windows = tariff.get_windows_today()
        for i in range(len(windows) - 1):
            assert windows[i].end == windows[i + 1].start

    def test_correct_tariff_sequence(self, tariff):
        windows = tariff.get_windows_today()
        tariff_sequence = [w.tariff for w in windows]
        # Expected: HC(00-02), HSC(02-06), HC(06-07), HP(07-23), HC(23-00)
        assert tariff_sequence == [TARIFF_HC, TARIFF_HSC, TARIFF_HC, TARIFF_HP, TARIFF_HC]

    def test_each_window_has_price(self, tariff):
        windows = tariff.get_windows_today()
        for w in windows:
            assert w.price > 0


# ── TariffManager: next_hsc_window ───────────────────────────────────


class TestNextHscWindow:
    @patch("custom_components.beem_ai.tariff_manager.datetime")
    def test_before_hsc_returns_today(self, mock_dt, tariff):
        mock_dt.now.return_value = datetime(2025, 6, 15, 1, 0, 0)
        mock_dt.combine = datetime.combine
        start, end = tariff.next_hsc_window()
        assert start == datetime(2025, 6, 15, 2, 0, 0)
        assert end == datetime(2025, 6, 15, 6, 0, 0)

    @patch("custom_components.beem_ai.tariff_manager.datetime")
    def test_during_hsc_returns_today(self, mock_dt, tariff):
        mock_dt.now.return_value = datetime(2025, 6, 15, 3, 0, 0)
        mock_dt.combine = datetime.combine
        start, end = tariff.next_hsc_window()
        assert start == datetime(2025, 6, 15, 2, 0, 0)
        assert end == datetime(2025, 6, 15, 6, 0, 0)

    @patch("custom_components.beem_ai.tariff_manager.datetime")
    def test_after_hsc_returns_tomorrow(self, mock_dt, tariff):
        mock_dt.now.return_value = datetime(2025, 6, 15, 10, 0, 0)
        mock_dt.combine = datetime.combine
        start, end = tariff.next_hsc_window()
        assert start == datetime(2025, 6, 16, 2, 0, 0)
        assert end == datetime(2025, 6, 16, 6, 0, 0)

    @patch("custom_components.beem_ai.tariff_manager.datetime")
    def test_at_exact_hsc_end_returns_tomorrow(self, mock_dt, tariff):
        mock_dt.now.return_value = datetime(2025, 6, 15, 6, 0, 0)
        mock_dt.combine = datetime.combine
        start, end = tariff.next_hsc_window()
        assert start == datetime(2025, 6, 16, 2, 0, 0)


# ── TariffManager: next_hc_window ────────────────────────────────────


class TestNextHcWindow:
    @patch("custom_components.beem_ai.tariff_manager.datetime")
    def test_during_hp_returns_tonight(self, mock_dt, tariff):
        mock_dt.now.return_value = datetime(2025, 6, 15, 14, 0, 0)
        mock_dt.combine = datetime.combine
        start, end = tariff.next_hc_window()
        assert start == datetime(2025, 6, 15, 23, 0, 0)
        assert end == datetime(2025, 6, 16, 7, 0, 0)

    @patch("custom_components.beem_ai.tariff_manager.datetime")
    def test_during_hc_evening_returns_current(self, mock_dt, tariff):
        mock_dt.now.return_value = datetime(2025, 6, 15, 23, 30, 0)
        mock_dt.combine = datetime.combine
        start, end = tariff.next_hc_window()
        assert start == datetime(2025, 6, 15, 23, 0, 0)
        assert end == datetime(2025, 6, 16, 7, 0, 0)

    @patch("custom_components.beem_ai.tariff_manager.datetime")
    def test_early_morning_returns_current_block(self, mock_dt, tariff):
        mock_dt.now.return_value = datetime(2025, 6, 15, 4, 0, 0)
        mock_dt.combine = datetime.combine
        start, end = tariff.next_hc_window()
        assert start == datetime(2025, 6, 14, 23, 0, 0)
        assert end == datetime(2025, 6, 15, 7, 0, 0)


# ── TariffManager: calculate_savings_vs_hp ───────────────────────────


class TestCalculateSavings:
    def test_savings_hc_vs_hp(self, tariff):
        savings = tariff.calculate_savings_vs_hp(10.0, TARIFF_HC)
        expected = round(10.0 * (0.1841 - 0.1470), 4)
        assert savings == expected

    def test_savings_hsc_vs_hp(self, tariff):
        savings = tariff.calculate_savings_vs_hp(10.0, TARIFF_HSC)
        expected = round(10.0 * (0.1841 - 0.1296), 4)
        assert savings == expected

    def test_savings_hp_vs_hp_is_zero(self, tariff):
        assert tariff.calculate_savings_vs_hp(10.0, TARIFF_HP) == 0.0

    def test_savings_zero_kwh(self, tariff):
        assert tariff.calculate_savings_vs_hp(0.0, TARIFF_HSC) == 0.0


# ── TariffManager: hours_until_next_hp ───────────────────────────────


class TestHoursUntilNextHp:
    @patch("custom_components.beem_ai.tariff_manager.datetime")
    def test_during_hp_returns_zero(self, mock_dt, tariff):
        now = datetime(2025, 6, 15, 12, 0, 0)
        mock_dt.now.return_value = now
        mock_dt.combine = datetime.combine
        # get_tariff_at uses dt.time() on the passed datetime, not mock
        # We need to also make get_tariff_at work - it takes a real datetime
        # Since hours_until_next_hp calls get_tariff_at(now) and now is mocked:
        assert tariff.hours_until_next_hp() == 0.0

    @patch("custom_components.beem_ai.tariff_manager.datetime")
    def test_at_midnight_returns_7_hours(self, mock_dt, tariff):
        now = datetime(2025, 6, 15, 0, 0, 0)
        mock_dt.now.return_value = now
        mock_dt.combine = datetime.combine
        assert tariff.hours_until_next_hp() == 7.0

    @patch("custom_components.beem_ai.tariff_manager.datetime")
    def test_at_2300_returns_8_hours(self, mock_dt, tariff):
        now = datetime(2025, 6, 15, 23, 0, 0)
        mock_dt.now.return_value = now
        mock_dt.combine = datetime.combine
        assert tariff.hours_until_next_hp() == 8.0


# ═══════════════════════════════════════════════════════════════════════
#  SafetyManager Tests
# ═══════════════════════════════════════════════════════════════════════


# ── validate_plan ────────────────────────────────────────────────────


class TestValidatePlan:
    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_enforces_min_soc_floor_summer(self, mock_dt, safety):
        mock_dt.now.return_value = datetime(2025, 7, 15)  # July = summer
        plan = CurrentPlan(min_soc=10, target_soc=15.0)
        result = safety.validate_plan(plan)
        assert result.min_soc == 20
        assert result.target_soc == 20.0

    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_enforces_min_soc_floor_winter(self, mock_dt, safety):
        mock_dt.now.return_value = datetime(2025, 1, 15)  # January = winter
        plan = CurrentPlan(min_soc=10, target_soc=30.0)
        result = safety.validate_plan(plan)
        assert result.min_soc == 50
        assert result.target_soc == 50.0

    def test_caps_target_soc_at_100(self, safety):
        plan = CurrentPlan(target_soc=110.0)
        result = safety.validate_plan(plan)
        assert result.target_soc == 100.0

    def test_caps_charge_power_at_5000(self, safety):
        plan = CurrentPlan(charge_power_w=7000)
        result = safety.validate_plan(plan)
        assert result.charge_power_w == 5000

    def test_negative_charge_power_set_to_zero(self, safety):
        plan = CurrentPlan(charge_power_w=-100)
        result = safety.validate_plan(plan)
        assert result.charge_power_w == 0

    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_valid_plan_unchanged(self, mock_dt, safety):
        mock_dt.now.return_value = datetime(2025, 7, 15)  # summer
        plan = CurrentPlan(
            target_soc=80.0, charge_power_w=3000, min_soc=25
        )
        result = safety.validate_plan(plan)
        assert result.target_soc == 80.0
        assert result.charge_power_w == 3000
        assert result.min_soc == 25


# ── is_winter ────────────────────────────────────────────────────────


class TestIsWinter:
    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_january_is_winter(self, mock_dt, safety):
        mock_dt.now.return_value = datetime(2025, 1, 15)
        assert safety.is_winter is True

    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_july_is_not_winter(self, mock_dt, safety):
        mock_dt.now.return_value = datetime(2025, 7, 15)
        assert safety.is_winter is False

    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_november_is_winter(self, mock_dt, safety):
        mock_dt.now.return_value = datetime(2025, 11, 1)
        assert safety.is_winter is True

    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_march_is_winter(self, mock_dt, safety):
        mock_dt.now.return_value = datetime(2025, 3, 31)
        assert safety.is_winter is True

    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_april_is_not_winter(self, mock_dt, safety):
        mock_dt.now.return_value = datetime(2025, 4, 1)
        assert safety.is_winter is False


# ── check_battery_stale ──────────────────────────────────────────────


class TestCheckBatteryStale:
    def test_no_update_is_stale(self, safety, state_store):
        # last_updated is None by default
        assert safety.check_battery_stale() is True

    def test_recent_update_not_stale(self, safety, state_store):
        state_store.update_battery(soc=50.0)
        assert safety.check_battery_stale() is False

    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_old_update_is_stale(self, mock_dt, safety, state_store, event_bus):
        from unittest.mock import MagicMock

        alert_cb = MagicMock()
        event_bus.subscribe(Event.SAFETY_ALERT, alert_cb)

        # Set battery with a real last_updated
        state_store.update_battery(soc=50.0)

        # Mock datetime.now to return a time 10 minutes in the future
        mock_dt.now.return_value = datetime.now() + timedelta(minutes=10)
        assert safety.check_battery_stale() is True
        alert_cb.assert_called_once()


# ── should_emergency_stop ────────────────────────────────────────────


class TestShouldEmergencyStop:
    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_critically_low_soc_discharging(self, mock_dt, safety, state_store):
        mock_dt.now.return_value = datetime(2025, 7, 15)  # summer, min_soc=20
        # emergency_floor = max(10, 20-10) = 10
        state_store.update_battery(soc=10.0, battery_power_w=-500.0)
        assert safety.should_emergency_stop() is True

    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_low_soc_but_charging(self, mock_dt, safety, state_store):
        mock_dt.now.return_value = datetime(2025, 7, 15)
        state_store.update_battery(soc=5.0, battery_power_w=500.0)
        assert safety.should_emergency_stop() is False

    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_normal_soc_discharging(self, mock_dt, safety, state_store):
        mock_dt.now.return_value = datetime(2025, 7, 15)
        state_store.update_battery(soc=50.0, battery_power_w=-500.0)
        assert safety.should_emergency_stop() is False

    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_winter_higher_emergency_floor(self, mock_dt, safety, state_store):
        mock_dt.now.return_value = datetime(2025, 1, 15)  # winter, min_soc=50
        # emergency_floor = max(10, 50-10) = 40
        state_store.update_battery(soc=40.0, battery_power_w=-500.0)
        assert safety.should_emergency_stop() is True


# ── get_safe_fallback_plan ───────────────────────────────────────────


class TestGetSafeFallbackPlan:
    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_fallback_plan_summer(self, mock_dt, safety):
        mock_dt.now.return_value = datetime(2025, 7, 15)
        plan = safety.get_safe_fallback_plan()
        assert plan.target_soc == 20.0
        assert plan.min_soc == 20
        assert plan.charge_power_w == 0
        assert plan.allow_grid_charge is False
        assert plan.prevent_discharge is False
        assert plan.phase == "fallback"
        assert "fallback" in plan.reasoning.lower()

    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_fallback_plan_winter(self, mock_dt, safety):
        mock_dt.now.return_value = datetime(2025, 1, 15)
        plan = safety.get_safe_fallback_plan()
        assert plan.target_soc == 50.0
        assert plan.min_soc == 50

    @patch("custom_components.beem_ai.safety_manager.datetime")
    def test_fallback_plan_has_created_at(self, mock_dt, safety):
        fake_now = datetime(2025, 7, 15, 10, 0, 0)
        mock_dt.now.return_value = fake_now
        plan = safety.get_safe_fallback_plan()
        assert plan.created_at == fake_now
