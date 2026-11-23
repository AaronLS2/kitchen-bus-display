from datetime import datetime, timedelta
from pathlib import Path
import threading
import tomllib
import unittest
from unittest.mock import Mock, patch

import journeys as app


class JourneyTests(unittest.TestCase):
    def setUp(self):
        self.config = tomllib.loads((Path(__file__).resolve().parent / "fixture_config.toml").read_text())
        self.j = self.config["journeys"][0]
        self.now = datetime.fromisoformat("2026-11-13T07:30:00-05:00")

    def row(self, trip, minutes, vehicle="1234", **extra):
        return {"RouteID": "D5X", "DirectionText": "North to Takoma", "TripID": trip,
                "VehicleID": vehicle, "Minutes": minutes, **extra}

    def pair(self, origin, destination):
        return app.pair({"Predictions": origin}, {"Predictions": destination}, self.j, self.now, self.now)

    def test_matches_same_trip_not_next_destination_bus(self):
        result = self.pair([self.row("a", 6), self.row("b", 18)],
                           [self.row("earlier", 1), self.row("b", 40), self.row("a", 28)])
        self.assertEqual([r["dropoff_at"] for r in result], [self.now + timedelta(minutes=m) for m in (28, 40)])

    def test_never_matches_vehicle_alone(self):
        result = self.pair([self.row(None, 6)], [self.row("other-trip", 28)])
        self.assertIsNone(result[0]["dropoff_at"])

    def test_conflicting_vehicle_or_duplicate_trip_is_unknown(self):
        for dest in [[self.row("a", 28, vehicle="5678")], [self.row("a", 28), self.row("a", 29)]]:
            self.assertIsNone(self.pair([self.row("a", 6)], dest)[0]["dropoff_at"])

    def test_destination_before_boarding_is_unknown(self):
        self.assertIsNone(self.pair([self.row("a", 6)], [self.row("a", 2)])[0]["dropoff_at"])

    def test_filters_route_and_direction(self):
        bad = [self.row("a", 6, RouteID="D50"), self.row("b", 7, DirectionText="South to Metro Center")]
        self.assertEqual(self.pair(bad, []), [])

    def test_missing_destination_preserves_boarding(self):
        result = self.pair([self.row("a", 6)], [])
        self.assertEqual(result[0]["arrival_at"], self.now + timedelta(minutes=6))
        self.assertIsNone(result[0]["dropoff_at"])

    def test_destination_errors_do_not_erase_origin(self):
        for bad in [None, {}, {"Predictions": None}]:
            def fetch(stop, key):
                return {"Predictions": [self.row("a", 6)]} if stop == self.j["stop_id"] else bad
            result = app.collect([self.j], "test-key", fetch)
            self.assertEqual(len(result[0]["predictions"]), 1)
            self.assertIsNone(result[0]["predictions"][0]["dropoff_at"])

    def test_route_failures_are_independent(self):
        def fetch(stop, key):
            if stop == self.j["stop_id"]:
                raise TimeoutError
            return {"Predictions": []}
        result = app.collect(self.config["journeys"], "test-key", fetch)
        self.assertIsNone(result[0]["predictions"])
        self.assertEqual(result[1]["predictions"], [])

    def test_bitmap_fits_even_at_twelve_oclock_and_99_plus(self):
        snapshot = {"at": self.now, "predictions": [{"arrival_at": self.now + timedelta(minutes=120),
                   "dropoff_at": self.now.replace(hour=12, minute=58)}]}
        for j in self.config["journeys"]:
            pixels = app.pixels_for(j, snapshot, self.now, self.config["timezone"])
            self.assertEqual(len(pixels), 52 * 16)
            self.assertTrue(any(pixels))

    def test_stale_snapshot_drops_numbers(self):
        snapshot = {"at": self.now, "predictions": [{"arrival_at": self.now + timedelta(minutes=6), "dropoff_at": None}]}
        later = self.now + timedelta(seconds=76)
        actual = app.pixels_for(self.j, snapshot, later, self.config["timezone"])
        expected = app.pixels_for(self.j, {"at": later, "predictions": None}, later, self.config["timezone"])
        self.assertEqual(actual, expected)

    def test_new_configuration_validates(self):
        app.validate(self.config)

    def test_loop_flips_without_refetching_every_page(self):
        device, stop = Mock(), threading.Event()
        snapshot = {"at": datetime.now().astimezone(), "predictions": []}
        calls = []
        def show(payload):
            calls.append(payload)
            if len(calls) == 2:
                stop.set()
        device.show.side_effect = show
        # No real waits; monotonic is held within a single poll interval.
        with patch.object(stop, "wait"), patch.object(app.time, "monotonic", return_value=1), \
             patch.object(app, "collect", return_value=[snapshot, snapshot]) as collect, \
             patch.object(app, "payload_for", side_effect=lambda j, *args: j["route"]):
            app.run(self.config, device, "test", stop, Mock(), lambda *args: True, lambda: "blank")
        self.assertEqual(calls, ["D5X", "D6X", "blank"])
        self.assertEqual(collect.call_count, 1)



def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _msg(*fields):
    out = b""
    for number, value in fields:
        if isinstance(value, int):
            out += _varint(number << 3) + _varint(value)
        else:
            value = value.encode() if isinstance(value, str) else value
            out += _varint(number << 3 | 2) + _varint(len(value)) + value
    return out


def feed(stamp, trips, *, updated_at=None, stop_relationship=None):
    """trips: [(trip_id, route, vehicle, {stop_id: epoch})] -> GTFS-RT FeedMessage bytes."""
    entities = []
    for i, (trip, route, vehicle, stops) in enumerate(trips):
        relationship = [] if stop_relationship is None else [(5, stop_relationship)]
        updates = [(2, _msg((4, stop), (2, _msg((2, when))), *relationship)) for stop, when in stops.items()]
        timestamp = [] if updated_at is None else [(4, updated_at)]
        update = _msg((1, _msg((1, trip), (5, route))), (3, _msg((1, vehicle))), *timestamp, *updates)
        entities.append((2, _msg((1, str(i)), (3, update))))
    return _msg((1, _msg((1, "2.0"), (3, stamp))), *entities)


class TripUpdateFeedTests(unittest.TestCase):
    def setUp(self):
        self.config = tomllib.loads((Path(__file__).resolve().parent / "fixture_config.toml").read_text())
        self.j = self.config["journeys"][0]
        self.dropoff = self.j["gtfs_destination_stop_id"]
        self.stamp = int(datetime.now().timestamp())

    def row(self, trip, minutes, vehicle="1234"):
        return {"RouteID": "D5X", "DirectionText": "North to Takoma", "TripID": trip,
                "VehicleID": vehicle, "Minutes": minutes}

    def collect(self, raw, origin):
        def fetch(stop, key):
            assert stop == self.j["stop_id"], "destination next-bus stop must not be polled in feed mode"
            return {"Predictions": origin}
        def fetch_trips(key):
            if isinstance(raw, Exception):
                raise raw
            return raw
        return app.collect([self.j], "k", fetch, fetch_trips)[0]["predictions"]

    def test_feed_supplies_dropoff_time_for_same_trip(self):
        raw = feed(self.stamp, [
            ("earlier", "D5X", "9", {self.dropoff: self.stamp + 120}),
            ("a", "D5X", "1234", {"6922": self.stamp + 360, self.dropoff: self.stamp + 28 * 60}),
            ("x", "D6X", "1234", {self.dropoff: self.stamp + 600}),
        ])
        result = self.collect(raw, [self.row("a", 6)])
        expected = datetime.fromtimestamp(self.stamp).astimezone() + timedelta(minutes=28)
        self.assertEqual(result[0]["dropoff_at"], expected)

    def test_feed_vehicle_conflict_is_unknown(self):
        raw = feed(self.stamp, [("a", "D5X", "5678", {self.dropoff: self.stamp + 28 * 60})])
        self.assertIsNone(self.collect(raw, [self.row("a", 6)])[0]["dropoff_at"])

    def test_stale_or_failed_feed_keeps_boarding_times(self):
        for raw in [feed(self.stamp - 600, [("a", "D5X", "1234", {self.dropoff: self.stamp})]),
                    TimeoutError(), b"\xff\xff\xff"]:
            result = self.collect(raw, [self.row("a", 6)])
            self.assertEqual(len(result), 1)
            self.assertIsNone(result[0]["dropoff_at"])

    def test_stale_trip_in_fresh_feed_keeps_boarding_but_hides_dropoff(self):
        raw = feed(self.stamp, [("a", "D5X", "1234", {self.dropoff: self.stamp + 1680})],
                   updated_at=self.stamp - 1800)
        result = self.collect(raw, [self.row("a", 6)])
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["dropoff_at"])

    def test_trip_timestamp_uses_wall_clock_not_feed_header(self):
        # Header is still acceptable, but this trip is already over 120 seconds old.
        raw = feed(self.stamp - 100, [("a", "D5X", "1234", {self.dropoff: self.stamp + 1680})],
                   updated_at=self.stamp - 180)
        self.assertIsNone(self.collect(raw, [self.row("a", 6)])[0]["dropoff_at"])

    def test_recent_trip_keeps_exact_prediction(self):
        when = self.stamp + 1687
        raw = feed(self.stamp, [("a", "D5X", "1234", {self.dropoff: when})],
                   updated_at=self.stamp - 30)
        self.assertEqual(self.collect(raw, [self.row("a", 6)])[0]["dropoff_at"].timestamp(), when)

    def test_future_trip_timestamp_is_not_trusted(self):
        raw = feed(self.stamp, [("a", "D5X", "1234", {self.dropoff: self.stamp + 1680})],
                   updated_at=self.stamp + 300)
        self.assertIsNone(self.collect(raw, [self.row("a", 6)])[0]["dropoff_at"])

    def test_skipped_dropoff_keeps_boarding_but_hides_destination(self):
        raw = feed(self.stamp, [("a", "D5X", "1234", {self.dropoff: self.stamp + 1680})],
                   stop_relationship=1)
        result = self.collect(raw, [self.row("a", 6)])
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["dropoff_at"])

    def test_absolute_dropoff_time_does_not_round_via_minutes(self):
        base = datetime.fromisoformat("2026-11-16T07:30:40-05:00")
        when = datetime.fromisoformat("2026-11-16T07:58:00-05:00")
        raw = feed(int(base.timestamp()), [("a", "D5X", "1234", {self.dropoff: int(when.timestamp())})])
        destination = app.destination_rows(app.trip_updates(raw, {"D5X"}), self.j)
        result = app.pair({"Predictions": [self.row("a", 6)]}, destination, self.j, base, base)
        self.assertEqual(result[0]["dropoff_at"], when)
        self.assertEqual(app.clock_text(result[0]["dropoff_at"], "America/New_York"), "7:58")

    def test_example_config_has_gtfs_ids(self):
        app.validate(self.config)
        self.assertTrue(all("gtfs_destination_stop_id" in j for j in self.config["journeys"]))


class UpdatedTimeTests(unittest.TestCase):
    def setUp(self):
        self.config = tomllib.loads((Path(__file__).resolve().parent / "fixture_config.toml").read_text())
        self.j = self.config["journeys"][0]
        self.tz = self.config["timezone"]

    def corner(self, pixels):
        return [pixels[y * 52 + x] for y in range(9, 14) for x in range(0, 13)]

    def page(self, fetched, now, predictions=()):
        snapshot = {"at": fetched, "predictions": list(predictions)}
        return app.pixels_for(self.j, snapshot, now, self.tz)

    def test_corner_shows_fetch_time_not_current_time(self):
        fetched = datetime.fromisoformat("2026-11-16T07:36:10-05:00")
        bus = {"arrival_at": fetched + timedelta(minutes=6), "dropoff_at": fetched + timedelta(minutes=28)}
        a = self.page(fetched, fetched + timedelta(seconds=40), [bus])
        b = self.page(fetched, fetched + timedelta(seconds=5), [bus])
        self.assertEqual(self.corner(a), self.corner(b))
        self.assertTrue(any(self.corner(a)))
        later = self.page(fetched + timedelta(minutes=1), fetched + timedelta(minutes=1), [bus])
        self.assertNotEqual(self.corner(a), self.corner(later))

    def test_corner_also_shown_with_no_times(self):
        fetched = datetime.fromisoformat("2026-11-16T07:36:00-05:00")
        self.assertTrue(any(self.corner(self.page(fetched, fetched))))

    def test_wide_times_skip_corner_instead_of_overlapping(self):
        fetched = datetime.fromisoformat("2026-11-16T10:36:00-05:00")
        self.assertFalse(any(self.corner(self.page(fetched, fetched))))

    def test_narrow_colon_width(self):
        self.assertEqual(app.text_width("8:04", narrow=True), 13)
        self.assertEqual(app.text_width("12:58", narrow=True), 17)


class MinimumMinutesTests(unittest.TestCase):
    def setUp(self):
        self.config = tomllib.loads((Path(__file__).resolve().parent / "fixture_config.toml").read_text())
        self.d5x, self.d6x = self.config["journeys"]
        self.tz = self.config["timezone"]
        self.now = datetime.fromisoformat("2026-11-16T07:30:00-05:00")

    def buses(self, *seconds):
        return [{"arrival_at": self.now + timedelta(seconds=s), "dropoff_at": None} for s in seconds]

    def page(self, journey, predictions, now=None):
        return app.pixels_for(journey, {"at": self.now, "predictions": predictions}, now or self.now, self.tz)

    def test_configured_minimums(self):
        self.assertEqual((self.d5x["min_minutes"], self.d6x["min_minutes"]), (2, 4))

    def test_too_close_buses_drop_and_next_moves_up(self):
        # D5X: a bus 1:59 away is hidden; the 2:00 and 9:00 buses are shown.
        self.assertEqual(self.page(self.d5x, self.buses(119, 120, 540)), self.page(self.d5x, self.buses(120, 540)))
        # D6X: 3:59 is hidden, 4:00 is shown.
        self.assertEqual(self.page(self.d6x, self.buses(239, 240, 900)), self.page(self.d6x, self.buses(240, 900)))

    def test_bus_drops_between_polls_and_spare_fills_in(self):
        fetched = self.buses(250, 600, 1200)
        later = self.now + timedelta(seconds=20)  # first bus now 3:50 away: below D6X's 4
        expected = app.pixels_for(self.d6x, {"at": self.now, "predictions": fetched[1:]}, later, self.tz)
        self.assertEqual(self.page(self.d6x, fetched, later), expected)

    def test_all_too_close_says_no_times(self):
        self.assertEqual(self.page(self.d6x, self.buses(60, 200)), self.page(self.d6x, []))

    def test_pair_keeps_spares(self):
        rows = [{"RouteID": "D5X", "DirectionText": "North to Takoma", "TripID": str(i),
                 "VehicleID": str(i), "Minutes": i} for i in range(1, 7)]
        self.assertEqual(len(app.pair({"Predictions": rows}, None, self.d5x, self.now, self.now)), 4)

    def test_minimum_validated(self):
        for bad in (-1, 31, 2.5, "2"):
            self.d5x["min_minutes"] = bad
            with self.assertRaises(ValueError):
                app.validate(self.config)


class MessagePayloadTests(unittest.TestCase):
    def test_test_screen_keeps_spaces_as_blank_columns(self):
        pixels = app.message_payload(["BUS TEST", "NOT LIVE"])["draw"][0]["db"][4]
        self.assertEqual(len(pixels), 52 * 16)
        x = (52 - (4 * 8 - 1)) // 2
        space_columns = range(x + 3 * 4 - 1, x + 4 * 4)  # gap before "T", plus space glyph
        for y in range(1, 6):
            self.assertTrue(all(pixels[y * 52 + c] == 0 for c in space_columns))
        self.assertTrue(any(pixels))

    def test_message_rejects_text_wider_than_display(self):
        with self.assertRaises(ValueError):
            app.message_payload(["BUSBUSBUSBUSBUS"])

if __name__ == "__main__":
    unittest.main()

