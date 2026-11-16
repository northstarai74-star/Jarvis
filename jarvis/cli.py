"""Command line. PINs are always typed at a hidden prompt, never passed as arguments."""

from __future__ import annotations

import argparse
import getpass
import sys

from . import ops
from .approvals import ApprovalError
from .pins import PinError

LEVEL_MARK = {"ok": "OK     ", "warn": "WARN   ", "fail": "FAIL   ", "blocked": "BLOCKED"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS business assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show where things stand")
    sub.add_parser("pending", help="list approvals waiting for a PIN")
    p = sub.add_parser("approve", help="approve a request with the approval PIN")
    p.add_argument("id")
    p = sub.add_parser("reject", help="reject a request")
    p.add_argument("id")
    sub.add_parser("backup", help="back up the database")
    sub.add_parser("doctor", help="check the system and list missing keys")
    sub.add_parser("audit-verify", help="check the audit chain")
    p = sub.add_parser("pin", help="set a PIN")
    p.add_argument("verb", choices=["set"])
    p.add_argument("purpose", choices=["approval", "voice"])
    p = sub.add_parser("stop", help="hard stop: nothing with an effect runs")
    p.add_argument("reason", nargs="?", default="owner asked")
    sub.add_parser("unstop", help="ask to lift the hard stop (needs PIN approval)")

    p = sub.add_parser("templates", help="list, show, or ask to approve email wording")
    p.add_argument("verb", choices=["list", "show", "approve"])
    p.add_argument("name", nargs="?")
    sub.add_parser("go-live", help="ask to allow real sending (needs PIN approval)")
    sub.add_parser("pause", help="stop sending emails until go-live is approved again")
    p = sub.add_parser("cap", help="ask to set the daily send cap (needs PIN approval)")
    p.add_argument("number", type=int)
    sub.add_parser("firms", help="list firms in the CRM")
    p = sub.add_parser("reply", help="record a reply from a text file")
    p.add_argument("from_email")
    p.add_argument("file")
    p.add_argument("--subject", default="")

    p = sub.add_parser("leads", help="import, verify, promote or list leads")
    p.add_argument("verb", choices=["import", "verify", "promote", "list"])
    p.add_argument("arg", nargs="?", help="a CSV/JSON file for import, or a status for list")

    p = sub.add_parser("autopilot", help="plan the day, send what is due, or run all day")
    p.add_argument("verb", choices=["draft", "tick", "morning", "evening", "step", "run"])

    p = sub.add_parser("call", help="phone the owner with a reason")
    p.add_argument("reason")
    sub.add_parser("webhooks", help="run the Twilio and Cal.com webhook server")
    sub.add_parser("dashboard", help="open the read-only dashboard on this computer")
    sub.add_parser("watchdog", help="check for trouble; serious trouble trips the hard stop")
    p = sub.add_parser("dry-run", help="run the whole story with fakes in a throwaway folder")
    p.add_argument("--keep", action="store_true", help="keep the folder to look at the emails")
    return parser


def _show(out, result) -> int:
    if result.status == "done":
        out(f"Done: {result.action}" + (f" -> {result.value}" if result.value is not None else ""))
        return 0
    if result.status == "pending":
        out(f"Waiting for approval. Run: jarvis approve {result.approval_id}")
        return 0
    out(f"{result.status.capitalize()}: {result.action}: {result.reason}")
    return 1


def cmd_status(app, args, out, read_pin) -> int:
    j = app.jarvis
    out(f"Hard stop:   {'ON' if j.is_stopped() else 'off'}")
    out(f"Live:        {'yes' if j.is_live() else 'no'}")
    out(f"Daily cap:   {j.cap() or 'not set'}")
    out(f"Sent today:  {j.sends_today()}")
    out(f"Pending:     {len(j.approvals.pending())} approval(s)")
    result = j.audit.verify()
    out(f"Audit chain: {'intact' if result.ok else 'BROKEN'} ({result.count} entries)")
    return 0


def cmd_pending(app, args, out, read_pin) -> int:
    rows = app.jarvis.approvals.pending()
    if not rows:
        out("Nothing is waiting for approval.")
    for row in rows:
        out(f"{row['id']}  {row['summary']}  (expires {row['expires_at']})")
    return 0


def cmd_approve(app, args, out, read_pin) -> int:
    try:
        result = app.jarvis.approve(args.id, read_pin("Approval PIN: "))
    except (ApprovalError, PinError) as exc:
        out(f"Not approved: {exc}")
        return 1
    return _show(out, result)


def cmd_reject(app, args, out, read_pin) -> int:
    try:
        app.jarvis.approvals.reject(args.id)
    except ApprovalError as exc:
        out(f"Not rejected: {exc}")
        return 1
    out(f"Rejected {args.id}.")
    return 0


def cmd_backup(app, args, out, read_pin) -> int:
    return _show(out, ops.backup(app.jarvis))


def cmd_doctor(app, args, out, read_pin) -> int:
    checks = ops.doctor(app.jarvis)
    for check in checks:
        out(f"{LEVEL_MARK[check.level]}  {check.name}: {check.detail}".rstrip(": "))
    return 1 if any(c.level == "fail" for c in checks) else 0


def cmd_audit_verify(app, args, out, read_pin) -> int:
    result = app.jarvis.audit.verify()
    if result.ok:
        out(f"Audit chain intact: {result.count} entries.")
        return 0
    out(f"Audit chain BROKEN at entry {result.bad_id}: {result.reason}")
    return 1


def cmd_pin(app, args, out, read_pin) -> int:
    pins = app.jarvis.pins
    try:
        if pins.is_set(args.purpose):
            if not pins.verify(args.purpose, read_pin(f"Current {args.purpose} PIN: ")):
                out("Wrong PIN.")
                return 1
        new = read_pin(f"New {args.purpose} PIN (4-8 digits): ")
        if new != read_pin("Again: "):
            out("The two entries did not match.")
            return 1
        pins.set_pin(args.purpose, new)
    except PinError as exc:
        out(f"PIN not set: {exc}")
        return 1
    out(f"The {args.purpose} PIN is set.")
    return 0


def cmd_stop(app, args, out, read_pin) -> int:
    return _show(out, app.jarvis.stop(args.reason))


def cmd_unstop(app, args, out, read_pin) -> int:
    return _show(out, app.jarvis.execute("system.unstop", {}, actor="owner"))


def cmd_templates(app, args, out, read_pin) -> int:
    if args.verb == "list":
        for row in app.templates.all():
            out(f"{row['name']:<9} {'approved' if row['approved'] else 'NOT approved'}"
                f"  {row['subject']}")
        return 0
    row = app.templates.get(args.name or "")
    if row is None:
        out("Name a template: direct, soft or followup.")
        return 1
    if args.verb == "show":
        out(f"Subject: {row['subject']}")
        out("")
        out(row["body"])
        return 0
    return _show(out, app.templates.request_approval(row["name"]))


def cmd_go_live(app, args, out, read_pin) -> int:
    return _show(out, app.sales.request_go_live())


def cmd_pause(app, args, out, read_pin) -> int:
    return _show(out, app.sales.pause())


def cmd_cap(app, args, out, read_pin) -> int:
    return _show(out, app.sales.request_cap(args.number))


def cmd_firms(app, args, out, read_pin) -> int:
    firms = app.crm.firms()
    if not firms:
        out("No firms yet.")
    for firm in firms:
        out(f"{firm['id']:>4}  {firm['status']:<15} {firm['name']}  <{firm['email']}>")
    return 0


def cmd_reply(app, args, out, read_pin) -> int:
    from pathlib import Path
    body = Path(args.file).read_text(encoding="utf-8", errors="replace")
    result = app.sales.handle_reply(args.from_email, body, args.subject)
    if result.ok:
        out(f"Recorded as: {result.value['category']}")
        return 0
    return _show(out, result)


def cmd_leads(app, args, out, read_pin) -> int:
    leads = app.leads
    if args.verb == "import":
        if not args.arg:
            out("Give a CSV or JSON file to import.")
            return 1
        try:
            result = leads.import_file(args.arg)
        except (OSError, ValueError) as exc:
            out(f"Import failed: {exc}")
            return 1
        if not result.ok:
            return _show(out, result)
        v = result.value
        out(f"Imported {v['imported']}, duplicates {v['duplicates']}, skipped {v['skipped']}.")
        return 0
    if args.verb == "verify":
        tally = leads.verify_all()
        out("Verified: " + (", ".join(f"{n} {s}" for s, n in sorted(tally.items()))
                            or "nothing was waiting"))
        return 0
    if args.verb == "promote":
        out(f"Promoted {leads.promote_ready()} lead(s) to the CRM.")
        return 0
    for lead in leads.list(args.arg):
        reason = f"  ({lead['reason']})" if lead["reason"] else ""
        out(f"{lead['id']:>4}  {lead['status']:<12} {lead['name']}{reason}")
    counts = leads.counts()
    out("Totals: " + ", ".join(f"{s} {n}" for s, n in counts.items()))
    return 0


def cmd_autopilot(app, args, out, read_pin) -> int:
    auto = app.autopilot
    if args.verb == "draft":
        plan = auto.draft_day()
        out(f"Queued {plan['first']} first emails and {plan['followup']} follow-ups "
            f"({plan['drafted']} newly written).")
        if plan["note"]:
            out(plan["note"])
        return 0
    if args.verb == "tick":
        result = auto.tick()
        out(f"Sent {result['sent']}." + (f" {result['note']}" if result["note"] else ""))
        return 0
    if args.verb in ("morning", "evening"):
        result = auto.morning_summary() if args.verb == "morning" else auto.evening_summary()
        out(result.value if result.ok else f"{result.status}: {result.reason}")
        return 0 if result.ok else 1
    if args.verb == "step":
        result = auto.step()
        out(f"Did: {', '.join(result['did']) or 'nothing new'}. Sent {result['sent']}.")
        return 0
    import time as _time
    out("Autopilot running. Press Ctrl+C to stop.")
    try:
        while True:
            for problem in app.watchdog():
                out(f"PROBLEM  {problem}")
            result = auto.step()
            if result["did"] or result["sent"]:
                out(f"Did: {', '.join(result['did']) or '-'}. Sent {result['sent']}.")
            _time.sleep(30)
    except KeyboardInterrupt:
        out("Autopilot stopped.")
    return 0


def cmd_call(app, args, out, read_pin) -> int:
    return _show(out, app.phone.alert_owner(args.reason, actor="owner"))


def cmd_webhooks(app, args, out, read_pin) -> int:
    from .phone.server import make_webhook_server
    missing = app.settings.missing_keys()
    for group in ("phone (Twilio)", "booking (Cal.com)"):
        if group in missing:
            out(f"BLOCKED  {group}: missing {', '.join(missing[group])}")
    server = make_webhook_server(app, port=app.settings.get_int("WEBHOOK_PORT", 8766))
    out(f"Webhooks listening on http://127.0.0.1:{server.server_address[1]} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        out("Webhook server stopped.")
    finally:
        server.server_close()
    return 0


def cmd_dashboard(app, args, out, read_pin) -> int:
    from .dashboard.server import make_dashboard_server
    server = make_dashboard_server(app, port=app.settings.get_int("DASHBOARD_PORT", 8765))
    out(f"Dashboard: http://127.0.0.1:{server.server_address[1]}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        out("Dashboard stopped.")
    finally:
        server.server_close()
    return 0


def cmd_watchdog(app, args, out, read_pin) -> int:
    problems = app.watchdog()
    if not problems:
        out("Watchdog: all clear.")
        return 0
    for problem in problems:
        out(f"PROBLEM  {problem}")
    if app.jarvis.is_stopped():
        out("The hard stop is ON.")
    return 1


def cmd_dry_run(app, args, out, read_pin) -> int:
    from . import dryrun
    report = dryrun.run(real_settings=app.settings, out=out, keep=args.keep)
    out("")
    out(f"Dry run: {report['passed']} of {report['total']} checks passed.")
    if report["blocked"]:
        out("Blocked until these keys are in .env:")
        for group, keys in report["blocked"].items():
            out(f"  {group}: {', '.join(keys)}")
    return 0 if report["passed"] == report["total"] else 1


COMMANDS = {
    "dry-run": cmd_dry_run,
    "watchdog": cmd_watchdog,
    "dashboard": cmd_dashboard,
    "call": cmd_call,
    "webhooks": cmd_webhooks,
    "autopilot": cmd_autopilot,
    "leads": cmd_leads,
    "templates": cmd_templates,
    "go-live": cmd_go_live,
    "pause": cmd_pause,
    "cap": cmd_cap,
    "firms": cmd_firms,
    "reply": cmd_reply,
    "status": cmd_status,
    "pending": cmd_pending,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "backup": cmd_backup,
    "doctor": cmd_doctor,
    "audit-verify": cmd_audit_verify,
    "pin": cmd_pin,
    "stop": cmd_stop,
    "unstop": cmd_unstop,
}


def main(argv=None, app=None, out=None, read_pin=None) -> int:
    out = out or print
    read_pin = read_pin or getpass.getpass
    args = build_parser().parse_args(argv)
    own_app = app is None
    if own_app:
        from .app import App
        from .settings import Settings
        app = App(Settings.load())
    try:
        return COMMANDS[args.command](app, args, out, read_pin)
    finally:
        if own_app:
            app.close()


if __name__ == "__main__":
    sys.exit(main())
