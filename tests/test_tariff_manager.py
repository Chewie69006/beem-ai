"""Tests for BeemAI TariffManager."""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from custom_components.beem_ai.tariff_manager import (
    TARIFF_HP,
    TariffManager,
    TariffPeriod,
)


# ── Shared fixtures ──────────────────────────────────────────────────


@pytest.fixture
def custom_tariff():
    """TariffManager with custom periods."""
    periods = [
        {"label": "Nuit", "start": "23:00", "end": "06:00", "price": 0.12},
        {"label": "Matin", "start": "06:00", "end": "08:00", "price": 0.18},
    ]
    return TariffManager(default_price=0.25, periods=periods)


@pytest.fixture
def no_periods_tariff():
    """TariffManager with no periods — only default price applies."""
    return TariffManager(default_price=0.27)


# ── TariffManager: no periods ────────────────────────────────────────


class TestNoPeriods:
    """When no periods are configured, only default price applies."""

    def _dt(self, hour, minute=0):
        return datetime(2025, 6, 15, hour, minute, 0)

    def test_all_times_are_hp(self, no_periods_tariff):
        for hour in [0, 3, 7, 12, 18, 23]:
            assert no_periods_tariff.get_tariff_at(self._dt(hour)) == TARIFF_HP

    def test_all_times_default_price(self, no_periods_tariff):
        for hour in [0, 3, 7, 12, 18, 23]:
            assert no_periods_tariff.get_price_at(self._dt(hour)) == 0.27

    def test_no_cheapest_period(self, no_periods_tariff):
        assert no_periods_tariff.is_in_cheapest_period(self._dt(3)) is False

    def test_not_in_any_period(self, no_periods_tariff):
        assert no_periods_tariff.is_in_any_period(self._dt(3)) is False

    def test_cheapest_tariff_fallback(self, no_periods_tariff):
        label, price = no_periods_tariff.get_cheapest_tariff()
        assert label == TARIFF_HP
        assert price == 0.27


# ── TariffManager: custom periods ───────────────────────────────────


class TestCustomPeriods:
    def _dt(self, hour, minute=0):
        return datetime(2025, 6, 15, hour, minute, 0)

    def test_night_period(self, custom_tariff):
        """23:00-06:00 should be 'Nuit'."""
        assert custom_tariff.get_tariff_at(self._dt(23, 30)) == "Nuit"
        assert custom_tariff.get_tariff_at(self._dt(0, 0)) == "Nuit"
        assert custom_tariff.get_tariff_at(self._dt(3, 0)) == "Nuit"
        assert custom_tariff.get_tariff_at(self._dt(5, 59)) == "Nuit"

    def test_morning_period(self, custom_tariff):
        """06:00-08:00 should be 'Matin'."""
        assert custom_tariff.get_tariff_at(self._dt(6, 0)) == "Matin"
        assert custom_tariff.get_tariff_at(self._dt(7, 30)) == "Matin"

    def test_default_period(self, custom_tariff):
        """08:00-23:00 should be default (HP)."""
        assert custom_tariff.get_tariff_at(self._dt(8, 0)) == TARIFF_HP
        assert custom_tariff.get_tariff_at(self._dt(12, 0)) == TARIFF_HP
        assert custom_tariff.get_tariff_at(self._dt(22, 59)) == TARIFF_HP

    def test_price_at(self, custom_tariff):
        assert custom_tariff.get_price_at(self._dt(0, 0)) == 0.12
        assert custom_tariff.get_price_at(self._dt(7, 0)) == 0.18
        assert custom_tariff.get_price_at(self._dt(12, 0)) == 0.25

    def test_midnight_crossing(self, custom_tariff):
        """Period 23:00-06:00 crosses midnight."""
        assert custom_tariff.get_tariff_at(self._dt(23, 0)) == "Nuit"
        assert custom_tariff.get_tariff_at(self._dt(0, 0)) == "Nuit"
        assert custom_tariff.get_tariff_at(self._dt(5, 59)) == "Nuit"
        assert custom_tariff.get_tariff_at(self._dt(6, 0)) == "Matin"


# ── TariffManager: is_in_cheapest_period ─────────────────────────────


class TestIsInCheapestPeriod:
    def _dt(self, hour, minute=0):
        return datetime(2025, 6, 15, hour, minute, 0)

    def test_cheapest_period_custom(self, custom_tariff):
        """'Nuit' at 0.12 is cheapest in custom config."""
        assert custom_tariff.is_in_cheapest_period(self._dt(0, 0)) is True
        assert custom_tariff.is_in_cheapest_period(self._dt(7, 0)) is False

    def test_is_in_any_period(self, custom_tariff):
        assert custom_tariff.is_in_any_period(self._dt(0, 0)) is True
        assert custom_tariff.is_in_any_period(self._dt(7, 0)) is True
        assert custom_tariff.is_in_any_period(self._dt(12, 0)) is False


# ── TariffManager: get_cheapest_tariff ───────────────────────────────


class TestGetCheapestTariff:
    def test_custom_cheapest(self, custom_tariff):
        label, price = custom_tariff.get_cheapest_tariff()
        assert label == "Nuit"
        assert price == 0.12


# ── TariffManager: get_price_kwh ─────────────────────────────────────


class TestGetPriceKwh:
    def test_hp_price(self, custom_tariff):
        assert custom_tariff.get_price_kwh(TARIFF_HP) == custom_tariff.default_price

    def test_named_period_price(self, custom_tariff):
        assert custom_tariff.get_price_kwh("Nuit") == 0.12
        assert custom_tariff.get_price_kwh("Matin") == 0.18

    def test_current_tariff_price(self, custom_tariff):
        # get_price_kwh with no argument should use current time
        price = custom_tariff.get_price_kwh()
        assert price > 0


# ── TariffManager: reconfigure ───────────────────────────────────────


class TestReconfigure:
    def _dt(self, hour, minute=0):
        return datetime(2025, 6, 15, hour, minute, 0)

    def test_reconfigure_default_price(self, custom_tariff):
        custom_tariff.reconfigure({"tariff_default_price": "0.30"})
        assert custom_tariff.default_price == 0.30

    def test_reconfigure_periods_json(self, no_periods_tariff):
        periods = [
            {"label": "Nuit", "start": "22:00", "end": "07:00", "price": 0.10},
        ]
        no_periods_tariff.reconfigure({"tariff_periods_json": json.dumps(periods)})
        assert no_periods_tariff.get_tariff_at(self._dt(23, 0)) == "Nuit"
        assert no_periods_tariff.get_price_at(self._dt(23, 0)) == 0.10

    def test_reconfigure_periods_list(self, no_periods_tariff):
        """reconfigure also accepts a list directly."""
        periods = [
            {"label": "Test", "start": "10:00", "end": "14:00", "price": 0.05},
        ]
        no_periods_tariff.reconfigure({"tariff_periods_json": periods})
        assert no_periods_tariff.get_tariff_at(self._dt(12, 0)) == "Test"


# ── TariffManager: get_daily_reset_hour ──────────────────────────────


class TestGetDailyResetHour:
    def test_no_periods_returns_midnight(self, no_periods_tariff):
        assert no_periods_tariff.get_daily_reset_hour() == 0

    def test_cheapest_period_on_the_hour(self, custom_tariff):
        """'Nuit' starts at 23:00 (on the hour) → reset at 23."""
        assert custom_tariff.get_daily_reset_hour() == 23

    def test_cheapest_period_not_on_the_hour_rounds_up(self):
        """Period starting at 21:26 → reset at 22."""
        periods = [
            {"label": "Night", "start": "21:26", "end": "06:00", "price": 0.10},
        ]
        tm = TariffManager(default_price=0.25, periods=periods)
        assert tm.get_daily_reset_hour() == 22

    def test_rounds_up_across_midnight(self):
        """Period starting at 23:30 → (23+1) % 24 = 0."""
        periods = [
            {"label": "Night", "start": "23:30", "end": "06:00", "price": 0.10},
        ]
        tm = TariffManager(default_price=0.25, periods=periods)
        assert tm.get_daily_reset_hour() == 0

    def test_tiebreaker_longest_duration(self):
        """Two periods at same price — picks the longer one."""
        periods = [
            {"label": "Short", "start": "02:00", "end": "04:00", "price": 0.10},
            {"label": "Long", "start": "22:00", "end": "06:00", "price": 0.10},
        ]
        tm = TariffManager(default_price=0.25, periods=periods)
        # Long starts at 22:00 (on the hour) → 22
        assert tm.get_daily_reset_hour() == 22
