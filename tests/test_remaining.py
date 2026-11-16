"""Regression coverage for gaps found during the completion review."""

import os
import threading
from unittest.mock import patch

from jarvis.settings import Settings
from jarvis.app import App
from jarvis.db import Database
from tests.helpers import JarvisTestCase
from tests.helpers import PHONE_ENV


class CoreCompletionTests(JarvisTestCase):
    def test_daily_cap_is_shared_by_separate_database_connections(self):
        from tests.helpers import FakeSender
        path = self.root / 'shared.db'
        first = App(self.settings, db=Database(path), clock=self.clock, sender=FakeSender())
        second = App(self.settings, db=Database(path), clock=self.clock, sender=FakeSender())
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        j = first.jarvis
        j.db.kv_set('live', '1')
        j.db.kv_set('daily_cap', '1')
        j.db.execute("UPDATE templates SET approved = 1 WHERE name = 'direct'")
        digest = first.templates.get('direct')['body_hash']
        barrier = threading.Barrier(2)
        results = []
        def send(app):
            barrier.wait()
            results.append(app.jarvis.execute('email.send', {
                'to': 'hello@example.test', 'template': 'direct',
                'template_hash': digest, 'kind': 'initial',
            }, lambda p: 'fake delivery'))
        workers = [threading.Thread(target=send, args=(app,)) for app in (first, second)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sorted(r.status for r in results), ['done', 'refused'])
        self.assertEqual(j.sends_today(), 1)

    def test_process_secret_cannot_override_dotenv(self):
        (self.root / '.env').write_text('SMTP_PASSWORD=local-fake\n', encoding='utf-8')
        with patch.dict(os.environ, {'SMTP_PASSWORD': 'process-fake'}):
            settings = Settings.load(self.root)
        self.assertEqual(settings.get('SMTP_PASSWORD'), 'local-fake')

    def test_process_secret_is_not_loaded_without_dotenv(self):
        with patch.dict(os.environ, {'SMTP_PASSWORD': 'process-fake'}):
            settings = Settings.load(self.root)
        self.assertEqual(settings.get('SMTP_PASSWORD'), '')


class SalesCompletionTests(JarvisTestCase):
    def test_edited_outreach_body_cannot_use_old_template_approval(self):
        self.go_live()
        draft = self.app.sales.draft(self.add_firm()).value
        self.db.execute('UPDATE outreach SET body = ? WHERE id = ?', ('Unapproved words', draft))
        self.assertEqual(self.app.sales.send(draft).status, 'error')
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.jarvis.sends_today(), 0)

    def test_smtp_requires_an_explicit_fake(self):
        from jarvis.sales.sender import SmtpSender
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            SmtpSender('example.test', 587, 'fake', 'fake', 'Jarvis', 'a@example.test', self.clock)


class LeadsCompletionTests(JarvisTestCase):
    def test_promotion_rechecks_company_status(self):
        self.register.add('01234567', 'Example Ltd')
        self.app.leads.import_rows([{
            'name': 'Example Ltd', 'company_number': '01234567',
            'email': 'hello@example.test', 'email_source': 'https://example.test/contact',
        }])
        lead = self.app.leads.list()[0]['id']
        self.assertEqual(self.app.leads.verify(lead).value['status'], 'ready')
        self.register.add('01234567', 'Example Ltd', status='dissolved')
        self.assertEqual(self.app.leads.promote(lead).status, 'error')
        self.assertEqual(self.app.leads.get(lead)['status'], 'rejected')
        self.assertEqual(self.app.crm.firms(), [])


class AutopilotCompletionTests(JarvisTestCase):
    def test_manual_send_cannot_bypass_followup_wait(self):
        self.go_live(cap=10)
        firm = self.add_firm()
        self.assertTrue(self.app.sales.send(self.app.sales.draft(firm).value).ok)
        draft = self.app.sales.draft(firm, 'followup', 'followup').value
        self.clock.advance(minutes=2)
        result = self.app.sales.send(draft)
        self.assertEqual(result.status, 'error')
        self.assertIn('five days', result.reason)
        self.assertEqual(len(self.sender.sent), 1)
        self.clock.advance(days=7)
        self.assertTrue(self.app.sales.send(draft).ok)


class PhoneCompletionTests(JarvisTestCase):
    env = PHONE_ENV

    def test_malformed_keypad_attempts_count_toward_lockout(self):
        self.set_pins()
        sid = self.app.phone.alert_owner('Test').value['sid']
        for _ in range(3):
            self.app.phone.pin({'CallSid': sid, 'Digits': 'bad'})
        self.assertTrue(self.jarvis.pins.is_locked('voice'))
        self.assertFalse(self.jarvis.pins.is_locked('approval'))

    def test_signed_malformed_booking_shapes_are_rejected(self):
        import json
        from jarvis.phone.calcom import sign
        for event, payload in (([], {}), ('BOOKING_CREATED', []),
                               ('BOOKING_CREATED', {'attendees': 'not-a-list'})):
            raw = json.dumps({'triggerEvent': event, 'payload': payload}).encode()
            signature = sign(PHONE_ENV['CALCOM_WEBHOOK_SECRET'], raw)
            self.assertEqual(self.app.calcom.handle(raw, signature)[0], 400)
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM bookings'), 0)
        self.assertEqual(self.twilio.calls, [])


class DashboardCompletionTests(JarvisTestCase):
    def test_reading_expired_approvals_does_not_mutate_database(self):
        from jarvis.dashboard.server import build_state
        request = self.app.sales.request_go_live()
        self.clock.advance(days=1)
        before = self.db._conn.total_changes
        state = build_state(self.app)
        self.assertEqual(state['approvals'], [])
        self.assertEqual(self.db._conn.total_changes, before)
        self.assertEqual(self.db.scalar('SELECT status FROM approvals WHERE id = ?',
                                       (request.approval_id,)), 'pending')

    def test_dashboard_cannot_bind_to_all_interfaces(self):
        from jarvis.dashboard.server import make_dashboard_server
        with self.assertRaisesRegex(ValueError, 'localhost'):
            make_dashboard_server(self.app, host='0.0.0.0')


class OpsCompletionTests(JarvisTestCase):
    def test_doctor_explains_offline_adapter_boundary(self):
        from jarvis.ops import doctor
        checks = {check.name: check for check in doctor(self.jarvis)}
        self.assertIn('keys do not enable live adapters', checks['service mode'].detail)
