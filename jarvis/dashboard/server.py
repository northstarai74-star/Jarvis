"""The dashboard server.

Read-only by construction: it answers GET and HEAD and nothing else, and no
route changes anything. It listens on 127.0.0.1, refuses requests whose Host
header is not this machine (which blocks DNS-rebinding tricks), and sends a
strict Content-Security-Policy. The page itself only ever writes text with
textContent, so nothing a lead typed can run as code.
"""

from __future__ import annotations

import json
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..clock import in_send_window, iso

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html"),
    "/static/style.css": ("style.css", "text/css"),
    "/static/orb.js": ("orb.js", "text/javascript"),
    "/static/app.js": ("app.js", "text/javascript"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
ACTIVE_CALL = ("initiated", "ringing", "in-progress")


def _activity_text(entry) -> str:
    """One readable line for an audit entry."""
    event, detail = entry["event"], entry["detail"]
    if event.startswith("action."):
        outcome = event.split(".", 1)[1]
        text = f"{detail.get('action', '?')}: {outcome}"
        if detail.get("reason"):
            text += f" ({detail['reason']})"
        return text
    extras = ", ".join(f"{k} {v}" for k, v in detail.items() if isinstance(v, (str, int)))
    return f"{event}" + (f" ({extras})" if extras else "")


def orb_state(app) -> tuple:
    """(state, reason). States: standby, listening, thinking, speaking, alert."""
    j = app.jarvis
    now = j.clock.now()
    if j.is_stopped():
        return "alert", "Hard stop is on"
    if not j.audit.verify().ok:
        return "alert", "Audit chain is broken"
    recent = iso(now - timedelta(minutes=5))
    on_call = j.db.one(
        "SELECT 1 FROM calls WHERE status IN (?, ?, ?) AND updated_at >= ?",
        (*ACTIVE_CALL, recent),
    )
    if on_call:
        return "speaking", "On a call with you"
    if j.pins.is_locked("approval") or j.pins.is_locked("voice"):
        return "alert", "A PIN is locked after wrong attempts"
    waiting = len(j.approvals.pending_readonly())
    if waiting:
        return "alert", f"{waiting} approval{'s' if waiting != 1 else ''} waiting for you"
    if j.is_live() and in_send_window(now):
        queued = j.db.scalar("SELECT COUNT(*) FROM outreach WHERE status = 'queued'")
        if queued:
            return "thinking", f"Working through {queued} queued email{'s' if queued != 1 else ''}"
        return "listening", "Live and waiting for replies"
    return "standby", "Standing by"


def build_state(app) -> dict:
    """Everything the page shows. Only what is safe to show: no PINs, no keys."""
    j = app.jarvis
    db = j.db
    state, reason = orb_state(app)
    return {
        "now": j.clock.now_iso(),
        "orb": state,
        "orb_reason": reason,
        "stats": {
            "live": j.is_live(),
            "stopped": j.is_stopped(),
            "cap": j.cap(),
            "sent_today": j.sends_today(),
            "pending": len(j.approvals.pending_readonly()),
            "firms": db.scalar("SELECT COUNT(*) FROM firms"),
            "leads_ready": db.scalar("SELECT COUNT(*) FROM leads WHERE status = 'ready'"),
            "replies": db.scalar("SELECT COUNT(*) FROM replies"),
            "sender": getattr(app.sender, "name", "unknown"),
        },
        "activity": [
            {"ts": e["ts"], "actor": e["actor"], "text": _activity_text(e)}
            for e in j.audit.recent(40)
        ],
        "meetings": [
            {"start": r["start_time"], "name": r["attendee_name"], "title": r["title"],
             "status": r["status"], "firm": r["firm_name"] or ""}
            for r in db.query(
                "SELECT b.*, f.name AS firm_name FROM bookings b "
                "LEFT JOIN firms f ON f.id = b.firm_id ORDER BY b.start_time DESC LIMIT 20")
        ],
        "approvals": [
            {"id": r["id"], "summary": r["summary"], "created": r["created_at"],
             "expires": r["expires_at"]}
            for r in j.approvals.pending_readonly()
        ],
        "calls": [
            {"when": r["created_at"], "status": r["status"], "reason": r["reason"]}
            for r in db.query("SELECT * FROM calls ORDER BY id DESC LIMIT 20")
        ],
    }


def make_dashboard_server(app, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("dashboard must bind to localhost")

    class Handler(BaseHTTPRequestHandler):
        server_version = "Jarvis"
        sys_version = ""

        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _host_ok(self) -> bool:
            port_now = self.server.server_address[1]
            allowed = {f"127.0.0.1:{port_now}", f"localhost:{port_now}"}
            return self.headers.get("Host", "") in allowed

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                body = json.dumps(build_state(app)).encode("utf-8")
                return self._send(200, body, "application/json")
            if path in STATIC_FILES:
                name, content_type = STATIC_FILES[path]
                return self._send(200, (STATIC_DIR / name).read_bytes(), content_type)
            return self._send(404, b"not found", "text/plain")

        do_HEAD = do_GET

        def _read_only(self):
            self._send(405, b"read-only", "text/plain")

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _read_only

    return ThreadingHTTPServer((host, port), Handler)
