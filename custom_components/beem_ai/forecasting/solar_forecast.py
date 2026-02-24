"""Ensemble solar forecast aggregator (async).

Combines multiple forecast sources (Open-Meteo, Forecast.Solar, Solcast)
into a single weighted-average forecast and publishes the result to the
shared StateStore via the internal EventBus.
"""

import logging

from ..event_bus import Event

log = logging.getLogger(__name__)

# Default P10/P90 scaling when Solcast is unavailable
P10_SCALE = 0.7
P90_SCALE = 1.3


class SolarForecast:
    """Aggregate forecasts from multiple sources into a single view."""

    def __init__(self, state_store, event_bus, sources: list):
        self._state_store = state_store
        self._event_bus = event_bus
        self._sources = sources

        # Equal initial weights keyed by source name
        self._weights: dict[str, float] = {}
        if sources:
            equal_w = 1.0 / len(sources)
            for src in sources:
                self._weights[src.name] = equal_w

        self.sources_used: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reconfigure(self, config: dict):
        """Propagate config changes to all sources."""
        for source in self._sources:
            if hasattr(source, "reconfigure"):
                source.reconfigure(config)

    def set_weights(self, weights: dict[str, float]):
        """Update source weights (e.g. after accuracy tracking).

        *weights* maps source name -> float.  Values are normalised
        internally so they don't need to sum to 1.
        """
        self._weights.update(weights)

    async def refresh(self):
        """Fetch from every source, merge, and publish the result."""
        results: list[tuple[str, dict]] = []

        for source in self._sources:
            try:
                data = await source.fetch()
                if data:
                    results.append((source.name, data))
                    log.info("Source %s returned data", source.name)
                else:
                    log.warning("Source %s returned empty data", source.name)
            except Exception:
                log.exception("Source %s failed", source.name)

        self.sources_used = [name for name, _ in results]

        if not results:
            log.warning("All forecast sources failed -- no update")
            self._publish(confidence="low")
            return

        # Merge hourly values via weighted average
        today = self._weighted_merge(results, "today")
        tomorrow = self._weighted_merge(results, "tomorrow")

        # Daily kWh -- weighted average of source totals
        today_kwh = self._weighted_scalar(results, "today_kwh")
        tomorrow_kwh = self._weighted_scalar(results, "tomorrow_kwh")

        # Confidence intervals
        solcast_data = self._find_source(results, "solcast")

        if solcast_data:
            today_p10 = solcast_data.get("today_p10", {})
            today_p90 = solcast_data.get("today_p90", {})
            tomorrow_p10 = solcast_data.get("tomorrow_p10", {})
            tomorrow_p90 = solcast_data.get("tomorrow_p90", {})
        else:
            today_p10 = {h: round(w * P10_SCALE, 1) for h, w in today.items()}
            today_p90 = {h: round(w * P90_SCALE, 1) for h, w in today.items()}
            tomorrow_p10 = {h: round(w * P10_SCALE, 1) for h, w in tomorrow.items()}
            tomorrow_p90 = {h: round(w * P90_SCALE, 1) for h, w in tomorrow.items()}

        confidence = self._determine_confidence(len(results))

        self._state_store.update_forecast(
            solar_today=today,
            solar_tomorrow=tomorrow,
            solar_today_kwh=round(today_kwh, 2),
            solar_tomorrow_kwh=round(tomorrow_kwh, 2),
            solar_today_p10=today_p10,
            solar_today_p90=today_p90,
            solar_tomorrow_p10=tomorrow_p10,
            solar_tomorrow_p90=tomorrow_p90,
            sources_used=list(self.sources_used),
            confidence=confidence,
        )

        self._publish(confidence)

        log.info(
            "Forecast updated -- sources=%s, confidence=%s, "
            "today=%.2f kWh, tomorrow=%.2f kWh",
            self.sources_used,
            confidence,
            today_kwh,
            tomorrow_kwh,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _publish(self, confidence: str):
        self._event_bus.publish(
            Event.FORECAST_UPDATED,
            {"sources_used": self.sources_used, "confidence": confidence},
        )

    @staticmethod
    def _determine_confidence(source_count: int) -> str:
        if source_count >= 3:
            return "high"
        if source_count == 2:
            return "medium"
        return "low"

    def _active_weights(self, source_names: list[str]) -> dict[str, float]:
        """Return normalised weights for the given sources."""
        raw = {name: self._weights.get(name, 1.0) for name in source_names}
        total = sum(raw.values())
        if total == 0:
            equal = 1.0 / max(len(raw), 1)
            return {name: equal for name in raw}
        return {name: w / total for name, w in raw.items()}

    def _weighted_merge(
        self, results: list[tuple[str, dict]], key: str
    ) -> dict[int, float]:
        """Merge hourly dicts from multiple sources using weighted average."""
        source_names = [name for name, _ in results]
        weights = self._active_weights(source_names)

        # Collect all hours across sources
        all_hours: set[int] = set()
        for _, data in results:
            hourly = data.get(key, {})
            all_hours.update(hourly.keys())

        merged: dict[int, float] = {}
        for hour in sorted(all_hours):
            weighted_sum = 0.0
            weight_sum = 0.0
            for name, data in results:
                hourly = data.get(key, {})
                if hour in hourly:
                    w = weights[name]
                    weighted_sum += hourly[hour] * w
                    weight_sum += w
            if weight_sum > 0:
                merged[hour] = round(weighted_sum / weight_sum, 1)

        return merged

    def _weighted_scalar(
        self, results: list[tuple[str, dict]], key: str
    ) -> float:
        """Weighted average of a scalar value across sources."""
        source_names = [name for name, _ in results]
        weights = self._active_weights(source_names)

        weighted_sum = 0.0
        weight_sum = 0.0
        for name, data in results:
            val = data.get(key, 0.0)
            if val:
                w = weights[name]
                weighted_sum += val * w
                weight_sum += w
        if weight_sum > 0:
            return weighted_sum / weight_sum
        return 0.0

    @staticmethod
    def _find_source(
        results: list[tuple[str, dict]], name: str
    ) -> dict | None:
        """Return data dict for a specific source, or None."""
        for src_name, data in results:
            if src_name == name:
                return data
        return None
