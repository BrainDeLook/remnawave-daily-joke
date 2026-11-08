import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from zoneinfo import ZoneInfoNotFoundError

from daily_joke import app


def configuration(**changes):
    result = {
        "panel_url": "https://panel.invalid/access?key=example",
        "api_key": "test-token",
        "cookie": "access=test-cookie",
        "daily_time": "09:30",
        "timezone": "UTC",
        "examples": ["Пример старого анекдота."],
        "example_count": 8,
        "max_chars": 240,
        "model": "test-model",
        "dry_run": False,
    }
    result.update(changes)
    return result


class FakeOpener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return io.BytesIO(json.dumps(next(self.responses), ensure_ascii=False).encode("utf-8"))


class ExampleCountTests(unittest.TestCase):
    def test_selected_count_and_all_share_prompt_for_both_generators(self):
        examples = [f"Анекдот номер {i}" for i in range(12)]
        for generator_class in (app.Generator, app.TookenGenerator):
            for count, expected in ((3, 3), (0, 12), (20, 12)):
                with self.subTest(generator=generator_class.__name__, count=count):
                    generator = generator_class(configuration(examples=examples, example_count=count))
                    messages = generator.joke_messages([])
                    selected = json.loads(messages[1]["content"])["style_examples"]
                    self.assertEqual(len(selected), expected)
                    self.assertEqual(len(set(selected)), expected)
                    self.assertTrue(set(selected).issubset(examples))

class PanelTests(unittest.TestCase):
    def test_update_preserves_other_headers_uuid_and_utf8(self):
        joke = "Ёжик купил Wi-Fi — теперь весь лес на связи. 🦔"
        encoded = "rwEncodeBase64:" + joke
        settings = {
            "uuid": "123e4567-e89b-12d3-a456-426614174000",
            "customResponseHeaders": {"Announce": "old", "ANNOUNCE": "older",
                                      "X-Custom": "оставить", "profile-title": "unchanged"},
            "unrelated": "must not be patched",
        }
        original = json.loads(json.dumps(settings))
        opener = FakeOpener([{"response": {}}, {"response": {
            "uuid": settings["uuid"], "customResponseHeaders": {
                "announce": encoded, "X-Custom": "оставить", "profile-title": "unchanged"}}}])
        with mock.patch.object(app.urllib.request, "build_opener", return_value=opener):
            panel = app.Panel(configuration())
        panel.update(settings, joke)
        self.assertEqual(settings, original)
        patch, verify = opener.requests
        self.assertEqual(patch.get_method(), "PATCH")
        self.assertEqual(patch.full_url, "https://panel.invalid/api/subscription-settings")
        self.assertEqual(json.loads(patch.data), {
            "uuid": settings["uuid"], "customResponseHeaders": {
                "announce": encoded, "X-Custom": "оставить", "profile-title": "unchanged"}})
        self.assertEqual(encoded.removeprefix("rwEncodeBase64:"), joke)
        self.assertEqual(patch.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(patch.get_header("Cookie"), "access=test-cookie")
        self.assertEqual(verify.get_method(), "GET")

    def test_update_handles_null_headers_and_checks_readback(self):
        opener = FakeOpener([{}, {"response": {"uuid": "id", "customResponseHeaders": None}}])
        with mock.patch.object(app.urllib.request, "build_opener", return_value=opener):
            panel = app.Panel(configuration())
        with self.assertRaisesRegex(app.SafeError, "verification failed"):
            panel.update({"uuid": "id", "customResponseHeaders": None}, "Новая шутка")
        self.assertEqual(set(json.loads(opener.requests[0].data)["customResponseHeaders"]), {"announce"})

    def test_get_denies_malformed_settings(self):
        for response in [None, [], {}, {"uuid": 1}, {"uuid": "id", "customResponseHeaders": []},
                         {"uuid": "id", "customResponseHeaders": {"x": 1}}]:
            with self.subTest(response=response):
                opener = FakeOpener([{"response": response}])
                with mock.patch.object(app.urllib.request, "build_opener", return_value=opener):
                    panel = app.Panel(configuration())
                with self.assertRaises(app.SafeError):
                    panel.get()

    def test_invalid_urls_and_credentials_are_denied(self):
        for changes in [{"panel_url": "http://panel.invalid"},
                        {"panel_url": "https://user:password@panel.invalid"},
                        {"panel_url": "https:///missing-host"},
                        {"api_key": " "}, {"api_key": "token\r\nInjected: value"},
                        {"cookie": "cookie\nInjected: value"}]:
            with self.subTest(changes=changes), self.assertRaises(app.SafeError):
                app.Panel(configuration(**changes))

    def test_access_link_never_receives_bearer_token(self):
        opener = FakeOpener([{}])
        with mock.patch.object(app.urllib.request, "build_opener", return_value=opener):
            panel = app.Panel(configuration(cookie=""))
        panel.unlock()
        request, = opener.requests
        self.assertEqual(request.full_url, configuration()["panel_url"])
        self.assertIsNone(request.get_header("Authorization"))

    def test_redirect_is_not_followed(self):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/must-not-be-requested")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            opener = app.urllib.request.build_opener(app.urllib.request.ProxyHandler({}), app.NoRedirect())
            with self.assertRaisesRegex(app.SafeError, "redirect refused"):
                app.http_json(opener, f"http://127.0.0.1:{server.server_port}/redirect")
            self.assertEqual(paths, ["/redirect"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


def fake_timezone(name):
    if name == "UTC":
        return dt.timezone.utc
    raise ZoneInfoNotFoundError(name)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(app, "ZoneInfo", side_effect=fake_timezone)
        patch.start()
        self.addCleanup(patch.stop)

    def load(self, value):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "options.json"
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            return app.load_config(path)

    def test_valid_config(self):
        self.assertEqual(self.load(configuration()), configuration())

    def test_malformed_config_is_denied(self):
        cases = [{"daily_time": "9:30"}, {"daily_time": "24:00"},
                 {"daily_time": "09:60"}, {"timezone": "Not/A_Timezone"},
                 {"example_count": -1}, {"example_count": True}, {"example_count": "all"},
                 {"examples": []}, {"examples": [""]}, {"examples": [42]},
                 {"examples": ["я" * 1201]}, {"examples": "это не список"},
                 {"max_chars": 79}, {"max_chars": 1001}, {"api_key": " "},
                 {"panel_url": "http://panel.invalid"}]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises((app.SafeError, ValueError, ZoneInfoNotFoundError)):
                    self.load(configuration(**changes))

    def test_broken_json_is_denied(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "options.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                app.load_config(path)


class JokeTests(unittest.TestCase):
    def test_unicode_and_outer_quotes(self):
        self.assertEqual(app.valid_joke('  «Ёжик пошёл домой 🦔»  ', [], [], 80), "Ёжик пошёл домой 🦔")
        self.assertEqual(app.valid_joke('"Русская шутка"', [], [], 80), "Русская шутка")

    def test_duplicates_ignore_case_spaces_and_punctuation(self):
        for examples, history in [(["Ёжик, ПРИВЕТ!"], []),
                                  ([], [{"text": "Ёжик, ПРИВЕТ!", "date": "2026-01-01"}])]:
            with self.subTest(examples=examples, history=history):
                self.assertIsNone(app.valid_joke("ёжик... привет?", examples, history, 80))

    def test_rejects_invalid_content(self):
        for text in ["", "   ", "English only", "😀", "я" * 81,
                     "Шутка\x00", "Шутка\rвнутри", "Шутка\u200b", "<think>Мысли</think>Шутка"]:
            with self.subTest(text=text):
                self.assertIsNone(app.valid_joke(text, [], [], 80))
        self.assertEqual(app.valid_joke("я" * 80, [], [], 80), "я" * 80)
        self.assertEqual(app.valid_joke("Шутка\nс продолжением\tтут", [], [], 80),
                         "Шутка\nс продолжением\tтут")

    def test_generator_retries_invalid_and_duplicate_responses(self):
        generator = app.Generator(configuration())
        with mock.patch.object(generator, "call", side_effect=[
            {"message": {"content": "English"}},
            {"message": {"content": "Пример старого анекдота!"}},
            {"message": {"content": "Совсем новая шутка"}},
            {"message": {"content": '{"choice": 1}'}},
        ]) as call:
            self.assertEqual(generator.generate([]), "Совсем новая шутка")
        self.assertEqual(call.call_count, 4)
        for request in call.call_args_list:
            self.assertEqual(request.args[1]["keep_alive"], 0)
            self.assertEqual(request.args[1]["options"]["num_ctx"], 4096)

    def test_review_selects_existing_candidate_instead_of_rewriting(self):
        generator = app.Generator(configuration())
        with mock.patch.object(generator, "call", side_effect=[
            {"message": {"content": "Первая новая шутка"}},
            {"message": {"content": "Вторая новая шутка"}},
            {"message": {"content": "Третья новая шутка"}},
            {"message": {"content": '{"choice": 2}'}},
        ]):
            self.assertEqual(generator.generate([]), "Вторая новая шутка")

    def test_failed_review_never_returns_unreviewed_text(self):
        generator = app.Generator(configuration())
        for content in ['{"choice": 0}', '{"choice": 2}', '{"choice": true}',
                        '{"choice": "1"}', '[]', 'broken JSON']:
            with self.subTest(content=content):
                with mock.patch.object(generator, "call", return_value={"message": {"content": content}}):
                    with self.assertRaises(app.SafeError):
                        generator.choose(["Русская шутка"])

    def test_model_envelope_is_validated_and_truncation_rejected(self):
        generator = app.Generator(configuration())
        for response in [{}, {"message": []}, {"message": {"content": 42}}]:
            with self.subTest(response=response):
                with mock.patch.object(generator, "call", return_value=response):
                    with self.assertRaises(app.SafeError):
                        generator.chat([], 0.8)
        with mock.patch.object(generator, "call", return_value={
            "message": {"content": "Незавершённая история"}, "done_reason": "length"}):
            self.assertEqual(generator.chat([], 0.8), "")

    def test_generator_stops_after_three_invalid_responses(self):
        generator = app.Generator(configuration())
        with mock.patch.object(generator, "call", return_value={"message": {"content": ""}}) as call:
            with self.assertRaises(app.SafeError):
                generator.generate([])
        self.assertEqual(call.call_count, 3)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"
        timezone_patch = mock.patch.object(app, "ZoneInfo", side_effect=fake_timezone)
        timezone_patch.start()
        self.addCleanup(timezone_patch.stop)
        self.panel_patch = mock.patch.object(app, "Panel")
        self.panel = self.panel_patch.start().return_value
        self.addCleanup(self.panel_patch.stop)
        self.panel.get.return_value = {"uuid": "settings-id", "customResponseHeaders": {"X": "kept"}}
        self.generator = mock.Mock()
        self.generator.generate.return_value = "Новая шутка дня"
        now = dt.datetime(2026, 11, 7, 10, tzinfo=dt.timezone.utc)
        self.clock = mock.patch.object(app.dt, "datetime")
        clock = self.clock.start()
        self.addCleanup(self.clock.stop)
        clock.now.return_value = now

    def read_state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_due_before_at_after_and_next_day(self):
        for hour, minute, state, expected in [
            (9, 29, {}, False), (9, 30, {}, True), (23, 59, {}, True),
            (9, 30, {"last_date": "2026-11-07"}, False),
            (9, 30, {"last_date": "2026-11-06"}, True),
        ]:
            with self.subTest(hour=hour, minute=minute, state=state):
                now = mock.Mock()
                now.date.return_value.isoformat.return_value = "2026-11-07"
                now.strftime.return_value = f"{hour:02}:{minute:02}"
                self.assertEqual(app.due(configuration(), state, now), expected)

    def test_success_persists_history_and_backup(self):
        state = {"history": [{"date": "old", "text": str(i)} for i in range(60)]}
        app.run_once(configuration(), self.generator, state, self.path)
        self.panel.update.assert_called_once_with(self.panel.get.return_value, "Новая шутка дня")
        self.assertEqual(state["last_date"], "2026-11-07")
        self.assertNotIn("pending", state)
        self.assertEqual(len(state["history"]), 60)
        self.assertEqual(state["history"][-1], {"date": "2026-11-07", "text": "Новая шутка дня"})
        self.assertEqual(self.read_state(), state)
        self.assertEqual(json.loads((self.path.parent / "before-update.json").read_text(encoding="utf-8")),
                         self.panel.get.return_value)
        self.assertFalse(self.path.with_suffix(".tmp").exists())

    def test_preserves_leading_bracketed_announcement_in_publish_and_preview(self):
        for old in ("rwEncodeBase64: [Финляндия снова работает]\n\nСтарый анекдот",
                    "[Финляндия снова работает]\n\nСтарый анекдот"):
            for preview in (False, True):
                with self.subTest(old=old, preview=preview):
                    self.panel.reset_mock()
                    self.panel.get.return_value = {"uuid": "id", "customResponseHeaders": {"Announce": old}}
                    state = {}
                    result = app.run_once(configuration(dry_run=preview), self.generator, state, self.path)
                    expected = "[Финляндия снова работает]\n\nНовая шутка дня"
                    self.assertEqual(result, expected)
                    if preview:
                        self.panel.update.assert_not_called()
                    else:
                        self.panel.update.assert_called_once_with(self.panel.get.return_value, expected)
                        self.assertEqual(state["history"][-1]["text"], "Новая шутка дня")

    def test_brackets_inside_old_joke_are_not_preserved(self):
        self.panel.get.return_value = {"uuid": "id", "customResponseHeaders": {
            "announce": "rwEncodeBase64:Старая шутка [с примечанием]"}}
        result = app.run_once(configuration(), self.generator, {}, self.path)
        self.assertEqual(result, "Новая шутка дня")

    def test_failed_update_persists_pending_and_retry_reuses_same_joke(self):
        state = {}

        def fail_after_persist(settings, joke):
            self.assertEqual(self.read_state()["pending"], {"date": "2026-11-07", "text": joke})
            raise app.SafeError("simulated failure")

        self.panel.update.side_effect = fail_after_persist
        with self.assertRaises(app.SafeError):
            app.run_once(configuration(), self.generator, state, self.path)
        self.assertNotIn("last_date", state)
        self.assertNotIn("history", state)
        recovered = self.read_state()
        self.panel.update.side_effect = None
        app.run_once(configuration(), self.generator, recovered, self.path)
        self.generator.generate.assert_called_once_with([])
        self.assertEqual(self.panel.update.call_args_list[0], self.panel.update.call_args_list[1])
        self.assertNotIn("pending", self.read_state())
        self.assertEqual(len(recovered["history"]), 1)

    def test_stale_pending_generates_fresh_joke(self):
        state = {"pending": {"date": "2026-11-06", "text": "Вчерашняя шутка"}}
        app.run_once(configuration(), self.generator, state, self.path)
        self.generator.generate.assert_called_once_with([])
        self.panel.update.assert_called_once_with(self.panel.get.return_value, "Новая шутка дня")

    def test_dry_run_never_updates_panel_or_creates_backup(self):
        state = {}
        app.run_once(configuration(dry_run=True), self.generator, state, self.path)
        self.panel.update.assert_not_called()
        self.assertEqual(self.read_state(), {"last_date": "2026-11-07", "preview_history": [
            {"date": "2026-11-07", "text": "Новая шутка дня"}]})
        self.assertFalse((self.path.parent / "before-update.json").exists())

    def test_preview_history_is_sent_on_next_preview_and_bounded(self):
        state = {"history": [{"text": "Опубликованный анекдот"}]}
        for text in ("Первая шутка", "Вторая шутка", "Третья шутка", "Четвёртая шутка"):
            self.generator.generate.return_value = text
            app.run_once(configuration(dry_run=True), self.generator, state, self.path)
        self.assertEqual([x["text"] for x in state["preview_history"]],
                         ["Вторая шутка", "Третья шутка", "Четвёртая шутка"])
        self.assertEqual([x["text"] for x in self.generator.generate.call_args.args[0]],
                         ["Опубликованный анекдот", "Первая шутка", "Вторая шутка", "Третья шутка"])

    def test_dry_run_reuses_pending_without_generating(self):
        state = {"pending": {"date": "2026-11-07", "text": "Уже выбранная шутка"}}
        app.run_once(configuration(dry_run=True), self.generator, state, self.path)
        self.generator.generate.assert_not_called()
        self.panel.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
