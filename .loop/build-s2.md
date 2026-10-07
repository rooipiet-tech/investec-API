# build-s2: email-payment-v2, iteration 2, slice 2 (S8, S7, S10)

Branch claude/clever-babbage-kyspbq, start ff6d7e5 ("slice 2 build approved"). Not pushed. uv.lock restored after every run, never committed.
Final: 3.11.15 `uv run pytest -q` = 1245 passed, 12 skipped, 0 failed (baseline 807). Scratch CPython 3.12.3 venv outside the repo, CI=true = 1256 passed, 1 skipped, 0 failed.
Only NEW files: 5 modules under src/invespend/payments/ and new tests/fixtures. No existing file or test touched (git diff --name-status vs ff6d7e5 shows additions only). Nothing wired into cli.py/ingest/workflows; no network, no SMTP.

## Stages (red = xfail-marked commit, suite green; green = marker removed). 3.11 counts passed/skipped
| Stage | Suite after |
|---|---|
| S8 red (tests + stdlib image fixtures) | 807 / 12 / 59 xfailed + 3 xpassed |
| S8 green | 874 / 12 |
| S7 red (content, commands, bankdetails, injection, call-order, redos tests) | 874 / 12 / 270 xfailed + 27 xpassed |
| S7 green | 1170 / 12 |
| S10 red (notify, selfloop tests) | 1170 / 12 / 47 xfailed + 1 xpassed |
| S10 green (+ notify cleaning timing test) | 1245 / 12 (3.12 + CI=true: 1256 / 1 skipped) |

## Files
* S8: `payments/images.py`; `tests/test_payment_images.py`; `tests/fixtures/payments_v2/make_fixtures.py` + `images/{tiny.jpg,tiny.png,tiny.gif,tiny.webp,adversarial.png}` (stdlib/base64 only, deterministic, test asserts the committed bytes equal the generator output).
* S7: `payments/content.py` (text_view, gather_payloads, forward_or_reply_indicators, split_typed_and_quoted, html_to_text, TextView/Region/Payloads), `payments/commands.py` (Command, parse_command), `payments/bankdetails.py` (BankDetails, extract_bank_details); tests `test_payment_content.py`, `test_payment_commands.py`, `test_payment_bankdetails.py`, `test_payment_image_injection.py`, `test_payment_v2_callorder.py` (three-spy harness), `test_payment_v2_redos_s2.py` (killed-subprocess timing, 200KB hostile inputs, all < 0.35 s); helper `tests/mail_helpers.py`.
* S10: `payments/notify_v2.py`; tests `test_payment_v2_notify.py`, `test_payment_v2_selfloop.py`; helper `tests/notify_cases.py`.

## Readings chosen / deviations (safer reading where the plan was silent or ambiguous)
1. **Command kinds follow the FINAL plan text**: `approve_all | approve | cancel_all | cancel | invalid`, `invalid` reasons `bad_syntax | duplicate_number | both_verbs_in_text`. The orchestrator note said "ambiguous"; the plan's `invalid/both_verbs_in_text` is what is built. Out-of-range is S11's job (parser has no batch), as in the plan.
2. **`last3` is not named** in `validate_extraction` dropped names: it contains a digit and the binding regex is `^[a-z_]{1,32}$` (the plan's example list names it; the regex wins). It is counted.
3. **Amount strictness in images**: `validate_extraction` pre-checks a strict shape before calling `extract._norm_amount`, because `_norm_amount("100.5")` returns 1005.00 (decimal point silently dropped). `100.5`, `1.234`, `R100`, `1e3`, negatives are rejected to None; floats are formatted to 2dp first. A bad value for an allowed key becomes None (never guessed); all-None means `fields` is None.
4. **`images.py` extras beyond the plan list** (small, pure): `image_ref_from_bytes` (reasons empty/oversize/unsupported), `max_image_bytes()` (env `PAYMENTS_MAX_IMAGE_BYTES`, unset/unparsable/<=0 -> 5 MB default), `is_zar`, `run_extractor` -> `ExtractionOutcome(fields, dropped_names, dropped_count, note)` (never raises, notes are reason codes only), `Null/Fake` extractors carry `note` / `calls`. `get_extractor` reads `anthropic_api_key` / `image_extractor_model` with `getattr` (config.py is frozen, the attributes do not exist yet); with both set it imports `.images_claude.ClaudeVisionExtractor` and calls it as `ClaudeVisionExtractor(api_key=..., model=...)` (S14 must match), ImportError -> Null with note `image_adapter_unavailable`.
5. **`reconcile`-level image tests deferred to S11**: `test_image_dollar_amount_conflicts_with_zar_body`, `test_image_only_non_zar_currency_parks`, `test_reconciled_currency_must_be_zar` need `routing.py`/reconcile which does not exist yet. S8 side is covered by `test_image_dollar_currency_is_never_zar` and the currency table.
6. **`bankdetails.py` built** (plan lists it under the S7 header; the S10 paste builder signature needs `BankDetails`). Conflicting values for one label -> None; input > 20,000 chars fails closed to all-None.
7. **`Payloads` has two additive fields**: `extractions` (name, `ExtractResult`) and `notes` (reason codes). `extract.extract_attachment` returns an `ExtractResult`, not text, so the attachment `Region.text` is a rendering `amount: ZAR 100.00\npayee: Acme` for `ok` results and "" otherwise. Unsupported attachment types (docx, plain .txt, ...) are skipped with note `attachment_skipped_unsupported` (plan: pdf/xlsx/csv only). `extract.py` untouched; the helper is called as `extract.extract_attachment` so spies/monkeypatch work; an exception from it becomes `needs_review`.
8. **gather_payloads** also walks attachments/images inside unwrapped message/rfc822 (depth cap, `rfc822_depth_exceeded` note); inner headers are never read. Unauthenticated-safety is structural: only `text_view` runs before auth; `tests/test_payment_v2_callorder.py` composes the real pieces in the plan's order (text_view, loop guard, auth, command, trigger, age gate, gather_payloads, image extractor) around spies; S11 must repeat this against its real handler.
9. **text_view with both text/plain and text/html (multipart/alternative)**: both are split; if either has no boundary while an indicator exists the result is unconfident (typed ""), otherwise the SHORTER typed text wins (more conservative). Plan only says markers are for the no-HTML case; this is the safe extension. A `message/rfc822` forward-as-attachment with typed text above it is unconfident (typed "") by the plan's rule (rfc822 is an indicator, no boundary).
10. HTML limits: part count 200, MIME depth 20, html tag stack 500, per-part 1,000,000 bytes, line cut 2000 chars before marker regexes; `mime_limit` is an indicator (fails closed).
11. **Batch/other emails**: third-party values are flattened to one line, `|` replaced by `/`, capped at 100 chars, 9+ digit runs masked as `[number hidden]` (non-paste emails), source account shown as last 3 digits only even if the caller passes a full number. Image items get the advisory `(read from an image; check the amount and payee)` on an indented line. `build_outcome_email(outcome=...)` is duck typed: `status` in success/dry_run/failed/needs_authorisation/anything else = unknown ("MAY HAVE BEEN PAID"), or a truthy `dry_run` attribute. The payment reference number is deliberately not included. `build_problem_email(kind="cycle_failed")` ignores detail and provider text; `ref` may be None for it. Subjects never contain third-party text.
12. Builders refuse a subject that starts with a reply prefix (ValueError), non-finite amounts (ValueError) and unknown problem kinds (ValueError).

## Not done / open
* Nothing in S8/S7/S10 skipped except item 5 (S11-dependent). No real vision adapter (S14), no schema, no wiring, no docs.
* Plan text mentions `.eml` fixtures; messages are built programmatically with `tests/mail_helpers.py` (no real addresses, only example.com/example.invalid).
