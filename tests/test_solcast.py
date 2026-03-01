"""Tests for Solcast multi-site source with rooftop_sites endpoint."""

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.beem_ai.forecasting.solcast import SolcastSource, MAX_REQUESTS_PER_DAY


def _make_session():
    """Create a mock aiohttp session."""
    return MagicMock()


def _make_forecast_entry(period_end: str, pv50: float, pv10: float, pv90: float) -> dict:
    """Create a single forecast entry using rooftop_sites field names."""
    return {
        "period_end": period_end,
        "period": "PT30M",
        "pv_estimate": pv50,
        "pv_estimate10": pv10,
        "pv_estimate90": pv90,
    }


def _make_response(forecasts: list[dict]) -> dict:
    """Wrap forecasts in the Solcast response envelope."""
    return {"forecasts": forecasts}


# ------------------------------------------------------------------
# Constructor
# ------------------------------------------------------------------


def test_init_with_site_ids():
    """Multi-site constructor stores list of site_ids."""
    src = SolcastSource(
        session=_make_session(),
        api_key="key-123",
        site_ids=["site-a", "site-b"],
    )
    assert src.site_ids == ["site-a", "site-b"]
    assert src.api_key == "key-123"
    assert src.name == "solcast"


def test_init_no_site_ids():
    """No site_ids defaults to empty list."""
    src = SolcastSource(session=_make_session(), api_key="key-123")
    assert src.site_ids == []


# ------------------------------------------------------------------
# Budget tracking
# ------------------------------------------------------------------


def test_budget_tracks_multiple_calls():
    """Budget accounts for N calls per refresh with N sites."""
    src = SolcastSource(
        session=_make_session(),
        api_key="key-123",
        site_ids=["a", "b"],
    )
    assert src._budget_available(2) is True

    # Simulate using 9 calls
    src._request_count = 9
    src._request_date = date.today()
    assert src._budget_available(2) is False  # 9 + 2 > 10
    assert src._budget_available(1) is True   # 9 + 1 <= 10


def test_budget_resets_on_new_day():
    """Budget resets when day changes."""
    src = SolcastSource(
        session=_make_session(),
        api_key="key-123",
        site_ids=["a"],
    )
    src._request_count = 10
    src._request_date = date.today() - timedelta(days=1)
    assert src._budget_available(1) is True


# ------------------------------------------------------------------
# fetch()
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_returns_empty_without_api_key():
    """No API key returns empty dict."""
    src = SolcastSource(
        session=_make_session(),
        api_key=None,
        site_ids=["site-a"],
    )
    result = await src.fetch()
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_returns_empty_without_site_ids():
    """No site_ids returns empty dict."""
    src = SolcastSource(
        session=_make_session(),
        api_key="key-123",
        site_ids=[],
    )
    result = await src.fetch()
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_returns_empty_when_budget_exhausted():
    """Exhausted budget returns empty dict."""
    src = SolcastSource(
        session=_make_session(),
        api_key="key-123",
        site_ids=["site-a"],
    )
    src._request_count = 10
    src._request_date = date.today()
    result = await src.fetch()
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_single_site():
    """Single site fetch parses correctly and records 1 API call."""
    today = date.today()
    forecasts = [
        _make_forecast_entry(
            f"{today.isoformat()}T10:00:00Z", pv50=2.0, pv10=1.5, pv90=2.5
        ),
        _make_forecast_entry(
            f"{today.isoformat()}T11:00:00Z", pv50=3.0, pv10=2.0, pv90=4.0
        ),
    ]

    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=_make_response(forecasts))
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=mock_resp)

    src = SolcastSource(
        session=session,
        api_key="key-123",
        site_ids=["site-a"],
    )

    result = await src.fetch()

    assert src._request_count == 1
    assert "today" in result
    assert "today_kwh" in result
    # Verify the URL uses rooftop_sites endpoint
    call_args = session.get.call_args
    assert "rooftop_sites/site-a/forecasts" in call_args[0][0]


@pytest.mark.asyncio
async def test_fetch_multi_site_sums_values():
    """Multi-site fetch sums hourly values across sites and records N API calls."""
    today = date.today()

    # Use a UTC timestamp; the local hour will depend on timezone
    ts = f"{today.isoformat()}T10:00:00Z"
    # Compute expected local hour for the assertion
    expected_hour = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().hour

    # Site A: 2 kW
    forecasts_a = [
        _make_forecast_entry(ts, pv50=2.0, pv10=1.5, pv90=2.5),
    ]
    # Site B: 1 kW
    forecasts_b = [
        _make_forecast_entry(ts, pv50=1.0, pv10=0.5, pv90=1.5),
    ]

    call_count = 0

    async def mock_json(content_type=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_response(forecasts_a)
        return _make_response(forecasts_b)

    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = mock_json
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=mock_resp)

    src = SolcastSource(
        session=session,
        api_key="key-123",
        site_ids=["site-a", "site-b"],
    )

    result = await src.fetch()

    # Should have made 2 API calls
    assert src._request_count == 2
    assert session.get.call_count == 2

    # Values should be summed: 2000 + 1000 = 3000 W at the local hour
    assert result["today"][expected_hour] == 3000.0


# ------------------------------------------------------------------
# reconfigure()
# ------------------------------------------------------------------


def test_reconfigure_updates_site_ids():
    """reconfigure() updates site_ids from config dict."""
    src = SolcastSource(
        session=_make_session(),
        api_key="key-123",
        site_ids=["old-site"],
    )

    src.reconfigure({
        "solcast_api_key": "new-key",
        "solcast_site_ids": ["new-a", "new-b"],
        "panel_arrays": [{"kwp": 2.5}, {"kwp": 1.5}],
    })

    assert src.api_key == "new-key"
    assert src.site_ids == ["new-a", "new-b"]
    assert src.total_kwp == 4.0


# ------------------------------------------------------------------
# _parse_multi() — rooftop_sites field names
# ------------------------------------------------------------------


def test_parse_uses_pv_estimate_fields():
    """Parser reads pv_estimate fields from rooftop_sites endpoint."""
    today = date.today()
    forecasts = [[
        {
            "period_end": f"{today.isoformat()}T12:00:00Z",
            "period": "PT30M",
            "pv_estimate": 3.5,
            "pv_estimate10": 2.0,
            "pv_estimate90": 5.0,
        },
    ]]

    src = SolcastSource(session=_make_session(), api_key="k", site_ids=["s"])
    result = src._parse_multi(forecasts)

    # 3.5 kW = 3500 W
    total_w = sum(result["today"].values())
    assert total_w > 0  # At least some data parsed
    total_p10 = sum(result["today_p10"].values())
    total_p90 = sum(result["today_p90"].values())
    assert total_p10 < total_w < total_p90


def test_parse_missing_fields_produce_zero():
    """Missing pv_estimate fields default to 0."""
    today = date.today()
    forecasts = [[
        {
            "period_end": f"{today.isoformat()}T12:00:00Z",
            "period": "PT30M",
            # No pv_estimate fields at all
        },
    ]]

    src = SolcastSource(session=_make_session(), api_key="k", site_ids=["s"])
    result = src._parse_multi(forecasts)

    total = sum(result["today"].values()) + sum(result["tomorrow"].values())
    assert total == 0
