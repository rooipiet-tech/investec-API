# Research: Playwright beneficiary helper (run: beneficiary-helper)
Read-only mapping. Nothing installed or modified except this file.

## 1. How the unregistered-payee notification is produced
Flow (all in src/invespend/payments/, v2 mode):
- cycle.py:240 `bankdetails.extract_bank_details(details_text)` parses labelled lines from the sender's email body/attachment text.
- cycle.py:241-246 account number cross-checked vs image-read account (`account_conflict` park); image fallback fills number/bank/reference (cycle.py:244-247).
- cycle.py:250 `routing.beneficiary_path(rec.payee, ctx.beneficiaries)` decides registered vs new_payee; routing.route -> `route.kind == "new_payee"` (cycle.py:~296).
- cycle.py:296-306: row created with status `awaiting_beneficiary` (expires `awaiting_expiry`), then `BankDetails` rebuilt as `shown` (payee_name = details.payee_name or rec.payee; bank = details.bank or image_bank; account_number; branch_code; reference = their_reference) and `notify_v2.build_paste_details_email(...)` is sent to `auth_from` (the verified sender, must be in cfg.allowed_senders, cycle.py:253). `store.mark_notified(iid,"paste",when)` sets `paste_notified_at` (instructions.py:59,68).
- notify_v2.py:143-172 `build_paste_details_email`. Subject `Add this beneficiary in Investec [INV-<ref>]` (notify_v2.py:172, `_inv` at :133). Body = loop-guard sentinel first line + lines. This is the ONLY email carrying the full account number (module docstring notify_v2.py:5-6). Values via `raw()` = `_clean(mask=False, pipes=False)` (strip control chars, collapse whitespace, cap 100 chars, "(not provided)" if empty; notify_v2.py:80-88).
- Exact body text (notify_v2.py:162-170): "This payee is not in your Investec beneficiary list and the API cannot create beneficiaries." / "Add the beneficiary in Investec Online or the mobile app, entering these fields in this order:" / blank / 9 lines `Name: value` / blank / `Branch code (reference only, not one of the fields above): <v>` / blank / "Once the payee appears ... Nothing has been paid."
- After the user adds the payee in Investec, the beneficiary appears in the API list; a later cycle observes it, offers a batch for approval (`build_beneficiary_observed_email`, notify_v2.py:250). Hold: PAYMENTS_HOLD_HOURS; runbook notes community FAQ says a beneficiary may need one payment in Investec Online first (docs/PAYMENTS_RUNBOOK.md:92-94).
- Legacy notify.py (88 lines) is the token approval email (build_approval_email/progress); unrelated to beneficiaries, and legacy flow is retired (tests/test_payment_legacy_retired.py).

## 2. Data shape of beneficiary details
- `BankDetails` frozen dataclass (bankdetails.py:35-40): payee_name, bank, account_number, branch_code, reference — all `str | None`.
- Parsing (bankdetails.py:16-33, 62-77): labelled lines only, regex fullmatch per line; `MAX_CHARS=20_000`, `MAX_VALUE=100`; account digits only after stripping space/hyphen, 6-20 digits (:49-52); branch code 4-8 digits (:54-56); conflicting duplicate labels -> None; control chars rejected; oversize input -> all None.
- Presentation order contract: `BENEFICIARY_FIELD_ORDER` (notify_v2.py:30-33) = Beneficiary name, Bank, Account number, Amount, Their reference, My reference, Payment notification, Beneficiary email address, Beneficiary mobile number. `REFERENCE_ONLY_FIELDS=("Branch code",)` (:34) rendered after the list. Asymmetry: the email calls build with payment_notification/email/mobile defaulting to "" (cycle.py:300 passes none) so these render "(not provided)"; my_reference = their_reference.
- Not in the email: currency (always ZAR, cycle.py base), source account last3 (in row only).
- beneficiaries.py: `Beneficiary` frozen dataclass (:14-28): beneficiary_id, name, match_names, account_last3, email (lowercased). `from_api` (:36-58) maps Investec dict keys beneficiaryId, beneficiaryName|name, accountNumber, emailAddress. `ALLOWLIST` is empty in code (:31). `resolve_beneficiary_strict` exact name/email (:96-113). This is for registered payees (read side), not the creation input.
- extract.py (199 lines): `ExtractResult(status, amount, currency, payee, reason)`; amount/payee extraction from text/csv/xlsx/pdf, images skipped (fail closed). Provides payee+amount only, not bank details (bankdetails.py does that). Note cycle.py also uses content.py/images_claude.py for the live path (image_extras = bank, reference).
- Persistence: instruction row stores only `account_hmac` (HMAC fingerprint), payee_name_norm, references; NEVER the full account number (test_payment_v2_cycle.py:255-261 asserts `"5550001111" not in str(row)`). So the ONLY durable source of full details is the paste email in the user's mailbox. Implication for the helper: input must come from the user (paste/stdin/the email text), not from the DB.

## 3. CLI surface (src/invespend/cli.py, 491 lines)
Subcommands built at cli.py:409-481: init-db; ingest (--days --from --to --resume --full); report (--send); statements (--send --days); backup; backfill-hashes; approve-payments (--dry-run/--live mutex, --once; v2 branch via payments/v2_cli.py run_v2, exit 0/2); pay-selftest (--beneficiary --amount --source --reference). Entry: `invespend = invespend.cli:main` (pyproject). Parser is argparse, `required=True` subparsers, each `set_defaults(func=...)`. A new subcommand would be added by one `sub.add_parser` + func; the frozen-surface test (tests/test_payment_frozen_surface.py, tests/test_cli.py) will need checking: any new subcommand changes the observable surface, so the spec must explicitly allow it (or ship as separate module/`python -m` entry / extra console script to avoid touching cli.py).
- Mind CLAUDE.md lists 6 subcommands as "observable surface" but cli.py already has 8.

## 4. Deps / packaging (pyproject.toml)
setuptools>=68; requires-python >=3.10; deps: requests, psycopg[binary], pandas, openpyxl, python-dotenv, pypdf<6. Optional `dev = [pytest>=7.4]`. packages.find where=src. No playwright anywhere in code. Natural fit: new optional extra (e.g. `browser = ["playwright>=1.x"]`) so core/CI installs stay unchanged; browser binaries need separate `playwright install chromium` (not a pip dep). Imports must be lazy (as pypdf is in extract.py) so tests/CI work without it. Python 3.12 pinned in all workflows; no Dockerfile checked.

## 5. CI workflows (.github/workflows/)
tests.yml: ubuntu-latest, py3.12, `pip install -e ".[dev]"`, `pytest -q`, on PR/push main. backfill, export-groups, ingest, weekly-report, payments-cycle (cron every 15 min, gated on repo var PAYMENTS_CYCLE_ENABLED=='true', environment `payments`, PAYMENTS_MODE=v2, PAYMENTS_STATE_BACKEND=postgres, timeout 10m, permissions contents: read). Helper is interactive/local: must NOT be added to any scheduled workflow; headed browser cannot run on GH runners. CI should only run unit tests with playwright mocked/absent (do not add `playwright install` to tests.yml).

## 6. Secrets handling
- .env git-ignored (.gitignore: `.env`, `*.env`, `!.env.example`); .env.example documents placeholders only, grouped by feature (payments section is last, mostly blank/placeholder, DRY-RUN default).
- config.py (361 lines): `Settings.load()` at :276 reads env via `_opt/_require/_opt_bool` helpers; secrets via write trio (:239-270). Helper needs NO Investec/bank credentials; ideally needs no Settings at all (read details from stdin/file/clipboard), avoiding `.env` loading entirely. Banking login/2FA stays manual in a headed browser; use a fresh non-persistent context (no storage_state/cookies/trace/video/screenshots/HAR written) since those could capture session tokens and account numbers.
- Scrubbing precedent: v2_cli.scrub_message/secret_values (v2_cli.py:16-60) redact settings secrets and 6+ digit runs in error envelopes; outcome.sanitize_provider_message. Helper logs must follow the same rule: never print full account numbers (use last3 as elsewhere, notify_v2._last3 :101). Full account number is currently printed only in the paste email to the verified user.
- PAYMENTS_FINGERPRINT_KEY etc. irrelevant to helper.

## 7. Relevant tests
87 files in tests/ (incl. helpers). Directly relevant:
- test_payment_bankdetails.py (BankDetails parsing), test_payment_extract.py, test_payment_beneficiaries.py, test_payment_beneficiary_strict.py, test_beneficiary_*.py (DB-side matching/ingest freeze/migration/sync/payee/representative).
- test_payment_v2_notify.py, test_payment_v2_cycle.py:245-262 (paste email content; account number only to verified sender; row stores hmac only), :94-99, :311; test_payment_v2_security.py:291; test_payment_v2_batch.py:71 (paste_notified_at); tests/notify_cases.py helper.
- test_payment_docs.py:65 asserts runbook contains words incl. "Playwright", "separate later project" (docs/PAYMENTS_RUNBOOK.md:170-171) — editing that runbook sentence would break the test; update both together if the spec requires.
- test_payment_frozen_surface.py and test_cli.py guard CLI/module surface (not read in detail; planner must read before adding a subcommand).
- pytest not installed in this env (python3 -m pytest fails), so baseline cannot be run here as-is; CLAUDE.md baseline 62 is stale (file count suggests many more tests since payments v2).

## 8. Playwright / Chromium availability (not installed)
- `import playwright`: ModuleNotFoundError. No chromium/chromium-browser/google-chrome on PATH. ~/.cache/ms-playwright absent. Python 3.13 system, pytest absent. Outbound HTTPS only via proxy (so `pip install playwright` and `playwright install chromium` downloads may be blocked/need proxy config). Conclusion: end-to-end browser run is not testable here; must be designed with injectable page/driver and tests using fakes, or `pytest.importorskip("playwright")`. Also no display: headed mode (needed for manual login) cannot run in this sandbox.

## 9. Prior decisions/constraints on this work
- .loop/decisions.md:5 Q1: unregistered payee -> NOTIFY; Playwright helper = later project. .loop/spec.json:320 (out-of-scope text: separate run, own security review, no login/2FA automation); .loop/domain.md:141; .loop/GOAL.md:18. docs/PAYMENTS_RUNBOOK.md:170-171.
- Active run email-payment-v2 mid-build: do not touch its .loop files nor payments modules in flight (src/invespend/payments/* edits risk colliding). Safest: new isolated module (e.g. src/invespend/beneficiary_helper/) that reuses `BankDetails`, `BENEFICIARY_FIELD_ORDER`, and `bankdetails.extract_bank_details` read-only, with zero edits to payments code.

## 10. Risks / smells relevant to the helper
- MEDIUM: field order lives only in notify_v2.py:30-33 (constant, reusable) but Investec Online's actual form order/labels are unverified in-repo; the "order Investec asks" is an assumption inherited from the email text.
- MEDIUM: `Amount`, `Their reference`, `My reference`, `Payment notification` are payment-time fields, not necessarily beneficiary-creation fields; Branch code listed as reference-only (probably auto-derived from bank selection in UI).
- MEDIUM: full account number has no durable store; helper input channel (stdin/paste/clipboard/file) creates a new place where it can leak (shell history, logs, screenshots, Playwright trace).
- LOW: bankdetails/`_clean` already sanitise control chars and length caps (reusable validation).
- HIGH (for security criteria): any browser automation on a banking domain; selectors fragile; final submit must stay manual; no persistent profile; domain allowlist check before filling (refuse to fill if page URL not Investec host).
- Observability: adding a subcommand modifies `build_parser` (cli.py:409) and may trip frozen-surface tests.
