import http.client
import json
import re
import threading

from jarvis.dashboard.server import STATIC_DIR, build_state, make_dashboard_server, orb_state
from tests.helpers import APPROVAL_PIN, PHONE_ENV, VOICE_PIN, JarvisTestCase


class OrbStateTests(JarvisTestCase):
    env = PHONE_ENV

    def state(self):
        return orb_state(self.app)[0]

    def test_standby_by_default(self):
        self.assertEqual(orb_state(self.app), ("standby", "Standing by"))

    def test_listening_when_live_in_the_window_with_nothing_queued(self):
        self.go_live()
        self.assertEqual(self.state(), "listening")

    def test_thinking_while_emails_are_queued(self):
        self.go_live()
        self.add_firm(1)
        self.app.autopilot.draft_day()
        self.assertEqual(self.state(), "thinking")

    def test_standby_outside_the_window_even_when_live(self):
        self.go_live()
        self.clock.set_uk("2026-11-16 20:00:00")
        self.assertEqual(self.state(), "standby")

    def test_speaking_during_a_call(self):
        self.go_live()
        self.app.phone.alert_owner("Hello")
        self.assertEqual(self.state(), "speaking")
        self.clock.advance(minutes=6)
        self.assertEqual(self.state(), "listening")

    def test_alert_for_waiting_approvals(self):
        self.set_pins()
        self.app.sales.request_go_live()
        state, reason = orb_state(self.app)
        self.assertEqual(state, "alert")
        self.assertIn("1 approval waiting", reason)

    def test_alert_for_hard_stop_beats_everything(self):
        self.go_live()
        self.app.phone.alert_owner("Hello")
        self.jarvis.stop("test")
        self.assertEqual(orb_state(self.app), ("alert", "Hard stop is on"))

    def test_alert_for_broken_audit_chain(self):
        self.jarvis.audit.append("t", "e", {})
        self.db.execute("DROP TRIGGER audit_no_update")
        self.db.execute("UPDATE audit_log SET actor = 'x'")
        self.assertEqual(orb_state(self.app), ("alert", "Audit chain is broken"))

    def test_alert_for_locked_pin(self):
        self.set_pins()
        for _ in range(3):
            self.jarvis.pins.verify("voice", "0000")
        self.assertEqual(self.state(), "alert")


class StateTests(JarvisTestCase):
    env = {**PHONE_ENV, "SMTP_PASSWORD": "smtp-secret-value"}

    def test_state_has_every_panel(self):
        self.go_live(cap=5)
        firm_id = self.add_firm(1)
        self.app.sales.send(self.app.sales.draft(firm_id).value)
        self.app.sales.handle_reply("hello@firm1.test", "Yes, interested")
        pending = self.app.sales.request_cap(9)
        self.app.phone.alert_owner("Testing")
        state = build_state(self.app)
        self.assertEqual(state["stats"]["sent_today"], 1)
        self.assertEqual(state["stats"]["cap"], 5)
        self.assertTrue(state["stats"]["live"])
        self.assertEqual(state["approvals"][0]["id"], pending.approval_id)
        self.assertEqual(state["calls"][0]["reason"], "Testing")
        self.assertTrue(any("email.send: done" in a["text"] for a in state["activity"]))
        self.assertEqual(state["meetings"], [])
        json.dumps(state)  # must be plain JSON

    def test_state_never_contains_pins_hashes_or_keys(self):
        self.go_live()
        self.jarvis.audit.append("t", "note", {"text": "password is smtp-secret-value"})
        text = json.dumps(build_state(self.app))
        pin_row = self.db.one("SELECT * FROM pins WHERE purpose = 'approval'")
        for secret in (APPROVAL_PIN, VOICE_PIN, pin_row["hash"], pin_row["salt"],
                       "smtp-secret-value", PHONE_ENV["TWILIO_AUTH_TOKEN"],
                       PHONE_ENV["CALCOM_WEBHOOK_SECRET"]):
            self.assertNotIn(secret, text)

    def test_refusals_show_their_reason(self):
        self.jarvis.execute("payout.create", {"amount": 10})
        texts = [a["text"] for a in build_state(self.app)["activity"]]
        self.assertIn("payout.create: refused (money out is never allowed)", texts)


class ServerTests(JarvisTestCase):
    def setUp(self):
        super().setUp()
        self.server = make_dashboard_server(self.app, port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest(method, path, skip_host=True)
            conn.putheader("Host", f"127.0.0.1:{self.port}" if host is None else host)
            conn.putheader("Content-Length", "0")
            conn.endheaders()
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def test_page_and_assets_are_served(self):
        for path, kind in (("/", "text/html"), ("/static/app.js", "text/javascript"),
                           ("/static/orb.js", "text/javascript"),
                           ("/static/style.css", "text/css")):
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(kind), path)
            self.assertGreater(len(body), 100)

    def test_state_endpoint_returns_json(self):
        status, headers, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["orb"], "standby")

    def test_security_headers_on_every_response(self):
        for path in ("/", "/api/state", "/static/app.js", "/missing"):
            _, headers, _ = self.request("GET", path)
            csp = headers["Content-Security-Policy"]
            self.assertIn("default-src 'none'", csp)
            self.assertIn("script-src 'self'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertNotIn("unsafe", csp)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_host_header_must_be_this_machine(self):
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1:1", ""):
            status, _, _ = self.request("GET", "/api/state", host=host)
            self.assertEqual(status, 403, host)
        self.assertEqual(self.request("GET", "/", host=f"localhost:{self.port}")[0], 200)

    def test_it_is_read_only(self):
        before = self.db.scalar("SELECT COUNT(*) FROM audit_log")
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for path in ("/api/state", "/", "/api/approve"):
                self.assertEqual(self.request(method, path)[0], 405, (method, path))
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM audit_log"), before)

    def test_reading_the_state_changes_nothing(self):
        self.set_pins()
        self.app.sales.request_go_live()
        before = self.db.scalar("SELECT COUNT(*) FROM audit_log")
        self.request("GET", "/api/state")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM audit_log"), before)
        self.assertEqual(len(self.jarvis.approvals.pending()), 1)

    def test_no_path_escapes_the_static_folder(self):
        for path in ("/static/../server.py", "/static/..%2f..%2fsettings.py", "/.env",
                     "/static/", "/static/index.html", "//etc/passwd"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_head_has_no_body(self):
        status, headers, body = self.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_binds_to_this_computer_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_hostile_text_arrives_as_json_text_not_markup(self):
        self.add_firm(1, name="<img src=x onerror=alert(1)>")
        self.app.sales.draft(1)
        self.set_pins()
        self.jarvis.register("payment.link.create", lambda p: 1,
                             lambda p: "<script>alert(1)</script>")
        self.jarvis.execute("payment.link.create", {"amount": 1})
        status, headers, body = self.request("GET", "/api/state")
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(json.loads(body)["approvals"][0]["summary"],
                         "<script>alert(1)</script>")


class FrontEndTests(JarvisTestCase):
    def read(self, name):
        return (STATIC_DIR / name).read_text(encoding="utf-8")

    def test_scripts_write_text_only(self):
        banned = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                  "eval(", "new Function", "setAttribute(\"on", "srcdoc")
        for name in ("app.js", "orb.js"):
            source = self.read(name)
            for word in banned:
                self.assertNotIn(word, source, f"{word} in {name}")
        self.assertIn("textContent", self.read("app.js"))

    def test_page_has_no_inline_script_or_style(self):
        html = self.read("index.html")
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", html))
        self.assertNotIn("<style", html)
        self.assertIsNone(re.search(r"\sstyle=", html))
        self.assertIsNone(re.search(r"\son[a-z]+=", html))
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)

    def test_page_has_the_orb_and_four_panels(self):
        html = self.read("index.html")
        for element_id in ("orb", "activity", "meetings", "approvals", "calls", "state"):
            self.assertIn(f'id="{element_id}"', html)

    def test_orb_knows_every_state(self):
        source = self.read("orb.js")
        for state in ("standby", "listening", "thinking", "speaking", "alert"):
            self.assertRegex(source, rf"\b{state}:\s*\{{")
            self.assertIn(f'"{state}"', self.read("app.js"))

    def test_scripts_load_nothing_from_elsewhere(self):
        for name in ("app.js", "orb.js", "style.css"):
            source = self.read(name)
            self.assertNotIn("http://", source)
            self.assertNotIn("https://", source)
        self.assertEqual(re.findall(r"fetch\(([^,)]+)", self.read("app.js")), ['"/api/state"'])
