from datetime import timedelta

from jarvis import cli
from jarvis.clock import in_send_window, parse_iso
from tests.helpers import JarvisTestCase


class AutopilotTestCase(JarvisTestCase):
    start = "2026-11-16 09:00:00"  # Monday, before the window opens

    def setUp(self):
        super().setUp()
        self.auto = self.app.autopilot

    def add_firms(self, count, first=1):
        return [self.add_firm(n) for n in range(first, first + count)]

    def queued(self):
        return self.db.query(
            "SELECT * FROM outreach WHERE status = 'queued' ORDER BY scheduled_at"
        )

    def run_until(self, uk_time, step_seconds=60):
        """Tick once a minute until the given UK time. Returns the send times."""
        target = uk_time
        while self.clock.now() < self._utc(target):
            self.auto.tick()
            self.clock.advance(seconds=step_seconds)
        return [parse_iso(r["ts"]) for r in self.db.query("SELECT ts FROM send_log ORDER BY id")]

    def _utc(self, uk_time):
        from datetime import datetime
        from jarvis.clock import uk_to_utc
        return uk_to_utc(datetime.strptime(uk_time, "%Y-%m-%d %H:%M:%S"))


class DraftTests(AutopilotTestCase):
    def test_before_approvals_it_writes_drafts_to_read_and_queues_nothing(self):
        self.add_firms(8)
        plan = self.auto.draft_day()
        self.assertEqual(plan["drafted"], 5)
        self.assertEqual(plan["first"], 0)
        self.assertIn("no daily cap", plan["note"])
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.sender.sent, [])

    def test_plan_fills_the_cap_and_no_more(self):
        self.go_live(cap=10)
        self.add_firms(15)
        plan = self.auto.draft_day()
        self.assertEqual(plan["first"], 10)
        self.assertEqual(len(self.queued()), 10)

    def test_plan_is_spread_across_the_window(self):
        self.go_live(cap=10)
        self.add_firms(10)
        self.auto.draft_day()
        times = [parse_iso(r["scheduled_at"]) for r in self.queued()]
        self.assertTrue(all(in_send_window(t) for t in times))
        gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        self.assertEqual(min(gaps), max(gaps))
        self.assertEqual(gaps[0], 42 * 60)  # 7 hours shared between 10 emails
        self.assertEqual(times[0], self._utc("2026-11-16 09:30:00"))

    def test_drafting_twice_does_not_double_up(self):
        self.go_live(cap=10)
        self.add_firms(6)
        self.auto.draft_day()
        plan = self.auto.draft_day()
        self.assertEqual(plan["drafted"], 0)
        self.assertEqual(len(self.queued()), 6)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM outreach"), 6)

    def test_preview_drafts_are_reused_once_the_cap_is_approved(self):
        self.add_firms(3)
        self.auto.draft_day()
        self.go_live(cap=5)
        plan = self.auto.draft_day()
        self.assertEqual(plan["drafted"], 0)
        self.assertEqual(plan["first"], 3)

    def test_late_in_the_day_only_what_fits_at_three_a_minute_is_queued(self):
        self.go_live(cap=10)
        self.add_firms(10)
        self.clock.set_uk("2026-11-16 16:29:00")
        self.auto.draft_day()
        self.assertEqual(len(self.queued()), 3)

    def test_no_plan_at_the_weekend_or_after_hours(self):
        self.go_live(cap=10)
        self.add_firms(3)
        for when in ("2026-11-16 16:30:00", "2026-11-21 10:00:00", "2026-11-22 10:00:00"):
            self.clock.set_uk(when)
            plan = self.auto.draft_day()
            self.assertIn("no sending window", plan["note"], when)
        self.assertEqual(self.queued(), [])

    def test_already_sent_today_reduces_the_plan(self):
        self.go_live(cap=4)
        self.add_firms(10)
        self.clock.set_uk("2026-11-16 10:00:00")
        for firm_id in (1, 2):
            self.app.sales.send(self.app.sales.draft(firm_id).value)
        plan = self.auto.draft_day()
        self.assertEqual(plan["first"], 2)

    def test_reworded_template_drops_old_drafts_and_writes_new_ones(self):
        self.go_live(cap=5, templates=("direct",))
        self.add_firms(2)
        self.auto.draft_day()
        self.app.templates.propose("direct", "New words", "New body. Reply STOP to opt out.")
        self.approve(self.app.templates.request_approval("direct"))
        plan = self.auto.draft_day()
        self.assertEqual(plan["drafted"], 2)
        subjects = {r["subject"] for r in self.queued()}
        self.assertEqual(subjects, {"New words"})

    def test_both_first_templates_are_used_when_both_are_approved(self):
        self.go_live(cap=10)
        self.add_firms(6)
        self.auto.draft_day()
        self.assertEqual({r["template"] for r in self.queued()}, {"direct", "soft"})

    def test_suppressed_firms_are_left_out(self):
        self.go_live(cap=10)
        self.add_firms(3)
        self.app.crm.suppress("hello@firm2.test", "asked")
        self.auto.draft_day()
        self.assertEqual(len(self.queued()), 2)

    def test_hard_stop_means_no_plan(self):
        self.go_live(cap=10)
        self.add_firms(3)
        self.jarvis.stop("test")
        self.assertIn("stop", self.auto.draft_day()["note"])
        self.assertEqual(self.queued(), [])


class SendingTests(AutopilotTestCase):
    def test_a_full_day_sends_exactly_the_cap_inside_the_window(self):
        self.go_live(cap=10)
        self.add_firms(15)
        self.auto.draft_day()
        times = self.run_until("2026-11-16 18:00:00")
        self.assertEqual(len(times), 10)
        self.assertEqual(len(self.sender.sent), 10)
        self.assertTrue(all(in_send_window(t) for t in times))
        self.assertEqual(len({m["to"] for m in self.sender.sent}), 10)

    def test_never_more_than_three_in_any_minute(self):
        self.go_live(cap=50)
        self.add_firms(50)
        self.clock.set_uk("2026-11-16 16:20:00")
        self.auto.draft_day()
        times = self.run_until("2026-11-16 16:40:00", step_seconds=5)
        self.assertEqual(len(times), 30)  # ten minutes at three a minute
        for i, t in enumerate(times):
            in_minute = [u for u in times[i:] if (u - t) < timedelta(seconds=60)]
            self.assertLessEqual(len(in_minute), 3)

    def test_nothing_is_sent_before_the_window_opens(self):
        self.go_live(cap=5)
        self.add_firms(5)
        self.auto.draft_day()
        result = self.auto.tick()
        self.assertEqual(result["sent"], 0)
        self.assertIn("outside", result["note"])

    def test_tick_without_go_live_sends_nothing(self):
        self.set_pins()
        self.add_firms(2)
        self.auto.draft_day()
        self.clock.set_uk("2026-11-16 12:00:00")
        self.assertEqual(self.auto.tick()["sent"], 0)
        self.assertEqual(self.sender.sent, [])

    def test_hard_stop_halts_sending_mid_day(self):
        self.go_live(cap=10)
        self.add_firms(10)
        self.auto.draft_day()
        self.run_until("2026-11-16 12:00:00")
        sent_before = len(self.sender.sent)
        self.assertGreater(sent_before, 0)
        self.jarvis.stop("test")
        self.run_until("2026-11-16 17:00:00")
        self.assertEqual(len(self.sender.sent), sent_before)

    def test_firm_suppressed_after_queueing_is_skipped(self):
        self.go_live(cap=5)
        self.add_firms(3)
        self.auto.draft_day()
        self.app.crm.suppress("hello@firm1.test", "asked")
        self.run_until("2026-11-16 17:00:00")
        self.assertEqual(sorted(m["to"] for m in self.sender.sent),
                         ["hello@firm2.test", "hello@firm3.test"])

    def test_yesterdays_leftovers_wait_for_a_new_plan(self):
        self.go_live(cap=5)
        self.add_firms(5)
        self.clock.set_uk("2026-11-16 16:00:00")
        self.auto.draft_day()
        self.jarvis.stop("test")
        self.run_until("2026-11-17 10:00:00", step_seconds=600)
        self.approve(self.jarvis.execute("system.unstop", {}))
        self.assertEqual(self.auto.tick()["sent"], 0)
        self.auto.draft_day()
        self.run_until("2026-11-17 17:00:00")
        self.assertEqual(len(self.sender.sent), 5)

    def test_tick_records_a_heartbeat(self):
        self.auto.tick()
        self.assertEqual(self.db.kv_get("heartbeat"), self.clock.now_iso())


class FollowupTests(AutopilotTestCase):
    def setUp(self):
        super().setUp()
        self.go_live(cap=10)
        self.add_firms(10)
        self.auto.draft_day()
        self.run_until("2026-11-16 17:00:00")
        self.assertEqual(len(self.sender.sent), 10)
        self.sender.sent.clear()

    def kinds_queued(self):
        rows = self.queued()
        return (sum(1 for r in rows if r["kind"] == "initial"),
                sum(1 for r in rows if r["kind"] == "followup"))

    def test_no_followup_before_five_days(self):
        self.clock.set_uk("2026-11-20 09:00:00")  # Friday, four days on
        self.auto.draft_day()
        self.assertEqual(self.kinds_queued(), (0, 0))

    def test_followups_take_at_most_thirty_percent_of_the_cap(self):
        self.add_firms(20, first=11)
        self.clock.set_uk("2026-11-23 09:00:00")  # the next Monday
        plan = self.auto.draft_day()
        self.assertEqual((plan["first"], plan["followup"]), (7, 3))
        self.run_until("2026-11-23 17:00:00")
        self.assertEqual(self.jarvis.sends_today(), 10)
        self.assertEqual(self.jarvis.sends_today("followup"), 3)

    def test_followup_share_holds_even_with_no_new_firms(self):
        self.clock.set_uk("2026-11-23 09:00:00")
        plan = self.auto.draft_day()
        self.assertEqual((plan["first"], plan["followup"]), (0, 3))

    def test_followups_use_the_followup_wording(self):
        self.clock.set_uk("2026-11-23 09:00:00")
        self.auto.draft_day()
        self.run_until("2026-11-23 17:00:00")
        self.assertTrue(all(m["subject"].startswith("Following up") for m in self.sender.sent))

    def test_no_followup_to_anyone_who_replied_or_opted_out(self):
        self.app.sales.handle_reply("hello@firm1.test", "Not interested")
        self.app.sales.handle_reply("hello@firm2.test", "STOP")
        self.app.sales.handle_reply("hello@firm3.test", "Yes please, call me")
        self.clock.set_uk("2026-11-23 09:00:00")
        self.auto.draft_day()
        self.run_until("2026-11-23 17:00:00")
        to = {m["to"] for m in self.sender.sent}
        self.assertEqual(len(to), 3)
        self.assertFalse(to & {"hello@firm1.test", "hello@firm2.test", "hello@firm3.test"})

    def test_an_out_of_office_reply_does_not_cancel_the_followup(self):
        self.app.sales.handle_reply("hello@firm1.test", "I am out of the office",
                                    "Automatic reply")
        self.clock.set_uk("2026-11-23 09:00:00")
        self.auto.draft_day()
        self.run_until("2026-11-23 17:00:00")
        self.assertIn("hello@firm1.test", {m["to"] for m in self.sender.sent})

    def test_a_firm_is_followed_up_once_only(self):
        for day in ("2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27",
                    "2026-11-30", "2026-12-01"):
            self.clock.set_uk(f"{day} 09:00:00")
            self.auto.draft_day()
            self.run_until(f"{day} 17:00:00", step_seconds=300)
        self.assertEqual(len(self.sender.sent), 10)
        self.assertEqual(len({m["to"] for m in self.sender.sent}), 10)

    def test_without_approved_followup_wording_none_are_queued(self):
        self.app.templates.propose("followup", "Changed", "Changed. Reply STOP to opt out.")
        self.clock.set_uk("2026-11-23 09:00:00")
        self.assertEqual(self.auto.draft_day()["followup"], 0)


class SummaryTests(AutopilotTestCase):
    def test_morning_summary_tells_the_plan_and_what_is_waiting(self):
        self.go_live(cap=5)
        self.add_firms(3)
        self.auto.draft_day()
        pending = self.app.sales.request_cap(8)
        text = self.auto.morning_summary().value
        self.assertIn("Monday 16 November 2026", text)
        self.assertIn("Sending is live; the daily cap is 5.", text)
        self.assertIn("Planned today: 3 first emails and 0 follow-ups.", text)
        self.assertIn("Waiting for your approval: 1.", text)
        self.assertIn(pending.approval_id, text)
        self.assertEqual(self.db.kv_get("summary_morning"), text)

    def test_evening_summary_reports_the_day(self):
        self.go_live(cap=5)
        self.add_firms(5)
        self.auto.draft_day()
        self.run_until("2026-11-16 16:40:00")
        self.app.sales.handle_reply("hello@firm1.test", "STOP")
        self.app.sales.handle_reply("hello@firm2.test", "Yes, interested")
        text = self.auto.evening_summary().value
        self.assertIn("Sent 5 of 5 emails (0 follow-ups).", text)
        self.assertIn("1 interested", text)
        self.assertIn("1 stop", text)
        self.assertIn("email.send x5", text)
        self.assertIn("suppression.add x1", text)
        self.assertIn("Nothing is waiting for your approval.", text)

    def test_summary_callback_receives_the_text(self):
        seen = []
        self.auto.on_summary = lambda kind, text: seen.append((kind, text))
        self.auto.morning_summary()
        self.assertEqual(seen[0][0], "morning")

    def test_step_does_morning_once_then_evening_once(self):
        self.go_live(cap=3)
        self.add_firms(3)
        self.assertEqual(self.auto.step()["did"], ["morning"])
        self.assertEqual(self.auto.step()["did"], [])
        did = []
        while self.clock.now() < self._utc("2026-11-16 17:00:00"):
            did += self.auto.step()["did"]
            self.clock.advance(minutes=1)
        self.assertEqual(did, ["evening"])
        self.assertEqual(len(self.sender.sent), 3)

    def test_step_does_nothing_at_the_weekend(self):
        self.go_live(cap=3)
        self.add_firms(3)
        self.clock.set_uk("2026-11-21 10:00:00")
        self.assertEqual(self.auto.step(), {"did": [], "sent": 0,
                                            "note": "outside the sending window"})


class AutopilotCliTests(AutopilotTestCase):
    def run_cli(self, *argv):
        lines = []
        code = cli.main(list(argv), app=self.app, out=lines.append, read_pin=lambda p: "")
        return code, "\n".join(lines)

    def test_draft_tick_and_summaries(self):
        self.go_live(cap=2)
        self.add_firms(2)
        code, text = self.run_cli("autopilot", "draft")
        self.assertIn("Queued 2 first emails", text)
        self.clock.set_uk("2026-11-16 09:30:00")
        code, text = self.run_cli("autopilot", "tick")
        self.assertIn("Sent 1.", text)
        code, text = self.run_cli("autopilot", "morning")
        self.assertIn("Good morning", text)
        code, text = self.run_cli("autopilot", "evening")
        self.assertIn("End of day", text)
        code, text = self.run_cli("autopilot", "step")
        self.assertEqual(code, 0)
