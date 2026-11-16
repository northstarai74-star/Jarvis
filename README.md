# JARVIS

Local lead-to-viewing assistant for an estate and letting agency. Python 3.12,
standard library, SQLite and unittest only. No packages or keys are required
for the fake-service dry run. The existing project was continued in
`C:\Users\lenovo\Desktop\JARVIS`, outside OneDrive.

## Run on this computer

From this folder in PowerShell:

```powershell
.\run.cmd status
.\run.cmd dry-run
.\run.cmd audit-verify
.\run.cmd doctor
.\run.cmd dashboard
```

`run.cmd` selects the locally installed Python 3.12 runtime without downloading
anything. The dashboard is at http://127.0.0.1:8765/ while the command runs.
Stop it with Ctrl+C. It is read-only; approvals happen at the CLI or through
the fake phone workflow, not the dashboard.

Run the complete tests with the installed interpreter:

```powershell
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest discover -v
```

## Local setup

Copy `.env.example` to `.env` if configuration is needed. Leave service keys
blank for offline work. Production secrets are read from `.env`, not process
environment variables. Tests inject made-up configuration. PINs are entered
at hidden prompts and stored only as salted PBKDF2 hashes in SQLite:

```powershell
.\run.cmd pin set approval
.\run.cmd pin set voice
.\run.cmd templates show direct
.\run.cmd templates approve direct
.\run.cmd pending
.\run.cmd approve REQUEST_ID
.\run.cmd go-live
.\run.cmd cap 10
```

Approve each pending request separately. The two PINs must differ. Three wrong
attempts lock that PIN for 15 minutes. Go-live here enables file-outbox sends,
not external delivery. Setting SMTP credentials cannot enable network delivery.

## Existing implementation found during review

All seven phase implementations and 327 tests already existed:

| Phase | Existing modules and behavior |
| --- | --- |
| 1 | Settings, SQLite/RLock, hash-chained audit, policy tiers, PINs, expiring single-use parameter-bound approvals, resume recipes and CLI |
| 2 | Firms, branches/shops, outreach, replies, three templates, outbox, suppression, classifier, go-live and shared cap |
| 3 | CSV/JSON intake, six lead statuses, fake Companies House, verification and CRM promotion |
| 4 | UK weekday send window, spread-out queue, rate limit, five-day follow-ups, daily draft and summaries, fake clock |
| 5 | Fake Twilio, keypad state machine, separate voice PIN, signed booking webhook and owner call |
| 6 | Local dashboard, canvas orb, four panels, CSP, Host check and textContent rendering |
| 7 | Verified SQLite backups, doctor, watchdog and end-to-end dry run |

The review found missing setup documentation/configuration examples and several
boundary checks. See `COMPLETION.md` for the final changes and verification.

## Offline boundaries and operations

The file outbox is the application sender. SMTP's interface can be exercised
only with an explicitly injected fake factory. Companies House and Twilio have
fake clients; their production factories remain disconnected. Ordinary imports
therefore wait for verification unless a fake register is supplied. The dry run
supplies that register and fake phone, exercises PIN approvals, and signs a
simulated booking payload with a made-up secret. No real calendar event is made.

Use `dry-run --keep` to retain the isolated temporary database, imported CSV and
outbox for inspection. The normal database and its approvals are not altered.

`stop` trips the hard stop; `unstop` requires PIN approval. Safety intake
(STOP replies and booking recording), backups and owner alerts remain allowed
during a stop. `backup` writes checked database copies under `data/backups`,
retaining 14 copies and excluding `.env`. Backups contain CRM data and PIN hashes;
keep them private. `watchdog` checks once; `autopilot run` invokes it each cycle.

The approval and audit protections assume trusted local application code and
database administrators. Lead text is never executed. A hash chain detects
accidental or partial tampering; it is not an externally anchored ledger.

## What blocks real services

No real service is enabled in this build. Future integration would require
adapter implementation/enablement and validation, as well as these `.env` values:

- SMTP: `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `SENDER_EMAIL`.
- Twilio: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
  `OWNER_PHONE`, `PUBLIC_BASE_URL`.
- Cal.com: `CALCOM_WEBHOOK_SECRET`, `BOOKING_LINK`, plus configured webhook delivery.
- Companies House: `COMPANIES_HOUSE_API_KEY` and a real register adapter.

Adding keys alone does not make the project live. No connector or outside
service was used to build or verify this project.
