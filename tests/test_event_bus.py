"""Tests for BeemAI EventBus."""

from unittest.mock import MagicMock

from custom_components.beem_ai.event_bus import Event, EventBus


class TestSubscribeAndPublish:
    def test_subscribe_and_publish_fires_callback(self, event_bus):
        callback = MagicMock()
        event_bus.subscribe(Event.BATTERY_DATA_UPDATED, callback)
        event_bus.publish(Event.BATTERY_DATA_UPDATED, {"soc": 85})
        callback.assert_called_once_with({"soc": 85})

    def test_publish_passes_none_when_no_data(self, event_bus):
        callback = MagicMock()
        event_bus.subscribe(Event.PLAN_UPDATED, callback)
        event_bus.publish(Event.PLAN_UPDATED)
        callback.assert_called_once_with(None)

    def test_callback_receives_correct_data(self, event_bus):
        received = []
        event_bus.subscribe(Event.TARIFF_CHANGED, lambda d: received.append(d))
        event_bus.publish(Event.TARIFF_CHANGED, "HP")
        assert received == ["HP"]


class TestMultipleSubscribers:
    def test_multiple_subscribers_all_called(self, event_bus):
        cb1 = MagicMock()
        cb2 = MagicMock()
        cb3 = MagicMock()

        event_bus.subscribe(Event.FORECAST_UPDATED, cb1)
        event_bus.subscribe(Event.FORECAST_UPDATED, cb2)
        event_bus.subscribe(Event.FORECAST_UPDATED, cb3)

        event_bus.publish(Event.FORECAST_UPDATED, {"kwh": 20})

        cb1.assert_called_once_with({"kwh": 20})
        cb2.assert_called_once_with({"kwh": 20})
        cb3.assert_called_once_with({"kwh": 20})

    def test_subscribers_on_different_events_isolated(self, event_bus):
        cb_battery = MagicMock()
        cb_plan = MagicMock()

        event_bus.subscribe(Event.BATTERY_DATA_UPDATED, cb_battery)
        event_bus.subscribe(Event.PLAN_UPDATED, cb_plan)

        event_bus.publish(Event.BATTERY_DATA_UPDATED, "data")

        cb_battery.assert_called_once_with("data")
        cb_plan.assert_not_called()


class TestUnsubscribe:
    def test_unsubscribe_removes_callback(self, event_bus):
        callback = MagicMock()
        event_bus.subscribe(Event.MQTT_CONNECTED, callback)
        event_bus.unsubscribe(Event.MQTT_CONNECTED, callback)
        event_bus.publish(Event.MQTT_CONNECTED)
        callback.assert_not_called()

    def test_unsubscribe_nonexistent_callback_no_error(self, event_bus):
        callback = MagicMock()
        # Should not raise
        event_bus.unsubscribe(Event.MQTT_CONNECTED, callback)

    def test_unsubscribe_one_keeps_others(self, event_bus):
        cb1 = MagicMock()
        cb2 = MagicMock()

        event_bus.subscribe(Event.SAFETY_ALERT, cb1)
        event_bus.subscribe(Event.SAFETY_ALERT, cb2)
        event_bus.unsubscribe(Event.SAFETY_ALERT, cb1)

        event_bus.publish(Event.SAFETY_ALERT, "alert!")

        cb1.assert_not_called()
        cb2.assert_called_once_with("alert!")


class TestExceptionHandling:
    def test_exception_in_callback_does_not_crash_others(self, event_bus):
        cb_good = MagicMock()

        def cb_bad(data):
            raise ValueError("boom")

        event_bus.subscribe(Event.PLAN_UPDATED, cb_bad)
        event_bus.subscribe(Event.PLAN_UPDATED, cb_good)

        # Should not raise
        event_bus.publish(Event.PLAN_UPDATED, "test")

        cb_good.assert_called_once_with("test")

    def test_exception_does_not_remove_subscriber(self, event_bus):
        call_count = []

        def cb_flaky(data):
            call_count.append(1)
            if len(call_count) == 1:
                raise RuntimeError("first call fails")

        event_bus.subscribe(Event.TARIFF_CHANGED, cb_flaky)
        event_bus.publish(Event.TARIFF_CHANGED, "first")
        event_bus.publish(Event.TARIFF_CHANGED, "second")

        assert len(call_count) == 2


class TestPublishNoSubscribers:
    def test_publish_with_no_subscribers_is_safe(self, event_bus):
        # Should not raise
        event_bus.publish(Event.SYSTEM_ENABLED, {"state": True})

    def test_publish_unknown_event_after_other_subscriptions(self, event_bus):
        event_bus.subscribe(Event.BATTERY_DATA_UPDATED, MagicMock())
        # Publishing a different event with no subscribers should be fine
        event_bus.publish(Event.WATER_HEATER_CHANGED, "on")
