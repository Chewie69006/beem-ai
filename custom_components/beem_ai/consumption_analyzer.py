"""Historical consumption learning using Exponential Moving Average."""

import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_CONSUMPTION_W = 500.0
_EMA_ALPHA = 0.1
_ANOMALY_STDDEV_THRESHOLD = 3.0


class ConsumptionAnalyzer:
    """Learns household consumption patterns per day-of-week and hour.

    Uses EMA (Exponential Moving Average) with alpha=0.1 to smooth
    consumption readings into 168 buckets (7 days x 24 hours).
    Tracks variance via Welford's online algorithm for anomaly detection.
    """

    def __init__(self, data_dir: str | Path):
        self._data_dir = Path(data_dir)
        self._file_path = self._data_dir / "consumption_history.json"

        # EMA values: _ema[day_of_week][hour] = average watts
        self._ema: dict[int, dict[int, float]] = {}
        # Welford's online stats: count, mean, M2
        self._count: dict[int, dict[int, int]] = {}
        self._mean: dict[int, dict[int, float]] = {}
        self._m2: dict[int, dict[int, float]] = {}

        self._init_buckets()

    def _init_buckets(self) -> None:
        """Initialize all 168 buckets with defaults."""
        for day in range(7):
            self._ema[day] = {h: _DEFAULT_CONSUMPTION_W for h in range(24)}
            self._count[day] = {h: 0 for h in range(24)}
            self._mean[day] = {h: _DEFAULT_CONSUMPTION_W for h in range(24)}
            self._m2[day] = {h: 0.0 for h in range(24)}

    def record_consumption(self, consumption_w: float) -> None:
        """Record a consumption reading for the current time slot.

        Updates both the EMA and the Welford running statistics.
        Called on each MQTT tick.
        """
        now = datetime.now()
        day = now.weekday()
        hour = now.hour

        # Update EMA: new = alpha * observation + (1 - alpha) * old
        old_ema = self._ema[day][hour]
        self._ema[day][hour] = _EMA_ALPHA * consumption_w + (1 - _EMA_ALPHA) * old_ema

        # Update Welford's online algorithm for variance tracking
        self._count[day][hour] += 1
        n = self._count[day][hour]
        old_mean = self._mean[day][hour]
        delta = consumption_w - old_mean
        self._mean[day][hour] = old_mean + delta / n
        delta2 = consumption_w - self._mean[day][hour]
        self._m2[day][hour] += delta * delta2

    def get_hourly_forecast(self, day_of_week: int) -> dict[int, float]:
        """Return {hour: avg_watts} for a given day of the week."""
        return dict(self._ema.get(day_of_week, {}))

    def get_forecast_kwh_tomorrow(self) -> float:
        """Sum hourly EMA for tomorrow's day-of-week, converted to kWh."""
        tomorrow = (datetime.now() + timedelta(days=1)).weekday()
        hourly = self._ema.get(tomorrow, {})
        total_wh = sum(hourly.values())  # Each bucket is 1 hour of watts
        return total_wh / 1000.0

    def get_forecast_kwh_today_remaining(self) -> float:
        """Sum EMA from current hour+1 to 23 for today, converted to kWh."""
        now = datetime.now()
        day = now.weekday()
        current_hour = now.hour
        hourly = self._ema.get(day, {})
        total_wh = sum(
            hourly.get(h, _DEFAULT_CONSUMPTION_W) for h in range(current_hour + 1, 24)
        )
        return total_wh / 1000.0

    def get_hourly_consumption_forecast_tomorrow(self) -> dict[int, float]:
        """Return {hour: watts} for tomorrow's day-of-week."""
        tomorrow = (datetime.now() + timedelta(days=1)).weekday()
        return self.get_hourly_forecast(tomorrow)

    def is_anomaly(self, consumption_w: float) -> bool:
        """True if current reading > 3 standard deviations from the mean.

        Uses Welford's tracked variance for the current time slot.
        Returns False if insufficient data (< 2 samples).
        """
        now = datetime.now()
        day = now.weekday()
        hour = now.hour

        n = self._count[day][hour]
        if n < 2:
            return False

        variance = self._m2[day][hour] / n
        if variance <= 0:
            return False

        stddev = math.sqrt(variance)
        mean = self._mean[day][hour]

        return abs(consumption_w - mean) > _ANOMALY_STDDEV_THRESHOLD * stddev

    def save(self) -> None:
        """Persist analytics data to data/consumption_history.json."""
        data = {
            "ema": {str(d): {str(h): v for h, v in hours.items()} for d, hours in self._ema.items()},
            "count": {str(d): {str(h): v for h, v in hours.items()} for d, hours in self._count.items()},
            "mean": {str(d): {str(h): v for h, v in hours.items()} for d, hours in self._mean.items()},
            "m2": {str(d): {str(h): v for h, v in hours.items()} for d, hours in self._m2.items()},
        }
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with open(self._file_path, "w") as f:
                json.dump(data, f, indent=2)
            log.debug("Saved consumption history to %s", self._file_path)
        except OSError:
            log.exception("Failed to save consumption history")

    def load(self) -> None:
        """Load persisted data from data/consumption_history.json."""
        if not self._file_path.exists():
            log.info("No consumption history found at %s, using defaults", self._file_path)
            return

        try:
            with open(self._file_path) as f:
                data = json.load(f)

            for day_str, hours in data.get("ema", {}).items():
                day = int(day_str)
                for hour_str, value in hours.items():
                    hour = int(hour_str)
                    self._ema[day][hour] = float(value)

            for day_str, hours in data.get("count", {}).items():
                day = int(day_str)
                for hour_str, value in hours.items():
                    hour = int(hour_str)
                    self._count[day][hour] = int(value)

            for day_str, hours in data.get("mean", {}).items():
                day = int(day_str)
                for hour_str, value in hours.items():
                    hour = int(hour_str)
                    self._mean[day][hour] = float(value)

            for day_str, hours in data.get("m2", {}).items():
                day = int(day_str)
                for hour_str, value in hours.items():
                    hour = int(hour_str)
                    self._m2[day][hour] = float(value)

            log.info("Loaded consumption history from %s", self._file_path)
        except (OSError, json.JSONDecodeError, ValueError):
            log.exception("Failed to load consumption history, using defaults")
