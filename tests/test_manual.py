import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from daily_joke import app
from daily_joke.manual import ManualControl, ManualHandler, ManualServer


class ManualTests(unittest.TestCase):
    def test_manual_bypasses_daily_limit_and_serializes_generation(self):
        control = ManualControl(True)
        self.assertFalse(control.request())  # Model is still loading.
        control.ready = True
        now = mock.Mock()
        now.date.return_value.isoformat.return_value = "2026-11-07"
        now.strftime.return_value = "19:33"
        scheduled = app.due({"daily_time": "19:33"}, {"last_date": "2026-11-07"}, now)
        self.assertFalse(scheduled)
        self.assertTrue(control.request())
        self.assertFalse(control.request())
        self.assertEqual(control.take(scheduled), "manual")
        self.assertFalse(control.request())
        self.assertIsNone(control.take(True))
        control.finish("Предпросмотр: Новая шутка")
        self.assertTrue(control.request())
        self.assertEqual(control.take(False), "manual")

    def test_schedule_and_error_release_manual_button(self):
        control = ManualControl(False)
        control.ready = True
        self.assertEqual(control.take(True), "schedule")
        self.assertFalse(control.request())
        control.finish("Network error")
        self.assertFalse(control.snapshot()["disabled"])
        self.assertTrue(control.request())
        self.assertEqual(control.take(False), "manual")

    def test_http_requires_ingress_and_run_token(self):
        control = ManualControl(True)
        control.ready = True
        server = ManualServer(("127.0.0.1", 0), control)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{server.server_port}"
        with self.assertRaises(urllib.error.HTTPError) as error:
            opener.open(base + "/status")
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        with mock.patch.object(ManualHandler, "allowed", return_value=True):
            with opener.open(base + "/") as response:
                self.assertIn('Сгенерировать предпросмотр', response.read().decode())
            with opener.open(base + "/status") as response:
                status = json.load(response)
            self.assertTrue(status["dry_run"])
            for token, expected in [("wrong", 403), (status["token"], 202), (status["token"], 409)]:
                request = urllib.request.Request(base + "/run", method="POST", headers={"X-Run-Token": token})
                if expected == 202:
                    with opener.open(request) as response:
                        self.assertEqual(response.status, expected)
                else:
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        opener.open(request)
                    self.assertEqual(error.exception.code, expected)
                    error.exception.close()

