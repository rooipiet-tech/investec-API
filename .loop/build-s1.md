# build-s1: email-payment-v2, iteration 1, slice 1 (S0-S6)

Branch claude/clever-babbage-kyspbq, base of work 638e948 (code == plan base 2eee10c). Not pushed.
Final: `uv run pytest -q` = 651 passed, 12 skipped, 0 failed (baseline 330). uv.lock restored after every run, never committed.
Local interpreter is CPython 3.11.15 (frozen-surface help comparisons are 3.12-only and skip there; verified 12/12 passing under a scratch CPython 3.12.3 venv outside the repo).

## Stages, commits, test counts (passed / skipped / xfailed)
| Stage | Commit | Suite after |
|---|---|---|
| S0 baseline + fixtures | 75bbc51 | 330 measured at start; 332 / 10 skipped after adding the frozen-surface test |
| S1 F42 proof (tests only) | 87d1aca | 346 / 11 |
| S2 v2_inbox, loopguard, refs | a5c6a1d | 410 / 11 |
| S3 red (xfail-marked) | b7f207b | 410 / 11 / 50 xfailed + 1 xpassed |
| S3 green | 0597639 | 461 / 11 |
| S4 red (xfail-marked) | 72325ce | 461 / 11 / 33 xfailed + 12 xpassed |
| S4 green | 2e3d1bd | 506 / 11 |
| S5 sender_auth | 0a9db3c | 559 / 12 |
| S6 trigger, amounts, strict resolvers | e470ec6 | 651 / 12 |

## Files
* S0: `.loop/baseline/BASE_SHA` (full 2eee10c sha; help captures regenerated under 3.12.3, byte-identical to the previous ones, deterministic over two runs), `.loop/state.json` (decision log line), `tests/fixtures/cli_help/*` (9), `tests/test_payment_frozen_surface.py`.
* S1: `tests/fixtures/legacy_sha256.json` (9 pinned files, `preexisting_payments_modules`, `legacy_six`, `cli_base_fixture`), `tests/fixtures/legacy/cli_2eee10c.py.txt`, `tests/test_payment_legacy_retired.py`.
* S2: `src/invespend/payments/{v2_inbox,loopguard,refs}.py`, `tests/test_payment_{v2_inbox,v2_loopguard,refs}.py`.
* S3: `src/invespend/investec_client.py` (additive plus docstring/comment fix; `_get_token(force)`, `_write_session`, `_post_once`, `create_payment(..., *, fresh_token=False)`), `src/invespend/payments/outcome.py`, `tests/test_payment_client_hardening.py`.
* S4: `src/invespend/config.py` (`_opt_cap`, two cap-parse lines), `src/invespend/payments/caps.py` (`_cap`, `_amount`, bodies of the two check functions), `.env.example` (caps 20000 / 50000 and the unverified R20,000 caveat), `tests/test_payment_caps_failclosed.py`.
* S5: `src/invespend/payments/sender_auth.py`, `tests/test_payment_sender_auth.py`, `tests/fixtures/payments_v2/auth/*.eml` (30).
* S6: `src/invespend/payments/{trigger,amounts}.py`, additive helpers in `beneficiaries.py` (`resolve_beneficiary_strict`) and `accounts.py` (`source_profile_id`, `resolve_source_unique`), `tests/test_payment_{trigger,amounts,beneficiary_strict}.py`.
* Verified untouched vs 2eee10c: payments/{pipeline,token,inbox,notify,dedup,selftest,extract,store,pg_store,audit}.py, cli.py, db/, .github/, pyproject.toml, uv.lock, every pre-existing test file.

## Deviations / readings chosen (safer reading where the plan was silent or contradictory)
1. **Red commits are xfail-marked.** Plan says commit the failing test first; CLAUDE/task says the suite must stay green at every commit. The S3 and S4 red commits carry a module-level `pytestmark = pytest.mark.xfail(strict=False, ...)` (so the red state is real: 50 and 33 failures, shown as xfailed); the green commit removes the marker line. Non-strict because a few tests already passed before the fix.
2. **`Ref: R20261` (TR3-11).** The plan's 6+ digit rule cannot reject `R20261` (5 digits). Added one extra rule in `amounts.py`: a currency-marked number immediately after a `ref`/`reference` label is not an amount. The 6+ digit, letter, `-` and `/` boundary rules are as specified. A plain 5-digit `R12345` still counts (the human sees it in the batch email).
3. **Valid trigger plus malformed trigger** in the same typed text (`pay 123 ... pay 1234`) returns `ambiguous` (the plan only says malformed is ignored and not guessed; ambiguous means no payment).
4. **Group syntax in From** (`friends: piet@example.com;` parses to one address under strict getaddresses) is rejected as `multiple_from` by an explicit group-syntax scan; a trailing-comma / empty second entry is likewise `multiple_from`.
5. `parse_payment_response`: `ErrorMessage` present and not `None` (even `""`) is a failure (the swagger says null when ok); `AuthorisationRequired` is read both at `data` level and per `TransferResponses` entry; `PaymentOutcome.message` for `needs_authorisation` is the sanitised entry `Status` text.
6. `_post_once`: a 200 whose JSON is not an object is treated as unknown outcome (same as undecodable); the provider message is also looked up under `data.` after the top-level fields (field names unverified until G2, C8).
7. `sanitize_provider_message` redaction order is address, token-like (24+ chars), then 6+ digit runs (digits first would split a token into a digit run plus a letter run).
8. `strip_trigger` is implemented in S6 (trigger.py) as the plan lists; wiring of stripped text into extraction remains S7/S11.
9. `labelled_payee_candidates` was added to `amounts.py` (plan names the TR3-12 test but no function); it reuses `extract._PAYEE_RE`.
10. Local suite counts: `test_strict_parser_available_on_this_interpreter` skips unless `CI=true`; the 3.12-only help comparisons skip on the local 3.11.

## Could not do / open
* Nothing in S0-S6 was skipped. Residual notes: the Gmail IMAP topmost Authentication-Results shape is still an empirical G1 item; Investec rejection body field names stay unverified until G2; the minimum CPython patch release that has `getaddresses(strict=)` is not recorded here (S13 runbook): 3.11.15 and 3.12.3 here both expose it.

## Fix round (review R1-R3, risk R1-R4)
Each fix: xfail-marked red test commit (suite green), then green commit removing the marker. Existing tests unmodified; frozen legacy files untouched.
* review R1 (`payments/outcome.py`): `parse_payment_response` no longer returns success without a non-empty `PaymentReferenceNumber`. New `PaymentOutcome.status == "unknown"` (reason `no_reference`; never auto-resend). Mapping (also in the module docstring): data `ErrorMessage` -> failed; `AuthorisationRequired` (data or entry) or Status wording authori[sz]/awaiting/pending -> needs_authorisation; entry `ErrorMessage` non-empty (`entry_error_message`) or Status with fail/reject/declin/deny/invalid/insufficient/error/cancel/expire (`entry_status`) -> failed even without a reference; no reference otherwise -> unknown; reference + clean/absent status -> success. Multi-entry precedence: needs_authorisation > failed > unknown > success. Status/field names remain UNVERIFIED until G2.
* review R2 (`investec_client._post_once`): 408 (response and HTTPError) -> `PaymentUnknownOutcome`; other 4xx except 429 still `PaymentRejected`.
* review R3 + risk R2 (`sanitize_provider_message`): input cut to 2000 chars first, then redaction, then 200; digit pattern `\d(?:[ \-]{0,3}\d){5,}` redacts space/hyphen grouped runs (linear, bounded separators).
* risk R1 + R4 (`payments/amounts.py`): `MAX_TYPED_CHARS = 20_000`, longer input FAILS CLOSED to no candidates (marked amounts and payee candidates; never truncated); ref look-behind is a 40-char window; currency marker left boundary also excludes `- / # _ .`; `[ \t]?` instead of `\s?` between marker and number (prefixed and labelled; suffix `ZAR` unchanged). Plan S6 text updated.
* risk R3 (`payments/loopguard.py`): `MAX_MSGID_CHARS = 998`, message_id cut before the unanchored search.
* Scan of sender_auth, trigger, refs, v2_inbox: no super-linear pattern found (all hostile cases < 2 s); `tests/test_payment_v2_redos.py` covers loopguard, amounts, trigger, refs, outcome, sender_auth, v2_inbox with 200KB hostile inputs. Timing tests run in a killed subprocess (`tests/timing_helper.py`).
* Tests: 3.11 local `uv run pytest -q` = 807 passed, 12 skipped, 0 failed. Scratch CPython 3.12 venv with CI=true = 818 passed, 1 skipped, 0 failed.
* Deviations: red commits xfail-marked (as in slice 1); the ReDoS suite takes ~12 s because each case is a subprocess; risk R5/R6 (info) not acted on in code; "run text parsing only after authenticate_sender" is a wiring concern for S11, not changed here.
