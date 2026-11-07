import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from daily_joke import app
from test_app import configuration, FakeOpener, fake_timezone


def response(text="Купил будильник с характером. Теперь он обижается, когда его завожу.", **changes):
    result = {"choices": [{"finish_reason": "stop", "message": {"content": text}}],
              "usage": {"prompt_tokens": 1100, "completion_tokens": 60, "total_tokens": 1160}}
    result.update(changes)
    return result


class TookenTests(unittest.TestCase):
    def generator(self, payload):
        opener = FakeOpener([payload])
        with mock.patch.object(app.urllib.request, "build_opener", return_value=opener):
            generator = app.TookenGenerator(configuration(
                provider="tooken", tooken_api_key="tooken-test", tooken_model="gemini-3.8-flash"))
        return generator, opener

    def test_single_paid_request_only_and_separate_credentials(self):
        generator, opener = self.generator(response())
        with mock.patch.object(generator, "call", side_effect=AssertionError("Ollama must not be called")):
            generator.prepare()
            joke = generator.generate([])
        self.assertIn("будильник", joke)
        request, = opener.requests
        self.assertEqual(request.full_url, "https://tooken.club/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer tooken-test")
        self.assertIsNone(request.get_header("Cookie"))
        body = json.loads(request.data)
        self.assertEqual(body["model"], "gemini-3.8-flash")
        self.assertEqual(body["max_tokens"], 512)
        self.assertNotIn("api_key", json.dumps(body))
        self.assertNotIn("panel.invalid", json.dumps(body))
        self.assertNotIn("keep_alive", body)
        self.assertEqual(generator.usage, {"input": 1100, "output": 60})

    def test_invalid_repeated_and_truncated_output_is_not_retried(self):
        for payload in [response("English only"), response("Пример старого анекдота!"),
                        response(choices=[{"finish_reason": "length", "message": {"content": "История"}}]),
                        response(choices=[]), response(choices=[None]),
                        response(choices=[{"finish_reason": "stop", "message": {"content": None}}]),
                        response(usage={"prompt_tokens": True, "completion_tokens": 1})]:
            with self.subTest(payload=payload):
                generator, opener = self.generator(payload)
                with self.assertRaises(app.SafeError):
                    generator.generate([])
                self.assertEqual(len(opener.requests), 1)

    def test_missing_usage_does_not_invent_counts(self):
        generator, _ = self.generator(response(usage=None))
        self.assertTrue(generator.generate([]))
        self.assertIsNone(generator.usage)

    def test_main_api_mode_never_starts_ollama(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            config = configuration(provider="tooken", tooken_api_key="test", tooken_model="gemini-3.8-flash")
            server = mock.Mock()
            with mock.patch.dict(app.os.environ, {"DATA_DIR": directory}), \
                 mock.patch.object(app, "load_config", return_value=config), \
                 mock.patch.object(app, "ManualServer", return_value=server), \
                 mock.patch.object(app.signal, "signal"), \
                 mock.patch.object(app.subprocess, "Popen") as process, \
                 mock.patch.object(app, "TookenGenerator") as generator, \
                 mock.patch.object(app, "ZoneInfo", side_effect=fake_timezone), \
                 mock.patch.object(app, "due", return_value=True), \
                 mock.patch.object(app.time, "sleep", side_effect=KeyboardInterrupt), \
                 mock.patch.object(app, "run_once", side_effect=app.SafeError("API failed")):
                with self.assertRaises(KeyboardInterrupt):
                    app.main()
                process.assert_not_called()
                generator.return_value.prepare.assert_called_once()
            state = json.loads((Path(directory) / "state.json").read_text(encoding="utf-8"))
            self.assertIn("last_date", state)  # No repeated paid automatic calls today.

    def test_api_config_validated_at_load_boundary(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "options.json"
            for changes in [{"tooken_api_key": ""}, {"tooken_api_key": "key\nheader"},
                            {"tooken_model": ""}, {"tooken_model": "model\nheader"},
                            {"provider": "unknown"}]:
                config = configuration(provider="tooken", tooken_api_key="test", tooken_model="gemini-3.8-flash")
                config.update(changes)
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.subTest(changes=changes), self.assertRaises(app.SafeError):
                    app.load_config(path)
