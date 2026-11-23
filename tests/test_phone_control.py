from datetime import datetime, timedelta
import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import Mock, patch

import bus_display as app
import journeys
import phone_control as phone


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = tomllib.loads((Path(__file__).resolve().parent / "fixture_config.toml").read_text())
        self.config['enabled'] = True
        self.path = Path(self.temp.name) / 'state.json'
        self.control = phone.Control(self.config, app.active, self.path)
        self.now = datetime.fromisoformat('2026-11-13T07:30:00-05:00')

    def test_damaged_state_file_falls_back_to_automatic(self):
        for bad in ('{bad json', '{"mode": "sideways", "until": null}', '{"mode": "on", "until": null}',
                    '{"mode": "off", "until": "2026-11-16T07:00:00"}', '[]', ''):
            self.path.write_text(bad)
            with self.assertLogs('kitchen-display', 'WARNING'):
                control = phone.Control(self.config, app.active, self.path)
            self.assertEqual((control.mode, control.until), ('auto', None), bad)
            self.assertTrue(control.active(self.config, self.now))

    def test_valid_state_still_restored(self):
        self.control.set_mode('off', self.now)
        restored = phone.Control(self.config, app.active, self.path)
        self.assertEqual(restored.mode, 'off')

    def test_automatic_obeys_schedule(self):
        self.assertTrue(self.control.active(self.config, self.now))
        self.assertFalse(self.control.active(self.config, self.now.replace(hour=12)))

    def test_on_overrides_weekend_then_expires(self):
        now = self.now + timedelta(days=1)
        self.control.set_mode('on', now)
        self.assertTrue(self.control.active(self.config, now + timedelta(minutes=29)))
        self.assertFalse(self.control.active(self.config, now + timedelta(minutes=30)))
        self.assertEqual(self.control.status(now + timedelta(minutes=30))['mode'], 'auto')

    def test_off_resumes_monday_not_tomorrow(self):
        self.control.set_mode('off', self.now)
        until = datetime.fromisoformat('2026-11-16T07:00:00-05:00')
        self.assertEqual(self.control.until, until)
        self.assertFalse(self.control.active(self.config, self.now))
        self.assertTrue(self.control.active(self.config, until))

    def test_off_before_morning_resumes_that_morning(self):
        self.control.set_mode('off', self.now.replace(hour=6))
        self.assertEqual(self.control.until, self.now.replace(hour=7, minute=0))

    def test_off_skips_holidays(self):
        now = datetime.fromisoformat('2026-11-24T08:00:00-05:00')
        self.control.set_mode('off', now)
        self.assertEqual(self.control.until, datetime.fromisoformat('2026-11-30T07:00:00-05:00'))

    def test_automatic_clears_override(self):
        self.control.set_mode('off', self.now)
        self.control.set_mode('auto', self.now)
        self.assertTrue(self.control.active(self.config, self.now))
        self.assertIsNone(self.control.until)

    def test_restart_keeps_override_and_expiry(self):
        self.control.set_mode('on', self.now)
        restored = phone.Control(self.config, app.active, self.path)
        self.assertTrue(restored.active(self.config, self.now))
        self.assertEqual(restored.until, self.now + timedelta(minutes=30))
        self.assertEqual(restored.status(self.now + timedelta(hours=1))['mode'], 'auto')
        self.control.set_mode('off', self.now)
        restored = phone.Control(self.config, app.active, self.path)
        self.assertFalse(restored.active(self.config, self.now))

    def test_disabled_config_cannot_be_overridden(self):
        self.config['enabled'] = False
        self.control.set_mode('on', self.now)
        self.assertFalse(self.control.active(self.config, self.now))

    def test_failed_push_is_not_confirmed(self):
        self.control.set_mode('off')
        device = Mock()
        device.show.side_effect = OSError('offline')
        wrapped = phone.ControlledDevice(device, self.control, app.blank_screen)
        with self.assertRaises(OSError):
            wrapped.show(app.blank_screen())
        self.assertTrue(self.control.status()['pending'])
        self.assertIsNotNone(self.control.status()['error'])
        device.show.side_effect = None
        wrapped.show(app.blank_screen())
        self.assertFalse(self.control.status()['pending'])
        self.assertIsNone(self.control.status()['error'])

    def test_command_during_push_stays_pending(self):
        device = Mock()
        device.show.side_effect = lambda _: self.control.set_mode('off')
        wrapped = phone.ControlledDevice(device, self.control, app.blank_screen)
        wrapped.show({'text': 'buses'})
        self.assertTrue(self.control.status()['pending'])

    def test_off_during_feed_fetch_blanks_instead_of_showing_arrivals(self):
        self.control.set_mode('on')
        stop, device = threading.Event(), Mock()
        calls = []
        def collect(*_):
            self.control.set_mode('off')
            return [{'at': datetime.now().astimezone(), 'predictions': []}] * 2
        def show(payload):
            calls.append(payload)
            stop.set()
        device.show.side_effect = show
        with patch.object(journeys, 'collect', side_effect=collect):
            journeys.run(self.config, device, 'test', stop, Mock(), self.control.active, app.blank_screen)
        self.assertTrue(all(p == app.blank_screen() for p in calls))


class WebTests(unittest.TestCase):
    def setUp(self):
        ControlTests.setUp(self)
        self.server = phone.start(self.control, '127.0.0.1', 0)
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        result = conn.getresponse()
        answer = (result.status, result.read())
        conn.close()
        return answer

    def test_page_and_valid_command(self):
        code, page = self.request('GET', '/')
        self.assertEqual(code, 200)
        self.assertIn('A panda and a bear'.encode(), page)
        token = re.search(rb"const token='([^']+)'", page).group(1).decode()
        code, payload = self.request('POST', '/api/mode', json.dumps({'mode': 'on'}), {'X-Control-Token': token})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(payload)['mode'], 'on')
        self.assertTrue(json.loads(payload)['pending'])
        code, payload = self.request('GET', '/api/status')
        self.assertEqual(json.loads(payload)['mode'], 'on')

    def test_cross_site_and_bad_requests_rejected(self):
        _, page = self.request('GET', '/')
        token = re.search(rb"const token='([^']+)'", page).group(1).decode()
        self.assertEqual(self.request('POST', '/api/mode', '{"mode":"off"}')[0], 403)
        self.assertEqual(self.request('POST', '/api/mode', '{"mode":"off"}',
                         {'X-Control-Token': token, 'Origin': 'https://example.com'})[0], 403)
        self.assertEqual(self.request('GET', '/', headers={'Host': 'example.com'})[0], 403)
        self.assertEqual(self.request('POST', '/api/mode', '{"mode":"invalid"}',
                         {'X-Control-Token': token})[0], 400)
        self.assertEqual(self.control.mode, 'auto')
