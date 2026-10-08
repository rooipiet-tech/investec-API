# Domain invariants and risks: Playwright beneficiary helper (Phase 0)

Sources: .loop/beneficiary-helper/GOAL.md, .loop/decisions.md (Q1, Q6, Amendment 3), docs/PAYMENTS_RUNBOOK.md
(lines 54-57 unregistered path, 170-171 "separate later project"), .loop/domain.md section 4 (lines 49-58).
No Investec Online / app DOM or flow was inspected: every UI fact below is [UNVERIFIED] until the human walks the real flow.
Tags: [INV] must hold, [REC] recommendation, [OPEN] human decision, [RISK].

## 1. Core concepts
- The Investec API cannot create beneficiaries (decisions.md Q1; domain.md:51). Creation is a manual act in Investec Online / the app.
  The helper only assists; it must never become a payment path (paymultiple stays in payments/ only).
- Existing pipeline already notifies on unregistered payees (runbook:54-57) and today only extracts payee name/bank/account/reference
  informationally (domain.md:43). Branch code, email, cell are not extracted today.
- Helper input = details the user supplies/confirms. Never auto-sourced from untrusted mail without human review (invoice-fraud pattern, domain.md:36,46).

## 2. Field order and validation
Human-binding order (decisions.md Q6, EXACT): 1 Beneficiary name, 2 Bank, 3 Account number, 4 Amount, 5 Their reference,
6 My reference, 7 Payment notification, 8 Beneficiary email address, 9 Beneficiary mobile number.
- [INV] Branch code is NOT in the ordered list; show only as a separate "reference only" line (Q6). Note: runbook:55-56 lists a shorter
  paste order (name, bank, account, reference) and domain.md:55 lists a different proposed order: Q6 supersedes both; spec must pick Q6 and
  flag the runbook text for a later doc fix (do not edit it in this run).
- [INV] Amount (field 4) is part of the app's payment-style form in Q6 order. For a beneficiary-create helper the amount must NOT be pre-filled
  or submitted by the helper as a payment; [OPEN] whether the app's add-beneficiary screen even has Amount. Default: leave blank / skip.
- [INV] Validation is advisory pre-checks only, fail closed on doubt (show error, do not fill): 
  - name: non-empty, trimmed, no control chars, NFKC-normalise and warn on mixed-script/lookalike chars; keep user's exact text otherwise.
  - bank: from a closed list of SA banks as the app shows them; no free text guess.
  - account number: digits only, strip spaces/hyphens only after showing the cleaned value; length varies by bank (roughly 7-16 digits) so only
    a loose range check; NEVER invent leading zeros or truncate; never float/int-cast (leading zeros lost).
  - branch code: 6 digits; universal branch codes per bank exist (e.g. big-four universal codes) [UNVERIFIED list, do not hard-code from memory];
    if the app derives it from bank, leave it.
  - their/my reference: conservative charset, length limit unknown (domain.md:44); truncate never silently, reject and ask.
  - email: single address, strict parse, ASCII; cell: SA format (+27 / 0 followed by 9 digits), normalise only with user confirmation.
- [INV] Read-back: after fill, the helper must show the user the values as the page holds them (not as the helper intended) for comparison.

## 3. SA banking specifics
- Account numbers: bank-specific lengths; PayShap/ShapID and Capitec/others differ; do not apply one length rule. Business accounts and
  Investec private-bank accounts may be 11 digits (e.g. 1000xxxxxxx) [UNVERIFIED].
- Branch code mismatch with bank is a classic misdirect; helper must warn when the app shows a different bank/branch after selection.
- Name-matching: SA banks increasingly run account verification (AVS); a "not matched" result must stop the flow, never be bypassed.
- Currency ZAR only (domain.md:42). Cross-border/foreign beneficiaries are out of scope.
- New beneficiary may be subject to Investec cooling-off / "pay once in Online first" (decisions.md Amendment 3; runbook:92-94). The helper
  must not try to pay to get past it.

## 4. 2FA / login boundaries (hard)
- [INV] The helper never automates login, OTP, push approval, biometric, or card-reader steps (GOAL.md). The user logs in manually in a headed
  browser; the helper starts only after the user signals "logged in".
- [INV] Beneficiary creation in Investec triggers its own approval (OTP / in-app approve). The helper never reads, waits-and-forwards, or
  supplies it. After the user confirms the final submit, 2FA is entirely the user's.
- [INV] No credentials, cookies, storage_state, tokens, HAR, traces, screenshots or videos are persisted. Use an ephemeral browser context
  (not launch_persistent_context against the user's real profile, not storage_state save). Playwright tracing/video/screenshot-on-failure
  OFF by default; they capture account numbers and session data.
- [INV] No page content, DOM, or screenshots are sent to Claude or any LLM at runtime. Helper is deterministic code.
- [REC] Do not attach to the user's already-logged-in browser via CDP unless the human accepts the risk: it exposes the full banking session
  to the script. If chosen, scope to a dedicated window and detach after.
- [RISK] Headless/bot-like automation may trip Investec bot/fraud detection and lock the profile. Prefer headed, human-paced, no stealth plugins.

## 5. ToS / fraud risks
- [RISK] Investec Online terms usually bar automated access to the banking site; unattended scripting could breach terms or void fraud
  protection. [OPEN] human accepts the ToS position. Mitigation: the human-driven, single-form-fill, human-submits model (option B below)
  and no scraping/reading of balances or other pages.
- [RISK] Invoice-fraud / business-email-compromise: details taken from an email can be attacker-supplied. The helper must make the user
  verify bank details against an independent channel before submit; never default-trust mail-extracted values (domain.md:36,46).
- [RISK] Phishing look-alike: helper must refuse to run unless the page origin is exactly the expected Investec host (hard-coded allowlist of
  origins; check URL after each navigation; abort on redirect elsewhere). Never navigate to a URL from mail or config-from-mail.
- [RISK] Selector drift: if the form layout/labels differ from expected, abort with a message rather than guess fields (wrong field fill
  = wrong beneficiary data).
- [RISK] Mobile app has no Playwright path; "mobile order" is a presentation of the same fields. Mobile-only automation is out of scope.

## 6. Must never be stored or logged
- Banking username, password, PIN, OTP, session cookies, storage_state, auth headers, device tokens, card numbers.
- Full account numbers, cell numbers, email addresses, names of payees in logs, audit rows, DB, CI output, exceptions, tracebacks, or
  Playwright artifacts. At most last-3 + a keyed fingerprint (existing PAYMENTS_FINGERPRINT_KEY approach, runbook:136); consistent with
  domain.md:56 (full numbers only in the owner-bound email, not audit/state).
- No beneficiary details in git, .env, test fixtures (use obviously fake data), GitHub Actions logs, or argv (visible in ps): pass via a
  local file the user controls (0600) or interactive prompt, deleted/cleared after use; avoid clipboard persistence.
- Exclude helper dir artifacts (traces, downloads, screenshots) in .gitignore; exceptions must be redacted before printing.
- [INV] Not run in GitHub Actions or any shared CI: local machine only (secrets in Environment `payments` are for the cycle, not this).

## 7. Human-in-the-loop points (required stops)
1. Start: human launches helper; shows source of details and requires explicit "details verified against independent source".
2. Login + 2FA: human, manually, in the browser.
3. Navigate to add-beneficiary: human clicks or helper navigates only to the allowlisted route after login signal.
4. Pre-fill review: helper prints masked summary; human confirms before any typing.
5. Post-fill review: read-back of page values; human compares.
6. FINAL SUBMIT: always the human's click. Helper never clicks Submit/Confirm/Approve/Pay and never dismisses the app's own confirmation or AVS warnings.
7. 2FA/approval of the beneficiary: human.
8. Teardown: close context, wipe temp inputs.
- Options for the spec: A) present-only (print/email ordered details, no browser); B) headed browser, human logs in, helper fills, human submits
  (REC default, subject to ToS acceptance); C) attach to the live session (not recommended).
- [INV] Dry-run default: helper with no flag only prints the ordered, masked plan and opens nothing; browser mode needs an explicit flag.

## 8. Coupling with the frozen codebase
- No change to existing CLI surface, payments/, schema, or migrations; helper is an isolated module/entry point and an optional dependency
  (Playwright is not in pyproject deps; do not add to core deps, use an extra and keep pytest baseline 62+ green without browsers installed).
- Tests must not need a real bank or network: use a local fake HTML form; assert field order = Q6, that Submit is never clicked, that no
  artifact/log contains full account numbers or secrets, that off-origin URLs abort.
- Helper must not read the payments DB or Investec API credentials; it takes details only from user input.
- [OPEN] Whether the notify email (runbook:54-57) should additionally carry Q6-ordered fields incl. branch code reference line: that touches
  frozen payments code and needs a separate decision.

## 9. Test gaps / risks ranked
1. UI drift and real-flow ordering: untestable offline; needs one human walkthrough.
2. ToS/bot detection: not testable; human decision.
3. Log/artifact leakage: must be covered by explicit tests (fake data canary in all outputs).
4. Origin check and "never click submit": testable with local fixture; must be Must-have.
