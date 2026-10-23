"""Tests for GridChargeTracker — detecting a grid charge of the battery."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from custom_components.beem_ai.grid_charge_tracker import (
    GRID_CHARGE_HOLD,
    GRID_CHARGE_MIN_W,
    GRID_CHARGE_SUSTAIN_S,
    GridChargeTracker,
)

NIGHT = datetime(2026, 10, 22, 2, 0, tzinfo=timezone.utc)


def _feed(tracker, t, battery_w, import_w, now=None):
    """One live sample at monotonic time ``t``."""
    with patch("time.monotonic", return_value=t):
        return tracker.update(
            battery_power_w=battery_w,
            import_w=import_w,
            now=now or NIGHT + timedelta(seconds=t),
        )


def _grid_charge(tracker, start=0.0, duration=GRID_CHARGE_SUSTAIN_S, step=10):
    """Off-peak charge: 2.5 kW into the battery, house on the grid too."""
    changed = []
    t = start
    while t <= start + duration:
        changed.append(_feed(tracker, t, battery_w=2500, import_w=2900))
        t += step
    return changed


def test_fresh_tracker_is_not_reserved():
    tracker = GridChargeTracker()
    assert tracker.is_reserved(NIGHT) is False
    assert tracker.last_charge_at is None
    assert tracker.reserved_until is None


def test_sustained_grid_charge_reserves_the_battery():
    tracker = GridChargeTracker()

    changed = _grid_charge(tracker)

    assert tracker.charging is True
    assert tracker.is_reserved(NIGHT) is True
    # Persisted once, when the charge is confirmed.
    assert changed.count(True) == 1
    assert changed[-1] is True


def test_short_grid_charge_is_ignored():
    """Under GRID_CHARGE_SUSTAIN_S — e.g. a load spike the Beem takes a
    few seconds to regulate away while charging from the panels."""
    tracker = GridChargeTracker()

    _feed(tracker, 0, battery_w=2000, import_w=1500)
    _feed(tracker, GRID_CHARGE_SUSTAIN_S - 1, battery_w=2000, import_w=1500)

    assert tracker.charging is False
    assert tracker.is_reserved(NIGHT) is False


def test_spikes_never_add_up():
    """Each sample without grid charge restarts the sustain window."""
    tracker = GridChargeTracker()
    for i in range(20):
        t = i * 60.0
        _feed(tracker, t, battery_w=2000, import_w=1500)
        _feed(tracker, t + 10, battery_w=2000, import_w=0)

    assert tracker.is_reserved(NIGHT + timedelta(hours=1)) is False


def test_solar_charge_is_not_grid_charge():
    tracker = GridChargeTracker()
    for t in range(0, 3600, 10):
        _feed(tracker, t, battery_w=1500, import_w=0)
    assert tracker.is_reserved(NIGHT) is False


def test_import_while_discharging_is_not_grid_charge():
    """Evening peak: the battery discharges and the house still imports."""
    tracker = GridChargeTracker()
    for t in range(0, 3600, 10):
        _feed(tracker, t, battery_w=-3000, import_w=2000)
    assert tracker.is_reserved(NIGHT) is False


def test_only_the_part_the_import_covers_counts():
    """Battery charging 1.5 kW from the panels with 100 W of import: the
    grid share (100 W) is under GRID_CHARGE_MIN_W."""
    tracker = GridChargeTracker()
    for t in range(0, 600, 10):
        _feed(tracker, t, battery_w=1500, import_w=GRID_CHARGE_MIN_W - 1)
    assert tracker.is_reserved(NIGHT) is False


def test_reserve_lasts_the_hold_after_the_last_grid_charge():
    tracker = GridChargeTracker()
    _grid_charge(tracker, duration=3600)
    end = NIGHT + timedelta(seconds=3600)

    changed = _feed(tracker, 3610, battery_w=-300, import_w=0)

    assert changed is True  # charge ended: persist the final timestamp
    assert tracker.charging is False
    assert tracker.last_charge_at == end
    assert tracker.reserved_until == end + GRID_CHARGE_HOLD
    assert tracker.is_reserved(end + GRID_CHARGE_HOLD - timedelta(minutes=1))
    assert not tracker.is_reserved(end + GRID_CHARGE_HOLD)


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "grid_charge.json"
    tracker = GridChargeTracker(path)
    _grid_charge(tracker)
    tracker.save()

    restored = GridChargeTracker(path)
    restored.load()

    assert restored.last_charge_at == tracker.last_charge_at
    assert restored.is_reserved(NIGHT + timedelta(hours=8)) is True
    # Not charging any more until live samples say so.
    assert restored.charging is False


def test_load_missing_or_corrupt_file(tmp_path):
    missing = GridChargeTracker(tmp_path / "nope.json")
    missing.load()
    assert missing.last_charge_at is None

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    corrupt = GridChargeTracker(bad)
    corrupt.load()
    assert corrupt.last_charge_at is None


def test_load_naive_timestamp_is_utc(tmp_path):
    path = tmp_path / "grid_charge.json"
    path.write_text(json.dumps({"last_charge_at": "2026-10-22T05:00:00"}))
    tracker = GridChargeTracker(path)
    tracker.load()
    assert tracker.last_charge_at == datetime(
        2026, 10, 22, 5, 0, tzinfo=timezone.utc
    )


def test_save_without_path_is_a_noop():
    GridChargeTracker().save()  # must not raise
