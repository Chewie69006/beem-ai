"""Tests for BeemAI options flow."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from custom_components.beem_ai.options_flow import BeemAIOptionsFlow
from custom_components.beem_ai.const import (
    DEFAULT_MIN_SOC_SUMMER,
    DEFAULT_MIN_SOC_WINTER,
    DEFAULT_TARIFF_DEFAULT_PRICE,
    DEFAULT_TARIFF_PERIOD_COUNT,
    DEFAULT_SMART_CFTG,
    DEFAULT_WATER_HEATER_POWER_W,
    OPT_LOCATION_LAT,
    OPT_LOCATION_LON,
    OPT_MIN_SOC_SUMMER,
    OPT_MIN_SOC_WINTER,
    OPT_SMART_CFTG,
    OPT_SOLCAST_API_KEY,
    OPT_SOLCAST_SITE_ID,
    OPT_TARIFF_DEFAULT_PRICE,
    OPT_TARIFF_PERIOD_COUNT,
    OPT_TARIFF_PERIODS_JSON,
    OPT_WATER_HEATER_POWER_ENTITY,
    OPT_WATER_HEATER_POWER_W,
    OPT_WATER_HEATER_SWITCH,
)


def _make_config_entry(options=None):
    """Create a mock config entry with the given options."""
    entry = MagicMock()
    entry.options = options or {}
    return entry


def _make_flow(options=None):
    """Create an options flow with a mock config entry."""
    entry = _make_config_entry(options)
    flow = BeemAIOptionsFlow(entry)
    flow.config_entry = entry
    flow.async_show_form = MagicMock()
    flow.async_create_entry = MagicMock()
    return flow


VALID_INIT_INPUT = {
    OPT_LOCATION_LAT: 48.85,
    OPT_LOCATION_LON: 2.35,
    OPT_SOLCAST_API_KEY: "key-123",
    OPT_SOLCAST_SITE_ID: "site-456",
    OPT_TARIFF_DEFAULT_PRICE: DEFAULT_TARIFF_DEFAULT_PRICE,
    OPT_TARIFF_PERIOD_COUNT: DEFAULT_TARIFF_PERIOD_COUNT,
    OPT_MIN_SOC_SUMMER: DEFAULT_MIN_SOC_SUMMER,
    OPT_MIN_SOC_WINTER: DEFAULT_MIN_SOC_WINTER,
    OPT_WATER_HEATER_SWITCH: "",
    OPT_WATER_HEATER_POWER_ENTITY: "",
    OPT_WATER_HEATER_POWER_W: DEFAULT_WATER_HEATER_POWER_W,
    OPT_SMART_CFTG: False,
}


# ------------------------------------------------------------------
# async_step_init
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_init_shows_form():
    """No input shows the init form with current values."""
    existing = {
        OPT_LOCATION_LAT: 48.85,
        OPT_LOCATION_LON: 2.35,
    }
    flow = _make_flow(options=existing)

    await flow.async_step_init(user_input=None)

    flow.async_show_form.assert_called_once()
    assert flow.async_show_form.call_args.kwargs["step_id"] == "init"


@pytest.mark.asyncio
async def test_step_init_proceeds_to_tariffs():
    """Valid init input stores options and proceeds to tariffs step."""
    flow = _make_flow()
    flow.async_step_tariffs = AsyncMock(return_value="tariffs_result")

    result = await flow.async_step_init(user_input=VALID_INIT_INPUT)

    flow.async_step_tariffs.assert_called_once()
    assert flow._tariff_period_count == DEFAULT_TARIFF_PERIOD_COUNT
    assert flow._options == VALID_INIT_INPUT


@pytest.mark.asyncio
async def test_step_init_smart_cftg_toggle():
    """Smart CFTG toggle is stored in options."""
    flow = _make_flow()
    flow.async_step_tariffs = AsyncMock(return_value="tariffs_result")

    input_with_cftg = {**VALID_INIT_INPUT, OPT_SMART_CFTG: True}
    await flow.async_step_init(user_input=input_with_cftg)

    assert flow._options[OPT_SMART_CFTG] is True


@pytest.mark.asyncio
async def test_step_init_no_panel_count_field():
    """Init form should not contain panel_count field."""
    flow = _make_flow()

    await flow.async_step_init(user_input=None)

    schema = flow.async_show_form.call_args.kwargs["data_schema"]
    field_names = [str(k) for k in schema.schema]
    assert "panel_count" not in field_names


# ------------------------------------------------------------------
# async_step_tariffs
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_tariffs_shows_form():
    """No input shows the tariffs form."""
    flow = _make_flow()
    flow._tariff_period_count = 2

    await flow.async_step_tariffs(user_input=None)

    flow.async_show_form.assert_called_once()
    assert flow.async_show_form.call_args.kwargs["step_id"] == "tariffs"


@pytest.mark.asyncio
async def test_step_tariffs_creates_entry():
    """Tariff input is serialized to JSON and creates entry directly."""
    flow = _make_flow()
    flow._tariff_period_count = 2
    flow._options = dict(VALID_INIT_INPUT)

    tariff_input = {
        "tariff_1_label": "HC",
        "tariff_1_start": "23:00",
        "tariff_1_end": "06:00",
        "tariff_1_price": 0.16,
        "tariff_2_label": "HSC",
        "tariff_2_start": "02:00",
        "tariff_2_end": "06:00",
        "tariff_2_price": 0.12,
    }

    await flow.async_step_tariffs(user_input=tariff_input)

    flow.async_create_entry.assert_called_once()
    saved_data = flow.async_create_entry.call_args.kwargs["data"]
    periods_json = saved_data[OPT_TARIFF_PERIODS_JSON]
    assert isinstance(periods_json, str)
    periods = json.loads(periods_json)
    assert len(periods) == 2
    assert periods[0]["label"] == "HC"
    assert periods[0]["start"] == "23:00"
    assert periods[0]["price"] == 0.16


@pytest.mark.asyncio
async def test_step_tariffs_existing_defaults():
    """Existing tariff periods populate form defaults."""
    existing_periods = [
        {"label": "Nuit", "start": "22:00", "end": "06:00", "price": 0.10},
    ]
    existing_options = {
        OPT_TARIFF_PERIODS_JSON: json.dumps(existing_periods),
    }
    flow = _make_flow(options=existing_options)
    flow._tariff_period_count = 1

    await flow.async_step_tariffs(user_input=None)

    call_kwargs = flow.async_show_form.call_args.kwargs
    schema = call_kwargs["data_schema"]

    for key_obj in schema.schema:
        key_str = str(key_obj)
        if key_str == "tariff_1_label":
            assert key_obj.default() == "Nuit"
        elif key_str == "tariff_1_start":
            assert key_obj.default() == "22:00"
        elif key_str == "tariff_1_price":
            assert key_obj.default() == 0.10


# ------------------------------------------------------------------
# Min SoC validation
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_min_soc_zero_is_valid():
    """Min SoC = 0 (disabled) should pass validation."""
    flow = _make_flow()
    flow.async_step_tariffs = AsyncMock(return_value="tariffs_result")

    input_data = {**VALID_INIT_INPUT, OPT_MIN_SOC_SUMMER: 0, OPT_MIN_SOC_WINTER: 0}
    await flow.async_step_init(user_input=input_data)

    flow.async_step_tariffs.assert_called_once()


@pytest.mark.asyncio
async def test_min_soc_29_is_invalid():
    """Min SoC = 29 should fail validation."""
    flow = _make_flow()

    input_data = {**VALID_INIT_INPUT, OPT_MIN_SOC_SUMMER: 29}
    await flow.async_step_init(user_input=input_data)

    flow.async_show_form.assert_called_once()
    errors = flow.async_show_form.call_args.kwargs.get("errors", {})
    assert OPT_MIN_SOC_SUMMER in errors


@pytest.mark.asyncio
async def test_min_soc_30_is_valid():
    """Min SoC = 30 should pass validation."""
    flow = _make_flow()
    flow.async_step_tariffs = AsyncMock(return_value="tariffs_result")

    input_data = {**VALID_INIT_INPUT, OPT_MIN_SOC_SUMMER: 30}
    await flow.async_step_init(user_input=input_data)

    flow.async_step_tariffs.assert_called_once()


@pytest.mark.asyncio
async def test_min_soc_100_is_valid():
    """Min SoC = 100 should pass validation."""
    flow = _make_flow()
    flow.async_step_tariffs = AsyncMock(return_value="tariffs_result")

    input_data = {**VALID_INIT_INPUT, OPT_MIN_SOC_SUMMER: 100}
    await flow.async_step_init(user_input=input_data)

    flow.async_step_tariffs.assert_called_once()


# ------------------------------------------------------------------
# Full flow: init -> tariffs -> entry
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow():
    """End-to-end: init -> tariffs -> create_entry."""
    flow = _make_flow()

    # Step 1: init
    await flow.async_step_init(user_input=VALID_INIT_INPUT)
    # Should show tariffs form
    flow.async_show_form.assert_called_once()
    assert flow.async_show_form.call_args.kwargs["step_id"] == "tariffs"

    # Step 2: tariffs -> creates entry directly
    flow.async_show_form.reset_mock()
    tariff_input = {
        "tariff_1_label": "HC",
        "tariff_1_start": "23:00",
        "tariff_1_end": "07:00",
        "tariff_1_price": 0.16,
        "tariff_2_label": "HSC",
        "tariff_2_start": "02:00",
        "tariff_2_end": "06:00",
        "tariff_2_price": 0.12,
    }
    await flow.async_step_tariffs(user_input=tariff_input)

    # Should create entry (no panels step)
    flow.async_create_entry.assert_called_once()
    saved_data = flow.async_create_entry.call_args.kwargs["data"]

    # Verify all data persisted
    assert saved_data[OPT_LOCATION_LAT] == 48.85
    assert OPT_TARIFF_PERIODS_JSON in saved_data

    periods = json.loads(saved_data[OPT_TARIFF_PERIODS_JSON])
    assert len(periods) == 2
