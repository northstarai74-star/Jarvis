import sqlite3
import threading
import unittest
from datetime import datetime

from jarvis.audit import REDACTED, redact
from jarvis.clock import FakeClock, UTC, in_send_window, uk_local, uk_to_utc
from jarvis.db import Database
from jarvis.settings import Settings, parse_env
from tests.helpers import JarvisTestCase


class EnvTests(unittest.TestCase):
    def test_parses_plain_quoted_and_commented_lines(self):
        env = parse_env(
            "# comment\n"
            "A=1\n"
            "export B = two words \n"
            "C=\"quoted # not a comment\"\n"
            "D='single'\n"
            "E=value # trailing comment\n"
            "\n"
            "no equals sign\n"
            "F=\n"
        )
        self.assertEqual(env["A"], "1")
        self.assertEqual(env["B"], "two words")
        self.assertEqual(env["C"], "quoted # not a comment")
        self.assertEqual(env["D"], "single")
        self.assertEqual(env["E"], "value")
        self.assertEqual(env["F"], "")
        self.assertNotIn("no equals sign", env)

    def test_value_may_contain_equals(self):
        self.assertEqual(parse_env("URL=https://x.test/?a=b")["URL"], "https://x.test/?a=b")


class SettingsTests(JarvisTestCase):
    def test_defaults_and_paths(self):
        self.assertEqual(self.settings.get("EMAIL_SENDER"), "file")
        self.assertEqual(self.settings.data_dir, self.root / "data")
        self.assertEqual(self.settings.db_path, self.root / "data" / "jarvis.db")

    def test_env_file_is_read_and_environment_wins(self):
        (self.root / ".env").write_text("OWNER_PHONE=+440000\nSMTP_HOST=file-host\n", "utf-8")
        s = Settings.load(self.root, environ={"SMTP_HOST": "env-host"})
        self.assertEqual(s.get("OWNER_PHONE"), "+440000")
        self.assertEqual(s.get("SMTP_HOST"), "env-host")

    def test_unknown_environment_variables_are_ignored(self):
        s = Settings.load(self.root, environ={"PATH": "/bin", "RANDOM_THING": "x"})
        self.assertNotIn("RANDOM_THING", s.values)

    def test_missing_keys_lists_every_service_when_nothing_is_set(self):
        missing = self.settings.missing_keys()
        self.assertIn("TWILIO_AUTH_TOKEN", missing["phone (Twilio)"])
        self.assertIn("SMTP_PASSWORD", missing["email (SMTP)"])
        self.assertIn("CALCOM_WEBHOOK_SECRET", missing["booking (Cal.com)"])

    def test_missing_keys_empty_group_disappears(self):
        s = Settings.load(self.root, environ={"COMPANIES_HOUSE_API_KEY": "k"})
        self.assertNotIn("company checks (Companies House)", s.missing_keys())

    def test_secrets_lists_only_set_secret_values(self):
        s = Settings.load(self.root, environ={"SMTP_PASSWORD": "hunter2", "SMTP_HOST": "h"})
        self.assertEqual(s.secrets(), ["hunter2"])

    def test_bad_integer_falls_back(self):
        s = Settings.load(self.root, environ={"MAX_DAILY_CAP": "lots"})
        self.assertEqual(s.get_int("MAX_DAILY_CAP", 50), 50)


class ClockTests(unittest.TestCase):
    def test_winter_is_gmt_and_summer_is_bst(self):
        winter = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
        summer = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
        self.assertEqual(uk_local(winter).hour, 10)
        self.assertEqual(uk_local(summer).hour, 11)

    def test_clock_change_edges(self):
        # BST 2026 starts 29 March 01:00 UTC and ends 25 October 01:00 UTC
        self.assertEqual(uk_local(datetime(2026, 3, 29, 0, 59, tzinfo=UTC)).hour, 0)
        self.assertEqual(uk_local(datetime(2026, 3, 29, 1, 0, tzinfo=UTC)).hour, 2)
        self.assertEqual(uk_local(datetime(2026, 10, 25, 0, 59, tzinfo=UTC)).hour, 1)
        self.assertEqual(uk_local(datetime(2026, 10, 25, 1, 0, tzinfo=UTC)).hour, 1)

    def test_round_trip(self):
        for local in (datetime(2026, 7, 1, 9, 30), datetime(2026, 12, 1, 9, 30)):
            self.assertEqual(uk_local(uk_to_utc(local)), local)

    def test_window_edges(self):
        clock = FakeClock("2026-11-16 09:29:59")
        self.assertFalse(in_send_window(clock.now()))
        clock.set_uk("2026-11-16 09:30:00")
        self.assertTrue(in_send_window(clock.now()))
        clock.set_uk("2026-11-16 16:29:59")
        self.assertTrue(in_send_window(clock.now()))
        clock.set_uk("2026-11-16 16:30:00")
        self.assertFalse(in_send_window(clock.now()))

    def test_weekend_is_outside_the_window(self):
        self.assertFalse(in_send_window(FakeClock("2026-11-21 11:00:00").now()))  # Saturday
        self.assertFalse(in_send_window(FakeClock("2026-11-22 11:00:00").now()))  # Sunday

    def test_window_uses_uk_time_in_summer(self):
        # 08:45 UTC in July is 09:45 in the UK
        self.assertTrue(in_send_window(datetime(2026, 7, 1, 8, 45, tzinfo=UTC)))
        self.assertFalse(in_send_window(datetime(2026, 7, 1, 15, 45, tzinfo=UTC)))

    def test_fake_clock_moves(self):
        clock = FakeClock("2026-11-16 10:00:00")
        clock.advance(days=1, minutes=5)
        self.assertEqual(clock.local_day(), "2026-11-17")
        self.assertEqual(clock.now_iso(), "2026-11-17T10:05:00Z")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)

    def test_kv_round_trip_and_overwrite(self):
        self.assertIsNone(self.db.kv_get("k"))
        self.assertEqual(self.db.kv_get("k", "d"), "d")
        self.db.kv_set("k", 1)
        self.db.kv_set("k", 2)
        self.assertEqual(self.db.kv_get("k"), "2")

    def test_transaction_rolls_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                self.db.kv_set("k", "v")
                raise RuntimeError("boom")
        self.assertIsNone(self.db.kv_get("k"))

    def test_nested_transaction_commits_once(self):
        with self.db.transaction():
            with self.db.transaction():
                self.db.kv_set("a", "1")
            self.db.kv_set("b", "2")
        self.assertEqual(self.db.kv_get("a"), "1")
        self.assertEqual(self.db.kv_get("b"), "2")

    def test_inner_failure_rolls_back_everything(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                self.db.kv_set("a", "1")
                with self.db.transaction():
                    raise RuntimeError("boom")
        self.assertIsNone(self.db.kv_get("a"))

    def test_lock_is_reentrant_and_threads_do_not_corrupt(self):
        def work(n):
            for i in range(50):
                with self.db.transaction():
                    current = int(self.db.kv_get("count", "0"))
                    self.db.kv_set("count", current + 1)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.db.kv_get("count"), "200")

    def test_integrity(self):
        self.assertTrue(self.db.integrity_ok())


class RedactionTests(unittest.TestCase):
    def test_sensitive_keys_are_blanked_at_any_depth(self):
        out = redact({"pin": "1234", "a": {"api_key": "k", "auth_token": "t"},
                      "list": [{"password": "p"}]})
        self.assertEqual(out["pin"], REDACTED)
        self.assertEqual(out["a"]["api_key"], REDACTED)
        self.assertEqual(out["a"]["auth_token"], REDACTED)
        self.assertEqual(out["list"][0]["password"], REDACTED)

    def test_ordinary_keys_containing_pin_letters_survive(self):
        out = redact({"shopping": "x", "opinion": "y", "template_hash": "h"})
        self.assertEqual(out, {"shopping": "x", "opinion": "y", "template_hash": "h"})

    def test_known_secret_values_are_scrubbed_from_text(self):
        out = redact({"note": "login with hunter2 now"}, secrets=("hunter2",))
        self.assertNotIn("hunter2", out["note"])

    def test_long_text_is_truncated(self):
        self.assertLess(len(redact("x" * 10_000)), 2100)


class AuditTests(JarvisTestCase):
    def test_chain_verifies(self):
        for i in range(5):
            self.jarvis.audit.append("t", "event", {"i": i})
        result = self.jarvis.audit.verify()
        self.assertTrue(result.ok)
        self.assertEqual(result.count, 5)

    def test_empty_log_verifies(self):
        self.assertTrue(self.jarvis.audit.verify().ok)

    def test_update_and_delete_are_blocked(self):
        self.jarvis.audit.append("t", "event", {})
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.execute("UPDATE audit_log SET actor = 'x'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.execute("DELETE FROM audit_log")

    def _force(self, sql):
        self.db.execute("DROP TRIGGER audit_no_update")
        self.db.execute("DROP TRIGGER audit_no_delete")
        self.db.execute(sql)

    def test_edited_row_is_detected(self):
        for i in range(3):
            self.jarvis.audit.append("t", "event", {"i": i})
        self._force("UPDATE audit_log SET detail = '{\"i\":99}' WHERE id = 2")
        result = self.jarvis.audit.verify()
        self.assertFalse(result.ok)
        self.assertEqual(result.bad_id, 2)

    def test_removed_middle_row_is_detected(self):
        for i in range(3):
            self.jarvis.audit.append("t", "event", {"i": i})
        self._force("DELETE FROM audit_log WHERE id = 2")
        self.assertFalse(self.jarvis.audit.verify().ok)

    def test_removed_last_row_is_detected(self):
        for i in range(3):
            self.jarvis.audit.append("t", "event", {"i": i})
        self._force("DELETE FROM audit_log WHERE id = 3")
        result = self.jarvis.audit.verify()
        self.assertFalse(result.ok)
        self.assertIn("end of log", result.reason)

    def test_secrets_never_reach_the_log(self):
        self.jarvis.audit.append("t", "event", {"pin": "4821", "token": "abc"})
        text = self.db.one("SELECT detail FROM audit_log")["detail"]
        self.assertNotIn("4821", text)
        self.assertNotIn("abc", text)


class AuditSecretValueTests(JarvisTestCase):
    env = {"SMTP_PASSWORD": "s3cr3t-value"}

    def test_configured_secret_is_scrubbed_even_under_an_innocent_key(self):
        self.jarvis.audit.append("t", "event", {"note": "used s3cr3t-value to log in"})
        self.assertNotIn("s3cr3t-value", self.db.one("SELECT detail FROM audit_log")["detail"])


if __name__ == "__main__":
    unittest.main()
