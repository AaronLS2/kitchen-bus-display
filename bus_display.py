"""School-morning WMATA display for stock-firmware Ulanzi TC002. Python 3.11+."""
import argparse
from datetime import date, datetime, time as daytime
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import signal
import threading
import time
import tomllib
import journeys
import phone_control
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from zoneinfo import ZoneInfo

LOG = logging.getLogger("kitchen-display")
API = "https://api.wmata.com/NextBusService.svc/json/jPredictions"
TRIP_UPDATES = "https://api.wmata.com/gtfs/bus-gtfsrt-tripupdates.pb"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Do not forward API credentials to another destination.


def request_json(url, payload=None, headers=None, local=False):
    body = None if payload is None else json.dumps(payload).encode()
    req = Request(url, data=body, headers={"Content-Type": "application/json", **(headers or {})})
    handlers = [NoRedirect()]
    if local:
        handlers.append(ProxyHandler({}))
    with build_opener(*handlers).open(req, timeout=5) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("Response too large")
    result = json.loads(raw) if raw.strip() else {}
    if isinstance(result, dict) and "code" in result and str(result["code"]) != "200":
        raise ValueError("API returned an application error")
    return result


def validate(config):
    ZoneInfo(config["timezone"])
    start, end = daytime.fromisoformat(config["start"]), daytime.fromisoformat(config["end"])
    if start.tzinfo or end.tzinfo or start >= end:
        raise ValueError("Use a same-day start before end, without timezone offsets")
    if type(config["enabled"]) is not bool:
        raise ValueError("enabled must be true or false")
    if not config["weekdays"] or any(type(d) is not int or d not in range(7) for d in config["weekdays"]):
        raise ValueError("weekdays must contain integers 0 through 6")
    if type(config["poll_seconds"]) is not int or not 30 <= config["poll_seconds"] <= 60:
        raise ValueError("poll_seconds must be between 30 and 60")
    for item in config["skip_dates"]:
        date.fromisoformat(item)
    if "school_year" in config:
        span = config["school_year"]
        if not isinstance(span, list) or len(span) != 2 or date.fromisoformat(span[0]) > date.fromisoformat(span[1]):
            raise ValueError("school_year must be [first_day, last_day]")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", config["device"]["app_name"]):
        raise ValueError("Use a simple app_name")
    return config


def validate_bus(config):
    bus = config["bus"]
    if not re.fullmatch(r"\d{7}", bus["stop_id"]):
        raise ValueError("Set the 7-digit bus stop_id")
    if not isinstance(bus["routes"], list) or not bus["routes"] or any(
        not isinstance(r, str) or not re.fullmatch(r"[A-Z0-9]{1,5}", r) for r in bus["routes"]
    ):
        raise ValueError("Set exact current route IDs, up to five characters each")
    if bus["direction"] not in ("0", "1"):
        raise ValueError("Confirm bus direction as '0' or '1'")


def in_school_year(config, day):
    span = config.get("school_year")
    return not span or date.fromisoformat(span[0]) <= day <= date.fromisoformat(span[1])


def active(config, now):
    local = now.astimezone(ZoneInfo(config["timezone"]))
    return (config["enabled"] and local.weekday() in config["weekdays"]
            and local.date().isoformat() not in config["skip_dates"]
            and in_school_year(config, local.date())
            and daytime.fromisoformat(config["start"]) <= local.time() < daytime.fromisoformat(config["end"]))


def arrivals(data, bus):
    if not isinstance(data, dict) or not isinstance(data.get("Predictions"), list):
        raise ValueError("Missing predictions array")
    found = []
    seen = set()
    for item in data["Predictions"]:
        if not isinstance(item, dict):
            raise ValueError("Malformed prediction")
        if item.get("RouteID") not in bus["routes"] or str(item.get("DirectionNum")) != bus["direction"]:
            continue
        minutes = item.get("Minutes")
        if type(minutes) is not int or minutes < 0:
            raise ValueError("Invalid arrival minutes")
        trip = item.get("TripID")
        identity = (item["RouteID"], str(trip)) if trip else None
        if identity is not None and identity in seen:
            continue
        if identity is not None:
            seen.add(identity)
        found.append((minutes, item["RouteID"]))
    return sorted(found)[:2]


def screen(lines, color="#B8C5CC"):
    # Stock firmware supports 5px type. Two fixed rows, no scroll or rotation.
    # Actual font width and kitchen-distance legibility still need hardware QA.
    return {"text": [
        {"content": line, "fontHeight": 5, "x": 0, "y": 1 + index * 8,
         "rect": [0, 0, 52, 16], "charSpacing": 0, "color": color}
        for index, line in enumerate(lines[:2])
    ]}


def blank_screen():
    # {} would DELETE the app, so send an explicit black rectangle instead.
    return {"draw": [{"df": [0, 0, 52, 16, "#000000"]}]}


def arrival_screen(predictions):
    if not predictions:
        return screen(["NO TIMES", "CHECK APP"], "#C69D58")
    return screen([f"{route} {'DUE' if minutes == 0 else str(minutes) + 'M'}"
                   for minutes, route in predictions])


class Device:
    def __init__(self, config):
        host = config["host"]
        ip = ipaddress.ip_address(host)
        if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            raise ValueError("Set device.host to the TC002's private LAN IPv4 address")
        self.base = f"http://{host}"
        self.query = urlencode({"name": config["app_name"]})

    def info(self):
        data = request_json(self.base + "/getBase", local=True)
        return {k: data.get(k) for k in ("appVer", "mcuVer", "ip")}

    def prepare(self):
        # Explicitly invoked only with `prepare`; global settings are not changed by probe.
        request_json(self.base + "/setConfig", {"carouselSpeed": 0, "scrollSpeed": 0}, local=True)

    def show(self, payload):
        request_json(self.base + "/api/custom?" + self.query, payload, local=True)
        # Pushing a custom app does not select it. Switching requires stock V1.1.0+.
        request_json(self.base + "/api/switchDiyApp?" + self.query, {}, local=True)


def fetch_predictions(stop_id, key):
    return request_json(API + "?" + urlencode({"StopID": stop_id}), headers={"api_key": key})


def fetch_trip_updates(key):
    """Raw GTFS-RT protobuf: per-trip predictions at every remaining stop."""
    req = Request(TRIP_UPDATES, headers={"api_key": key})
    with build_opener(NoRedirect()).open(req, timeout=10) as response:
        raw = response.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError("Trip update feed too large")
    return raw


def run(config, device, key, stop, is_active=None):
    is_active = is_active or active
    LOG.info("Starting; schedule uses %s", config["timezone"])
    try:
        while not stop.is_set():
            started = time.monotonic()
            now = datetime.now().astimezone()
            payload = blank_screen()
            if is_active(config, now):
                try:
                    data = fetch_predictions(config["bus"]["stop_id"], key)
                    payload = arrival_screen(arrivals(data, config["bus"]))
                except Exception as exc:
                    # No cached arrival survives a failed fetch. Avoid logging secrets/URLs.
                    LOG.warning("WMATA unavailable (%s)", type(exc).__name__)
                    payload = screen(["NO DATA", "CHECK APP"], "#C69D58")
            # A slow request can cross the end of the morning window.
            if not is_active(config, datetime.now().astimezone()):
                payload = blank_screen()
            try:
                device.show(payload)
            except Exception as exc:
                LOG.error("Display unreachable; its previous image may remain (%s)", type(exc).__name__)
            stop.wait(max(1, config["poll_seconds"] - (time.monotonic() - started)))
    finally:
        try:
            device.show(blank_screen())
        except Exception:
            LOG.error("Could not blank display on exit; clear it manually")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["probe", "prepare", "test", "blank", "preview", "run"])
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args()
    if args.command == "preview":
        print(json.dumps(journeys.demo(), indent=2))
        return
    with Path(args.config).open("rb") as source:
        config = validate(tomllib.load(source))
    device = Device(config["device"])
    if args.command == "probe":
        print(json.dumps(device.info(), indent=2))
    elif args.command == "prepare":
        device.prepare()
        print("Disabled scrolling and automatic carousel. Adjust brightness on the device.")
    elif args.command == "test":
        device.show(journeys.message_payload(["BUS TEST", "NOT LIVE"]))
        print("Test screen sent. Run blank to clear it.")
    elif args.command == "blank":
        device.show(blank_screen())
    else:
        if "journeys" in config:
            journeys.validate(config)
        else:
            validate_bus(config)
        if not config["enabled"]:
            raise ValueError("Set enabled=true after confirming the commute and schedule")
        key = os.environ.get("WMATA_API_KEY", "").strip()
        if not key:
            raise ValueError("Set WMATA_API_KEY in your local environment")
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        control = phone_control.Control(config, active, phone_control.state_path())
        web = config.get("phone", {})
        server = phone_control.start(control, web.get("host", "0.0.0.0"), web.get("port", 8765))
        device = phone_control.ControlledDevice(device, control, blank_screen)
        LOG.info("Phone controls available on home Wi-Fi at http://<host-IP>:%s", server.server_port)
        try:
            if "journeys" in config:
                journeys.run(config, device, key, stop, fetch_predictions, control.active, blank_screen,
                             fetch_trips=fetch_trip_updates)
            else:
                run(config, device, key, stop, control.active)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except Exception as exc:
        LOG.error("Stopped (%s): check config, credentials, connectivity, and firmware", type(exc).__name__)
        raise SystemExit(1)

