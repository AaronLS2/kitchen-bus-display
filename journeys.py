"""Two-route predictions, strict same-trip matching, and a 52x16 pixel layout."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import logging
import math
import re
import time
from zoneinfo import ZoneInfo

LOG = logging.getLogger("kitchen-display")

# Original 3x5 glyphs, rendered as a bitmap to avoid firmware font-width differences.
FONT = dict(zip("0123456789D5X6AMINOTESPCKHU-:?+ ", [
    "111101101101111", "010110010010111", "111001111100111",
    "111001111001111", "101101111001001", "111100111001111",
    "111100111101111", "111001010010010", "111101111101111",
    "111101111001111", "110101101101110", "111100111001111",
    "101101010101101", "111100111101111", "010101111101101",
    "101111111101101", "111010010010111", "101111111111101",
    "111101101101111", "111010010010010", "111100110100111",
    "111100111001111", "110101110100100", "111100100100111",
    "101101110101101", "101101111101101", "101101101101111",
    "000000111000000", "000010000010000", "111001010000010",
    "000010111010000", "000000000000000",
]))
FONT.update({"B": "110101110101110", "L": "100100100100111", "V": "101101101101010"})


def validate(config):
    journeys = config["journeys"]
    if not isinstance(journeys, list) or not 1 <= len(journeys) <= 4:
        raise ValueError("Configure one to four journeys")
    for journey in journeys:
        if not re.fullmatch(r"[A-Z0-9]{1,3}", journey["route"]):
            raise ValueError("Layout supports route names up to three characters")
        if any(char not in FONT for char in journey["route"]):
            raise ValueError("Add route glyphs before using this route")
        for key in ("stop_id", "destination_stop_id"):
            if not re.fullmatch(r"\d{7}", journey[key]):
                raise ValueError("Set seven-digit stop IDs")
        if "gtfs_destination_stop_id" in journey and not re.fullmatch(r"\d{1,7}", str(journey["gtfs_destination_stop_id"])):
            raise ValueError("gtfs_destination_stop_id must be the GTFS stop_id (stops.txt), not the stop code")
        if not journey["direction_text"].strip():
            raise ValueError("Set exact WMATA direction text")
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", journey["color"]):
            raise ValueError("Use an RGB hex color")
        reach = journey.get("min_minutes", 0)
        if type(reach) is not int or not 0 <= reach <= 30:
            raise ValueError("min_minutes must be a whole number from 0 to 30")
    flip = config.get("page_seconds", 6)
    if type(flip) is not int or not 3 <= flip <= 15:
        raise ValueError("page_seconds must be 3 to 15")


def rows(data, journey):
    if not isinstance(data, dict) or not isinstance(data.get("Predictions"), list):
        raise ValueError("Missing predictions array")
    selected = []
    for item in data["Predictions"]:
        if not isinstance(item, dict):
            raise ValueError("Malformed prediction")
        if item.get("RouteID") != journey["route"]:
            continue
        if str(item.get("DirectionText", "")).strip().casefold() != journey["direction_text"].strip().casefold():
            continue
        if type(item.get("Minutes")) is not int or item["Minutes"] < 0:
            raise ValueError("Invalid arrival minutes")
        selected.append(item)
    return sorted(selected, key=lambda item: item["Minutes"])


def _varint(data, i):
    value = shift = 0
    while True:
        byte = data[i]; i += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, i
        if shift > 63:
            raise ValueError("Malformed varint")


def _fields(data):
    """Decode one protobuf message level into (field number, value) pairs."""
    i, out = 0, []
    while i < len(data):
        key, i = _varint(data, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(data, i)
        elif wire == 2:
            length, i = _varint(data, i)
            if i + length > len(data):
                raise ValueError("Truncated field")
            value, i = data[i:i + length], i + length
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value, i = data[i:i + size], i + size
        else:
            raise ValueError("Unsupported wire type")
        out.append((number, value))
    return out


def _first(fields, number):
    return next((value for n, value in fields if n == number), None)


def trip_updates(raw, routes):
    """GTFS-RT FeedMessage -> (feed time, {trip_id: (vehicle_id, {gtfs_stop_id: epoch})})."""
    feed = _fields(raw)
    header = _first(feed, 1)
    stamp = _first(_fields(header), 3) if header is not None else None
    if not isinstance(stamp, int):
        raise ValueError("Feed has no timestamp")
    trips = {}
    now = datetime.now().timestamp()
    for number, entity in feed:
        if number != 2:
            continue
        update = _first(_fields(entity), 3)
        if update is None:
            continue
        update = _fields(update)
        # A newly generated feed can contain old individual trip predictions.
        # This timestamp is optional; without it, the feed-level check still applies.
        updated_at = _first(update, 4)
        if updated_at is not None and (
            not isinstance(updated_at, int) or not -60 <= now - updated_at <= FEED_MAX_AGE
        ):
            continue
        descriptor = _fields(_first(update, 1) or b"")
        route, trip = _first(descriptor, 5), _first(descriptor, 1)
        if not trip or route is None or route.decode() not in routes:
            continue
        vehicle = _first(update, 3)
        vehicle = _first(_fields(vehicle), 1) if vehicle is not None else None
        stops = {}
        for n, stop_update in update:
            if n != 2:
                continue
            stop_update = _fields(stop_update)
            if _first(stop_update, 5) in (1, 2):  # SKIPPED or NO_DATA
                continue
            stop = _first(stop_update, 4)
            event = _first(stop_update, 2) or _first(stop_update, 3)
            when = _first(_fields(event), 2) if event is not None else None
            if stop and isinstance(when, int):
                stops[stop.decode()] = when
        trips.setdefault(trip.decode(), []).append((vehicle.decode() if vehicle else None, stops))
    return stamp, trips


def destination_rows(feed, journey):
    """Express a trip-update feed as next-bus-style rows at the destination stop, for pair()."""
    stamp, trips = feed
    stop = str(journey["gtfs_destination_stop_id"])
    rows_ = []
    for trip, updates in trips.items():
        for vehicle, stops in updates:
            if stop in stops:
                rows_.append({"RouteID": journey["route"], "DirectionText": journey["direction_text"],
                              "TripID": trip, "VehicleID": vehicle,
                              "ArrivalTime": datetime.fromtimestamp(stops[stop]).astimezone(),
                              "Minutes": max(0, round((stops[stop] - stamp) / 60))})
    return {"Predictions": rows_}


SPARE_TRIPS = 4  # two shown, two spares for buses that become too close between polls


def identifier(value):
    return str(value).strip() if value is not None and str(value).strip() not in ("", "0") else None


def pair(origin, destination, journey, origin_at, destination_at):
    """Only join identical trips; list order or a shared vehicle alone is insufficient."""
    boarding = rows(origin, journey)
    downstream = rows(destination, journey) if destination is not None else []
    seen, result = set(), []
    for item in boarding:
        trip = identifier(item.get("TripID"))
        if trip and trip in seen:
            continue
        if trip:
            seen.add(trip)
        arrival_at = origin_at + timedelta(minutes=item["Minutes"])
        matches = [candidate for candidate in downstream
                   if trip and identifier(candidate.get("TripID")) == trip]
        dropoff_at = None
        if len(matches) == 1:
            candidate = matches[0]
            a, b = identifier(item.get("VehicleID")), identifier(candidate.get("VehicleID"))
            # GTFS provides an absolute timestamp: never round it through Minutes.
            # REST-only configurations still use their relative-minute prediction.
            predicted = candidate.get("ArrivalTime")
            if predicted is None:
                predicted = destination_at + timedelta(minutes=candidate["Minutes"])
            if not (a and b and a != b) and predicted > arrival_at:
                dropoff_at = predicted
        result.append({"arrival_at": arrival_at, "dropoff_at": dropoff_at})
        if len(result) == SPARE_TRIPS:
            break
    return result


FEED_MAX_AGE = 120  # seconds; older trip updates are not trusted for destination times


def collect(journeys, key, fetch, fetch_trips=None):
    use_feed = fetch_trips is not None and all("gtfs_destination_stop_id" in j for j in journeys)
    keys = ("stop_id",) if use_feed else ("stop_id", "destination_stop_id")
    stops = {j[k] for j in journeys for k in keys}
    def get(stop):
        try:
            data = fetch(stop, key)
            return data, datetime.now().astimezone()
        except Exception as exc:
            LOG.warning("Stop fetch unavailable (%s)", type(exc).__name__)
            return None, datetime.now().astimezone()
    def get_feed():
        try:
            stamp, trips = trip_updates(fetch_trips(key), {j["route"] for j in journeys})
            age = datetime.now().timestamp() - stamp
            if not -60 <= age <= FEED_MAX_AGE:
                LOG.warning("Trip update feed is stale (%ds); destination times hidden", age)
                return None
            return stamp, trips
        except Exception as exc:
            LOG.warning("Trip update feed unavailable (%s)", type(exc).__name__)
            return None
    with ThreadPoolExecutor(max_workers=5) as pool:
        feed_job = pool.submit(get_feed) if use_feed else None
        fetched = dict(zip(stops, pool.map(get, stops)))
        feed = feed_job.result() if feed_job else None
    snapshots = []
    for journey in journeys:
        origin, at = fetched[journey["stop_id"]]
        if use_feed:
            destination = destination_rows(feed, journey) if feed else None
            destination_at = datetime.fromtimestamp(feed[0]).astimezone() if feed else at
        else:
            destination, destination_at = fetched[journey["destination_stop_id"]]
        # A malformed destination feed must not erase valid boarding predictions.
        try:
            if destination is not None:
                rows(destination, journey)
        except ValueError:
            destination = None
        try:
            predictions = pair(origin, destination, journey, at, destination_at)
            snapshots.append({"at": at, "predictions": predictions})
        except (ValueError, TypeError):
            snapshots.append({"at": at, "predictions": None})
    return snapshots


def text_width(text, narrow=False):
    """Pixels used by text: 3px glyphs + 1px gaps; narrow colons take 1px + gap."""
    return sum(2 if narrow and char == ":" else 4 for char in text) - 1


def draw_text(pixels, text, x, y, color, narrow=False):
    color = int(color.lstrip("#"), 16)
    if x < 0 or x + text_width(text, narrow) > 52 or y + 5 > 16:
        raise ValueError("Text would exceed the physical display")
    for char in text:
        bits = FONT[char]
        if narrow and char == ":":
            for row in (1, 3):
                pixels[(y + row) * 52 + x] = color
            x += 2
            continue
        for n, bit in enumerate(bits):
            if bit == "1":
                pixels[(y + n // 3) * 52 + x + n % 3] = color
        x += 4


def message_payload(lines, color="#B8C5CC"):
    """Up to two centered bitmap lines; firmware text drops spaces on the TC002."""
    pixels = [0] * (52 * 16)
    for row, line in enumerate(lines[:2]):
        width = 4 * len(line) - 1
        draw_text(pixels, line, (52 - width) // 2, 1 + row * 8, color)
    return {"draw": [{"db": [0, 0, 52, 16, pixels]}]}


UPDATED = "#7F8C95"  # dim: time of the data shown, so a frozen screen looks old
COLUMNS = (15, 34)   # left edge of each 13px bus column


def clock_text(when, timezone):
    return when.astimezone(ZoneInfo(timezone)).strftime("%I:%M").lstrip("0")


def pixels_for(journey, snapshot, now, timezone):
    pixels = [0] * (52 * 16)
    def draw(text, x, y, color, narrow=False):
        draw_text(pixels, text, x, y, color, narrow)
    color = journey["color"]
    draw(journey["route"], 0, 1, color)
    predictions = snapshot["predictions"]
    age = (now - snapshot["at"]).total_seconds()
    if predictions is None or age < 0 or age > 75:
        draw("NO DATA", 16, 1, "#C69D58")
        draw("CHECK APP", 0, 9, "#C69D58")
        return pixels
    # When this data was fetched (not the current time): if the host dies and the
    # clock freezes, this visibly falls behind real time.
    updated = clock_text(snapshot["at"], timezone)
    if text_width(updated, narrow=True) <= COLUMNS[0] - 2:
        draw(updated, 0, 9, UPDATED, narrow=True)
    # Hide buses the kids can no longer reach (re-checked every page, not just per poll).
    reach = journey.get("min_minutes", 0) * 60
    if reach:
        predictions = [p for p in predictions if (p["arrival_at"] - now).total_seconds() >= reach]
    if not predictions:
        draw("NO TIMES", 16, 1, "#C69D58")
        return pixels
    for i, x in enumerate(COLUMNS):
        if i >= len(predictions):
            draw("--", x + 3, 1, "#53616B")
            draw("-:--", x, 9, "#53616B", narrow=True)
            continue
        item = predictions[i]
        seconds = (item["arrival_at"] - now).total_seconds()
        # Do not leave a departed bus shown as indefinitely DUE between fetches.
        minutes = max(0, math.ceil(seconds / 60))
        label = "--" if seconds < -30 else ("99+" if minutes > 99 else str(minutes))
        draw(label, x + max(0, (13 - text_width(label)) // 2), 1, "#D2DEE5")
        dropoff = item["dropoff_at"]
        clock = clock_text(dropoff, timezone) if dropoff and seconds >= -30 else "-:--"
        draw(clock, x, 9, color, narrow=True)
    return pixels


def payload_for(journey, snapshot, now, timezone):
    return {"draw": [{"db": [0, 0, 52, 16, pixels_for(journey, snapshot, now, timezone)]}]}


def demo():
    now = datetime(2000, 1, 3, 7, 30, tzinfo=ZoneInfo("America/New_York"))
    pages = {}
    for route, color, values in [("X2", "#52C7D9", [(6, 28), (18, 40)]),
                                  ("S9", "#E8AD55", [(8, 25), (20, 37)])]:
        snapshot = {"at": now, "predictions": [
            {"arrival_at": now + timedelta(minutes=boarding), "dropoff_at": now + timedelta(minutes=dropoff)}
            for boarding, dropoff in values]}
        pages[route] = payload_for({"route": route, "color": color}, snapshot, now, "America/New_York")
    return {"illustrative_not_live": True, "pages": pages}


def run(config, device, key, stop, fetch, active, blank, fetch_trips=None):
    snapshots, refreshed, page = [], 0.0, 0
    try:
        while not stop.is_set():
            cycle = time.monotonic()
            now = datetime.now().astimezone()
            payload = blank()
            if active(config, now):
                if not snapshots or cycle - refreshed >= config["poll_seconds"]:
                    snapshots = collect(config["journeys"], key, fetch, fetch_trips)
                    refreshed = cycle
                now = datetime.now().astimezone()
                index = page % len(config["journeys"])
                payload = payload_for(config["journeys"][index], snapshots[index], now, config["timezone"])
                page += 1
            else:
                snapshots, page = [], 0
            if not active(config, datetime.now().astimezone()):
                payload = blank()
            try:
                device.show(payload)
            except Exception as exc:
                LOG.error("Display unreachable; previous image may remain (%s)", type(exc).__name__)
            stop.wait(max(1, config["page_seconds"] - (time.monotonic() - cycle)))
    finally:
        try:
            device.show(blank())
        except Exception:
            LOG.error("Could not blank display on exit; clear it manually")

