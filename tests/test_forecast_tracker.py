"""Tests for the ForecastTracker module."""

from datetime import datetime, timedelta

import pytest

from custom_components.beem_ai.forecast_tracker import ForecastTracker, _MAX_HISTORY_DAYS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tracker(tmp_path):
    return ForecastTracker(data_dir=tmp_path)


def _date_str(days_ago: int = 0) -> str:
    """Return an ISO date string N days in the past."""
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Tests — record_actual and get_bias
# ---------------------------------------------------------------------------

class TestBias:
    def test_positive_bias_over_predicts(self, tracker):
        tracker.record_actual(_date_str(1), "src", predicted_kwh=10.0, actual_kwh=8.0)
        tracker.record_actual(_date_str(2), "src", predicted_kwh=12.0, actual_kwh=9.0)
        # bias = ((10-8) + (12-9)) / 2 = 2.5
        assert tracker.get_bias("src") == pytest.approx(2.5)

    def test_negative_bias_under_predicts(self, tracker):
        tracker.record_actual(_date_str(1), "src", predicted_kwh=5.0, actual_kwh=8.0)
        # bias = (5 - 8) / 1 = -3.0
        assert tracker.get_bias("src") == pytest.approx(-3.0)

    def test_no_records_returns_zero(self, tracker):
        assert tracker.get_bias("nonexistent") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Tests — get_accuracy
# ---------------------------------------------------------------------------

class TestAccuracy:
    def test_perfect_predictions(self, tracker):
        for d in range(5):
            tracker.record_actual(_date_str(d + 1), "src", predicted_kwh=10.0, actual_kwh=10.0)
        assert tracker.get_accuracy("src") == pytest.approx(1.0)

    def test_imperfect_predictions(self, tracker):
        # predicted=10, actual=8 => MAE=2, mean_actual=8
        # accuracy = 1 - 2/8 = 0.75
        tracker.record_actual(_date_str(1), "src", predicted_kwh=10.0, actual_kwh=8.0)
        assert tracker.get_accuracy("src") == pytest.approx(0.75)

    def test_very_bad_predictions_clamped_to_zero(self, tracker):
        # predicted=100, actual=1 => MAE=99, mean_actual=1
        # accuracy = 1 - 99/1 = -98 => clamped to 0.0
        tracker.record_actual(_date_str(1), "src", predicted_kwh=100.0, actual_kwh=1.0)
        assert tracker.get_accuracy("src") == pytest.approx(0.0)

    def test_no_records_returns_zero(self, tracker):
        assert tracker.get_accuracy("unknown") == pytest.approx(0.0)

    def test_zero_actual_returns_zero(self, tracker):
        tracker.record_actual(_date_str(1), "src", predicted_kwh=5.0, actual_kwh=0.0)
        assert tracker.get_accuracy("src") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Tests — get_weights
# ---------------------------------------------------------------------------

class TestWeights:
    def test_weights_normalised_to_one(self, tracker):
        for d in range(5):
            tracker.record_actual(_date_str(d + 1), "a", predicted_kwh=10.0, actual_kwh=10.0)
            tracker.record_actual(_date_str(d + 1), "b", predicted_kwh=10.0, actual_kwh=8.0)

        weights = tracker.get_weights(["a", "b"])
        assert sum(weights.values()) == pytest.approx(1.0)
        # Source 'a' is perfect, should have higher weight
        assert weights["a"] > weights["b"]

    def test_equal_accuracy_equal_weights(self, tracker):
        for d in range(5):
            tracker.record_actual(_date_str(d + 1), "x", predicted_kwh=10.0, actual_kwh=10.0)
            tracker.record_actual(_date_str(d + 1), "y", predicted_kwh=10.0, actual_kwh=10.0)

        weights = tracker.get_weights(["x", "y"])
        assert weights["x"] == pytest.approx(weights["y"], abs=0.01)

    def test_no_records_equal_weights(self, tracker):
        weights = tracker.get_weights(["a", "b", "c"])
        assert sum(weights.values()) == pytest.approx(1.0)
        # All get floor weight (0.01 each), normalised
        for w in weights.values():
            assert w == pytest.approx(1.0 / 3, abs=0.01)


# ---------------------------------------------------------------------------
# Tests — detect_bad_weather_streak
# ---------------------------------------------------------------------------

class TestBadWeatherStreak:
    def test_streak_detected(self, tracker):
        # Last 3 days: actual < predicted * 0.5
        for d in range(3):
            tracker.record_actual(
                _date_str(d), "src", predicted_kwh=10.0, actual_kwh=3.0
            )
        assert tracker.detect_bad_weather_streak(days=3) is True

    def test_no_streak(self, tracker):
        # Last 3 days: actual is reasonable
        for d in range(3):
            tracker.record_actual(
                _date_str(d), "src", predicted_kwh=10.0, actual_kwh=8.0
            )
        assert tracker.detect_bad_weather_streak(days=3) is False

    def test_insufficient_data_returns_false(self, tracker):
        tracker.record_actual(_date_str(0), "src", predicted_kwh=10.0, actual_kwh=1.0)
        assert tracker.detect_bad_weather_streak(days=3) is False

    def test_no_records_returns_false(self, tracker):
        assert tracker.detect_bad_weather_streak() is False


# ---------------------------------------------------------------------------
# Tests — Save / Load roundtrip
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        tracker1 = ForecastTracker(data_dir=tmp_path)
        for d in range(5):
            tracker1.record_actual(
                _date_str(d + 1), "src_a", predicted_kwh=10.0, actual_kwh=9.0
            )
            tracker1.record_actual(
                _date_str(d + 1), "src_b", predicted_kwh=8.0, actual_kwh=7.0
            )
        tracker1.save()

        tracker2 = ForecastTracker(data_dir=tmp_path)
        tracker2.load()

        assert tracker2.get_bias("src_a") == pytest.approx(tracker1.get_bias("src_a"))
        assert tracker2.get_accuracy("src_b") == pytest.approx(tracker1.get_accuracy("src_b"))

    def test_load_missing_file_no_crash(self, tmp_path):
        tracker = ForecastTracker(data_dir=tmp_path)
        tracker.load()
        assert tracker.get_bias("anything") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Tests — Pruning old records
# ---------------------------------------------------------------------------

class TestPruning:
    def test_old_records_pruned(self, tmp_path):
        tracker = ForecastTracker(data_dir=tmp_path)

        # Insert a record from 100 days ago (beyond the 90-day window)
        old_date = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
        recent_date = _date_str(1)

        tracker.record_actual(old_date, "src", predicted_kwh=10.0, actual_kwh=5.0)
        tracker.record_actual(recent_date, "src", predicted_kwh=10.0, actual_kwh=9.0)

        # The old record should have been pruned by record_actual
        assert len(tracker._records["src"]) == 1
        assert tracker._records["src"][0]["date"] == recent_date

    def test_pruning_on_load(self, tmp_path):
        """Records older than 90 days are pruned when loading."""
        tracker1 = ForecastTracker(data_dir=tmp_path)

        old_date = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
        recent_date = _date_str(1)

        # Directly inject old record bypassing _prune to simulate stale file
        tracker1._records["src"] = [
            {"date": old_date, "predicted_kwh": 10.0, "actual_kwh": 5.0},
            {"date": recent_date, "predicted_kwh": 10.0, "actual_kwh": 9.0},
        ]
        tracker1.save()

        tracker2 = ForecastTracker(data_dir=tmp_path)
        tracker2.load()

        # After load, the old record should be pruned
        assert len(tracker2._records["src"]) == 1
        assert tracker2._records["src"][0]["date"] == recent_date


# ---------------------------------------------------------------------------
# Tests — Safe defaults with no data
# ---------------------------------------------------------------------------

class TestSafeDefaults:
    def test_bias_default(self, tracker):
        assert tracker.get_bias("any") == pytest.approx(0.0)

    def test_accuracy_default(self, tracker):
        assert tracker.get_accuracy("any") == pytest.approx(0.0)

    def test_bad_weather_default(self, tracker):
        assert tracker.detect_bad_weather_streak() is False

    def test_weights_no_data(self, tracker):
        weights = tracker.get_weights(["a", "b"])
        assert sum(weights.values()) == pytest.approx(1.0)
