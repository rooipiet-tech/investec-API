# Research — email-payment-v2, Phase 0 (existing pipeline vs GOAL.md)

Paths relative to src/invespend/. Line counts: inbox 134, extract 199, pipeline 288, token 153, caps 51, dedup 28, accounts 38, beneficiaries 91, store 275, pg_store 249, audit 140, notify 88, selftest 224, investec_client 166.

## 1. Mail reading
- IMAP only, stdlib imaplib, SSL: `payments/inbox.py:112-134`. No Gmail API / OAuth. Config: IMAP_HOST/PORT(993)/USER/PASSWORD(spaces stripped)/MAILBOX(INBOX) `config.py:113-118,212-216`; .env.example:58-63 suggests imap.gmail.com + app password. Fetches `UNSEEN` (:126), `RFC822` fetch (:130) — fetch marks messages \Seen (no PEEK), so each message is processed at most once by a cycle; no explicit flagging/moving.
- Replies vs forwards: NOT handled. No In-Reply-To/References parsing (grep: none), no "Fwd:" / forwarded-block detection, no quote stripping. `parse_message` (:88-102) takes the whole text/plain body + subject as one blob; quoted/forwarded text is just more text (so quoted history can inject extra amounts -> "multiple amounts" -> parked, `extract.py:228`).
- Body: only text/plain parts without filename (`inbox.py:58-72`). HTML-only mail => empty body (no HTML->text). message/rfc822 attached forwards: parts with filename are attachments; an unnamed message/rfc822 part is not read as text.
- Attachments: any part with filename (`:75-85`). Dispatch `extract.py:309-333`: images (.png/.jpg/.jpeg/.gif/.bmp/.tiff/.webp/.heic) skipped, no OCR (:304-306); .pdf via pypdf (text only, no scanned-PDF OCR; pyproject.toml:16) (:288-301); .xlsx/.xls via pandas (:270-285); .csv (:249-267); other: try utf-8 text. 10 MB cap (:176,318). Attachment result takes priority over body if status ok/needs_review (`pipeline.py:35-50`).
- Extraction is regex only (`extract.py:158-168`): amount regex; payee ONLY via literal label `payee:`/`beneficiary:` (:165). No account-number / bank / branch / reference extraction at all. Exactly one distinct amount and <=1 payee else needs_review.

## 2. 'pay <3>' trigger and source account
- NO 'pay' trigger phrase exists. Only `_LAST3_RE` (`inbox.py:21`): `(?:\.{2,}|ending|acc(?:ount)?\s*(?:no\.?|number)?\s*[:#]?\s*\D*)(\d{3})\b`, IGNORECASE; searched over subject+body (:93,100). Triggers: "...123", "ending 123", "acc 123"/"account number: 123". `\D*` can swallow arbitrary non-digit text after "acc". "pay 123" does NOT match; "pay abc" nothing.
- Resolution `accounts.py:13-28`: last3 must be exactly 3 digits (`isdigit`) else None; matches `accountNumber` endswith(last3) among `client.get_accounts()` (`investec_client.py:92-93`; the one profile reachable by the API key); 0 or >1 matches -> None -> parked "source_unresolved" (`pipeline.py:161-164`). Letters never match (the regex needs \d{3}; resolver rejects non-digits). Source resolution happens BEFORE extraction, so mail without last3 is parked.
- "Any profile": get_accounts returns only accounts for the single OAuth credential; no multi-profile handling in code.

## 3. Payees / beneficiaries
- Allowlist: static `ALLOWLIST=[]` (`beneficiaries.py:402`, empty) or live `client.get_beneficiaries()` when PAYMENTS_BENEFICIARIES_FROM_API=true (`pipeline.py:85-92`; default false `config.py:112`); GET `/za/pb/v1/accounts/beneficiaries` (`investec_client.py:110-116`). Fetch failure -> empty -> all fail closed.
- Match (`beneficiaries.py:430-462`): case-insensitive exact name equality, exact email equality, or static match_names substring; 0 or >1 -> None. Payee text comes only from `payee:`/`beneficiary:` label. Unresolved -> parked "beneficiary_unresolved" (`pipeline.py:178-181`); nothing else happens.
- NO create-beneficiary code anywhere (grep for beneficiary create/add/post in src: only matching code in db.py:395 for labelling transactions). `investec_client.py:119-123` comment: Investec pays only beneficiaries already created online. Payment is strictly by beneficiaryId (`:150-154`), never raw account number (`beneficiaries.py:1-8`).

## 4. Approval flow
- Token `payments/token.py`: HMAC-SHA256 over (source_account_id|amount|beneficiary_id|dedup_key|nonce|expiry), wire form `nonce:expiry:mac` (:394), default TTL 86400s = 24h (:371), expiry checked at verify (:427), constant-time compare (:439); secret APPROVAL_SIGNING_SECRET must be >= min length and not placeholder (`config.py:18-26,132-142`).
- Intended flow: inbound request -> pending record (`pipeline.py:198-211`) -> consolidated approval email with token sent to `approval_recipients` (:119-128, `notify.py:26-55`) -> owner replies containing token -> next cycle verifies (`pipeline.py:213-230`) -> cap commit -> execute (:234-259). Approver = whoever's reply carries a valid token; no sender check.
- DEFECTS (reading only, not run): (a) `cli.py:268-335` calls `run_approval_cycle` WITHOUT `approval_recipients` or `sender_contact`, so the CLI/Railway job never sends the approval email (`pipeline.py:121`) — tokens never reach the user in production wiring. (b) dedup_key includes the inbound Message-ID (`dedup.py:21`, `pipeline.py:190`); a reply carrying the token has a NEW Message-ID, so it creates a different pending record with a different nonce -> `claim.nonce != record.nonce` -> "token_rejected" (:227). The token only verifies against the same message that carried it; no code links a reply to the original pending (no In-Reply-To lookup). Approval-by-reply therefore appears unreachable end-to-end except in tests that inject a message with both payment details and token. Verify in planning against tests/test_payment_pipeline.py.
- Delay/hold/scheduling: NONE. "24h" exists only as token TTL (`token.py:371`). On valid token, payment executes immediately in same cycle (`pipeline.py:245-255`). No scheduled-payment/`paymentDate` in client; no stored "execute_after".

## 5. Guardrails / security
- Caps `caps.py`: per-payment (:469) and daily aggregate (:481), 0 => fail-closed; per-payment checked `pipeline.py:184`, daily atomically in `store.execute_and_commit` (`store.py:177`, `pg_store.py:155`). Settings PER_PAYMENT_CAP/DAILY_AGGREGATE_CAP default 0 (`config.py:99-100`).
- Dry-run default: `live_enabled()` = PAYMENTS_LIVE_ENABLE AND write credential (write trio or INVESTEC_PAYMENTS_ENABLED) (`config.py:149-178`); `create_payment` only if live (`pipeline.py:245-252`). PAYMENTS_DRY_RUN field (`config.py:102`) is not consulted by live_enabled. `--live` CLI flag is advisory only (`cli.py:273`).
- Dedup `dedup.py:21-28` sha256(message_id|amount|beneficiary|currency); store.is_executed/pending idempotent.
- Audit `audit.py`: minimal-field allowlist (`:26`), file JSON or PgAuditLog (:95); steps in pipeline (cycle_start ... executed).
- Sender authentication: NONE. From header is stored (`inbox.py:96`) but never read for decisions; no SPF/DKIM/Authentication-Results check, no From allowlist (`inbox.py:8-12` states by design). What stops spoofing today = the HMAC token (unforgeable without secret, bound to payment, 24h, single-use via nonce) + beneficiary allowlist + caps. For the v2 trigger ("pay NNN" in body) there is no token, so a spoofed/any-sender email would create a pending only; execution still requires token. If v2 drops the token, From/auth-results checks are entirely new work.
- Prompt injection: NO LLM reads mail. All parsing is regex/pypdf/pandas (`extract.py`, `inbox.py`). No network egress during extraction. Exposure is regex-ambiguity (fail-closed) not LLM. Any v2 "search everything in the mail" via LLM would be a new injection surface.

## 6. Investec client (`investec_client.py`)
- Base https://openapi.investec.com (:31). Auth: OAuth2 client_credentials POST `/identity/v2/oauth2/token`, Basic id:secret + `x-api-key` header (:52-75); then `Authorization: Bearer` + `x-api-key` (:77-89,126-139).
- Reads: GET `/za/pb/v1/accounts`, `/accounts/{id}/balance`, `/accounts/{id}/transactions?fromDate&toDate`, `/accounts/beneficiaries`.
- Pay: POST `/za/pb/v1/accounts/{accountId}/paymultiple` (:124,141-166), JSON `{"paymentList":[{"beneficiaryId","amount":str,"myReference","theirReference"}]}`. Scope: needs payments-enabled credential (comment :119-123; code only distinguishes via config, scope not verified in code). Retries POST on 429/5xx (:43-49, allowed_methods includes POST — double-submit risk on 5xx for payments; no idempotency key). Response is returned unchecked (no per-item status parse, `pipeline.py:250-252`).
- Not in client: create beneficiary, scheduled/future-dated payment, get-beneficiary-categories, transfer. (External API facts for these must be confirmed by the domain step from Investec docs; not derivable from repo.)

## 7. Tests (tests/, counts of test functions)
test_payment_pipeline 21, test_pay_selftest 17, test_payment_client_config 19, test_payment_beneficiaries 15, test_payment_extract 12, test_payment_pg_store 10 (DB), test_beneficiary_payee 10, test_payment_dedup_caps 9, test_payment_token 9, test_payment_store 8, test_payment_audit 6, test_payment_accounts 5, test_payment_notify 5. Offline with fake inbox/client; ImapInbox and real SMTP are `pragma: no cover` (never tested). Per CLAUDE.md baseline 62 passing at the time of that doc (suite has grown).

## 8. Env/config
IMAP_HOST/PORT/USER/PASSWORD/MAILBOX; APPROVAL_SIGNING_SECRET; PER_PAYMENT_CAP; DAILY_AGGREGATE_CAP; PAYMENTS_DRY_RUN (unused by gate); PAYMENTS_LIVE_ENABLE; INVESTEC_WRITE_CLIENT_ID/SECRET/API_KEY; INVESTEC_PAYMENTS_ENABLED; PAYMENTS_BENEFICIARIES_FROM_API; PAYMENTS_STATE_BACKEND (file|postgres), PAYMENTS_STATE_DIR; retention 90d (`config.py:119-126`); SMTP via emailer.report_sender (`notify.py:20`); DATABASE_URL for pg state (migration 0009_payment_approvals.sql per DEPLOY.md).

## 9. Scheduling / deployment
- `railway.toml`: startCommand `invespend approve-payments --once`, cron `*/15 * * * *`, restartPolicy NEVER. Added in commit 25677ef. DEPLOY.md documents it.
- .github/workflows: backfill.yml, export-groups.yml, ingest.yml, tests.yml, weekly-report.yml — NONE mention payments/approve/IMAP (grep empty). So no GitHub Actions scheduling for payments.
- Deployment: repo evidence only (railway.toml, Dockerfile, DEPLOY.md); no Railway project id/URL/logs in repo; actual deployment status UNKNOWN. Note railway.toml notes one service = one command, so the payments cron preempts other jobs in that service.

## GAPS vs request
1. Trigger: no "pay <3>" parser; existing last-3 needs "...123"/"ending 123"/"acc 123". GOAL says letters/digits ambiguous; resolver is digits-only.
2. Reply/forward handling: no quote/forward parsing, no HTML, no message/rfc822 unwrap, no thread linkage; mail as one text blob; multiple amounts in quoted history -> parked.
3. "Search everything in the mail": only labelled `payee:`/`beneficiary:` + amount; no bank-detail extraction (account no/branch/ref), no free-text payee discovery.
4. Payee match: existing registered beneficiaries only (exact name/email), only one profile; no cross-profile search.
5. New beneficiary creation: absent (and the code comment says Investec requires beneficiaries created online first — API support unverified, domain step must confirm).
6. 24h delay: absent; payment executes the cycle the token is validated. Token TTL 24h is approval expiry, not payment delay. No scheduler for deferred execution (would need persisted execute_after + cron pass).
7. Auth of sender: none; v2 trigger without token needs SPF/DKIM/From-allowlist design.
8. Production wiring defects noted in section 4 (no approval email sent by CLI; reply Message-ID breaks dedup/nonce match).
9. Not deployed evidence; no GH Actions workflow for payments.
