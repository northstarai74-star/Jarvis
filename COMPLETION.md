# Completion review — 16 November 2026

Continued the existing project in `C:\Users\lenovo\Desktop\JARVIS` rather
than creating a second nested project. This path is outside OneDrive.

## What was already complete

All seven requested phase implementations existed, together with 327 unittest
tests and an end-to-end dry-run command. The baseline suite passed on the
locally installed Python 3.12.14. No download was needed. The shell's default
Python is 3.11.15, so the new `run.cmd` deliberately selects 3.12.

## Changes and phase gates

Each row records a complete `unittest discover` run on Python 3.12.14 after
the phase's changes and before proceeding to the next phase's changes.

| Phase | Completed work | Full-suite result |
| --- | --- | --- |
| 1: Core | Ignore process-environment secrets in production; add configuration example, ignore rules, runtime marker and launcher; serialize cap checks and send ledger updates across SQLite connections | 330 passed |
| 2: Sales | Always select file outbox; require explicit fake factory for SMTP interface; reject altered outreach subject/body before sending | 332 passed |
| 3: Leads | Recheck verification at promotion, including changed company status | 333 passed |
| 4: Autopilot | Enforce five-day follow-up wait at send time as well as planning time; reject follow-ups after replies/bookings | 334 passed |
| 5: Phone | Count malformed keypad attempts toward lockout; reject malformed signed booking payload shapes | 336 passed |
| 6: Dashboard | Read pending approvals without changing expiry state; reject non-local bind addresses | 338 passed |
| 7: Operations | Doctor checks the requested Python minor version and states the offline adapter boundary | 339 passed |

Added 12 regression tests. Updated the previous SMTP selection test to enforce
the requested offline behavior. Existing tests cover policy refusals, PIN
expiry/lockout, parameter binding, single use, audit tampering, suppression,
CSV/JSON import, send rate/cap, fake clock, phone signatures, dashboard HTTP
security, backup retention and watchdog behavior.

No connectors, real API keys, package downloads or external network access
were used. HTTP server tests use loopback only. Dashboard rendering is covered
by source and local HTTP tests; no visual browser inspection was performed.

## Final end-to-end dry run

Executed `run.cmd dry-run`: **33/33 checks passed**, exit status 0.

- Imported 8 Manus-style CSV leads.
- Verified: 4 ready, 1 needs_email, 2 review, 1 rejected.
- Promoted only the 4 ready leads into firms.
- Approved wording with a throwaway approval PIN.
- Approved go-live and cap 3 through the fake phone with the separate voice PIN.
- Wrote exactly 3 `.eml` files; all sends fell within the UK weekday window.
  Minimum spacing was 140 minutes.
- Classified an interested reply, suppressed STOP, and treated hostile reply
  instructions as data without changing the cap or generating approvals.
- Rejected an unsigned booking, recorded a correctly HMAC-signed booking, and
  triggered the fake owner call with the brief and next steps.
- Refused money out and an excessive cap.
- Produced the evening summary, verified the 55-entry audit chain, checked
  secret redaction, made a checked backup and passed the watchdog check.
- Built the dashboard state with one booking and two fake calls.

The temporary dry-run files were cleaned up. Run `run.cmd dry-run --keep` to
retain a new run's files. No real email, phone call or calendar event was made.

## Remaining owner setup and live-service blocks

The ordinary database has no personal PINs, approved templates, go-live approval
or daily cap. Those require the owner's choices; the dry run uses a separate
database and does not authorize ordinary operation. The README gives commands
for setting the separate PINs and approving requests.

There is no real `.env` file and no real keys were created. The supplied
`.env.example` contains blank credentials. Missing settings are:

| Service | Missing values |
| --- | --- |
| SMTP | SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SENDER_EMAIL |
| Twilio | TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, OWNER_PHONE, PUBLIC_BASE_URL |
| Cal.com | CALCOM_WEBHOOK_SECRET, BOOKING_LINK |
| Companies House | COMPANIES_HOUSE_API_KEY |

Live adapters also require implementation/enablement and live validation in a
future, separately authorized task. Keys alone cannot enable them in this
offline build. Ordinary lead verification intentionally waits in review when
no register is injected; the dry run supplies a fake register.

The shared cap is enforced across SQLite connections during normal operation.
SQLite and file-outbox writes are not one atomic transaction: a process crash
between writing an email file and committing the database can leave an orphan
outbox file. A future real sender needs durable delivery/idempotency handling
before production use. Local application code and database administrators are
trusted; the audit chain is not externally anchored.
