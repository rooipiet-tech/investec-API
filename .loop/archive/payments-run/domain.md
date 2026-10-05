# Domain constraints — email-driven payment-approval pipeline

reused_learnings: none (.compound/ empty — first run)

Domains: money-movement security/payment authz; POPIA (Act 4 of 2013); banking idempotency/instruction integrity;
email-channel auth & anti-spoofing; audit/non-repudiation.

## HARD constraints (each FORCES a testable spec criterion)
- **AUTH-INBOUND-NOT-AUTHENTICATOR**: inbound email (From/subject/body/keyword incl. "app") MUST NOT alone authorize a
  payment — SPF/DKIM/DMARC authenticate domain not intent. Only a server-issued secret bound to the payment may authorize.
  Forbids auto-pay on any plaintext keyword or sender identity.
- **TOKEN-HMAC-BOUND-SINGLE-USE-EXPIRING**: confirmation token = HMAC-SHA256 keyed by a secret never emailed, BOUND to
  (amount, beneficiaryId, source-email idempotency key, issuance ts/nonce), single-use (burned on first valid approval),
  time-bounded (expiry), verified with constant-time compare (hmac.compare_digest). Reject reused/expired/mismatched.
- **IDEMPOTENCY-DEDUP-KEY**: deterministic dedup key per payment (message-id+amount+beneficiary+currency); re-scan/retry/
  duplicate MUST NOT create a 2nd pending or 2nd execution. Mirrors db.py:127 transaction_hash + on-conflict-do-nothing.
- **CAPS-ENFORCED-PRE-EXECUTION**: per-payment cap AND daily aggregate cap both evaluated BEFORE execute(); daily total
  persisted (survives restart) keyed by date; under-cap sequence cannot breach aggregate.
- **ALLOWLIST-EXACT-BENEFICIARY-ID**: execute only to a pre-registered beneficiary matched EXACTLY by stable beneficiaryId,
  never a fuzzy/parsed account string/name/last-3. Parsed payee resolves to beneficiaryId exactly+unambiguously or fail-closed.
- **FAIL-CLOSED-ON-AMBIGUITY**: any low-confidence/ambiguous extraction (OCR/PDF/CSV mis-parse, multiple amounts/payees,
  currency mismatch, unparseable attachment) → NOT auto-included; parked for human resolution. Uncertainty defaults to NO pay.
- **DRY-RUN-DEFAULT-AUDITABLE-LIVE-SWITCH**: DRY_RUN default true; live needs a distinct logged enable flag AND a
  write-scoped Investec cred the repo does NOT hold (investec_client.py:1-4 read-only). Absent cred → dry-run only; in
  dry-run never call a write endpoint.
- **AUDIT-TRAIL-NON-REPUDIATION**: append-only timestamped record of every step (matched, extracted+confidence, approval
  sent, token issued, reply received, token verified/rejected, caps/allowlist outcome, dry/live, exec result). Never log
  full account numbers/PAN or the HMAC secret.
- **LAST3-IS-WEAK-SELECTOR**: last-3 digits = ~10^3 guessable, attacker-controllable field → coarse filter ONLY, never
  authentication; a non-guessable secondary check (payment-bound token) required before execution.
- **POPIA-LAWFUL-MINIMISATION** (s8 accountability, s9-11 lawful/consent, s13-14 purpose/retention): inbox scan + parsing
  = PI processing; lawful basis = account-holder consent (self-owned inbox); store only fields needed; define retention/cleanup.
- **POPIA-THIRD-PARTY-SENDER-CONTACT** (s9-11,s13,s69): sender is a third-party data subject; progress emails only when
  contact explicitly present; minimise content (no beneficiary IDs/balances/caps/token leaked).
- **POPIA-CLOUD-OCR-CROSS-BORDER-EGRESS** (s8,s20-21 operator,s72 cross-border): cloud OCR egresses financial PII abroad;
  with no tesseract binary the only OCR path is cloud → image OCR OUT OF SCOPE / graceful-skip by default.

## ADVISORY
- **EMAILER zero-attachment guard** (emailer.py:50 raises with no attachments): attachment-less approval/progress emails
  will raise → new isolated send path OR attach a summary file; do NOT change send_email() behaviour for frozen callers.

## Goal-vs-HARD-constraint BREACHES flagged (the goal is only safe once these are constrained)
1. Paying an arbitrary parsed account ⇒ breaches ALLOWLIST-EXACT + FAIL-CLOSED → must resolve to exact beneficiaryId or skip.
2. Approval = plaintext "approve"/"app" or reply-from-user ⇒ breaches AUTH-INBOUND + TOKEN-BINDING → must be valid bound HMAC token.
3. Live "approved" execution ⇒ impossible today (read-only cred, no payment endpoint) → scope to dry-run default; live is gated+credential-dependent.
4. Image OCR ⇒ only via cloud egress ⇒ breaches POPIA-CLOUD-OCR → descope to graceful-skip.
5. Emailing sender ⇒ third-party disclosure ⇒ only if contact present + minimal status content.

## Watch items
- Zero existing tests for inbound/parse/token/payment — new module needs its own tests; don't lean on the 62.
- Append-later flow: one token must not approve a payment it wasn't bound to.
- Daily-aggregate state must persist across cron runs. File-JSON (no schema change) vs spec-permitted 0009 migration = open.
- APPROVAL_SIGNING_SECRET env-only, never logged/emailed; constant-time verify.
- Don't log full account numbers; store last-3 or hash (db.py:127 habit).
- Verify Investec payment endpoint/scope before speccing live — all payment-API facts are UNVERIFIED assumptions.

confidence: high on in-repo security/idempotency authorities; medium on exact POPIA section mapping; low on Investec payment-API specifics.
