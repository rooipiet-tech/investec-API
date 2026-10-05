# Plan v1.0.0 — email-payment-approval (iteration 1)

reused_learnings: none (.compound/ empty)

## Change set: ONE isolated subpackage src/invespend/payments/ + additions-only edits.
Pure helpers first → I/O adapters → orchestration → ONE additive CLI subcommand. No change to
ingest/report/statements/categorize/groups/db/backup, existing 6 subcommands, emailer.send_email, or db/ migrations/views.

## Steps
1. payments/__init__.py — empty package marker. [F13]
2. payments/token.py — pure HMAC-SHA256 issue/verify bound to (source_account_id,amount,beneficiary_id,dedup_key,nonce,expiry);
   hmac.compare_digest; expiry; new_nonce via secrets; secret never in output. TokenClaim dataclass. [F2,F10]
3. payments/beneficiaries.py — code-config ALLOWLIST keyed by beneficiary_id (groups.py pattern); resolve_beneficiary exact,
   0/>1 -> None fail-closed; returns registered id, never parsed account. [F4]
4. payments/accounts.py — resolve_source_account(accounts,last3) exact match against real get_accounts; 0/>1 -> None. [F3]
5. payments/dedup.py — dedup_key(message_id,amount,beneficiary_id,currency) sha256 hex. [F7]
6. payments/caps.py — pure check_per_payment + check_daily_aggregate -> CapDecision(ok,reason). [F6]
7. payments/extract.py — ExtractResult(status ok|needs_review|skipped,...); extract_from_text/csv/xlsx(pandas)/pdf(pypdf);
   image -> skipped (NO cloud OCR); multiple amounts/payees/currency-mismatch/corrupt-pdf -> needs_review (fail-closed). [F5,F11,F18]
8. payments/store.py — file/JSON: idempotent add_pending (dedup key), mark_executed/burned, persisted daily-aggregate by ISO date
   (survives fresh process), minimal fields only (last-3/hash, no full PAN), cleanup(retention_days), atomic writes. [F6,F7,F17,F20]
9. payments/audit.py — append-only timestamped JSON-lines; redact account-shaped fields; never record secret. read_entries. [F9,F20,F15]
10. payments/notify.py — isolated send via emailer._smtp_send (NOT send_email): build/send consolidated approval email (per-payment
    bound tokens, append-later), minimised progress email only when sender_contact present. [F12,F14,F19]
11. investec_client.py — ADD _post + create_payment (additions only; existing read methods byte-identical); only reached in live mode. [F8,F13]
12. config.py + .env.example — NEW OPTIONAL Settings: approval_signing_secret, per_payment_cap, daily_aggregate_cap,
    payments_dry_run(default True), payments_live_enable(default False), investec_write_* (absent cred), imap_*, retention_days(90),
    state_dir; has_write_credential()/live_enabled(). Existing load() order + required vars unchanged. [F8,F15,F5]
13. payments/inbox.py — injectable InboxReader (stdlib imaplib+email); pure parse_message/extract_last3/extract_token;
    real ImapInbox; tests inject a fake. [F1,F3,F16]
14. payments/pipeline.py — run_approval_cycle wiring all helpers, fully injectable. Fail-closed branches never execute; ONLY a valid
    bound HMAC token approves; burn single-use; re-check caps pre-execute; dry-run default calls NO write endpoint; live only when
    live_enabled(); audit every step; progress email only if sender_contact; cleanup. Returns machine-readable dict. [F1-F12,F19,F20]
15. cli.py — ONE additive subcommand approve-payments (--dry-run/--live default dry-run, --once); machine-readable JSON; deterministic
    exit code. 6 existing subcommands untouched. [F8,F13,F16]
16. pyproject.toml — add pypdf (lightweight, pure-python). [F18,F11]
17. tests/test_payment_*.py (10 files) — offline, mock inbox/SMTP/Investec (template test_account_alert.py); one per concern + guard
    tests (send_email still raises on zero attachments; Settings.load works with only legacy env). [F1-F20]

## Coverage: every F1..F20 mapped (see planner coverage_check). 
## Risks: OQ1 Investec write endpoint unverified -> create_payment only mock/dry-run tested, builder must NOT assert an unverified URL
as fact; investec_client.py is the only non-new-file edit -> additions-only + git-diff freeze check; xlsx path depends on pandas/openpyxl
(may be absent locally -> tester proves 62-floor in full-dep env); notify reuses private emailer._smtp_send read-only; pipeline.py is the
largest convergence surface -> staged last after helpers are green.

## Plan-review gate (verdict: revise — 0 blockers). MANDATORY tightenings folded in for the builder:
- PR1 [F8]: live_enabled() MUST be the conjunction `payments_live_enable AND has_write_credential()`. Flag-only (no write cred) stays dry-run. Test all three states (default / flag-only / flag+cred).
- PR2 [F9,F15,F20]: audit AND store MUST write via an EXPLICIT minimal-field allowlist at write time (last-3 or hash only) — NEVER raw-then-redact. Raw parsed account strings from extract.py (F4/F18) must NEVER enter audit/store. Add scan tests asserting no full account number and no secret anywhere in audit+store output.
- PR3 [F6,F7]: daily-aggregate increment MUST be committed atomically with mark_executed/burn and re-read pre-execute under a single ordering — two pendings in one cycle cannot both pass then jointly exceed the aggregate. Test the under-cap-pushing-over-aggregate block + restart persistence.
- PR4 [F12]: progress email send gated STRICTLY on explicit sender-contact presence; add a built assertion the body/headers contain NONE of {beneficiaryId, full account, balance, caps, token}.
- PR5 [F13,F8]: investec_client.py changes are PURE ADDITIONS (_post + create_payment); existing read methods byte-identical; gate iteration on a git-diff check proving no existing signature/behaviour changed.
