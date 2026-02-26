"""Unit tests for OptimizationEngine (async)."""

import os
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from custom_components.beem_ai.optimization import POWER_STEPS, OptimizationEngine
from custom_components.beem_ai.safety_manager import SafetyManager
from custom_components.beem_ai.tariff_manager import TariffManager
from custom_components.beem_ai.event_bus import Event


@pytest.fixture
def tariff_manager():
    return TariffManager(default_price=0.27, periods=[
        {"label": "HC", "start": "23:00", "end": "02:00", "price": 0.20},
        {"label": "HSC", "start": "02:00", "end": "06:00", "price": 0.15},
        {"label": "HC", "start": "06:00", "end": "07:00", "price": 0.20},
    ])


@pytest.fixture
def safety_manager(state_store, event_bus):
    return SafetyManager(state=state_store, event_bus=event_bus)


@pytest.fixture
def engine(mock_hass, state_store, event_bus, tariff_manager, safety_manager):
    """Create an OptimizationEngine with a temporary data directory."""
    data_dir = tempfile.mkdtemp()
    mock_api_client = AsyncMock()
    return OptimizationEngine(
        hass=mock_hass,
        api_client=mock_api_client,
        state=state_store,
        event_bus=event_bus,
        tariff=tariff_manager,
        safety=safety_manager,
        data_dir=data_dir,
    )


# ------------------------------------------------------------------
# _calculate_target_soc()
# ------------------------------------------------------------------


class TestCalculateTargetSoc:
    """Test target SoC calculation across different forecast scenarios."""

    @pytest.fixture(autouse=True)
    def _force_summer(self):
        """Force summer mode so min_soc=20 and winter floor is inactive."""
        with patch.object(
            SafetyManager, "is_winter", new_callable=PropertyMock, return_value=False
        ):
            yield

    def test_very_sunny(self, engine, state_store):
        """Large positive net balance -> low target (leave room for solar)."""
        state_store.update_forecast(confidence="medium")
        capacity = 13.4
        target = engine._calculate_target_soc(
            net_balance=12.0, capacity=capacity, current_soc=50.0,
            production_kwh=15.0,
        )
        assert target == 20.0

    def test_moderate_sun(self, engine, state_store):
        """Positive but modest net balance -> moderate target."""
        state_store.update_forecast(confidence="medium")
        engine._estimate_night_consumption = MagicMock(return_value=3.0)

        target = engine._calculate_target_soc(
            net_balance=3.0, capacity=13.4, current_soc=50.0,
            production_kwh=8.0,
        )
        assert 20.0 <= target <= 75.0

    def test_slightly_cloudy(self, engine, state_store):
        """Slightly negative net balance -> higher target."""
        state_store.update_forecast(confidence="medium")
        target = engine._calculate_target_soc(
            net_balance=-2.0, capacity=13.4, current_soc=50.0,
            production_kwh=5.0,
        )
        assert target == 80.0

    def test_heavy_deficit(self, engine, state_store):
        """Large negative net balance -> high target, capped at 95."""
        state_store.update_forecast(confidence="medium")
        target = engine._calculate_target_soc(
            net_balance=-15.0, capacity=13.4, current_soc=40.0,
            production_kwh=1.0,
        )
        assert target == 95.0

    def test_heavy_deficit_low_soc(self, engine, state_store):
        """Heavy deficit with low current SoC still caps at 95."""
        state_store.update_forecast(confidence="medium")
        target = engine._calculate_target_soc(
            net_balance=-20.0, capacity=13.4, current_soc=10.0,
            production_kwh=0.5,
        )
        assert target <= 95.0


# ------------------------------------------------------------------
# Confidence adjustment
# ------------------------------------------------------------------


class TestConfidenceAdjustment:
    @pytest.fixture(autouse=True)
    def _force_summer(self):
        with patch.object(
            SafetyManager, "is_winter", new_callable=PropertyMock, return_value=False
        ):
            yield

    def test_low_confidence_adds_15_percent(self, engine, state_store):
        state_store.update_forecast(confidence="low")
        target = engine._calculate_target_soc(
            net_balance=12.0, capacity=13.4, current_soc=50.0,
            production_kwh=15.0,
        )
        assert target == 35.0

    def test_high_confidence_no_adjustment(self, engine, state_store):
        state_store.update_forecast(confidence="high")
        target = engine._calculate_target_soc(
            net_balance=12.0, capacity=13.4, current_soc=50.0,
            production_kwh=15.0,
        )
        assert target == 20.0


# ------------------------------------------------------------------
# Winter floor enforcement
# ------------------------------------------------------------------


class TestWinterFloor:
    def test_winter_floor_enforced(self, engine, state_store):
        state_store.update_forecast(confidence="medium")
        with patch.object(
            SafetyManager, "is_winter", new_callable=PropertyMock, return_value=True
        ):
            with patch.object(
                SafetyManager, "min_soc", new_callable=PropertyMock, return_value=50
            ):
                target = engine._calculate_target_soc(
                    net_balance=12.0, capacity=13.4, current_soc=50.0,
                    production_kwh=15.0,
                )
        assert target >= 50.0


# ------------------------------------------------------------------
# _calculate_charge_power()
# ------------------------------------------------------------------


class TestCalculateChargePower:
    def test_target_below_current_returns_zero(self, engine):
        power = engine._calculate_charge_power(
            current_soc=70.0, target_soc=60.0, capacity=13.4
        )
        assert power == 0

    def test_small_gap_picks_lowest_step(self, engine):
        power = engine._calculate_charge_power(
            current_soc=20.0, target_soc=25.0, capacity=13.4
        )
        assert power == 500

    def test_medium_gap_picks_correct_step(self, engine):
        power = engine._calculate_charge_power(
            current_soc=20.0, target_soc=50.0, capacity=13.4
        )
        assert power == 2500

    def test_large_gap_picks_max_step(self, engine):
        power = engine._calculate_charge_power(
            current_soc=10.0, target_soc=95.0, capacity=13.4
        )
        assert power == 5000

    def test_exact_current_equals_target(self, engine):
        power = engine._calculate_charge_power(
            current_soc=50.0, target_soc=50.0, capacity=13.4
        )
        assert power == 0

    def test_all_power_steps_are_reachable(self, engine):
        assert engine._calculate_charge_power(20.0, 23.7, 13.4) == 500
        assert engine._calculate_charge_power(20.0, 42.4, 13.4) == 1000

    def test_custom_window_hours(self, engine):
        """Wider window should result in lower power step."""
        power_4h = engine._calculate_charge_power(20.0, 50.0, 13.4, window_hours=4.0)
        power_8h = engine._calculate_charge_power(20.0, 50.0, 13.4, window_hours=8.0)
        assert power_8h <= power_4h


# ------------------------------------------------------------------
# Smart CFTG
# ------------------------------------------------------------------


class TestSmartCFTG:
    @pytest.mark.asyncio
    async def test_smart_cftg_disabled_noop(self, engine):
        """When smart_cftg is False, check_smart_cftg does nothing."""
        engine._smart_cftg = False
        await engine.check_smart_cftg()
        # No API calls
        engine._api_client.set_control_params.assert_not_called()

    @pytest.mark.asyncio
    async def test_smart_cftg_wrong_phase_noop(self, engine, state_store):
        """When not in an off-peak phase, smart CFTG does nothing."""
        engine._smart_cftg = True
        state_store.update_plan(phase="solar_mode")
        await engine.check_smart_cftg()
        engine._api_client.set_control_params.assert_not_called()

    @pytest.mark.asyncio
    async def test_smart_cftg_above_threshold_disables(self, engine, state_store, tariff_manager):
        """SoC above threshold -> disable CFTG, allow discharge."""
        engine._smart_cftg = True
        state_store.update_plan(phase="cheapest_charge")
        state_store.update_battery(soc=80.0)

        with patch.object(
            SafetyManager, "min_soc", new_callable=PropertyMock, return_value=50
        ):
            with patch.object(tariff_manager, "is_in_cheapest_period", return_value=True):
                await engine.check_smart_cftg()

        engine._api_client.set_control_params.assert_called_once()
        call_kwargs = engine._api_client.set_control_params.call_args.kwargs
        assert call_kwargs["allow_grid_charge"] is False
        assert call_kwargs["prevent_discharge"] is False

    @pytest.mark.asyncio
    async def test_smart_cftg_below_threshold_enables(self, engine, state_store, tariff_manager):
        """SoC below threshold -> enable CFTG at plan's power."""
        engine._smart_cftg = True
        engine._offpeak_charge_power = 1000
        state_store.update_plan(phase="cheapest_charge", target_soc=80.0)
        state_store.update_battery(soc=30.0)

        with patch.object(
            SafetyManager, "min_soc", new_callable=PropertyMock, return_value=50
        ):
            with patch.object(tariff_manager, "is_in_cheapest_period", return_value=True):
                await engine.check_smart_cftg()

        engine._api_client.set_control_params.assert_called_once()
        call_kwargs = engine._api_client.set_control_params.call_args.kwargs
        assert call_kwargs["allow_grid_charge"] is True
        assert call_kwargs["prevent_discharge"] is True
        assert call_kwargs["charge_power"] == 1000

    @pytest.mark.asyncio
    async def test_smart_cftg_threshold_zero_disables(self, engine, state_store, tariff_manager):
        """Threshold=0 -> always disable CFTG."""
        engine._smart_cftg = True
        state_store.update_plan(phase="cheapest_charge")
        state_store.update_battery(soc=10.0)

        with patch.object(
            SafetyManager, "min_soc", new_callable=PropertyMock, return_value=0
        ):
            with patch.object(tariff_manager, "is_in_cheapest_period", return_value=True):
                await engine.check_smart_cftg()

        call_kwargs = engine._api_client.set_control_params.call_args.kwargs
        assert call_kwargs["allow_grid_charge"] is False
        assert call_kwargs["prevent_discharge"] is False


# ------------------------------------------------------------------
# Integration: run_evening_optimization schedules phases
# ------------------------------------------------------------------


class TestEveningOptimization:
    @pytest.mark.asyncio
    async def test_schedules_phase_callbacks(self, engine, mock_hass, state_store):
        state_store.update_battery(soc=40.0, capacity_kwh=13.4)
        state_store.update_forecast(
            solar_tomorrow_kwh=8.0,
            consumption_tomorrow_kwh=10.0,
            confidence="medium",
        )

        with patch(
            "custom_components.beem_ai.optimization.async_call_later",
            return_value=MagicMock(),
        ) as mock_call_later:
            await engine.run_evening_optimization()
            assert mock_call_later.call_count >= 2

    @pytest.mark.asyncio
    async def test_publishes_plan_updated_event(self, engine, event_bus, state_store):
        received = []
        event_bus.subscribe(Event.PLAN_UPDATED, lambda d: received.append(d))

        state_store.update_battery(soc=40.0, capacity_kwh=13.4)
        state_store.update_forecast(
            solar_tomorrow_kwh=8.0,
            consumption_tomorrow_kwh=10.0,
            confidence="medium",
        )

        with patch(
            "custom_components.beem_ai.optimization.async_call_later",
            return_value=MagicMock(),
        ):
            await engine.run_evening_optimization()

        assert len(received) >= 1

    @pytest.mark.asyncio
    async def test_disabled_system_skips(self, engine, state_store):
        state_store.enabled = False

        with patch(
            "custom_components.beem_ai.optimization.async_call_later",
            return_value=MagicMock(),
        ) as mock_call_later:
            await engine.run_evening_optimization()
            mock_call_later.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancels_previous_handles(self, engine, state_store):
        state_store.update_battery(soc=40.0, capacity_kwh=13.4)
        state_store.update_forecast(
            solar_tomorrow_kwh=8.0,
            consumption_tomorrow_kwh=10.0,
            confidence="medium",
        )

        cancel_fn = MagicMock()
        with patch(
            "custom_components.beem_ai.optimization.async_call_later",
            return_value=cancel_fn,
        ):
            await engine.run_evening_optimization()
            first_handles = list(engine._scheduled_handles)
            assert len(first_handles) > 0

            await engine.run_evening_optimization()

        for handle in first_handles:
            handle.assert_called()

    @pytest.mark.asyncio
    async def test_reconfigure_updates_smart_cftg(self, engine):
        """reconfigure() updates smart_cftg flag."""
        engine.reconfigure({"smart_cftg": True, "dry_run": False})
        assert engine._smart_cftg is True
        engine.reconfigure({"smart_cftg": False})
        assert engine._smart_cftg is False
