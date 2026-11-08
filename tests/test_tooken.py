import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from daily_joke import app
from test_app import configuration, FakeOpener, fake_timezone


def response(text="Купил будильник с характером. Теперь он обижается, когда его завожу.", **changes):
    output = json.dumps({"candidates": [text, "Вторая новая шутка", "Третья новая шутка"], "choice": 1}, ensure_ascii=False)
    result = {"choices": [{"finish_reason": "stop", "message": {"content": output}}],
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
        self.assertEqual(body["max_tokens"], 2048)
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

    def test_review_selects_candidate_and_rejects_malformed_or_duplicate_options(self):
        def payload(value):
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value, ensure_ascii=False)}}]}

        candidates = ["Первый анекдот", "Второй анекдот", "Третий анекдот"]
        generator, _ = self.generator(payload({"candidates": candidates, "choice": 2}))
        self.assertEqual(generator.generate([]), candidates[1])
        for value in ({"candidates": candidates, "choice": 0},
                      {"candidates": candidates, "choice": True},
                      {"candidates": candidates[:2], "choice": 1},
                      {"candidates": ["Один", "Один", "Два"], "choice": 1}):
            with self.subTest(value=value):
                generator, opener = self.generator(payload(value))
                with self.assertRaises(app.SafeError):
                    generator.generate([])
                self.assertEqual(len(opener.requests), 1)

    def test_gpt_models_use_responses_without_temperature(self):
        for model, effort in [("gpt-6.1-terra", "none"), ("gpt-6.1-sol", "low")]:
            payload = {"status": "completed", "usage": {"input_tokens": 1500, "output_tokens": 150},
                       "output": [{"type": "reasoning", "summary": []},
                                  {"type": "message", "content": [{"type": "output_text", "text": json.dumps({"candidates": ["Новый анекдот на русском", "Вторая шутка", "Третья шутка"], "choice": 1}, ensure_ascii=False)}]}]}
            generator, opener = self.generator(payload)
            generator.config["tooken_model"] = model
            with self.subTest(model=model):
                self.assertEqual(generator.generate([]), "Новый анекдот на русском")
                request, = opener.requests
                self.assertEqual(request.full_url, "https://tooken.club/v1/responses")
                body = json.loads(request.data)
                self.assertNotIn("temperature", body)
                self.assertNotIn("max_tokens", body)
                self.assertEqual(body["max_output_tokens"], 2048)
                self.assertEqual(body["reasoning"]["effort"], effort)
                self.assertFalse(body["store"])
                self.assertEqual(generator.usage, {"input": 1500, "output": 150})

    def test_responses_rejects_partial_refused_and_malformed_content(self):
        for payload in [
            {"status": "incomplete", "output": []},
            {"status": "completed", "output": []},
            {"status": "completed", "output": [None]},
            {"status": "completed", "output": [{"type": "message", "content": None}]},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}]},
        ]:
            generator, opener = self.generator(payload)
            generator.config["tooken_model"] = "gpt-6.1-terra"
            with self.subTest(payload=payload), self.assertRaises(app.SafeError):
                generator.generate([])
            self.assertEqual(len(opener.requests), 1)

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
