import copy
from datetime import datetime
import json
from pathlib import Path
import threading
import tomllib
import unittest
from zoneinfo import ZoneInfo
from unittest.mock import Mock, patch

import bus_display as app
import journeys


class BusTests(unittest.TestCase):
    def setUp(self):
        self.config = tomllib.loads((Path(__file__).resolve().parent / "fixture_config.toml").read_text())
        self.config.update(enabled=True)
        self.config["bus"] = {"stop_id": "1000001", "routes": ["A1"], "direction": "0"}

    def test_config_example_is_disabled(self):
        cfg = copy.deepcopy(self.config)
        cfg["enabled"] = False
        self.assertFalse(app.active(cfg, datetime.fromisoformat("2026-11-13T08:00:00-05:00")))

    def test_schedule_boundaries(self):
        for clock, expected in [("06:59", False), ("07:00", True), ("08:59", True), ("09:00", False)]:
            with self.subTest(clock=clock):
                self.assertEqual(app.active(self.config, datetime.fromisoformat(f"2026-11-13T{clock}:00-05:00")), expected)

    def test_weekend_and_holiday(self):
        self.assertFalse(app.active(self.config, datetime.fromisoformat("2026-11-14T08:00:00-05:00")))
        self.config["skip_dates"] = ["2026-11-13"]
        self.assertFalse(app.active(self.config, datetime.fromisoformat("2026-11-13T08:00:00-05:00")))

    def test_dst(self):
        for stamp in ["2026-10-30T11:00:00+00:00", "2026-11-02T12:00:00+00:00"]:
            self.assertTrue(app.active(self.config, datetime.fromisoformat(stamp)))
        self.assertFalse(app.active(self.config, datetime.fromisoformat("2026-11-02T11:00:00+00:00")))

    def prediction(self, minutes, route="A1", direction="0", trip=""):
        return {"Minutes": minutes, "RouteID": route, "DirectionNum": direction, "TripID": trip}

    def test_filter_sort_and_limit(self):
        data = {"Predictions": [self.prediction(17), self.prediction(1, "B2"), self.prediction(2, direction="1"), self.prediction(8), self.prediction(30)]}
        self.assertEqual(app.arrivals(data, self.config["bus"]), [(8, "A1"), (17, "A1")])

    def test_dedupe_trip(self):
        row = self.prediction(8, trip="example")
        self.assertEqual(app.arrivals({"Predictions": [row, row]}, self.config["bus"]), [(8, "A1")])

    def test_empty_is_distinct_from_bad_response(self):
        self.assertEqual(app.arrivals({"Predictions": []}, self.config["bus"]), [])
        for data in [{}, {"Predictions": None}, {"message": "Unauthorized"}]:
            with self.assertRaises(ValueError):
                app.arrivals(data, self.config["bus"])

    def test_reject_bad_minutes(self):
        for minutes in [-1, True, "5", None, 1.5]:
            with self.assertRaises(ValueError):
                app.arrivals({"Predictions": [self.prediction(minutes)]}, self.config["bus"])

    def test_due_and_empty_display(self):
        self.assertEqual(app.arrival_screen([(0, "A1")])["text"][0]["content"], "A1 DUE")
        self.assertEqual(app.arrival_screen([])["text"][0]["content"], "NO TIMES")

    def test_blank_does_not_delete_app(self):
        self.assertTrue(app.blank_screen())
        self.assertEqual(app.blank_screen()["draw"][0]["df"], [0, 0, 52, 16, "#000000"])

    def test_device_push_then_switch(self):
        device = app.Device({"host": "192.168.1.55", "app_name": "school_bus"})
        with patch.object(app, "request_json", return_value={}) as request:
            device.show(app.blank_screen())
        urls = [call.args[0] for call in request.call_args_list]
        self.assertTrue(urls[0].endswith("/api/custom?name=school_bus"))
        self.assertTrue(urls[1].endswith("/api/switchDiyApp?name=school_bus"))

    def test_key_is_header_not_url(self):
        with patch.object(app, "request_json", return_value={}) as request:
            app.fetch_predictions("1000001", "test-secret")
        self.assertNotIn("test-secret", request.call_args.args[0])
        self.assertEqual(request.call_args.kwargs["headers"], {"api_key": "test-secret"})

    def test_failure_replaces_numbers_and_shutdown_blanks(self):
        device = Mock()
        stop = threading.Event()
        device.show.side_effect = lambda payload: stop.set()
        with patch.object(app, "active", return_value=True), patch.object(app, "fetch_predictions", side_effect=TimeoutError):
            app.run(self.config, device, "test-key", stop)
        self.assertEqual(device.show.call_args_list[0].args[0]["text"][0]["content"], "NO DATA")
        self.assertEqual(device.show.call_args_list[-1].args[0], app.blank_screen())

    def test_outside_window_never_fetches(self):
        device = Mock()
        stop = threading.Event()
        device.show.side_effect = lambda payload: stop.set()
        with patch.object(app, "active", return_value=False), patch.object(app, "fetch_predictions") as fetch:
            app.run(self.config, device, "test-key", stop)
        fetch.assert_not_called()

    def test_reject_overnight_window_and_bad_routes(self):
        self.config["end"] = "06:00"
        with self.assertRaises(ValueError):
            app.validate(self.config)
        self.config["bus"]["routes"] = []
        with self.assertRaises(ValueError):
            app.validate_bus(self.config)



class SchoolCalendarTests(unittest.TestCase):
    def setUp(self):
        self.config = app.validate(tomllib.loads((Path(__file__).resolve().parent / "fixture_config.toml").read_text()))
        self.config["enabled"] = True

    def at(self, day):
        local = datetime.fromisoformat(f"{day}T08:00:00").replace(tzinfo=ZoneInfo("America/New_York"))
        return app.active(self.config, local)

    def test_school_year_bounds_are_inclusive(self):
        self.assertTrue(self.at("2027-06-16"))
        self.assertFalse(self.at("2027-06-17"))
        self.assertFalse(self.at("2026-08-21"))

    def test_calendar_closures_are_dark(self):
        for day in ("2026-11-25", "2026-12-31", "2027-01-01", "2027-03-22"):
            self.assertFalse(self.at(day), day)
        self.assertTrue(self.at("2026-11-23"))  # virtual conference day kept on deliberately

    def test_bad_school_year_rejected(self):
        self.config["school_year"] = ["2027-06-16", "2026-08-24"]
        with self.assertRaises(ValueError):
            app.validate(self.config)


class ExampleConfigTests(unittest.TestCase):
    def test_example_config_is_valid_and_disabled(self):
        config = app.validate(tomllib.loads((Path(__file__).resolve().parents[1] / "config.example.toml").read_text()))
        journeys.validate(config)
        self.assertFalse(config["enabled"])

if __name__ == "__main__":
    unittest.main()
