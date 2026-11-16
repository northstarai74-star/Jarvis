import sqlite3

from jarvis import cli, ops
from jarvis.app import App
from jarvis.db import Database
from jarvis.settings import Settings
from tests.helpers import APPROVAL_PIN, PHONE_ENV, FakeSender, JarvisTestCase


def levels(checks):
    return {c.name: c.level for c in checks}


class BackupTests(JarvisTestCase):
    def test_backup_of_a_file_database_holds_the_data_and_passes_integrity(self):
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        db = Database(self.settings.db_path)
        app = App(self.settings, db=db, clock=self.clock, sender=FakeSender())
        self.addCleanup(db.close)
        app.crm.add_firm("Kept Ltd", "kept@firm.test")
        result = ops.backup(app.jarvis)
        self.assertTrue(result.ok, result.reason)
        copy = sqlite3.connect(result.value)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(copy.execute("SELECT name FROM firms").fetchone()[0], "Kept Ltd")

    def test_backup_copy_keeps_a_valid_audit_chain(self):
        for i in range(4):
            self.jarvis.audit.append("t", "e", {"i": i})
        path = ops.backup(self.jarvis).value
        restored = Database(path)
        self.addCleanup(restored.close)
        from jarvis.audit import AuditLog
        # the copy is taken inside the action, just before its own audit entry
        result = AuditLog(restored, self.clock).verify()
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.count, 4)

    def test_backup_never_contains_the_env_file_or_its_keys(self):
        (self.root / ".env").write_text("SMTP_PASSWORD=super-secret-key-value\n", "utf-8")
        path = ops.backup(self.jarvis).value
        with open(path, "rb") as handle:
            self.assertNotIn(b"super-secret-key-value", handle.read())
        names = [p.name for p in self.settings.backup_dir.iterdir()]
        self.assertTrue(all(name.endswith(".db") for name in names))

    def test_two_backups_in_one_second_do_not_overwrite(self):
        first = ops.backup(self.jarvis).value
        second = ops.backup(self.jarvis).value
        self.assertNotEqual(first, second)

    def test_backup_is_recorded_and_works_during_a_hard_stop(self):
        self.jarvis.stop("test")
        self.assertTrue(ops.backup(self.jarvis).ok)
        self.assertEqual(self.db.kv_get("last_backup"), self.clock.now_iso())


class DoctorTests(JarvisTestCase):
    def test_fresh_install(self):
        found = levels(ops.doctor(self.jarvis))
        self.assertEqual(found["approval PIN"], "fail")
        self.assertEqual(found["voice PIN"], "fail")
        self.assertEqual(found["database"], "ok")
        self.assertEqual(found["audit chain"], "ok")
        self.assertEqual(found["go-live"], "warn")
        self.assertEqual(found["daily cap"], "warn")
        self.assertEqual(found["backup"], "warn")
        self.assertEqual(found[".env file"], "warn")
        for group in ("email (SMTP)", "phone (Twilio)", "booking (Cal.com)",
                      "company checks (Companies House)"):
            self.assertEqual(found[group], "blocked")

    def test_healthy_system(self):
        self.go_live()
        ops.backup(self.jarvis)
        (self.root / ".env").write_text("X=1\n", "utf-8")
        found = levels(ops.doctor(self.jarvis))
        for name in ("approval PIN", "voice PIN", "hard stop", "templates", "go-live",
                     "daily cap", "backup", ".env file", "data folder", "location"):
            self.assertEqual(found[name], "ok", name)

    def test_reports_stop_lockout_old_backup_and_broken_chain(self):
        self.go_live()
        ops.backup(self.jarvis)
        self.clock.advance(days=3)
        self.jarvis.stop("test")
        for _ in range(3):
            self.jarvis.pins.verify("voice", "0000")
        self.db.execute("DROP TRIGGER audit_no_update")
        self.db.execute("UPDATE audit_log SET actor = 'x' WHERE id = 1")
        found = levels(ops.doctor(self.jarvis))
        self.assertEqual(found["hard stop"], "warn")
        self.assertEqual(found["voice PIN"], "warn")
        self.assertEqual(found["backup"], "warn")
        self.assertEqual(found["audit chain"], "fail")

    def test_warns_when_the_project_sits_in_onedrive(self):
        root = self.root / "OneDrive" / "Desktop" / "JARVIS"
        root.mkdir(parents=True)
        settings = Settings.load(root, environ={"PIN_ITERATIONS": "1000"})
        db = Database(":memory:")
        self.addCleanup(db.close)
        app = App(settings, db=db, clock=self.clock, sender=FakeSender())
        self.assertEqual(levels(ops.doctor(app.jarvis))["location"], "warn")

    def test_doctor_never_prints_key_values(self):
        text = " ".join(f"{c.name} {c.detail}" for c in ops.doctor(self.jarvis))
        self.assertNotIn(APPROVAL_PIN, text)


class KeyedDoctorTests(JarvisTestCase):
    env = PHONE_ENV

    def test_groups_with_all_keys_are_ok_and_values_stay_hidden(self):
        checks = ops.doctor(self.jarvis)
        found = levels(checks)
        self.assertEqual(found["phone (Twilio)"], "ok")
        self.assertEqual(found["booking (Cal.com)"], "ok")
        self.assertEqual(found["email (SMTP)"], "blocked")
        text = " ".join(c.detail for c in checks)
        self.assertNotIn(PHONE_ENV["TWILIO_AUTH_TOKEN"], text)
        self.assertNotIn(PHONE_ENV["CALCOM_WEBHOOK_SECRET"], text)


class WatchdogTests(JarvisTestCase):
    env = PHONE_ENV

    def test_all_clear(self):
        self.assertEqual(self.app.watchdog(), [])
        self.assertFalse(self.jarvis.is_stopped())
        self.assertEqual(self.twilio.calls, [])

    def test_broken_audit_chain_trips_the_stop_and_phones_the_owner(self):
        self.jarvis.audit.append("t", "e", {})
        self.db.execute("DROP TRIGGER audit_no_update")
        self.db.execute("UPDATE audit_log SET actor = 'x' WHERE id = 1")
        problems = self.app.watchdog()
        self.assertIn("audit chain broken", problems[0])
        self.assertTrue(self.jarvis.is_stopped())
        self.assertEqual(len(self.twilio.calls), 1)
        self.assertIn("Watchdog", self.db.one("SELECT reason FROM calls")["reason"])

    def test_a_burst_of_failures_trips_the_stop(self):
        def boom(params):
            raise RuntimeError("x")

        for _ in range(5):
            self.jarvis.execute("status.read", {}, boom)
        problems = self.app.watchdog()
        self.assertIn("5 actions failed", problems[0])
        self.assertTrue(self.jarvis.is_stopped())

    def test_old_failures_do_not_count(self):
        def boom(params):
            raise RuntimeError("x")

        for _ in range(5):
            self.jarvis.execute("status.read", {}, boom)
        self.clock.advance(minutes=11)
        self.assertEqual(self.app.watchdog(), [])

    def test_sending_past_the_cap_trips_the_stop(self):
        self.go_live(cap=1)
        for _ in range(2):  # written behind the gate's back
            self.db.execute("INSERT INTO send_log (day, kind, ref, ts) VALUES (?, 'initial', '', ?)",
                            (self.clock.local_day(), self.clock.now_iso()))
        self.assertIn("more emails", self.app.watchdog()[0])
        self.assertTrue(self.jarvis.is_stopped())

    def test_stale_autopilot_is_reported_but_does_not_stop(self):
        self.go_live()
        self.app.autopilot.tick()
        self.clock.advance(minutes=11)
        problems = self.app.watchdog()
        self.assertEqual(problems, ["autopilot has not ticked for over 10 minutes"])
        self.assertFalse(self.jarvis.is_stopped())
        self.assertEqual(len(self.twilio.calls), 1)

    def test_no_heartbeat_complaint_when_not_live_or_out_of_hours(self):
        self.assertEqual(self.app.watchdog(), [])
        self.go_live()
        self.clock.set_uk("2026-11-16 20:00:00")
        self.assertEqual(self.app.watchdog(), [])

    def test_same_problem_is_phoned_once_a_day_not_every_minute(self):
        self.go_live()
        self.clock.advance(minutes=11)
        for _ in range(5):
            self.app.watchdog()
            self.clock.advance(minutes=1)
        self.assertEqual(len(self.twilio.calls), 1)

    def test_watchdog_command(self):
        lines = []
        code = cli.main(["watchdog"], app=self.app, out=lines.append, read_pin=lambda p: "")
        self.assertEqual((code, lines), (0, ["Watchdog: all clear."]))
        self.jarvis.audit.append("t", "e", {})
        self.db.execute("DROP TRIGGER audit_no_delete")
        self.db.execute("DELETE FROM audit_log WHERE id = 1")
        lines.clear()
        code = cli.main(["watchdog"], app=self.app, out=lines.append, read_pin=lambda p: "")
        self.assertEqual(code, 1)
        self.assertIn("The hard stop is ON.", lines)


class WatchdogWithoutPhoneTests(JarvisTestCase):
    def test_watchdog_still_stops_when_the_phone_is_not_connected(self):
        self.jarvis.audit.append("t", "e", {})
        self.db.execute("DROP TRIGGER audit_no_update")
        self.db.execute("UPDATE audit_log SET actor = 'x' WHERE id = 1")
        self.assertTrue(self.app.watchdog())
        self.assertTrue(self.jarvis.is_stopped())
        self.assertEqual(self.twilio.calls, [])
