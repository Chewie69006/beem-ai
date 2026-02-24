"""Tests for BeemAI options flow."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from custom_components.beem_ai.options_flow import BeemAIOptionsFlow
from custom_components.beem_ai.const import (
    DEFAULT_MIN_SOC_SUMMER,
    DEFAULT_MIN_SOC_WINTER,
    DEFAULT_PANEL_COUNT,
    DEFAULT_TARIFF_HC,
    DEFAULT_TARIFF_HP,
    DEFAULT_TARIFF_HSC,
    DEFAULT_WATER_HEATER_POWER_W,
    OPT_LOCATION_LAT,
    OPT_LOCATION_LON,
    OPT_MIN_SOC_SUMMER,
    OPT_MIN_SOC_WINTER,
    OPT_PANEL_ARRAYS_JSON,
    OPT_PANEL_COUNT,
    OPT_SOLCAST_API_KEY,
    OPT_SOLCAST_SITE_ID,
    OPT_TARIFF_HC_PRICE,
    OPT_TARIFF_HP_PRICE,
    OPT_TARIFF_HSC_PRICE,
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
    OPT_TARIFF_HP_PRICE: DEFAULT_TARIFF_HP,
    OPT_TARIFF_HC_PRICE: DEFAULT_TARIFF_HC,
    OPT_TARIFF_HSC_PRICE: DEFAULT_TARIFF_HSC,
    OPT_MIN_SOC_SUMMER: DEFAULT_MIN_SOC_SUMMER,
    OPT_MIN_SOC_WINTER: DEFAULT_MIN_SOC_WINTER,
    OPT_WATER_HEATER_SWITCH: "",
    OPT_WATER_HEATER_POWER_ENTITY: "",
    OPT_WATER_HEATER_POWER_W: DEFAULT_WATER_HEATER_POWER_W,
    OPT_PANEL_COUNT: 2,
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
async def test_step_init_proceeds_to_panels():
    """Valid init input stores options and proceeds to panels step."""
    flow = _make_flow()
    # Spy on async_step_panels to verify it is called
    flow.async_step_panels = AsyncMock(return_value="panels_result")

    result = await flow.async_step_init(user_input=VALID_INIT_INPUT)

    # Should have called async_step_panels (await resolves MagicMock)
    flow.async_step_panels.assert_called_once()
    assert flow._panel_count == 2
    assert flow._options == VALID_INIT_INPUT


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

    # Init options are preserved
    assert saved_data[OPT_LOCATION_LAT] == 48.85

    # Panels are stored as JSON
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
async def test_existing_values_shown_as_defaults():
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

    # Extract the schema from the async_show_form call
    call_kwargs = flow.async_show_form.call_args.kwargs
    schema = call_kwargs["data_schema"]

    # Verify defaults by inspecting schema keys
    schema_dict = {str(k): k for k in schema.schema}

    # Check panel 1 defaults
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
