# Research — email-driven payment-approval pipeline (run: email-payment-approval-loop)

reused_learnings: none (.compound/ empty — first compounding run)

## In-repo facts (high confidence)
- **InvestecClient** (src/invespend/investec_client.py, 101 lines): OAuth2 client-credentials; token cached.
  Docstring states creds are **READ-ONLY ("cannot move money")**. Methods: get_accounts:84, get_balance:87,
  get_transactions:90. Only private `_get()`. **NO _post(), NO payments/transfers/beneficiaries.** Retry config
  already allows POST (:39) so adding `_post()` is mechanical, but needs a write-scoped credential the repo lacks.
- **Outbound email** (emailer.py, 77 lines): SMTP STARTTLS via Gmail app password. send_email(...) reusable for
  both the approval email to user and progress emails to senders. Fully solved.
- **Inbound email**: NONE in repo. stdlib `imaplib`+`email` available, zero new dep → feasible Gmail IMAP reader.
  Gmail MCP tools (search_threads/get_thread/create_draft/labels) are **READ+DRAFT only — no send**; session-bound,
  not available in headless cron. → Reading via MCP or IMAP is a candidate; **sending must use SMTP**.
- **config.py / Settings** (frozen dataclass): has investec_*, database_url, smtp_*, report_sender/recipients,
  ingest_window_days, backup_passphrase. NEW config the goal needs: write-scoped Investec cred; APPROVAL_SIGNING_SECRET
  (HMAC, stdlib); ACCOUNT_LAST3 triggers (could derive from get_accounts()); PER_PAYMENT_CAP + DAILY_AGGREGATE_CAP;
  beneficiary ALLOWLIST (code-config like groups.py); DRY_RUN flag (default simulate); IMAP host/mailbox.
- **Tests**: 62 functions / 11 files (matches baseline). Mock-heavy, pure-unit; DB tested via fake cursor/conn; no
  live Investec/SMTP. test_account_alert.py = template (monkeypatch globals, assert (subject,body) tuples).
  ⚠ This env lacks pytest+pandas/openpyxl/psycopg → suite can't run here as-is (env gap, not a regression).
- **Attachment libs**: pandas+openpyxl present → Excel/CSV OK, no new dep. **PDF: none installed** → needs NEW dep
  (pypdf, light, pure-python). **OCR: fully absent** — no pytesseract/PIL/pdf2image AND no tesseract binary on host.
  Image OCR needs a system binary or a cloud OCR API (network+cost+POPIA). Biggest dependency risk.
- **Isolation**: new subcommand registers cleanly at cli.py:261 without touching existing commands. Migrations
  0001-0008 have no payment/approval tables. Pending-approval state can be file/JSON (no schema change) OR new 0009
  migration (needs explicit spec permission — Golden Rule freezes schema).

## Investec payment API — ASSUMPTIONS (UNVERIFIED, not in repo)
- ASSUMPTION: POST /za/pb/v1/accounts/{id}/payments and /transfers exist under za/pb/v1.
- ASSUMPTION: payments need a SEPARATE higher-privilege scope + portal enablement; current key is read-only → 401/403 on writes.
- ASSUMPTION: beneficiaries are pre-registered in Investec online banking; API likely exposes a beneficiaries list →
  you generally CANNOT pay an arbitrary parsed account; Investec itself enforces an allowlist-like model.
- UNKNOWN: sandbox vs live payment support; server-side 2FA/OOB approval.

## HARD constraints
1. Golden Rule: new module + new CLI subcommand only; no change to ingest/report/statements/8 migrations/views; 62 tests green.
2. Existing Investec cred read-only → live payment needs a new write-scoped key the repo does not have; without it only DRY_RUN.
3. Guardrails: allowlisted beneficiaries only; per-payment + daily caps; HMAC-signed token in approval round-trip (NOT plaintext "app"); simulation default before any live transfer.
4. Image OCR not satisfiable with current/light deps (no tesseract binary).
5. MCP Gmail read/draft only; sending via SMTP.

## Candidate approaches (NOT chosen)
- inbox read: A) stdlib imaplib (zero dep, cron-friendly) | B) Gmail MCP (richer, session-bound, read-only).
- approval round-trip: A) HMAC-SHA256 token in approval email, validated on inbound before execute | B) server-side one-time nonce.
- pdf: A) pypdf (light) | B) pdfplumber (tables, heavier).
- ocr: A) images OUT OF SCOPE / graceful skip | B) optional pytesseract+binary | C) cloud OCR (POPIA egress).
- pending state: A) file/JSON store (no schema change) | B) new 0009 migration (needs spec permission).

## Risks
- HIGH: money movement from a parsed account = core fraud/spoofing surface; mitigated only by allowlist+caps+signed token+dry-run.
- HIGH: inbound email is forgeable; signed token must originate from OUR approval mail, never derivable from inbound content.
- MEDIUM: OCR/PDF mis-extraction → must FAIL CLOSED (no auto-pay on low-confidence parse).
- MEDIUM: env lacks pytest+deps → tester needs deps installed to prove 62-green floor.
- LOW: progress emails to senders leak in-flight payment; send only if sender contact explicitly present; minimise data.

## Domain flags (for domain-expert)
- POPIA/data-residency: scanning personal Gmail, storing parsed payment + third-party sender contact, any cloud-OCR egress.
- Money-movement security invariants: signed-token scheme, caps, allowlist semantics, live-vs-dry-run gate = Must-haves.
- Confirm Investec payment endpoint/scope reality before any executable behaviour is specced.
- Idempotency: re-scanning must not pay the same email twice (dedup key, analogous to transaction_hash).

## Unknowns
- Exact Investec payment endpoints/schema/scope. Whether user even has a write-scoped cred (→ live vs dry-run-only).
- Whether host can install tesseract (→ image OCR in scope?). File-state vs migration. IMAP poll vs MCP for approval detection.

confidence: high on in-repo facts; low on Investec payment-API specifics (unverified assumptions).
