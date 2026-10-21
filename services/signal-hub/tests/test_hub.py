import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trading_signal_hub.config import Config, ConfigurationError, load_config
from trading_signal_hub.store import bind_bot, connect, get_status, ingest_updates, initialize, list_events, metadata
from trading_signal_hub.telegram import ApiError, TelegramAPI, check_bot


def update(uid=1, *, chat=-100123, message=10, text="synthetic signal", kind="channel_post", **fields):
    return {"update_id": uid, kind: {"chat": {"id": chat}, "message_id": message,
                                    "date": 1792584000, "text": text, **fields}}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(Path(self.temp.name), "test-source", True, frozenset({-100123}), 25)
        initialize(self.config)

    def test_replay_is_deduplicated_across_restart(self):
        ingest_updates(self.config, [update()])
        initialize(self.config)
        ingest_updates(self.config, [update()])
        events = list_events(self.config)["items"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["revision"], 1)
        self.assertEqual(metadata(self.config)["next_offset"], "2")

    def test_edit_and_reply_preserve_provenance(self):
        ingest_updates(self.config, [update(), update(2, text="edited synthetic signal", kind="edited_channel_post", edit_date=1792584010),
                                     update(3, message=11, text="SL to BE", reply_to_message={"message_id": 10})])
        events = list_events(self.config)["items"]
        self.assertEqual([event["revision"] for event in events], [1, 2, 1])
        self.assertEqual(events[1]["event_type"], "edit")
        self.assertNotEqual(events[0]["content_hash"], events[1]["content_hash"])
        self.assertEqual(events[2]["reply_to_message_id"], 10)
        self.assertTrue(all(event["state"] == "observed" for event in events))

    def test_unallowed_messages_are_not_stored_but_offset_advances(self):
        counts = ingest_updates(self.config, [update(1, chat=999), update(2)])
        self.assertEqual(counts, {"inserted": 1, "ignored": 1})
        self.assertEqual(metadata(self.config)["next_offset"], "3")
        self.assertEqual(list_events(self.config)["items"][0]["chat_id"], -100123)

    def test_batch_rolls_back_events_and_offset_if_allowed_event_invalid(self):
        bad = update(2)
        bad["channel_post"]["date"] = "invalid"
        with self.assertRaises(ValueError):
            ingest_updates(self.config, [update(), bad])
        self.assertEqual(list_events(self.config)["items"], [])
        self.assertNotIn("next_offset", metadata(self.config))

    def test_same_parameters_new_message_is_not_deduplicated(self):
        ingest_updates(self.config, [update(), update(2, message=11)])
        self.assertEqual(len(list_events(self.config)["items"]), 2)

    def test_offset_cannot_move_backwards(self):
        ingest_updates(self.config, [update(100)])
        ingest_updates(self.config, [update(1)])
        self.assertEqual(metadata(self.config)["next_offset"], "101")

    def test_read_pagination_does_not_change_offset(self):
        ingest_updates(self.config, [update(1), update(2, message=11)])
        before = metadata(self.config)
        first = list_events(self.config, limit=1)
        second = list_events(self.config, after_sequence=first["next_cursor"])
        self.assertEqual(second["items"][0]["update_id"], 2)
        self.assertEqual(metadata(self.config), before)
        with self.assertRaises(ValueError):
            list_events(self.config, limit=1000)

    def test_database_bot_and_source_binding(self):
        bind_bot(self.config, 123)
        bind_bot(self.config, 123)
        with self.assertRaises(ValueError):
            bind_bot(self.config, 456)
        with self.assertRaises(ValueError):
            initialize(Config(self.config.home, "another-source", True, frozenset(), 25))

    def test_readonly_connection_refuses_writes(self):
        import sqlite3
        with connect(self.config.database, readonly=True) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM events")

    def test_status_never_claims_execution(self):
        status = get_status(self.config)
        self.assertFalse(status["execution_supported"])
        self.assertIn("BOT_TOKEN_MISSING_OR_INVALID", status["blockers"])


class ConfigAndApiTests(unittest.TestCase):
    def test_secret_not_in_repr_and_unknown_config_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": ""}):
            home = Path(directory)
            raw = {"source_id": "test", "enabled": False, "allowed_chat_ids": []}
            (home / "config.json").write_text(json.dumps(raw))
            (home / ".env").write_text("TELEGRAM_BOT_TOKEN=123:synthetic_secret\n")
            config = load_config(home)
            self.assertNotIn("synthetic_secret", repr(config))
            self.assertFalse(get_status(config)["configured"])
            raw["unexpected"] = "execute"
            (home / "config.json").write_text(json.dumps(raw))
            with self.assertRaises(ConfigurationError):
                load_config(home)

    def test_string_chat_ids_and_booleans_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for ids in (["-100123"], [True], [0]):
                (path / "config.json").write_text(json.dumps({"source_id": "test", "enabled": False, "allowed_chat_ids": ids}))
                with self.assertRaises(ConfigurationError):
                    load_config(path)

    def test_api_has_no_sending_methods(self):
        with self.assertRaises(ApiError) as raised:
            TelegramAPI("123:synthetic_secret").call("sendMessage", {})
        self.assertEqual(str(raised.exception), "METHOD_NOT_ALLOWED")

    def test_existing_webhook_is_not_removed(self):
        class FakeAPI:
            def call(self, method):
                assert method == "getWebhookInfo"
                return {"url": "https://example.invalid/existing"}
        with self.assertRaisesRegex(ApiError, "EXISTING_WEBHOOK_REFUSED"):
            check_bot(None, FakeAPI())

    def test_transport_errors_do_not_expose_token(self):
        import urllib.error
        api = TelegramAPI("123:synthetic_secret")
        with patch.object(api._opener, "open", side_effect=urllib.error.URLError("https://token-bearing-url")):
            with self.assertRaises(ApiError) as raised:
                api.call("getMe")
        self.assertEqual(str(raised.exception), "TELEGRAM_TRANSPORT_ERROR")


if __name__ == "__main__":
    unittest.main()
