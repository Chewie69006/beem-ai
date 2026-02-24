"""Tracks forecast accuracy and computes bias correction."""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_HISTORY_DAYS = 90


class ForecastTracker:
    """Tracks actual solar production vs forecast predictions.

    Records per-source accuracy, computes bias correction factors,
    and generates accuracy-proportional weights for ensemble forecasting.
    """

    def __init__(self, data_dir: str | Path):
        self._data_dir = Path(data_dir)
        self._file_path = self._data_dir / "forecast_accuracy.json"

        # _records[source_name] = [{"date": str, "predicted_kwh": float, "actual_kwh": float}]
        self._records: dict[str, list[dict]] = {}

    def record_actual(
        self,
        date: str,
        source_name: str,
        predicted_kwh: float,
        actual_kwh: float,
    ) -> None:
        """Add an accuracy record for a forecast source.

        Automatically prunes records older than 90 days.
        """
        if source_name not in self._records:
            self._records[source_name] = []

        self._records[source_name].append({
            "date": date,
            "predicted_kwh": predicted_kwh,
            "actual_kwh": actual_kwh,
        })

        self._prune(source_name)

    def get_bias(self, source_name: str, days: int = 30) -> float:
        """Average (predicted - actual) over the last N days.

        Positive value means the source over-predicts.
        Returns 0.0 if no records exist.
        """
        records = self._recent_records(source_name, days)
        if not records:
            return 0.0

        total_bias = sum(r["predicted_kwh"] - r["actual_kwh"] for r in records)
        return total_bias / len(records)

    def get_accuracy(self, source_name: str, days: int = 30) -> float:
        """Compute accuracy as 1 - MAE / mean_actual, clamped to [0, 1].

        Returns 0.0 if no records or mean actual is zero.
        """
        records = self._recent_records(source_name, days)
        if not records:
            return 0.0

        mean_actual = sum(r["actual_kwh"] for r in records) / len(records)
        if mean_actual <= 0:
            return 0.0

        mae = sum(abs(r["predicted_kwh"] - r["actual_kwh"]) for r in records) / len(records)
        accuracy = 1.0 - mae / mean_actual
        return max(0.0, min(1.0, accuracy))

    def get_weights(self, source_names: list[str], days: int = 30) -> dict[str, float]:
        """Compute accuracy-proportional weights for ensemble forecasting.

        Weights are normalized to sum to 1.0.
        Sources with zero accuracy get a small floor weight.
        """
        accuracies = {name: self.get_accuracy(name, days) for name in source_names}

        # Use a small floor so new/bad sources aren't completely excluded
        floor = 0.01
        adjusted = {name: max(acc, floor) for name, acc in accuracies.items()}

        total = sum(adjusted.values())
        if total <= 0:
            # Equal weights as fallback
            equal = 1.0 / len(source_names) if source_names else 0.0
            return {name: equal for name in source_names}

        return {name: val / total for name, val in adjusted.items()}

    def detect_bad_weather_streak(self, days: int = 3) -> bool:
        """True if the last N days all had actual < predicted * 0.5.

        Checks across all tracked sources. A deficit pattern suggests
        sustained bad weather that forecasts are not capturing.
        Returns False if insufficient data.
        """
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        for source_name, records in self._records.items():
            recent = [r for r in records if r["date"] > cutoff]
            if len(recent) < days:
                continue

            # Check the last N records for this source
            last_n = sorted(recent, key=lambda r: r["date"])[-days:]
            all_deficit = all(
                r["actual_kwh"] < r["predicted_kwh"] * 0.5 for r in last_n
            )
            if all_deficit:
                return True

        return False

    def save(self) -> None:
        """Persist accuracy records to data/forecast_accuracy.json."""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with open(self._file_path, "w") as f:
                json.dump(self._records, f, indent=2)
            log.debug("Saved forecast accuracy to %s", self._file_path)
        except OSError:
            log.exception("Failed to save forecast accuracy")

    def load(self) -> None:
        """Load persisted records from data/forecast_accuracy.json."""
        if not self._file_path.exists():
            log.info("No forecast accuracy data found at %s, starting fresh", self._file_path)
            return

        try:
            with open(self._file_path) as f:
                data = json.load(f)

            if isinstance(data, dict):
                self._records = data
                # Prune all sources on load
                for source_name in list(self._records):
                    self._prune(source_name)
                log.info("Loaded forecast accuracy from %s", self._file_path)
            else:
                log.warning("Invalid format in %s, starting fresh", self._file_path)
        except (OSError, json.JSONDecodeError, ValueError):
            log.exception("Failed to load forecast accuracy, starting fresh")

    def _recent_records(self, source_name: str, days: int) -> list[dict]:
        """Return records from the last N days for a source."""
        if source_name not in self._records:
            return []

        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return [r for r in self._records[source_name] if r["date"] > cutoff]

    def _prune(self, source_name: str) -> None:
        """Remove records older than 90 days."""
        if source_name not in self._records:
            return

        cutoff = (datetime.now() - timedelta(days=_MAX_HISTORY_DAYS)).strftime("%Y-%m-%d")
        self._records[source_name] = [
            r for r in self._records[source_name] if r["date"] > cutoff
        ]
