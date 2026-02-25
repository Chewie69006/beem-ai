"""Tests for BeemAI options flow."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from custom_components.beem_ai.options_flow import BeemAIOptionsFlow
from custom_components.beem_ai.const import (
    DEFAULT_MIN_SOC_SUMMER,
    DEFAULT_MIN_SOC_WINTER,
    DEFAULT_PANEL_COUNT,
    DEFAULT_TARIFF_DEFAULT_PRICE,
    DEFAULT_TARIFF_PERIOD_COUNT,
    DEFAULT_SMART_CFTG,
    DEFAULT_WATER_HEATER_POWER_W,
    OPT_LOCATION_LAT,
    OPT_LOCATION_LON,
    OPT_MIN_SOC_SUMMER,
    OPT_MIN_SOC_WINTER,
    OPT_PANEL_ARRAYS_JSON,
    OPT_PANEL_COUNT,
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
    OPT_PANEL_COUNT: 2,
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
    assert flow._panel_count == 2
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
async def test_step_tariffs_proceeds_to_panels():
    """Tariff input is serialized to JSON and proceeds to panels."""
    flow = _make_flow()
    flow._tariff_period_count = 2
    flow._options = dict(VALID_INIT_INPUT)
    flow.async_step_panels = AsyncMock(return_value="panels_result")

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

    flow.async_step_panels.assert_called_once()
    # Periods should be stored as JSON
    periods_json = flow._options[OPT_TARIFF_PERIODS_JSON]
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
# async_step_panels
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_panels_shows_form():
    """No input shows the panels form."""
    flow = _make_flow()
    flow._panel_count = 2

    await flow.async_step_panels(user_input=None)

    flow.async_show_form.assert_called_once()
    assert flow.async_show_form.call_args.kwargs["step_id"] == "panels"


@pytest.mark.asyncio
async def test_step_panels_creates_entry():
    """Panel data saved as JSON and creates entry."""
    flow = _make_flow()
    flow._panel_count = 2
    flow._options = dict(VALID_INIT_INPUT)

    panel_input = {
        "panel_1_tilt": 25,
        "panel_1_azimuth": 180,
        "panel_1_kwp": 4.5,
        "panel_2_tilt": 35,
        "panel_2_azimuth": 200,
        "panel_2_kwp": 3.0,
    }

    await flow.async_step_panels(user_input=panel_input)

    flow.async_create_entry.assert_called_once()
    call_kwargs = flow.async_create_entry.call_args.kwargs
    saved_data = call_kwargs["data"]

    assert saved_data[OPT_LOCATION_LAT] == 48.85

    panels = json.loads(saved_data[OPT_PANEL_ARRAYS_JSON])
    assert len(panels) == 2
    assert panels[0] == {"tilt": 25, "azimuth": 180, "kwp": 4.5}
    assert panels[1] == {"tilt": 35, "azimuth": 200, "kwp": 3.0}


@pytest.mark.asyncio
async def test_panel_arrays_stored_as_json():
    """Verify panel data is JSON-encoded string, not a raw list."""
    flow = _make_flow()
    flow._panel_count = 1
    flow._options = {}

    panel_input = {
        "panel_1_tilt": 30,
        "panel_1_azimuth": 180,
        "panel_1_kwp": 5.0,
    }

    await flow.async_step_panels(user_input=panel_input)

    saved_data = flow.async_create_entry.call_args.kwargs["data"]
    raw_json = saved_data[OPT_PANEL_ARRAYS_JSON]
    assert isinstance(raw_json, str)
    parsed = json.loads(raw_json)
    assert parsed == [{"tilt": 30, "azimuth": 180, "kwp": 5.0}]


@pytest.mark.asyncio
async def test_existing_panel_values_shown_as_defaults():
    """Existing panel options populate form defaults."""
    existing_panels = [
        {"tilt": 10, "azimuth": 90, "kwp": 2.5},
        {"tilt": 45, "azimuth": 270, "kwp": 6.0},
    ]
    existing_options = {
        OPT_PANEL_ARRAYS_JSON: json.dumps(existing_panels),
    }
    flow = _make_flow(options=existing_options)
    flow._panel_count = 2

    await flow.async_step_panels(user_input=None)

    call_kwargs = flow.async_show_form.call_args.kwargs
    schema = call_kwargs["data_schema"]

    for key_obj in schema.schema:
        key_str = str(key_obj)
        if key_str == "panel_1_tilt":
            assert key_obj.default() == 10
        elif key_str == "panel_1_azimuth":
            assert key_obj.default() == 90
        elif key_str == "panel_1_kwp":
            assert key_obj.default() == 2.5
        elif key_str == "panel_2_tilt":
            assert key_obj.default() == 45
        elif key_str == "panel_2_azimuth":
            assert key_obj.default() == 270
        elif key_str == "panel_2_kwp":
            assert key_obj.default() == 6.0


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
# Full flow: init -> tariffs -> panels -> entry
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow():
    """End-to-end: init -> tariffs -> panels -> create_entry."""
    flow = _make_flow()

    # Step 1: init
    await flow.async_step_init(user_input=VALID_INIT_INPUT)
    # Should show tariffs form
    flow.async_show_form.assert_called_once()
    assert flow.async_show_form.call_args.kwargs["step_id"] == "tariffs"

    # Step 2: tariffs
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
    # Should show panels form
    flow.async_show_form.assert_called_once()
    assert flow.async_show_form.call_args.kwargs["step_id"] == "panels"

    # Step 3: panels
    panel_input = {
        "panel_1_tilt": 30,
        "panel_1_azimuth": 180,
        "panel_1_kwp": 5.0,
        "panel_2_tilt": 30,
        "panel_2_azimuth": 180,
        "panel_2_kwp": 5.0,
    }
    await flow.async_step_panels(user_input=panel_input)

    # Should create entry
    flow.async_create_entry.assert_called_once()
    saved_data = flow.async_create_entry.call_args.kwargs["data"]

    # Verify all data persisted
    assert saved_data[OPT_LOCATION_LAT] == 48.85
    assert OPT_TARIFF_PERIODS_JSON in saved_data
    assert OPT_PANEL_ARRAYS_JSON in saved_data

    periods = json.loads(saved_data[OPT_TARIFF_PERIODS_JSON])
    assert len(periods) == 2
    panels = json.loads(saved_data[OPT_PANEL_ARRAYS_JSON])
    assert len(panels) == 2
