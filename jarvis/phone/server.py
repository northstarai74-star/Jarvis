"""The webhook server for Twilio and Cal.com.

It listens on this computer only. To receive real webhooks it must be put
behind a public HTTPS address (PUBLIC_BASE_URL), which is a setup step for
the owner. Every request is checked for a valid signature before anything
else happens.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .calcom import SIGNATURE_HEADER
from .twilio import valid_signature

MAX_BODY = 64 * 1024
TWILIO_ROUTES = ("voice", "menu", "pin", "decision", "status")


def make_webhook_server(app, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    settings = app.settings

    class Handler(BaseHTTPRequestHandler):
        server_version = "Jarvis"
        sys_version = ""

        def log_message(self, *args):  # request bodies can hold PIN digits: log nothing
            pass

        def _reply(self, code: int, body: str, content_type: str = "text/plain") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._reply(405, "POST only")

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                # Throw away a bounded amount so the sender can read our answer.
                left = min(max(length, 0), 1024 * 1024)
                while left > 0:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
                self.close_connection = True
                return self._reply(413, "body too large")
            raw = self.rfile.read(length)
            path = self.path.split("?", 1)[0]

            if path == "/calcom":
                code, message = app.calcom.handle(raw, self.headers.get(SIGNATURE_HEADER, ""))
                return self._reply(code, message)

            if path.startswith("/twilio/") and path[len("/twilio/"):] in TWILIO_ROUTES:
                route = path[len("/twilio/"):]
                try:
                    form = {k: v[0] for k, v in
                            parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}
                except UnicodeDecodeError:
                    return self._reply(400, "unreadable body")
                url = settings.get("PUBLIC_BASE_URL").rstrip("/") + path
                if not valid_signature(settings.get("TWILIO_AUTH_TOKEN"), url, form,
                                       self.headers.get("X-Twilio-Signature", "")):
                    app.jarvis.audit.append("twilio", "webhook.bad_signature", {"route": route})
                    return self._reply(403, "bad signature")
                twiml = getattr(app.phone, route)(form)
                if not twiml:
                    return self._reply(204, "")
                return self._reply(200, twiml, "text/xml")

            return self._reply(404, "not found")

    return ThreadingHTTPServer((host, port), Handler)
