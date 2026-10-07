# build-s3a: email-payment-v2, iteration 3, slice 3 STEP A (S9 then S11)

Branch claude/clever-babbage-kyspbq, start e84b4e5 ("slice 3 (step A) build approved"). Not pushed. uv.lock restored after every run, never committed. S12/S13/S14 NOT built (step B).
Live stays OFF, dry-run is the default everywhere, every test uses fakes (inbox, SMTP, Investec client, image extractor); nothing sends mail or calls an API.

## Stages, commits, test counts
Red = xfail-marked commit (suite green), green = marker removed. "3.11" = local `uv run pytest -q` (CPython 3.11.15). Baseline entering the step: 1550 passed / 12 skipped.

| Stage | Commit | 3.11 suite after (passed / skipped / xfailed) |
|---|---|---|
| item 16 test edit (own commit, 2 assertions only) | 9c4ae34 | 1550 / 12 |
| S9 red (migration, store, SQL-shape, state-table tests) | a9e417a | 1550 / 14 / 140 xfailed + 2 xpassed |
| S9 green (0013 + instructions.py + pg_instructions.py + pinned hashes) | f4e5c8d | 1692 / 14 |
| S11 red (harness + 10 test files) | 9466920 | 1692 / 14 / 338 xfailed + 2 xpassed |
| S11 green (money path) | 58cd100 | 2033 / 15 |
| final fix commit (audit received/auth steps, age window 0 for approvals, report) | see git log | 2034 / 15 |

Final: 3.11 `uv run pytest -q` = 2034 passed, 15 skipped (the Pg-gated tests skip), 0 failed. With a scratch Postgres 16 (INVESPEND_TEST_DATABASE_URL) = 2123 passed, 11 skipped. Scratch CPython 3.12.3 venv outside the repo (editable install, CI=true) = 2045 passed, 4 skipped; the Pg-gated subset on 3.12 against the scratch cluster = 200 passed.

## Scratch Postgres
initdb under /usr/lib/postgresql/16/bin (trust auth, port 54329). The postgres OS user could not traverse the session scratchpad dir, so the cluster lived in /tmp/s3a_pg (outside the repo, deleted at the end). Results: migration 0013 applies after 0001-0012 on a fresh database; `test_0013_init_db_twice_idempotent` (policy count stays one per table, index count two, RLS on for all four) and `test_0013_second_init_db_takes_no_share_or_stronger_lock_on_the_four_tables` (a second session holds ROW EXCLUSIVE on the four tables, 0013 re-run under a 2s lock_timeout completes) pass; the whole InstructionStore suite, the state-table property walk (all 12x12 pairs), the two-thread CAS tests (claim x2, cancel vs claim, approve vs cancel), the post-approval CHECK (psycopg `CheckViolation`) and a full v2 lifecycle through the real `PgInstructionStore` (offer, approve, execute, stale submitting -> needs_review, reload) pass. No test depends on Postgres: all Pg tests are gated on the env var.

## Files (new unless marked)
* S9: `db/migrations/0013_payment_instructions.sql` (the ONLY new migration; 4 tables 37/5/5/3 columns, table CHECKs incl. batch all-or-none and post-approval statuses need approved_at AND batch_ref, status CHECK == TRANSITIONS keys, RLS + deny_all via guarded DO blocks, guarded indexes, no unconditional ALTER/CREATE INDEX); `payments/instructions.py` (TRANSITIONS, INITIAL_STATUSES, POST_APPROVAL_STATUSES, DUPLICATE_GUARD_STATUSES, FINALIZE_RELEASES, COLUMN_OWNERS (50 keys), `offer_digest`, ClaimResult/ApproveResult/BeneficiaryObservation, MemoryInstructionStore, lazy `instructions.PgInstructionStore` re-export); `payments/pg_instructions.py`; tests `test_payment_v2_migration.py`, `test_payment_instruction_store.py`, `test_payment_instruction_store_sql.py` (fake-connection SQL shape for EVERY mutating Pg method + rollback-on-exception for all 20), `test_payment_state_table.py`, helper `tests/instr_helpers.py`, fixture `tests/fixtures/migration_hashes_v2.json` (pins 0012 and 0013). Edited existing test file: `tests/test_beneficiary_migration.py`, exactly the two assertions (own commit 9c4ae34).
* S11: `payments/routing.py`, `execute.py`, `approval.py`, `batch.py`, `cycle.py`, `mode.py`, `v2_cli.py`, plus small helpers `fingerprints.py` and `notices.py`; additive edits: `config.py` (new fields + parsers only), `payments/audit.py` (key `ref` only), `cli.py` (ONE inserted branch, added lines only; difflib test vs the S1 base copy passes). Tests: `test_payment_v2_{routing,config,batch,approval,execute,never_unapproved,cycle,security,preflight,pg_cycle}.py`, `test_payment_state_table_cycle.py`, harness `tests/v2_harness.py`.
* Untouched (sha256 pins green): pipeline, token, inbox, notify, dedup, selftest, extract, store, pg_store. `git diff --name-status e84b4e5 HEAD` shows only additions plus M for cli.py, config.py, audit.py, tests/test_beneficiary_migration.py. `.github/`, `pyproject.toml`, uv.lock untouched; no new dependency.

## Mutation spot-checks (done, then reverted)
Each of these made at least one test fail (killed): snapshot taken after message handling, approve not age-gated, offer_digest check removed, authentication skipped, notify_to filter removed, needs_review reservation released, every post-claim exception treated as a rejection, loop guard removed, age gate removed, cancel made age-gated, unoffer on any send error, daily cap not checked at claim.

## Readings chosen / deviations (safer reading where the plan was silent; reason given)
1. **Module `payments/fingerprints.py` and `payments/notices.py` added** (not in the plan list): `beneficiaries.py` is not on the additive-edit list, so the HMAC helpers (`beneficiary_fingerprint`, `account_hmac`, `raw_by_id`) are a new module; `notices.safe_send` keeps a lost notice from breaking an already-committed state change (audited by class name).
2. **`PgInstructionStore` lives in `pg_instructions.py`** as the task says; `instructions.PgInstructionStore` is a lazy PEP 562 re-export so the TR3-9 monkeypatch seam and call-time lookup work without a circular import.
3. **Public `daily_total(when)`** added to both stores (read-only, SAST day) for tests; `StoreNotInitialised` raised by `PgInstructionStore.ping()` when a 0013 table is missing.
4. **`claim_for_execution` raises ValueError when the passed amount differs from the stored row** (the reservation must equal what finalize releases from RETURNING).
5. **`finalize` stores `outcome_message` only for failed / needs_authorisation** (sanitised again in the store); ignored otherwise (plan: null otherwise).
6. **`approve_item` reasons**: a row already in status `expired` answers `not_awaiting` (literal plan); the handler maps the row status to the acknowledgement word (`expired`, `already_executed`, ...). Cancelling an executed/submitting item is acknowledged `too_late` (plan); re-approving an executed item `already_executed`.
7. **`list_unnotified_batches` only returns batches with still-awaiting, unexpired rows** (no resend spam for expired batches); `set_held` also requires `expires_at > now`.
8. **`observe_beneficiary` + `mark_bootstrap_done` are separate transactions** (the store protocol has no bulk call); a crash in between leaves the marker absent and the next run re-observes with `established=True`, which is idempotent.
9. **Batch numbering ties**: rows are numbered by `received_at, instruction_id` (hash), so same-receipt-time items get an arbitrary but stable order; tests that need an order give increasing receipt times.
10. **Dispatch**: the inserted branch is `payments_mode(settings) != "legacy"` -> `run_v2`; `run_v2` answers `known modes` only and returns the rc 2 envelope for an unknown value, before any fetch (plan wrote `== "v2"` but also requires the unknown-mode envelope).
11. **Reconcile details** (plan silent): a missing image currency is never assumed ZAR but is tolerated when another candidate supplies ZAR; an attachment extractor `needs_review` with reason other than "no amount found"/"empty body" stops the instruction (`parked_attachment_ambiguous`), those two reasons are ignored; two different account numbers (text vs image) -> `parked_account_conflict`; payee names compared case/space-insensitively; no payee -> `none` (`parked_no_payee`), no amount -> `none` (`parked_no_amount`).
12. **Fail-closed extras**: approve with window 0 means nothing is fresh (also a future-dated reply); `fingerprint_key_missing` parks at creation for registered/held routes, pre-offer and pre-POST; `account_hmac` is computed only when the key is set.
13. **Notice sweep is partial**: the hold notice (stamp `hold`), the unnotified-batch resend and the stuck-message `resend` are swept; the paste email is NOT rebuilt by the sweep because the mail body is deliberately never stored (R6-T7 was optional). A crash between create and the paste send leaves the user without the paste email; the stuck-message resend notice (message row still `processing`) covers that case only when the crash happened before the outcome update.
14. **`test_message1_raises_...`** asserts message 1 ends with outcome `error` (the handler sets it) and message 2's approval is processed; "reported by the stuck sweep" applies to rows left `processing` (process crash), covered by `test_stuck_processing_message_gets_resend_notice_only_if_authenticated_and_once`.
15. **Audit step names** emitted: cycle_start, message_received, auth_ok, auth_failed, own_notification, ignored, trigger_ignored, typed_text_unsplittable, expired_age, instruction_created, instruction_exists, parked, held, batch_offered, batch_send_failed, batch_send_unknown, offer_deferred_list_unavailable, approve, cancel, approval_noop, approval_predates_batch, cancel_from_raw_first_line, notice_suppressed (count = running suppressed total), execute, live_execute, executed, failed, needs_review, needs_authorisation, expired, claim_error, claim_cas_lost, execute_cas_lost, execute_refused_not_approved, finalize_error, stale_submitting, image_extra_fields_dropped, image_skipped, message_error, no_message_id, preflight_failed, hold_setting_fallback, retention_too_short, resend_notice, cycle_end. Only allowlisted detail keys are used (`ref` added). A 12-hex instruction ref containing a 6+ digit run is audited with that run redacted (R7-T10, documented, tested).
16. **New env names** (S13 docs follow in step B): PAYMENTS_MODE, PAYMENTS_ALLOWED_SENDERS, PAYMENTS_NOTIFY_RECIPIENTS, PAYMENTS_AUTHSERV_ID(S), PAYMENTS_MAX_MESSAGE_AGE_HOURS, PAYMENTS_APPROVAL_EXPIRY_HOURS, PAYMENTS_APPROVED_GRACE_HOURS, PAYMENTS_HELD_EXPIRY_HOURS, PAYMENTS_AWAITING_EXPIRY_DAYS, PAYMENTS_MAX_BATCH_ITEMS, PAYMENTS_SUBMITTING_STALE_MINUTES, PAYMENTS_STUCK_MESSAGE_MINUTES, PAYMENTS_DUPLICATE_WINDOW_DAYS, PAYMENTS_FINGERPRINT_KEY, PAYMENTS_HOLD_REGISTERED_RECENT, PAYMENTS_HOLD_HOURS, IMAGE_EXTRACTOR_MODEL, ANTHROPIC_API_KEY, PAYMENTS_MAX_IMAGE_BYTES, IMAGE_EXTRACTOR_RETRIES. Secret fields use `repr=False`. `.env.example` is NOT edited here (S13).
17. **F10 grep test** is an allowlist of files, intentionally loose.
18. **Test-suite note**: `tests/test_payment_v2_preflight.py` patches `cli.Settings.load` (not `config.Settings.load`) because some existing tests reload `config`, which would otherwise leave `cli.Settings` pointing at the old class when the whole suite runs.

## Not done / open
* S12 workflow, S13 docs/runbook/.env.example, S14 Claude vision adapter (step B). `images.get_extractor` still returns the Null extractor unless key AND model are set and `images_claude` exists (ImportError -> Null with note `image_adapter_unavailable`).
* No CI postgres service (separate approval); the Pg-gated tests only run locally with INVESPEND_TEST_DATABASE_URL.
* G1/G2 items from the gates checklist stay open (real Gmail Authentication-Results shape, Investec rejection body field names, per-payment limit).

## Fix round (RS3A-1..7 + tester note), branch claude/clever-babbage-kyspbq
Red commit (xfail strict, suite green) then per-item green commits; new tests in `tests/test_payment_v2_s3a_fix.py`. Not pushed; uv.lock restored.

Changes
1. RS3A-1 (HIGH) `outcome.py`: blank/whitespace/None data- and entry-level `ErrorMessage` is "no error"; `failed` only for a non-empty message, a positive entry status, or a 4xx `PaymentRejected`. New keyword `parse_payment_response(..., unrecognised_is_unknown=False)`; `execute.py` passes True, so an unrecognised 200 (no data object / no entries / `{}` / non-dict) is `unknown` (reason `unrecognised_shape`) -> `needs_review`, reservation kept, never resent, duplicate guard blocks re-instruction (needs_review is in DUPLICATE_GUARD_STATUSES, verified end to end), reruns make 1 POST. Entry without reference stays `unknown`; AuthorisationRequired unchanged.
2. RS3A-2 `amounts.has_foreign_currency_token` (USD/EUR/GBP/AUD/CAD/NZD/CHF/JPY/CNY, dollar(s)/euro(s)/pound(s), $ euro pound yen; case-insensitive, fails closed over 20,000 chars); `cycle._candidates` adds a FOREIGN currency candidate per text region so `reconcile` parks `currency_conflict`; a lone `$100` (no marked amount) is also reported `currency_conflict`. `R100 per month` still R100.
3. RS3A-3 `sanitize_provider_message` also redacts `Bearer|Basic <v>`, `key|token|secret|password|passphrase|authorization [:=] <v>` (keyword kept, value redacted, as existing tests require) and `sk-|pk-|tok_|key-|secret-|api-` token-likes; linear, tested with hostile inputs.
4. RS3A-4 `config._hold_hours` and `mode.v2_settings`: > 24*365 (also non-finite/negative/unparsable) -> 24; unset/blank env still 0; cycle no longer errors on 1e308.
5. RS3A-5 `cycle._normalised_mailbox`: strip whitespace, quotes, trailing `.`/`/`; reject `""`/`inbox` (so `INBOX.`, `INBOX/`, `"INBOX"`) with `mailbox_not_dedicated` before any fetch. DECISION: only exact inbox spellings are rejected; `INBOX.Payments`, `Payments`, `[Gmail]/Payments` stay allowed (a dedicated sub-label is legitimate; rejecting every `inbox*` prefix would also block it).
6. RS3A-6 an empty beneficiary list on a successful fetch is treated like `beneficiary_list_unavailable` (instruction parks, offers deferred, no paste email). DEVIATION: bootstrap is STILL marked done on the empty list, because the existing, unmodifiable test `test_bootstrap_with_empty_list_then_first_added_beneficiary_is_recent_held_when_hold_nonzero` asserts `bootstrap_done()` after an empty list.
7. RS3A-7 `v2_cli._SECRET_NAME` adds `pass`, `imap_user`, `smtp_user`.
8. Early caps: `mode.caps_configured` (per-payment cap > 0 and finite). Cycle parks a new instruction `caps_not_configured` (one problem email) before the duplicate guard, and `batch.offer_batches` parks any still-awaiting row `caps_not_configured` before offering. DEVIATION / BLOCKED: the DAILY cap could not be moved early, because the existing test `test_payment_v2_execute.py::test_caps_unset_or_zero_block_everything[daily_aggregate_cap]` pins "daily unset -> offered, approved, then parked `daily_cap` at claim". Still fail closed (0 POSTs, tested). A strict-xfail test `test_daily_cap_unset_parks_at_offer_time_BLOCKED` records the wish; unblock by editing that one test's else-branch and dropping the xfail, then add the daily check to `caps_configured`.

Counts (3.11 `uv run pytest -q` / scratch CPython 3.12 CI=true / with scratch Postgres 16): 2160 passed 15 skipped 2 xfailed / 2171 passed 4 skipped 2 xfailed / 3.11+PG 2249 passed 11 skipped 2 xfailed, 3.12+PG 2260 passed 0 skipped 2 xfailed. The 2 xfailed are the intentional BLOCKED daily-cap cases. Scratch venv and cluster deleted.
Note: `tests/test_payment_v2_cycle.py::test_all_captured_emails_have_no_token_secret_or_full_account_number_except_paste` is intermittently red (~1 run in 8, also on the unmodified code via stash): a `\d{9,}` match on a random value in an email; pre-existing, not touched.

## Fix round part 2
1. Daily cap early: `mode.caps_configured` now requires per-payment AND daily cap > 0 (finite); cycle/offer park `caps_not_configured` (one owner notice, nothing offered). Run-created `test_caps_unset_or_zero_block_everything[daily_aggregate_cap]` edited to the new fail-closed expectation; BLOCKED xfail converted to a passing test (`test_daily_cap_unset_parks_at_offer_time`); claim-time daily re-check kept and tested by removing the cap after the offer.
2. `outcome.parse_payment_response`: `unrecognised_is_unknown` keyword removed; unrecognised 200 shape is always `unknown` (`unrecognised_shape`); `execute.py` call and `test_parse_missing_transferresponses_*` updated (needs_review, reservation kept, duplicate guard, 1 POST all still tested).
3. Flake ROOT CAUSE: not Message-ID/Date/boundary. The email body carries the subject-tag `[INV-<12 random hex>]` (refs.py instruction ref, hash-derived); 12 hex chars contain 9+ consecutive decimal digits by chance (~1 in 8). Test now masks `[INV-<12hex>]` before the `\d{9,}` scan; the real assertions (no account number/token/secret outside the paste email) are unchanged. 200/200 runs green; the other digit-run tests (cycle no_digit_runs, execute no_9plus, store row) 150/150 green each (they use lookbehind or fixed refs); notify tests use fixed REF.
4. RS3A-6 unchanged.
Counts: 3.11 2162 passed 15 skipped (5 consecutive runs identical, no flakes); 3.11+PG 2251 passed 11 skipped; scratch 3.12 CI=true+PG16 2262 passed 0 skipped 0 xfailed. Scratch venv/cluster deleted. Red was confirmed locally (15 failures) before the code change; items 1+2 committed together with their tests so every commit is green.

## Fix round 3 (RS3AF-1..7 and the review blocker): allow-list outcome classifier
Commits: red (tests xfail) then one green commit. New tests: `tests/test_payment_v2_s3a_fix3.py` (table driven, end to end through `tests/v2_harness.py` in simulated live mode, timing tests for every touched regex).

Final outcome rules (`payments/outcome.py::parse_payment_response`; Status wording is NEVER used to infer authorisation or failure):
| # | Condition | Outcome | reason | execute.py mapping |
|---|---|---|---|---|
| 1 | body or `data` not a dict | unknown | unrecognised_shape | needs_review, reservation kept |
| 2 | AuthorisationRequired true (bool True or "true", any case) at data or entry level | needs_authorisation | authorisation_required | needs_authorisation, reservation KEPT (finalize release=False; payment may be authorised later online) |
| 3 | >= 1 reference and an error (data or entry) | unknown | ref_and_error | needs_review |
| 4 | >= 1 reference, several entries and not all (dict) entries have one | unknown | mixed_entries | needs_review |
| 5 | >= 1 reference, an entry Status EQUAL (strip/casefold) to failed/declined/rejected/unsuccessful/unsuccessful payment | unknown | status_conflict | needs_review |
| 6 | >= 1 reference, none of the above | success | ok | executed |
| 7 | no reference, non-placeholder STRING ErrorMessage | failed | error_message | failed, reservation released, message sanitised (re-instruction allowed) |
| 8 | no reference, anything else | unknown | no_reference / unrecognised_shape | needs_review |
Reference = non-empty STRING PaymentReferenceNumber after strip, not a placeholder (none/null/n/a/na/nil/0/false/-/ok). Error = non-empty STRING ErrorMessage after strip, not a placeholder (none/null/n/a/na/nil/0/false/true/ok/no error/no errors/success/successful/-); non-string ErrorMessage values are ignored. 4xx PaymentRejected stays the only other `failed`. Duplicate guard unchanged (needs_review, needs_authorisation, submitting, executed guarded). STATE TABLE: `FINALIZE_RELEASES["needs_authorisation"]` is now False (instructions.py, plan.md amendment, run-created tests updated: store release-mismatch, state-table (j), execute finalize-flag table, needs_authorisation reservation test).

Other items
* RS3AF-3 `amounts.has_foreign_currency_token`: NFKC fold, format/zero-width characters dropped, whole-word tokens (US, USD, USDT, USDC, BTC, ETH, JPY, yen, CNY, rmb, yuan, INR, rupee(s), MXN, peso(s), dolar(es), dolar with accent, dollar(s), euro(s), pound(s), GBP, EUR, AUD, CAD, NZD, CHF, SGD, HKD, AED, symbols $ euro pound yen rupee) plus runs of single letters split by up to 3 separators (space, tab, . - _ /), consumed once (linear). `R100 per month` stays R100.
* RS3AF-4 `sanitize_provider_message(text, secrets=())`: NFKC fold, exact configured secrets (any case, optional whitespace between characters), `[a-z_]{0,20}(key|token|secret|passw(or)?d|pwd|authorization|bearer|passphrase)` + value (underscore aware), JWT, AKIA, ghp_/gho_/sk-/tok_ style tokens; every quantifier bounded, input cut to 2000 first. execute.py passes the settings' secret values (`v2_cli.secret_values`) into the parse and into `_failed`.
* RS3AF-5 `v2_cli.scrub_message`: case-insensitive, whitespace-tolerant value matching plus the shared bearer/secret-word rules (`outcome.redact_secret_words`).
* RS3AF-6 `cycle._normalised_mailbox`/`_is_shared_inbox`: NFKC, Cf characters dropped, trailing `. / :` stripped; refuses "", INBOX, `INBOX:`, `INBOX.INBOX`, `INBOX/INBOX`; sub-labels (`INBOX.Payments`, `INBOX.INBOX.Payments`) allowed.
* RS3AF-7 NO CHANGE (reasoned): bootstrap stays marked done on an empty first list. Plan rule TA: persisting the marker is what stops the first beneficiary added LATER from being treated as an already-established payee (it must count as new and be held). Not marking it would make that first payee established the moment it appears; the parked instruction already fails closed.

Deviations / readings
1. Red commit used a module-level non-strict xfail (a per-test strict marker was not practical for ~300 parametrised cases); the mark was removed in the green commit.
2. `US` (the bare code) is matched UPPER-CASE only so the pronoun "us" does not park ordinary text; lower-case "usd", "dollars" etc. are still caught.
3. A non-dict item inside TransferResponses next to a real entry counts as `mixed_entries` (unknown), stricter than "ignore non-dict items" and in the fail-toward-needs_review direction.
4. needs_authorisation keeps the sanitised entry Status text as `outcome_message` (display only).
5. Old run-created tests updated to the new rules, intent kept: test_payment_fix_outcome (failure status/err with reference now unknown, wording ignored), test_payment_client_hardening (authorisation beats error), test_payment_v2_s3a_fix (failed cases use a no-reference body), store/state-table/execute release-flag assertions for needs_authorisation. No test existing at 2eee10c was edited.

Counts (fix round 3): 3.11 `uv run pytest -q` 2465 passed 15 skipped; 3.11 + scratch Postgres 16 2554 passed 11 skipped; scratch CPython 3.12.3 venv outside the repo, CI=true, + scratch Postgres 16 (new empty /tmp dir): 2565 passed 0 skipped, three consecutive runs identical. Venv and cluster deleted; uv.lock restored, not committed.

## Fix round 4 (RS3AF3-1..5): final tightening of the money-path classifier and gates
Commits: red (`tests/test_payment_v2_s3a_fix4.py`, module-level xfail) then one green commit (xfail removed). Principle: only a DEFINITE failure is 'failed'; every ambiguous 200 is 'unknown' (needs_review, reservation kept); the Investec 200 shape is unverified until G2.

Final outcome rules (`payments/outcome.py::parse_payment_response`):
| # | Condition | Outcome | reason |
|---|---|---|---|
| 1 | body or `data` not a dict | unknown | unrecognised_shape |
| 2 | AuthorisationRequired true / "true" at data or entry level | needs_authorisation (reservation kept) | authorisation_required |
| 3 | any key containing `authori` (any depth) with a value not clearly false, or body > 200k nodes / depth > 64 | unknown | authorisation_unclear / body_too_large |
| 4 | >= 1 strict reference (non-placeholder STRING entry `PaymentReferenceNumber`) + error | unknown | ref_and_error |
| 5 | strict reference, several entries and not all with one / Status equal failed-like | unknown | mixed_entries / status_conflict |
| 6 | strict reference, nothing else | success | ok |
| 7 | no strict reference, but reference-like content anywhere (any key whose NFKC/Cf-folded name contains `ref`, any depth, value of ANY type: non-blank non-placeholder text, nonzero number, non-empty list/dict) | unknown | ref_and_error / reference_unrecognised |
| 8 | no reference-like content, non-placeholder error WITHOUT success-sounding words (success*, processed, complete(d), approved, accepted, paid, done, ok, no error(s), without error(s)) | failed (released, sanitised) | error_message |
| 9 | same with a success-sounding error | unknown | error_sounds_like_success |
| 10 | anything else | unknown | no_reference / unrecognised_shape |
Placeholder compare folds NFKC, Cf (zero-width/joiners/BOM/RTL), casefold and leading/trailing punctuation ("N/A.", "OK!", zero-width-only and punctuation-only are NOT references). Walk is iterative, node/depth capped (linear).

Other items
* RS3AF3-2: `scrub_message` cuts to 5000 chars before redaction; `redact_secret_words` cuts to 20,000; JWT pattern anchored at a token boundary (one start per run, linear). `eyJ`*70000 and `_`*200000 finish well under 2s in scrub_message, redact_secret_words and sanitize_provider_message.
* RS3AF3-3 `amounts.py`: `ISO_4217_CODES` data (all active codes except ZAR + USDT/USDC/BTC/ETH/RMB). Policy: any Unicode `Sc` symbol parks; curated codes/words (kroner, krona, reais, rouble/ruble, dirham, riyal, shekel, baht, ringgit, rupiah, zloty, forint, koruna, dinar, naira, shilling ...) whole-word case-insensitive; other ISO codes only as UPPER-CASE standalone tokens or glued to a digit (lower-case glued, `100sek`); ambiguous words (real, won, franc(s), lira ...) only directly after a number; letter runs split by up to 3 separators of any kind incl. newlines ("U\nS\nD", "E-U-R"; curated substring, other codes exact); NFKC + Cf drop + Cyrillic/Greek look-alike fold. Lower-case ordinary words (try, all, top, sun) and non-code capitals (SUN CO) do not park.
* RS3AF3-4: scrub_message NFKC + Cf prefold; secret keywords may be split by up to 2 whitespace chars (`ke\ny=ZZ`), optional plural `s`; up to three filler tokens (`:`, `=`, is, was, are, equals) skipped; vendor prefixes xox[abprs]-, xapp-, rk_/sk_ live/test, whsec_, glpat-, npm_, github_pat_, ASIA[0-9A-Z]{16}, SG.x.y; configured secret values fold Cf too.
* RS3AF3-5 `cycle._is_shared_inbox`: refuses backslash, `*`, `%` anywhere; modified UTF-7 (`&AEkATgBCAE8AWA-`) decoded before the inbox comparison. Sub-labels (Payments, INBOX.Payments, [Gmail]/Payments) and other UTF-7 names stay allowed.

Deviations / readings
1. Run-created fix3 tests edited (none existed at 2eee10c): int reference + error now unknown (was failed); AuthorisationRequired 1/"yes"/"True!" beside a reference now unknown (was success) because an unclear flag is ambiguous; placeholder-int reason accepts reference_unrecognised.
2. Strict reference (success path) stays narrow (entry-level `PaymentReferenceNumber` string); the broad scan only ever blocks 'failed'/'success', never creates success.
3. A whitespace/punctuation-only ErrorMessage longer than 5000 chars is ignored (never a definite failure); a reference-like value that long folds to "present".
4. scrub_message output is now NFKC-folded (full-width chars appear as ASCII) and truncated at 5000 chars.
5. Documented false positives of the currency gate (only park): all-capitals words that are codes (TRY, ALL, TOP, BOB), `100 real`, `R100 Franc ...`.

Counts (fix round 4): 3.11 `uv run pytest -q` 3255 passed 15 skipped (twice); 3.11 + scratch Postgres 16 (new empty /tmp dir) 3344 passed 11 skipped x3; scratch CPython 3.12 venv outside the repo, CI=true, + Postgres 3355 passed 0 skipped x3. Cluster and venv deleted; uv.lock restored, not committed.
