"""Tests for BeemAI options flow."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from custom_components.beem_ai.options_flow import BeemAIOptionsFlow
from custom_components.beem_ai.const import (
    DEFAULT_TARIFF_DEFAULT_PRICE,
    DEFAULT_TARIFF_PERIOD_COUNT,
    DOMAIN,
    OPT_LOCATION_LAT,
    OPT_LOCATION_LON,
    OPT_SOLCAST_API_KEY,
    OPT_SOLCAST_SITE_IDS_JSON,
    OPT_TARIFF_DEFAULT_PRICE,
    OPT_TARIFF_PERIOD_COUNT,
    OPT_TARIFF_PERIODS_JSON,
)


def _make_config_entry(options=None, entry_id="test-entry"):
    """Create a mock config entry with the given options."""
    entry = MagicMock()
    entry.options = options or {}
    entry.entry_id = entry_id
    return entry


def _make_flow(options=None, panel_arrays=None):
    """Create an options flow with a mock config entry and coordinator."""
    entry = _make_config_entry(options)
    flow = BeemAIOptionsFlow(entry)
    flow.config_entry = entry

    # Mock hass with coordinator containing panel_arrays
    coordinator = MagicMock()
    coordinator.panel_arrays = panel_arrays or [
        {"tilt": 30, "azimuth": 180, "kwp": 2.5},
        {"tilt": 15, "azimuth": 270, "kwp": 1.5},
    ]
    flow.hass = MagicMock()
    flow.hass.data = {DOMAIN: {entry.entry_id: coordinator}}

    flow.async_show_form = MagicMock()
    flow.async_create_entry = MagicMock()
    return flow


VALID_INIT_INPUT = {
    OPT_LOCATION_LAT: 48.85,
    OPT_LOCATION_LON: 2.35,
    OPT_SOLCAST_API_KEY: "key-123",
    OPT_TARIFF_DEFAULT_PRICE: DEFAULT_TARIFF_DEFAULT_PRICE,
    OPT_TARIFF_PERIOD_COUNT: DEFAULT_TARIFF_PERIOD_COUNT,
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
async def test_step_init_no_solcast_site_id_field():
    """Init form should not contain the old solcast_site_id field."""
    flow = _make_flow()

    await flow.async_step_init(user_input=None)

    schema = flow.async_show_form.call_args.kwargs["data_schema"]
    field_names = [str(k) for k in schema.schema]
    assert "solcast_site_id" not in field_names


@pytest.mark.asyncio
async def test_step_init_proceeds_to_solcast():
    """Valid init input stores options and proceeds to solcast step."""
    flow = _make_flow()
    flow.async_step_solcast = AsyncMock(return_value="solcast_result")

    result = await flow.async_step_init(user_input=VALID_INIT_INPUT)

    flow.async_step_solcast.assert_called_once()
    assert flow._tariff_period_count == DEFAULT_TARIFF_PERIOD_COUNT
    assert flow._options == VALID_INIT_INPUT


# ------------------------------------------------------------------
# async_step_solcast
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_solcast_shows_form():
    """No input shows the solcast form with per-array fields."""
    flow = _make_flow()

    await flow.async_step_solcast(user_input=None)

    flow.async_show_form.assert_called_once()
    assert flow.async_show_form.call_args.kwargs["step_id"] == "solcast"
    schema = flow.async_show_form.call_args.kwargs["data_schema"]
    field_names = [str(k) for k in schema.schema]
    assert "solcast_site_0_id" in field_names
    assert "solcast_site_1_id" in field_names


@pytest.mark.asyncio
async def test_step_solcast_with_existing_values():
    """Existing site IDs populate form defaults."""
    existing_site_ids = [
        {"array_index": 0, "site_id": "site-aaa"},
        {"array_index": 1, "site_id": "site-bbb"},
    ]
    flow = _make_flow(options={
        OPT_SOLCAST_SITE_IDS_JSON: json.dumps(existing_site_ids),
    })

    await flow.async_step_solcast(user_input=None)

    schema = flow.async_show_form.call_args.kwargs["data_schema"]
    for key_obj in schema.schema:
        key_str = str(key_obj)
        if key_str == "solcast_site_0_id":
            assert key_obj.default() == "site-aaa"
        elif key_str == "solcast_site_1_id":
            assert key_obj.default() == "site-bbb"


@pytest.mark.asyncio
async def test_step_solcast_proceeds_to_tariffs():
    """Solcast input serializes to JSON and proceeds to tariffs step."""
    flow = _make_flow()
    flow._panel_array_count = 2
    flow._options = dict(VALID_INIT_INPUT)
    flow.async_step_tariffs = AsyncMock(return_value="tariffs_result")

    await flow.async_step_solcast(user_input={
        "solcast_site_0_id": "site-aaa",
        "solcast_site_1_id": "site-bbb",
    })

    flow.async_step_tariffs.assert_called_once()
    site_ids_json = flow._options[OPT_SOLCAST_SITE_IDS_JSON]
    site_ids = json.loads(site_ids_json)
    assert len(site_ids) == 2
    assert site_ids[0] == {"array_index": 0, "site_id": "site-aaa"}
    assert site_ids[1] == {"array_index": 1, "site_id": "site-bbb"}


@pytest.mark.asyncio
async def test_step_solcast_empty_fields_excluded():
    """Empty site IDs are not included in the JSON output."""
    flow = _make_flow()
    flow._panel_array_count = 2
    flow._options = dict(VALID_INIT_INPUT)
    flow.async_step_tariffs = AsyncMock(return_value="tariffs_result")

    await flow.async_step_solcast(user_input={
        "solcast_site_0_id": "site-aaa",
        "solcast_site_1_id": "",  # empty
    })

    site_ids = json.loads(flow._options[OPT_SOLCAST_SITE_IDS_JSON])
    assert len(site_ids) == 1
    assert site_ids[0]["site_id"] == "site-aaa"


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
# Full flow: init -> solcast -> tariffs -> entry
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_flow():
    """End-to-end: init -> solcast -> tariffs -> create_entry."""
    flow = _make_flow()

    # Step 1: init -> shows solcast form
    await flow.async_step_init(user_input=VALID_INIT_INPUT)
    flow.async_show_form.assert_called_once()
    assert flow.async_show_form.call_args.kwargs["step_id"] == "solcast"

    # Step 2: solcast -> shows tariffs form
    flow.async_show_form.reset_mock()
    await flow.async_step_solcast(user_input={
        "solcast_site_0_id": "site-aaa",
        "solcast_site_1_id": "site-bbb",
    })
    flow.async_show_form.assert_called_once()
    assert flow.async_show_form.call_args.kwargs["step_id"] == "tariffs"

    # Step 3: tariffs -> creates entry
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

    # Should create entry
    flow.async_create_entry.assert_called_once()
    saved_data = flow.async_create_entry.call_args.kwargs["data"]

    # Verify all data persisted
    assert saved_data[OPT_LOCATION_LAT] == 48.85
    assert OPT_TARIFF_PERIODS_JSON in saved_data
    assert OPT_SOLCAST_SITE_IDS_JSON in saved_data

    # Verify Solcast site IDs
    site_ids = json.loads(saved_data[OPT_SOLCAST_SITE_IDS_JSON])
    assert len(site_ids) == 2
    assert site_ids[0]["site_id"] == "site-aaa"

    # Verify tariff periods
    periods = json.loads(saved_data[OPT_TARIFF_PERIODS_JSON])
    assert len(periods) == 2
