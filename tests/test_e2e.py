import unittest
from pathlib import Path

from jarvis import cli, dryrun
from tests.helpers import JarvisTestCase


class EndToEndTests(unittest.TestCase):
    def test_the_whole_story_passes_every_check(self):
        lines = []
        report = dryrun.run(out=lines.append)
        failed = [(name, detail) for name, ok, detail in report["checks"] if not ok]
        self.assertEqual(failed, [], "\n".join(lines))
        self.assertGreaterEqual(report["total"], 30)

    def test_the_dry_run_cleans_up_after_itself(self):
        lines = []
        dryrun.run(out=lines.append, keep=True)
        kept = Path([l for l in lines if l.startswith("Kept")][0].split(": ", 1)[1])
        self.assertTrue((kept / "data" / "outbox").is_dir())
        self.assertEqual(len(list((kept / "data" / "outbox").glob("*.eml"))), dryrun.CAP)
        import shutil
        shutil.rmtree(kept, ignore_errors=True)


class DryRunCommandTests(JarvisTestCase):
    def test_command_reports_checks_and_what_is_blocked(self):
        lines = []
        code = cli.main(["dry-run"], app=self.app, out=lines.append, read_pin=lambda p: "")
        text = "\n".join(lines)
        self.assertEqual(code, 0, text)
        self.assertNotIn("FAIL", text)
        self.assertIn("checks passed", text)
        self.assertIn("Blocked until these keys are in .env:", text)
        self.assertIn("TWILIO_AUTH_TOKEN", text)

    def test_dry_run_leaves_the_real_database_alone(self):
        before = self.db.scalar("SELECT COUNT(*) FROM audit_log")
        cli.main(["dry-run"], app=self.app, out=lambda line: None, read_pin=lambda p: "")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM audit_log"), before)
        self.assertEqual(self.app.crm.firms(), [])
        self.assertEqual(self.sender.sent, [])
