import unittest

from jarvis import cli, ops
from jarvis.approvals import ApprovalError, params_hash
from jarvis.pins import PinError, PinLocked
from jarvis.policy import Facts, PolicyEngine, Tier, followup_limit, is_money_out
from tests.helpers import APPROVAL_PIN, VOICE_PIN, JarvisTestCase


def good_send(**over):
    params = {"to": "a@firm.test", "template": "direct", "template_hash": "h", "kind": "initial"}
    params.update(over)
    return params


def good_facts(**over):
    facts = Facts(live=True, cap=10, in_window=True, approved_templates={"direct": "h"})
    for key, value in over.items():
        setattr(facts, key, value)
    return facts


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.engine = PolicyEngine()

    def decide(self, action, params=None, facts=None):
        return self.engine.decide(action, params or {}, facts or Facts())

    def test_tiers(self):
        self.assertEqual(self.decide("status.read").tier, Tier.T0)
        self.assertEqual(self.decide("lead.promote").tier, Tier.T1)
        self.assertEqual(self.decide("sales.go_live").tier, Tier.T2)
        self.assertTrue(self.decide("sales.go_live").needs_approval)
        self.assertFalse(self.decide("lead.promote").needs_approval)

    def test_unknown_action_is_refused(self):
        d = self.decide("made.up")
        self.assertFalse(d.allowed)
        self.assertEqual(d.tier, Tier.T3)

    def test_empty_or_non_text_action_is_refused(self):
        self.assertFalse(self.decide("").allowed)
        self.assertFalse(self.decide(None).allowed)

    def test_listed_t3_actions_are_refused(self):
        for action in ("pin.reveal", "env.read", "audit.erase", "shell.exec"):
            self.assertFalse(self.decide(action).allowed, action)

    def test_money_out_is_always_t3(self):
        for action in ("payment.send", "payout.create", "refund.issue", "bank.transfer",
                       "invoice.pay", "money.out", "supplier.pay", "wire.send"):
            d = self.decide(action)
            self.assertEqual(d.tier, Tier.T3, action)
            self.assertFalse(d.allowed, action)

    def test_money_in_is_allowed_with_approval(self):
        d = self.decide("payment.link.create", {"amount": 100})
        self.assertTrue(d.allowed)
        self.assertTrue(d.needs_approval)

    def test_money_in_action_with_outward_direction_is_refused(self):
        self.assertFalse(self.decide("payment.link.create", {"direction": "out"}).allowed)

    def test_outward_direction_on_any_action_is_refused(self):
        self.assertTrue(is_money_out("status.read", {"direction": "outgoing"}))

    def test_ordinary_actions_are_not_money(self):
        for action in ("email.send", "booking.record", "backup.create", "sales.pause"):
            self.assertFalse(is_money_out(action, {}), action)

    def test_stop_blocks_effects_but_not_reads_or_safety_actions(self):
        stopped = Facts(stopped=True)
        self.assertFalse(self.decide("lead.promote", facts=stopped).allowed)
        self.assertFalse(self.decide("sales.go_live", facts=stopped).allowed)
        self.assertTrue(self.decide("status.read", facts=stopped).allowed)
        self.assertTrue(self.decide("system.unstop", facts=stopped).allowed)
        self.assertTrue(self.decide("call.owner_alert", facts=stopped).allowed)
        self.assertTrue(self.decide("suppression.add", facts=stopped).allowed)

    def test_send_allowed_when_everything_is_in_order(self):
        d = self.decide("email.send", good_send(), good_facts())
        self.assertTrue(d.allowed)
        self.assertTrue(d.counts_as_send)
        self.assertFalse(d.needs_approval)

    def test_send_refusals(self):
        cases = [
            (good_send(), good_facts(live=False), "not live"),
            (good_send(), good_facts(cap=0), "cap"),
            (good_send(), good_facts(in_window=False), "window"),
            (good_send(), good_facts(sends_today=10), "daily cap"),
            (good_send(), good_facts(sends_last_minute=3), "rate"),
            (good_send(template_hash="other"), good_facts(), "wording"),
            (good_send(template="soft"), good_facts(), "wording"),
            (good_send(to=""), good_facts(), "recipient"),
            (good_send(), good_facts(is_suppressed=lambda e: True), "suppressed"),
            (good_send(), good_facts(stopped=True), "stop"),
        ]
        for params, facts, word in cases:
            d = self.decide("email.send", params, facts)
            self.assertFalse(d.allowed, word)
            self.assertIn(word, d.reason)

    def test_followups_are_limited_to_thirty_percent_of_the_cap(self):
        self.assertEqual(followup_limit(10), 3)
        self.assertEqual(followup_limit(3), 0)
        ok = self.decide("email.send", good_send(kind="followup"), good_facts(followups_today=2))
        full = self.decide("email.send", good_send(kind="followup"), good_facts(followups_today=3))
        self.assertTrue(ok.allowed)
        self.assertFalse(full.allowed)
        # the limit does not hold back first emails
        self.assertTrue(self.decide("email.send", good_send(), good_facts(followups_today=3)).allowed)

    def test_cap_must_be_a_sane_whole_number(self):
        for bad in (0, -1, 51, "10", 2.5, True, None):
            self.assertFalse(self.decide("cap.set", {"cap": bad}).allowed, repr(bad))
        self.assertTrue(self.decide("cap.set", {"cap": 50}).allowed)

    def test_owner_calls_have_a_daily_limit(self):
        self.assertTrue(self.decide("call.owner_alert", facts=Facts(calls_today=5)).allowed)
        self.assertFalse(self.decide("call.owner_alert", facts=Facts(calls_today=6)).allowed)


class PinTests(JarvisTestCase):
    def test_right_and_wrong_pin(self):
        self.set_pins()
        self.assertTrue(self.jarvis.pins.verify("approval", APPROVAL_PIN))
        self.assertFalse(self.jarvis.pins.verify("approval", "0000"))

    def test_pin_is_stored_hashed_with_pbkdf2_and_a_salt(self):
        self.set_pins()
        row = self.db.one("SELECT * FROM pins WHERE purpose = 'approval'")
        self.assertNotIn(APPROVAL_PIN, row["hash"])
        self.assertEqual(len(row["hash"]), 64)
        self.assertEqual(len(row["salt"]), 32)
        self.assertEqual(row["iterations"], 1000)

    def test_pins_are_separate(self):
        self.set_pins()
        self.assertFalse(self.jarvis.pins.verify("approval", VOICE_PIN))
        self.assertFalse(self.jarvis.pins.verify("voice", APPROVAL_PIN))

    def test_the_two_pins_cannot_be_the_same(self):
        self.jarvis.pins.set_pin("approval", APPROVAL_PIN)
        with self.assertRaises(PinError):
            self.jarvis.pins.set_pin("voice", APPROVAL_PIN)

    def test_bad_pins_are_rejected(self):
        for bad in ("123", "123456789", "12ab", "", None):
            with self.assertRaises(PinError):
                self.jarvis.pins.set_pin("approval", bad)

    def test_unknown_purpose_and_unset_pin(self):
        with self.assertRaises(PinError):
            self.jarvis.pins.set_pin("other", "1234")
        with self.assertRaises(PinError):
            self.jarvis.pins.verify("approval", "1234")

    def test_three_strikes_locks_even_the_right_pin(self):
        self.set_pins()
        for _ in range(3):
            self.assertFalse(self.jarvis.pins.verify("approval", "0000"))
        self.assertTrue(self.jarvis.pins.is_locked("approval"))
        with self.assertRaises(PinLocked):
            self.jarvis.pins.verify("approval", APPROVAL_PIN)

    def test_lockout_ends_after_fifteen_minutes(self):
        self.set_pins()
        for _ in range(3):
            self.jarvis.pins.verify("approval", "0000")
        self.clock.advance(minutes=14)
        with self.assertRaises(PinLocked):
            self.jarvis.pins.verify("approval", APPROVAL_PIN)
        self.clock.advance(minutes=2)
        self.assertTrue(self.jarvis.pins.verify("approval", APPROVAL_PIN))

    def test_success_resets_the_strike_count(self):
        self.set_pins()
        self.jarvis.pins.verify("approval", "0000")
        self.jarvis.pins.verify("approval", "0000")
        self.assertTrue(self.jarvis.pins.verify("approval", APPROVAL_PIN))
        self.jarvis.pins.verify("approval", "0000")
        self.jarvis.pins.verify("approval", "0000")
        self.assertFalse(self.jarvis.pins.is_locked("approval"))

    def test_locking_one_pin_leaves_the_other_working(self):
        self.set_pins()
        for _ in range(3):
            self.jarvis.pins.verify("voice", "0000")
        self.assertTrue(self.jarvis.pins.verify("approval", APPROVAL_PIN))

    def test_pin_attempts_are_audited_without_the_pin(self):
        self.set_pins()
        self.jarvis.pins.verify("approval", "9999")
        text = " ".join(r["detail"] for r in self.events())
        self.assertNotIn("9999", text)
        self.assertNotIn(APPROVAL_PIN, text)
        self.assertEqual(len(self.events("pin.wrong")), 1)


class GateTests(JarvisTestCase):
    def setUp(self):
        super().setUp()
        self.set_pins()
        self.ran = []
        self.jarvis.register("sales.go_live", lambda p: self.ran.append(p) or "live")

    def test_t0_runs_and_is_audited(self):
        r = self.jarvis.execute("status.read", {}, lambda p: "fine")
        self.assertTrue(r.ok)
        self.assertEqual(r.value, "fine")
        self.assertEqual(len(self.events("action.done")), 1)

    def test_t3_never_runs(self):
        ran = []
        r = self.jarvis.execute("payout.create", {"amount": 5}, lambda p: ran.append(1))
        self.assertEqual(r.status, "refused")
        self.assertEqual(ran, [])
        self.assertEqual(len(self.events("action.refused")), 1)

    def test_unknown_action_never_runs(self):
        ran = []
        r = self.jarvis.execute("anything.else", {}, lambda p: ran.append(1))
        self.assertEqual(r.status, "refused")
        self.assertEqual(ran, [])

    def test_t2_waits_for_approval_and_does_not_run(self):
        r = self.jarvis.execute("sales.go_live", {"live": True})
        self.assertEqual(r.status, "pending")
        self.assertEqual(self.ran, [])
        self.assertEqual(len(self.jarvis.approvals.pending()), 1)

    def test_approved_action_runs_itself_once(self):
        r = self.jarvis.execute("sales.go_live", {"live": True})
        done = self.approve(r)
        self.assertTrue(done.ok)
        self.assertEqual(self.ran, [{"live": True}])
        # the approval is spent: asking again needs a new one
        again = self.jarvis.execute("sales.go_live", {"live": True})
        self.assertEqual(again.status, "pending")
        self.assertNotEqual(again.approval_id, r.approval_id)
        self.assertEqual(len(self.ran), 1)

    def test_approval_is_bound_to_the_exact_params(self):
        self.jarvis.register("cap.set", lambda p: self.ran.append(p))
        r = self.jarvis.execute("cap.set", {"cap": 5})
        self.jarvis.approvals.approve(r.approval_id, APPROVAL_PIN)
        other = self.jarvis.execute("cap.set", {"cap": 50})
        self.assertEqual(other.status, "pending")
        self.assertEqual(self.ran, [])
        # the original still works for the original params
        self.assertTrue(self.jarvis.execute("cap.set", {"cap": 5}).ok)
        self.assertEqual(self.ran, [{"cap": 5}])

    def test_param_order_does_not_change_the_hash_but_values_do(self):
        self.assertEqual(params_hash("a", {"x": 1, "y": 2}), params_hash("a", {"y": 2, "x": 1}))
        self.assertNotEqual(params_hash("a", {"x": 1}), params_hash("a", {"x": 2}))
        self.assertNotEqual(params_hash("a", {"x": 1}), params_hash("b", {"x": 1}))

    def test_same_request_twice_gives_one_pending_approval(self):
        a = self.jarvis.execute("sales.go_live", {"live": True})
        b = self.jarvis.execute("sales.go_live", {"live": True})
        self.assertEqual(a.approval_id, b.approval_id)

    def test_wrong_pin_does_not_approve(self):
        r = self.jarvis.execute("sales.go_live", {"live": True})
        with self.assertRaises(ApprovalError):
            self.jarvis.approve(r.approval_id, "0000")
        self.assertEqual(self.ran, [])
        self.assertEqual(self.jarvis.approvals.get(r.approval_id)["status"], "pending")

    def test_voice_pin_does_not_work_at_the_keyboard(self):
        r = self.jarvis.execute("sales.go_live", {"live": True})
        with self.assertRaises(ApprovalError):
            self.jarvis.approve(r.approval_id, VOICE_PIN)
        self.assertEqual(self.ran, [])

    def test_locked_pin_blocks_approval(self):
        r = self.jarvis.execute("sales.go_live", {"live": True})
        for _ in range(3):
            with self.assertRaises(ApprovalError):
                self.jarvis.approve(r.approval_id, "0000")
        with self.assertRaises(PinLocked):
            self.jarvis.approve(r.approval_id, APPROVAL_PIN)
        self.assertEqual(self.ran, [])

    def test_pending_approval_expires(self):
        r = self.jarvis.execute("sales.go_live", {"live": True})
        self.clock.advance(minutes=721)
        with self.assertRaises(ApprovalError):
            self.jarvis.approve(r.approval_id, APPROVAL_PIN)
        self.assertEqual(self.jarvis.approvals.get(r.approval_id)["status"], "expired")

    def test_approved_but_unused_approval_expires(self):
        r = self.jarvis.execute("sales.go_live", {"live": True})
        self.jarvis.approvals.approve(r.approval_id, APPROVAL_PIN)
        self.clock.advance(minutes=721)
        self.assertEqual(self.jarvis.execute("sales.go_live", {"live": True}).status, "pending")
        self.assertEqual(self.ran, [])

    def test_rejected_approval_cannot_be_approved_or_run(self):
        r = self.jarvis.execute("sales.go_live", {"live": True})
        self.jarvis.approvals.reject(r.approval_id)
        with self.assertRaises(ApprovalError):
            self.jarvis.approve(r.approval_id, APPROVAL_PIN)
        with self.assertRaises(ApprovalError):
            self.jarvis.resume(r.approval_id)
        self.assertEqual(self.ran, [])

    def test_resume_refuses_tampered_params(self):
        r = self.jarvis.execute("sales.go_live", {"live": True})
        self.jarvis.approvals.approve(r.approval_id, APPROVAL_PIN)
        self.db.execute("UPDATE approvals SET params_json = '{\"live\":false}'")
        with self.assertRaises(ApprovalError):
            self.jarvis.resume(r.approval_id)
        self.assertEqual(self.ran, [])

    def test_resume_needs_a_recipe(self):
        r = self.jarvis.execute("payment.link.create", {"amount": 100})
        self.jarvis.approvals.approve(r.approval_id, APPROVAL_PIN)
        with self.assertRaises(ApprovalError):
            self.jarvis.resume(r.approval_id)

    def test_unknown_approval_id(self):
        with self.assertRaises(ApprovalError):
            self.jarvis.approve("nope", APPROVAL_PIN)

    def test_handler_error_is_caught_and_audited(self):
        def boom(params):
            raise ValueError("broken")

        r = self.jarvis.execute("status.read", {}, boom)
        self.assertEqual(r.status, "error")
        self.assertIn("broken", r.reason)
        self.assertEqual(len(self.events("action.error")), 1)

    def test_missing_handler_is_an_error(self):
        self.assertEqual(self.jarvis.execute("crm.read", {}).status, "error")

    def test_bad_params_are_refused(self):
        self.assertEqual(self.jarvis.execute("status.read", ["x"], lambda p: 1).status, "refused")
        self.assertEqual(
            self.jarvis.execute("status.read", {"x": object()}, lambda p: 1).status, "refused"
        )

    def test_hard_stop_blocks_and_unstop_needs_a_pin(self):
        self.assertTrue(self.jarvis.stop("test").ok)
        self.assertTrue(self.jarvis.is_stopped())
        self.assertEqual(self.jarvis.execute("lead.promote", {}, lambda p: 1).status, "refused")
        self.assertEqual(self.jarvis.execute("sales.go_live", {"live": True}).status, "refused")
        r = self.jarvis.execute("system.unstop", {})
        self.assertEqual(r.status, "pending")
        self.assertTrue(self.jarvis.is_stopped())
        self.assertTrue(self.approve(r).ok)
        self.assertFalse(self.jarvis.is_stopped())

    def test_stop_file_also_stops(self):
        self.settings.stop_file.write_text("stop", "utf-8")
        self.assertTrue(self.jarvis.is_stopped())
        self.approve(self.jarvis.execute("system.unstop", {}))
        self.assertFalse(self.settings.stop_file.exists())
        self.assertFalse(self.jarvis.is_stopped())

    def test_brief_lists_t1_actions_only(self):
        start = self.clock.now_iso()
        self.jarvis.execute("status.read", {}, lambda p: 1)
        self.jarvis.execute("lead.promote", {"lead_id": 1}, lambda p: 1)
        items = self.jarvis.brief_items(start)
        self.assertEqual([i["detail"]["action"] for i in items], ["lead.promote"])

    def test_pin_never_appears_in_the_audit_log(self):
        r = self.jarvis.execute("sales.go_live", {"live": True})
        self.approve(r)
        text = " ".join(row["detail"] for row in self.events())
        self.assertNotIn(APPROVAL_PIN, text)

    def test_audit_chain_stays_intact_through_all_of_this(self):
        self.approve(self.jarvis.execute("sales.go_live", {"live": True}))
        self.jarvis.execute("payout.create", {})
        self.assertTrue(self.jarvis.audit.verify().ok)


class CliTests(JarvisTestCase):
    def run_cli(self, *argv, pins=()):
        lines = []
        queue = list(pins)
        code = cli.main(list(argv), app=self.app, out=lines.append,
                        read_pin=lambda prompt: queue.pop(0))
        return code, "\n".join(lines)

    def test_status(self):
        code, text = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("Hard stop:   off", text)
        self.assertIn("Audit chain: intact", text)

    def test_pin_set_then_change_needs_the_old_pin(self):
        code, text = self.run_cli("pin", "set", "approval", pins=["4821", "4821"])
        self.assertEqual(code, 0)
        code, text = self.run_cli("pin", "set", "approval", pins=["0000"])
        self.assertEqual(code, 1)
        code, text = self.run_cli("pin", "set", "approval", pins=["4821", "9911", "9911"])
        self.assertEqual(code, 0)
        self.assertTrue(self.jarvis.pins.verify("approval", "9911"))

    def test_pin_set_mismatch(self):
        code, text = self.run_cli("pin", "set", "voice", pins=["1111", "2222"])
        self.assertEqual(code, 1)
        self.assertFalse(self.jarvis.pins.is_set("voice"))

    def test_pending_and_approve_flow(self):
        self.set_pins()
        self.jarvis.stop("test")
        code, text = self.run_cli("unstop")
        self.assertIn("jarvis approve", text)
        approval_id = text.split()[-1]
        code, text = self.run_cli("pending")
        self.assertIn(approval_id, text)
        code, text = self.run_cli("approve", approval_id, pins=["0000"])
        self.assertEqual(code, 1)
        self.assertTrue(self.jarvis.is_stopped())
        code, text = self.run_cli("approve", approval_id, pins=[APPROVAL_PIN])
        self.assertEqual(code, 0)
        self.assertFalse(self.jarvis.is_stopped())

    def test_reject(self):
        self.set_pins()
        self.jarvis.stop("test")
        r = self.jarvis.execute("system.unstop", {})
        code, text = self.run_cli("reject", r.approval_id)
        self.assertEqual(code, 0)
        code, text = self.run_cli("pending")
        self.assertIn("Nothing", text)

    def test_stop_command(self):
        code, text = self.run_cli("stop", "testing")
        self.assertEqual(code, 0)
        self.assertTrue(self.jarvis.is_stopped())

    def test_backup_writes_a_readable_copy_and_no_env(self):
        (self.root / ".env").write_text("SMTP_PASSWORD=x\n", "utf-8")
        self.db.kv_set("marker", "kept")
        code, text = self.run_cli("backup")
        self.assertEqual(code, 0)
        files = list(self.settings.backup_dir.iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].suffix, ".db")
        import sqlite3
        copy = sqlite3.connect(str(files[0]))
        self.addCleanup(copy.close)
        self.assertEqual(
            copy.execute("SELECT value FROM kv WHERE key = 'marker'").fetchone()[0], "kept"
        )

    def test_backups_are_pruned(self):
        for _ in range(ops.KEEP_BACKUPS + 3):
            self.clock.advance(minutes=1)
            ops.backup(self.jarvis)
        self.assertEqual(len(list(self.settings.backup_dir.iterdir())), ops.KEEP_BACKUPS)

    def test_audit_verify_command(self):
        code, text = self.run_cli("audit-verify")
        self.assertEqual(code, 0)
        self.assertIn("intact", text)

    def test_doctor_fails_without_pins_and_lists_blocked_keys(self):
        code, text = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("FAIL     approval PIN", text)
        self.assertIn("BLOCKED  phone (Twilio)", text)
        self.assertIn("TWILIO_AUTH_TOKEN", text)

    def test_doctor_passes_with_pins_set(self):
        self.set_pins()
        code, text = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("OK       audit chain", text)


if __name__ == "__main__":
    unittest.main()
