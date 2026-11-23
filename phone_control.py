"""Home-LAN phone controls, served by the same process that drives the display."""
from datetime import datetime, timedelta, time as daytime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

LOG = logging.getLogger('kitchen-display')


def next_morning(config, now, scheduled):
    local = now.astimezone(ZoneInfo(config['timezone']))
    start = daytime.fromisoformat(config['start'])
    for offset in range(367):
        candidate = datetime.combine(local.date() + timedelta(days=offset), start, local.tzinfo)
        if candidate > now and scheduled(config, candidate):
            return candidate
    return None  # No scheduled school morning in the configured year.


class Control:
    def __init__(self, config, scheduled, state_path):
        self.config, self.scheduled = config, scheduled
        self.path = Path(state_path)
        self.lock = threading.RLock()
        self.mode, self.until, self.revision = 'auto', None, 0
        self.last_sent, self.shown, self.applied_revision, self.error = None, None, -1, None
        if self.path.exists():
            try:
                self._load()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                # A damaged override file (e.g. SD card power loss) must never stop
                # the school-morning display; fall back to the normal schedule.
                LOG.warning('Ignoring unreadable phone control state (%s); using Automatic',
                            type(exc).__name__)
                self.mode, self.until = 'auto', None

    def _load(self):
        data = json.loads(self.path.read_text())
        if data['mode'] not in ('on', 'off', 'auto'):
            raise ValueError('Invalid saved display mode')
        until = datetime.fromisoformat(data['until']) if data['until'] else None
        if until is not None and until.tzinfo is None:
            raise ValueError('Saved expiry must include timezone')
        if data['mode'] == 'on' and until is None:
            raise ValueError('Temporary on must expire')
        self.mode, self.until = data['mode'], until

    def _expire(self, now):
        if self.until is not None and now >= self.until:
            self.mode, self.until = 'auto', None
            self.revision += 1

    def active(self, config, now):
        with self.lock:
            self._expire(now)
            return config['enabled'] and (self.mode == 'on' or
                   (self.mode == 'auto' and self.scheduled(config, now)))

    def set_mode(self, mode, now=None):
        if mode not in ('on', 'off', 'auto'):
            raise ValueError('Unknown mode')
        now = now or datetime.now().astimezone()
        until = now + timedelta(minutes=30) if mode == 'on' else (
            next_morning(self.config, now, self.scheduled) if mode == 'off' else None)
        with self.lock:
            # Persist before reporting success; a service restart must not undo Off.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'mode': mode, 'until': until.isoformat() if until else None}))
            temporary.replace(self.path)
            self.mode, self.until = mode, until
            self.revision += 1

    def status(self, now=None):
        now = now or datetime.now().astimezone()
        with self.lock:
            desired = self.active(self.config, now)
            pending = self.applied_revision != self.revision or self.shown != desired
            return {'mode': self.mode, 'until': self.until.isoformat() if self.until else None,
                    'timezone': self.config['timezone'], 'wants_on': desired,
                    'pending': pending, 'error': self.error,
                    'last_sent': self.last_sent.isoformat() if self.last_sent else None}


class ControlledDevice:
    def __init__(self, device, control, blank):
        self.device, self.control, self.blank = device, control, blank

    def show(self, payload):
        with self.control.lock:
            revision = self.control.revision
        try:
            self.device.show(payload)
        except Exception:
            with self.control.lock:
                self.control.error = 'Cannot reach the display. Its last image may still be visible.'
            raise
        with self.control.lock:
            self.control.error = None
            self.control.last_sent = datetime.now().astimezone()
            self.control.shown = payload != self.blank()
            self.control.applied_revision = revision


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(5)
        return sock, address


def start(control, host='0.0.0.0', port=8765):
    token = secrets.token_urlsafe(32)
    page = Path(__file__).with_name('phone.html').read_text().replace('__TOKEN__', token).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def local_host(self):
            # Reject public Host names to prevent DNS rebinding into the LAN.
            try:
                hostname = urlsplit('http://' + self.headers.get('Host', '')).hostname
                if hostname == 'localhost' or (hostname and hostname.endswith('.local')):
                    return True
                address = ipaddress.ip_address(hostname)
                return address.is_private and not address.is_unspecified
            except (ValueError, TypeError):
                return False

        def reply(self, code, body, content_type='application/json'):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.local_host():
                return self.reply(403, b'{}')
            if self.path == '/':
                return self.reply(200, page, 'text/html; charset=utf-8')
            if self.path == '/api/status':
                return self.reply(200, json.dumps(control.status()).encode())
            self.reply(404, b'{}')

        def do_POST(self):
            # Only our page can read the unpredictable token; no CORS is enabled.
            if not self.local_host() or not secrets.compare_digest(self.headers.get('X-Control-Token', ''), token):
                return self.reply(403, b'{}')
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self.reply(403, b'{}')
            if self.path != '/api/mode':
                return self.reply(404, b'{}')
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 128:
                    raise ValueError('Invalid request length')
                data = json.loads(self.rfile.read(length))
                control.set_mode(data['mode'])
            except (ValueError, KeyError, TypeError):
                return self.reply(400, b'{}')
            except OSError:
                LOG.exception('Could not save phone control state')
                return self.reply(500, b'{}')
            self.reply(200, json.dumps(control.status()).encode())

    server = Server((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def state_path():
    return os.environ.get('KITCHEN_DISPLAY_STATE', str(Path.home() / '.local/state/kitchen-display/control.json'))
